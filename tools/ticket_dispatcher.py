"""
ticket_dispatcher.py — Dispatch de acciones para agentes y herramientas.
"""

import asyncio
import base64
import contextlib
import hashlib
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
import uuid
from difflib import SequenceMatcher

from dotenv import load_dotenv
from openai import AsyncOpenAI

from tools.memoryTools.RAG_memory import MemoryRag
# ── Env / Clients / RAG ─────────────────────────────────────────────

load_dotenv()
RAG = MemoryRag()
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
user_preferences_path = os.getenv("USER_PREFERENCES_PATH", "/tmp/user_preferences.txt")

# ── Constantes ───────────────────────────────────────────────────────

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".sql"}
DEFAULT_MAX_CHARS = 200_000
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_PLAYWRIGHT_MAX_CHARS = 12_000
DEFAULT_PLAYWRIGHT_ELEMENT_LIMIT = 40
FILE_HASH_ALGORITHM = "md5"
HASH_CHUNK_BYTES = 1024 * 1024

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


def _file_hash(path: str) -> dict:
    """Calcula hash en streaming para no cargar archivos grandes en memoria."""
    hasher = hashlib.md5()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_BYTES), b""):
            hasher.update(chunk)

    stat = os.stat(path)
    return {
        "algorithm": FILE_HASH_ALGORITHM,
        "value": hasher.hexdigest(),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


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
            result = reader(path, mime, max_chars)
        else:
            result = _read_binary(path, mime)
        if result.get("success"):
            result["file_hash"] = _file_hash(path)
        return result
    except Exception as e:
        return {"success": False, "path": path, "mime": mime, "error": str(e)}


def action_file_hash(body: dict) -> dict:
    """Devuelve una huella eficiente del archivo. Requiere: path."""
    path = body.get("path")
    if not path:
        return {"success": False, "error": "missing required field: path"}

    real_path = _resolve_path(path)
    if not os.path.isfile(real_path):
        return {"success": False, "error": f"file not found: {real_path}", "path": real_path}

    compare_to = (body.get("compare_to") or "").strip() or None

    try:
        file_hash = _file_hash(real_path)
    except Exception as e:
        return {"success": False, "path": real_path, "error": str(e)}

    result = {
        "success": True,
        "path": real_path,
        "file_hash": file_hash,
    }
    if compare_to is not None:
        result["changed"] = file_hash["value"] != compare_to
    return result


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
            "file_hash": _file_hash(real_path),
        }
    except Exception as e:
        return {"success": False, "path": real_path, "error": str(e)}


def action_edit_file(body: dict) -> dict:
    """
    Modifica un archivo reemplazando old_text por new_text (search-and-replace).
    Requiere: path, old_text, new_text.
    Opcional: replace_all (bool, default False).
    """
    path = body.get("path")
    old_text = body.get("old_text")
    new_text = body.get("new_text")

    if not path:
        return {"success": False, "error": "missing required field: path"}
    if old_text is None:
        return {"success": False, "error": "missing required field: old_text"}
    if new_text is None:
        return {"success": False, "error": "missing required field: new_text"}

    real_path = _resolve_path(path)

    if not any(real_path.startswith(os.path.realpath(root)) for root in ALLOWED_WRITE_ROOTS):
        return {
            "success": False,
            "error": f"edit not allowed outside: {ALLOWED_WRITE_ROOTS}",
            "path": real_path,
        }

    if not os.path.isfile(real_path):
        return {"success": False, "error": f"file not found: {real_path}"}

    replace_all = bool(body.get("replace_all", False))

    try:
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        count = content.count(old_text)

        if count == 0:
            return {
                "success": False,
                "error": "old_text not found in file",
                "path": real_path,
            }

        if not replace_all and count > 1:
            return {
                "success": False,
                "error": f"old_text found {count} times — set replace_all=true or provide more context to make it unique",
                "path": real_path,
                "occurrences": count,
            }

        new_content = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)

        with open(real_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        replacements = count if replace_all else 1
        return {
            "success": True,
            "path": real_path,
            "replacements": replacements,
            "file_hash": _file_hash(real_path),
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
    return {
        "success": True, "path": path, "mime": mime, "kind": "binary",
        "bytes": size,
        "preview_omitted": True,
        "omission_reason": "binary preview disabled to avoid large base64 payloads in tool outputs",
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
            executable="/bin/bash",
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


async def action_web_fetch(body: dict) -> dict:
    """
    Descarga el contenido de una URL vía HTTP.
    Requiere: url (str).
    Opcional: method ('GET'|'POST', default 'GET'), headers (dict),
              data (dict|str, para POST), timeout (int, default 15),
              max_chars (int).
    """
    import httpx

    url = (body.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "missing required field: url"}

    method = (body.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return {"success": False, "error": "method must be 'GET' or 'POST'"}

    headers = body.get("headers") or {}
    data = body.get("data")
    timeout = min(int(body.get("timeout") or 15), 60)
    max_chars = max(int(body.get("max_chars") or DEFAULT_MAX_CHARS), 1)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            if method == "POST":
                if isinstance(data, dict):
                    response = await client.post(url, json=data, headers=headers)
                else:
                    response = await client.post(url, content=data or "", headers=headers)
            else:
                response = await client.get(url, headers=headers)

        content_type = response.headers.get("content-type", "")
        text = response.text
        truncated = len(text) > max_chars

        return {
            "success": response.is_success,
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "truncated": truncated,
            "content": text[:max_chars],
        }
    except httpx.TimeoutException:
        return {"success": False, "error": "timeout exceeded", "url": url}
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "si", "sí"}
    return bool(value)


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _playwright_output_dir(body: dict) -> str:
    requested = (body.get("output_dir") or "").strip()
    base = requested or os.getenv("PLAYWRIGHT_OUTPUT_DIR") or os.path.join("/tmp", "planner_playwright")
    output_dir = _resolve_path(base)
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


async def _collect_playwright_snapshot(
    page,
    max_chars: int,
    include_text: bool,
    include_elements: bool,
    include_html: bool,
) -> dict:
    snapshot: dict = {
        "title": await page.title(),
        "url": page.url,
    }

    if include_text:
        text = await page.locator("body").inner_text(timeout=3000)
        snapshot["text"], snapshot["text_truncated"] = _truncate_text(text, max_chars)

    if include_elements:
        elements = await page.evaluate(
            """(limit) => {
                const visibleText = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                const clip = (s, n = 160) => s && s.length > n ? s.slice(0, n) : s;
                const links = Array.from(document.querySelectorAll('a[href]')).slice(0, limit).map((a) => ({
                    text: clip(visibleText(a)),
                    href: a.href
                })).filter((x) => x.text || x.href);
                const buttons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]')).slice(0, limit).map((b) => ({
                    text: clip(visibleText(b)),
                    type: b.getAttribute('type') || b.tagName.toLowerCase()
                })).filter((x) => x.text || x.type);
                const fields = Array.from(document.querySelectorAll('input, textarea, select')).slice(0, limit).map((el) => ({
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                    type: el.getAttribute('type') || el.tagName.toLowerCase(),
                    placeholder: el.getAttribute('placeholder') || '',
                    label: clip((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || '')
                }));
                const forms = Array.from(document.querySelectorAll('form')).slice(0, limit).map((form) => ({
                    action: form.action || '',
                    method: form.method || 'get',
                    fields: Array.from(form.querySelectorAll('input, textarea, select')).slice(0, 20).map((el) => el.getAttribute('name') || el.id || el.getAttribute('placeholder') || el.tagName.toLowerCase()).filter(Boolean)
                }));
                return {links, buttons, fields, forms};
            }""",
            DEFAULT_PLAYWRIGHT_ELEMENT_LIMIT,
        )
        snapshot["elements"] = elements

    if include_html:
        html = await page.content()
        snapshot["html"], snapshot["html_truncated"] = _truncate_text(html, max_chars)

    return snapshot


async def _save_playwright_screenshot(page, body: dict, full_page: bool) -> dict:
    output_dir = _playwright_output_dir(body)
    filename = f"playwright_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
    path = os.path.join(output_dir, filename)
    screenshot_bytes = await page.screenshot(path=path, full_page=full_page)
    page_size = await page.evaluate(
        """() => ({
            width: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),
            height: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)
        })"""
    )
    return {
        "path": path,
        "mime": "image/png",
        "bytes": len(screenshot_bytes),
        "full_page": full_page,
        "viewport": page.viewport_size,
        "page_size": page_size,
    }


async def action_playwright_navigate(body: dict) -> dict:
    """
    Navega y/o interactúa con una página web usando Playwright (Chromium headless).
    Requiere: url (str).
    Opcional:
      - action: 'navigate' (default) | 'snapshot' | 'inspect' | 'click' | 'fill' | 'screenshot' | 'get_text'
      - selector (str): selector CSS/XPath para click/fill/get_text
      - value (str): texto a introducir en fill
      - wait_for (str): selector a esperar antes de retornar
      - timeout (int, default 15, max 60): segundos
      - headless (bool, default True)
      - max_chars (int, default 12000): máximo texto/HTML devuelto
      - include_html/include_text/include_elements (bool): controla el snapshot
      - screenshot_mode: 'path' (default) | 'base64' | 'both'
    """
    from playwright.async_api import async_playwright

    url = (body.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "missing required field: url"}

    action = (body.get("action") or "navigate").lower()
    selector = body.get("selector") or ""
    value = body.get("value") or ""
    wait_for = body.get("wait_for") or ""
    timeout_s = min(int(body.get("timeout") or 15), 60)
    timeout_ms = timeout_s * 1000
    headless = body.get("headless", True)
    max_chars = max(int(body.get("max_chars") or DEFAULT_PLAYWRIGHT_MAX_CHARS), 1)
    include_html = _coerce_bool(body.get("include_html"), False)
    include_text = _coerce_bool(body.get("include_text"), True)
    include_elements = _coerce_bool(body.get("include_elements"), True)
    screenshot_mode = (body.get("screenshot_mode") or "path").lower()
    if screenshot_mode not in {"path", "base64", "both"}:
        screenshot_mode = "path"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            page = await browser.new_page()
            await page.goto(url, timeout=timeout_ms)

            if wait_for:
                await page.wait_for_selector(wait_for, timeout=timeout_ms)

            result: dict = {"success": True, "url": page.url, "action": action}

            if action in {"navigate", "snapshot", "inspect"}:
                result["snapshot"] = await _collect_playwright_snapshot(
                    page=page,
                    max_chars=max_chars,
                    include_text=include_text,
                    include_elements=include_elements,
                    include_html=include_html,
                )

            elif action == "click":
                if not selector:
                    await browser.close()
                    return {"success": False, "error": "selector required for 'click'"}
                await page.click(selector, timeout=timeout_ms)
                result["snapshot"] = await _collect_playwright_snapshot(
                    page, max_chars, include_text, include_elements, include_html
                )

            elif action == "fill":
                if not selector:
                    await browser.close()
                    return {"success": False, "error": "selector required for 'fill'"}
                await page.fill(selector, value, timeout=timeout_ms)
                result["filled"] = value
                result["snapshot"] = await _collect_playwright_snapshot(
                    page, max_chars, include_text, include_elements, include_html
                )

            elif action == "get_text":
                if not selector:
                    await browser.close()
                    return {"success": False, "error": "selector required for 'get_text'"}
                text = await page.inner_text(selector, timeout=timeout_ms)
                result["text"], result["truncated"] = _truncate_text(text, max_chars)

            elif action == "screenshot":
                full_page = _coerce_bool(body.get("full_page"), True)
                screenshot = await _save_playwright_screenshot(page, body, full_page=full_page)
                result["screenshot"] = screenshot
                if screenshot_mode in {"base64", "both"}:
                    with open(screenshot["path"], "rb") as f:
                        result["screenshot_base64"] = base64.b64encode(f.read()).decode("ascii")
                result["snapshot"] = await _collect_playwright_snapshot(
                    page=page,
                    max_chars=max_chars,
                    include_text=include_text,
                    include_elements=False,
                    include_html=False,
                )

            else:
                await browser.close()
                return {"success": False, "error": f"unknown action: {action}"}

            await browser.close()
            return result

    except Exception as e:
        return {"success": False, "error": str(e), "url": url}


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXT_TO_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _resolve_image_source(source: str, explicit_mime: str | None) -> str:
    if source.startswith(("http://", "https://")) or source.startswith("data:image/"):
        return source

    candidate_path = _resolve_path(source)
    if os.path.isfile(candidate_path):
        size = os.path.getsize(candidate_path)
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {size}")
        ext = os.path.splitext(candidate_path)[1].lower()
        mime = explicit_mime or IMAGE_EXT_TO_MIME.get(ext) or mimetypes.guess_type(candidate_path)[0] or "image/png"
        with open(candidate_path, "rb") as f:
            raw = f.read()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    cleaned = "".join(source.split())
    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except Exception as exc:
        raise ValueError(f"source is not a valid path, URL or base64: {exc}") from None
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {len(decoded)}")
    mime = explicit_mime or "image/png"
    return f"data:{mime};base64,{cleaned}"


def _extract_response_text(response) -> str:
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for chunk in getattr(item, "content", []) or []:
            if getattr(chunk, "type", None) == "output_text":
                text = getattr(chunk, "text", None)
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n".join(parts)


async def action_interpret_image(body: dict) -> dict:
    """
    Analiza una imagen con un modelo de visión vía Responses API.
    Requiere: source (str) — path local, URL http(s), data URI o base64 puro.
    Opcional: prompt (str), detail ('low'|'high'|'auto'), mime_type (str), model (str).
    """
    source = (body.get("source") or "").strip()
    if not source:
        return {"success": False, "error": "missing required field: source"}

    prompt = (body.get("prompt") or "").strip() or "Describe detalladamente el contenido de esta imagen."
    detail = body.get("detail", "auto")
    if detail not in ("low", "high", "auto"):
        detail = "auto"
    model = body.get("model") or "gpt-5.5"
    explicit_mime = (body.get("mime_type") or "").strip() or None

    try:
        image_url = await asyncio.to_thread(_resolve_image_source, source, explicit_mime)
    except Exception as e:
        return {"success": False, "error": f"cannot load image: {e}"}

    try:
        response = await openai_client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url, "detail": detail},
                    ],
                }
            ],
        )
    except Exception as e:
        return {"success": False, "error": str(e), "model": model}

    return {
        "success": True,
        "model": model,
        "detail": detail,
        "interpretation": _extract_response_text(response),
    }


async def action_memory_query(body: dict):
    sql = (body.get("sql") or "").strip()
    if not sql:
        return {"success": False, "error": "Campo 'sql' requerido"}
    embed_text = (body.get("embed_text") or "").strip() or None
    return await RAG.execute_safe_query(sql, embed_text=embed_text)

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
    "file_hash": action_file_hash,
    "write_file": action_write_file,
    "edit_file": action_edit_file,
    "run_command": action_run_command,
    "run_python": action_run_python,
    "search_files": action_search_files,
    "web_fetch": action_web_fetch,
    "playwright_navigate": action_playwright_navigate,
    "ask_user": action_ask_user,
    "save_preference": action_save_preference,
    "memory_query": action_memory_query,
    "interpret_image": action_interpret_image,
}
