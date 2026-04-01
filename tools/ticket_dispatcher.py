"""
ticket_dispatcher.py — Dispatch de acciones para agentes y herramientas.
"""

import base64
import contextlib
import io
import json
import mimetypes
import multiprocessing
import os
import queue
import resource
import signal
import subprocess
import time
import traceback
from difflib import SequenceMatcher

from dotenv import load_dotenv
from openai import AsyncOpenAI
from qdrant_client import QdrantClient

from RAG.qdrant_server.qdrant_server import RAGService

# ── Env / Clients / RAG ─────────────────────────────────────────────

load_dotenv()

qdrant_client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
user_preferences_path = os.getenv("USER_PREFERENCES_PATH", "/tmp/user_preferences.txt")

RAG = RAGService(
    openai_client=openai_client,
    qdrant_client=qdrant_client,
    docs_dir=os.getenv("RAG_DOCS_DIR", "/home/ale/python/portillo/RAG/generated"),
)

# ── Constantes ───────────────────────────────────────────────────────

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml"}
DEFAULT_MAX_CHARS = 200_000
DEFAULT_SEARCH_LIMIT = 50
BINARY_PREVIEW_CAP = 200_000

ALLOWED_WRITE_ROOTS = [
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~"),
    "/mnt/d",
    "/mnt/c",
    "/tmp",
]

BLOCKED_COMMANDS = {
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "fork",
    "chmod -R 777 /", "chown -R", "shutdown", "reboot", "halt",
    "poweroff", "init 0", "init 6", "killall", "kill -9 1",
    "> /dev/sda", "mv / ", "wget | sh", "curl | sh", "curl | bash",
    "wget | bash", "python -c", "python3 -c",
}

BLOCKED_PREFIXES = {"sudo", "su", "doas"}

SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "sum": sum,
    "len": len, "range": range, "print": print,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "isinstance": isinstance, "type": type,
    "round": round, "pow": pow, "divmod": divmod,
    "True": True, "False": False, "None": None,
}


# ── Utilidad de rutas ────────────────────────────────────────────────

def _resolve_path(path: str) -> str:
    """Expande ~ y variables de entorno, luego resuelve a ruta absoluta."""
    return os.path.realpath(os.path.expandvars(os.path.expanduser(path)))


# ── Herramientas: Archivos ──────────────────────────────────────────

def action_search_files(body: dict) -> dict:
    """Busca archivos por nombre. Requiere: query. Opcional: root, limit."""
    query = (body.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "missing required field: query", "results": []}

    root = body.get("root") or "."
    limit = max(int(body.get("limit") or DEFAULT_SEARCH_LIMIT), 1)
    terms = query.split()
    q_cf = query.casefold()

    def score(name: str) -> float:
        n_cf = name.casefold()
        exact = 1.0 if q_cf in n_cf else 0.0
        coverage = sum(t.casefold() in n_cf for t in terms) / max(len(terms), 1)
        fuzzy = SequenceMatcher(a=q_cf, b=n_cf).ratio()
        return 0.9 * fuzzy + 0.8 * coverage + 0.6 * exact

    results = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                s = score(fn)
                if s > 0.25:
                    results.append({"path": os.path.join(dirpath, fn), "filename": fn, "score": s})

        results.sort(key=lambda r: r["score"], reverse=True)
        return {"success": True, "query": query, "root": root, "results": results[:limit]}
    except Exception as e:
        return {"success": False, "error": str(e), "results": []}


def action_read_file(body: dict) -> dict:
    """Lee un archivo. Requiere: path. Opcional: max_chars."""
    path = body.get("path")
    if not path:
        return {"success": False, "error": "missing required field: path"}

    path = _resolve_path(path)
    if not os.path.exists(path):
        return {"success": False, "error": f"file not found: {path}"}

    max_chars = max(int(body.get("max_chars") or DEFAULT_MAX_CHARS), 1)
    ext = os.path.splitext(path)[1].lower()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

    try:
        reader = _FILE_READERS.get(ext)
        if reader:
            return reader(path, mime, max_chars)
        return _read_binary(path, mime)
    except Exception as e:
        return {"success": False, "path": path, "mime": mime, "error": str(e)}


def action_write_file(body: dict) -> dict:
    """
    Escribe contenido a un archivo de texto.
    Requiere: path (str), content (str).
    Opcional: mode ('w'|'a'), encoding (default 'utf-8').
    """
    path = body.get("path")
    content = body.get("content")
    if not path:
        return {"success": False, "error": "missing required field: path"}
    if content is None:
        return {"success": False, "error": "missing required field: content"}

    mode = body.get("mode", "w")
    if mode not in ("w", "a"):
        return {"success": False, "error": "mode must be 'w' or 'a'"}
    encoding = body.get("encoding", "utf-8")

    # ✅ FIX: expandir ~ y $HOME antes de validar
    real_path = _resolve_path(path)

    if not any(real_path.startswith(os.path.realpath(root)) for root in ALLOWED_WRITE_ROOTS):
        return {
            "success": False,
            "error": f"write not allowed outside: {ALLOWED_WRITE_ROOTS}",
            "path": real_path,
        }

    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, mode, encoding=encoding) as f:
            f.write(content)
        return {
            "success": True,
            "path": real_path,
            "bytes_written": len(content.encode(encoding)),
        }
    except Exception as e:
        return {"success": False, "path": real_path, "error": str(e)}


# ── Lectores por tipo ────────────────────────────────────────────────

def _read_text(path: str, mime: str, max_chars: int) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(max_chars + 1)
    truncated = len(content) > max_chars
    return {
        "success": True, "path": path, "mime": mime, "kind": "text",
        "truncated": truncated, "content": content[:max_chars],
    }


def _read_pdf(path: str, mime: str, max_chars: int) -> dict:
    from PyPDF2 import PdfReader
    reader = PdfReader(path)
    content, total = [], 0
    for page in reader.pages:
        txt = page.extract_text() or ""
        if not txt:
            continue
        if total + len(txt) > max_chars:
            content.append(txt[:max_chars - total])
            total = max_chars
            break
        content.append(txt)
        total += len(txt)
    return {
        "success": True, "path": path, "mime": mime, "kind": "pdf_text",
        "pages": len(reader.pages), "truncated": total >= max_chars,
        "content": "\n".join(content),
    }


def _read_docx(path: str, mime: str, max_chars: int) -> dict:
    from docx import Document
    doc = Document(path)
    parts, total = [], 0
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        t += "\n"
        if total + len(t) > max_chars:
            parts.append(t[:max_chars - total])
            total = max_chars
            break
        parts.append(t)
        total += len(t)
    return {
        "success": True, "path": path, "mime": mime, "kind": "docx_text",
        "truncated": total >= max_chars, "content": "".join(parts),
    }


def _read_xlsx(path: str, mime: str, max_chars: int) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets, total = {}, 0
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            line = "\t".join("" if v is None else str(v) for v in row).rstrip()
            if not line:
                continue
            if total + len(line) + 1 > max_chars:
                rows.append(line[:max_chars - total])
                total = max_chars
                break
            rows.append(line)
            total += len(line) + 1
        if rows:
            sheets[ws.title] = rows
        if total >= max_chars:
            break
    return {
        "success": True, "path": path, "mime": mime, "kind": "xlsx_text",
        "sheets": list(sheets.keys()), "truncated": total >= max_chars,
        "content": sheets,
    }


def _read_pptx(path: str, mime: str, max_chars: int) -> dict:
    from pptx import Presentation
    prs = Presentation(path)
    texts, total = [], 0
    for si, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            t = getattr(shape, "text", "").strip()
            if not t:
                continue
            block = f"[slide {si}] {t}\n"
            if total + len(block) > max_chars:
                texts.append(block[:max_chars - total])
                total = max_chars
                break
            texts.append(block)
            total += len(block)
        if total >= max_chars:
            break
    return {
        "success": True, "path": path, "mime": mime, "kind": "pptx_text",
        "slides": len(prs.slides), "truncated": total >= max_chars,
        "content": "".join(texts),
    }


def _read_binary(path: str, mime: str) -> dict:
    size = os.path.getsize(path)
    preview_bytes = min(size, BINARY_PREVIEW_CAP)
    with open(path, "rb") as f:
        raw = f.read(preview_bytes)
    return {
        "success": True, "path": path, "mime": mime, "kind": "binary",
        "bytes": size, "preview_bytes": preview_bytes,
        "base64_preview": base64.b64encode(raw).decode("ascii"),
        "truncated": size > preview_bytes,
    }


_FILE_READERS = {
    **{ext: _read_text for ext in TEXT_EXTENSIONS},
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".xlsx": _read_xlsx,
    ".pptx": _read_pptx,
}


# ── Herramienta: Ejecutar comandos de terminal ──────────────────────

def _is_command_blocked(command: str) -> str | None:
    cmd_lower = command.strip().lower()
    first_word = cmd_lower.split()[0] if cmd_lower else ""
    for prefix in BLOCKED_PREFIXES:
        if first_word == prefix.strip():
            return f"comando bloqueado: no se permite '{prefix.strip()}'"
    for pattern in BLOCKED_COMMANDS:
        if pattern in cmd_lower:
            return f"comando bloqueado: patrón peligroso detectado '{pattern}'"
    return None


async def action_run_command(body: dict) -> dict:
    """
    Ejecuta un comando de terminal (bash).
    Requiere: command (str).
    Opcional: timeout (int, default 15, max 30), workdir (str).
    """
    command = (body.get("command") or "").strip()
    if not command:
        return {"success": False, "error": "missing required field: command"}

    timeout = min(int(body.get("timeout", 15)), 30)

    # ✅ FIX: expandir ~ y $HOME en workdir
    workdir = _resolve_path(body.get("workdir") or "~")

    blocked = _is_command_blocked(command)
    if blocked:
        return {"success": False, "error": blocked, "command": command}

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )

        stdout = result.stdout[-DEFAULT_MAX_CHARS:] if len(result.stdout) > DEFAULT_MAX_CHARS else result.stdout
        stderr = result.stderr[-50_000:] if len(result.stderr) > 50_000 else result.stderr

        return {
            "success": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout exceeded", "command": command, "timeout": timeout}
    except Exception as e:
        return {"success": False, "error": str(e), "command": command}


# ── Herramienta: Sandbox Python ─────────────────────────────────────

def _apply_sandbox_limits(memory_mb=512, cpu_seconds=10, max_files=64):
    mem = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_NOFILE, (max_files, max_files))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)
    except Exception:
        pass


def _run_sandboxed(code: str, result_queue: multiprocessing.Queue):
    try:
        _apply_sandbox_limits()
        safe_globals = {"__builtins__": SAFE_BUILTINS}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, safe_globals)

        result = safe_globals.get("RESULT", buf.getvalue().strip() or None)
        if result is not None:
            try:
                json.dumps(result)
            except TypeError:
                result = repr(result)

        result_queue.put((True, buf.getvalue(), result, None))
    except Exception as e:
        result_queue.put((False, "", None, traceback.format_exc()))


def action_run_python(body: dict) -> dict:
    """Ejecuta código Python en sandbox. Requiere: code. Opcional: timeout (max 10s)."""
    code = body.get("code", "")
    timeout = min(int(body.get("timeout", 5)), 10)

    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=_run_sandboxed, args=(code, q))

    start = time.perf_counter()
    p.start()
    p.join(timeout)
    elapsed = time.perf_counter() - start

    if p.is_alive():
        p.kill()
        p.join()
        return {"success": False, "error": "timeout exceeded", "execution_time": elapsed}

    try:
        success, stdout, result, tb = q.get_nowait()
    except queue.Empty:
        return {"success": False, "error": "no result returned", "execution_time": elapsed}

    resp = {"success": success, "stdout": stdout, "execution_time": elapsed}
    if success:
        resp["result"] = result
    else:
        resp["error"] = tb
    return resp


def action_ask_user(body: dict):
    raise body["question"]

def action_save_preference(body: dict):
    try:
        with open(f"{user_preferences_path}", "a") as f:
            f.write(f"\n -{body['preference']}")
            
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Agentes (proxy pass-through) ────────────────────────────────────

def _passthrough_agent(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


# ── Dispatcher ───────────────────────────────────────────────────────

_AGENTS = [
    "ExecutorAgent", "WebSearchAgent", "DeviceManagerAgent",
    "MCPManagerAgent", "MemoryAgent",
]

ticket_dispatcher: dict[str, callable] = {
    **{name: _passthrough_agent for name in _AGENTS},
    "read_file": action_read_file,
    "write_file": action_write_file,
    "run_command": action_run_command,
    "run_python": action_run_python,
    "search_files": action_search_files,
    "ask_user": action_ask_user,
    "save_preference": action_save_preference,
}
