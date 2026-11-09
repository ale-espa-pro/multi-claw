"""
ticket_dispatcher.py — Dispatch de acciones para agentes y herramientas.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import signal
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from app_paths import get_playwright_output_dir, get_user_preferences_path
from runner.images import IMAGE_EXT_TO_MIME, MAX_IMAGE_BYTES
from runner.sandbox import get_write_root, is_within
from tools.file_edit import action_edit_file
from tools.memoryTools.RAG_memory import MemoryRag
from tools.memoryTools.semantic_splitter import count_tokens

# ── Env / Clients / RAG ─────────────────────────────────────────────

load_dotenv()
user_preferences_path = get_user_preferences_path()

# Dependencias compartidas: la app las inyecta con configure(); si no,
# se crean de forma perezosa en el primer uso (scripts, tests).
_rag: MemoryRag | None = None
_openai_client: AsyncOpenAI | None = None


def configure(memory_rag: MemoryRag | None = None, openai_client: AsyncOpenAI | None = None):
    global _rag, _openai_client
    if memory_rag is not None:
        _rag = memory_rag
    if openai_client is not None:
        _openai_client = openai_client


def _get_rag() -> MemoryRag:
    global _rag
    if _rag is None:
        _rag = MemoryRag()
    return _rag


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


# ── Constantes ───────────────────────────────────────────────────────

DEFAULT_MAX_CHARS = 200_000
MAX_STDERR_CHARS = 50_000
DEFAULT_COMMAND_TIMEOUT = 15
MAX_COMMAND_TIMEOUT = 120
# Margen sobre MAX_PLAYWRIGHT_SESSION_TIMEOUT: una acción en curso no cuenta como inactiva.
PLAYWRIGHT_SESSION_IDLE_SECONDS = 2 * 60 * 60
DEFAULT_PLAYWRIGHT_MAX_CHARS = 12_000
DEFAULT_PLAYWRIGHT_ELEMENT_LIMIT = 40
DEFAULT_PLAYWRIGHT_SESSION_TIMEOUT = 300
MAX_PLAYWRIGHT_SESSION_TIMEOUT = 2_700
FILE_HASH_ALGORITHM = "md5"
HASH_CHUNK_BYTES = 1024 * 1024

# Filtro orientativo, no un sandbox: bash siempre permite esquivarlo. Las órdenes se
# buscan en posición de comando para no bloquear menciones como `grep fork`.
_AT_COMMAND = r"(?:^|[;&|(\n]\s*)(?:\S*/)?"
_ROOT_PATH = r"/\*?(?=\s|$)"
BLOCKED_PREFIX = re.compile(_AT_COMMAND + r"(sudo|su|doas)(?=\s|$)")
BLOCKED_PATTERNS = tuple(re.compile(pattern) for pattern in (
    _AT_COMMAND + r"(?:shutdown|reboot|halt|poweroff|killall|init\s+[06]|mkfs(?:\.\w+)?)(?=\s|$)",
    _AT_COMMAND + r"dd\s+if=",
    rf"\brm\s+-rf\s+{_ROOT_PATH}",
    rf"\bchmod\s+-r\s+777\s+{_ROOT_PATH}",
    rf"\bchown\s+-r\s+\S+\s+{_ROOT_PATH}",
    rf"\bmv\s+{_ROOT_PATH}",
    r"\bkill\s+-9\s+1(?=\s|$)",
    r":\(\)\s*\{",
    r">\s*/dev/sd[a-z]",
    r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:ba)?sh\b",
))

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


# ── Límites de salida ──────────────────────────────────────────────

def _clip_text_content(content: str, max_chars: int) -> tuple[str, bool, int]:
    shown = content[:max_chars]
    remaining_chars = max(len(content) - len(shown), 0)
    return shown, remaining_chars > 0, remaining_chars


def _count_remaining_tokens(content: str, max_chars: int) -> int:
    if len(content) <= max_chars:
        return 0
    return count_tokens(content[max_chars:])


# ── Herramienta: Ejecutar comandos de terminal ──────────────────────

def _is_command_blocked(command: str) -> str | None:
    cmd_lower = command.strip().lower()
    prefix = BLOCKED_PREFIX.search(cmd_lower)
    if prefix:
        return f"comando bloqueado: no se permite '{prefix.group(1)}'"
    for pattern in BLOCKED_PATTERNS:
        match = pattern.search(cmd_lower)
        if match:
            matched = match.group(0).strip(" ;&|(\n")
            return f"comando bloqueado: patrón peligroso detectado '{matched}'"
    return None


_draining_readers: set[asyncio.Task] = set()


async def _drain_stream(stream: asyncio.StreamReader, buffer: bytearray, max_bytes: int):
    """Acumula la salida conservando solo el final para acotar la memoria."""
    while chunk := await stream.read(64 * 1024):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            del buffer[:-max_bytes]


def _kill_process_group(process: asyncio.subprocess.Process):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


async def _wait_for_exit(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Espera a que termine bash. process.wait() no sirve: también espera el EOF
    de las tuberías, que un hijo en segundo plano puede mantener abiertas."""
    deadline = time.monotonic() + timeout
    while process.returncode is None:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def action_run_command(body: dict) -> dict:
    """
    Ejecuta un comando de terminal (bash).
    Requiere: command (str).
    Opcional: timeout (int, default 15, max 120), workdir (str).
    Al expirar el timeout mata todo el grupo de procesos y devuelve la salida parcial.
    """
    command = (body.get("command") or "").strip()
    if not command:
        return {"success": False, "error": "missing required field: command"}

    timeout = max(min(int(body.get("timeout") or DEFAULT_COMMAND_TIMEOUT), MAX_COMMAND_TIMEOUT), 1)

    # El swarm arranca en su carpeta; workdir debe permanecer dentro de ella.
    # Bash puede acceder a otras rutas: esta comprobación no aísla el comando.
    write_root = get_write_root()
    workdir = _resolve_path(body.get("workdir") or write_root or "~")
    if write_root is not None and not is_within(workdir, write_root):
        return {
            "success": False,
            "error": f"sandbox violation: workdir debe estar dentro de {write_root}",
            "workdir": workdir,
        }

    blocked = _is_command_blocked(command)
    if blocked:
        return {"success": False, "error": blocked, "command": command}

    try:
        # Sesión propia para que el timeout alcance a los hijos y no solo a bash.
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            start_new_session=True,
        )
    except OSError as e:
        return {"success": False, "error": str(e), "command": command}

    stdout, stderr = bytearray(), bytearray()
    # Un carácter UTF-8 ocupa hasta 4 bytes.
    readers = [
        asyncio.create_task(_drain_stream(process.stdout, stdout, DEFAULT_MAX_CHARS * 4)),
        asyncio.create_task(_drain_stream(process.stderr, stderr, MAX_STDERR_CHARS * 4)),
    ]
    try:
        timed_out = not await _wait_for_exit(process, timeout)
        if timed_out:
            _kill_process_group(process)
            await _wait_for_exit(process, 5)
        _, still_reading = await asyncio.wait(readers, timeout=0.5)
    except asyncio.CancelledError:
        for reader in readers:
            reader.cancel()
        raise
    finally:
        if process.returncode is None:
            _kill_process_group(process)

    # Un hijo en segundo plano heredó las tuberías: se siguen vaciando hasta su EOF
    # para que no se bloquee al escribir.
    for reader in still_reading:
        _draining_readers.add(reader)
        reader.add_done_callback(_draining_readers.discard)

    result = {
        "success": process.returncode == 0 and not timed_out,
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace")[-DEFAULT_MAX_CHARS:],
        "stderr": stderr.decode("utf-8", errors="replace")[-MAX_STDERR_CHARS:],
    }
    if timed_out:
        result.update(error="timeout exceeded", timeout=timeout)
    return result


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
        content, truncated, remaining_chars = _clip_text_content(text, max_chars)

        return {
            "success": response.is_success,
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "truncated": truncated,
            "remaining_chars": remaining_chars,
            "remaining_tokens": _count_remaining_tokens(text, max_chars),
            "content": content,
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


def _playwright_output_dir(body: dict) -> str:
    requested = (body.get("output_dir") or "").strip()
    base = requested or get_playwright_output_dir()
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
        (
            snapshot["text"],
            snapshot["text_truncated"],
            snapshot["text_remaining_chars"],
        ) = _clip_text_content(text, max_chars)
        snapshot["text_remaining_tokens"] = _count_remaining_tokens(text, max_chars)

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
        (
            snapshot["html"],
            snapshot["html_truncated"],
            snapshot["html_remaining_chars"],
        ) = _clip_text_content(html, max_chars)
        snapshot["html_remaining_tokens"] = _count_remaining_tokens(html, max_chars)

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


_PLAYWRIGHT_SESSIONS: dict[str, dict[str, Any]] = {}
_PLAYWRIGHT_SESSIONS_LOCK = asyncio.Lock()


def _serializable_preview(value: Any, max_chars: int = DEFAULT_PLAYWRIGHT_MAX_CHARS) -> Any:
    if isinstance(value, str):
        clipped, truncated, remaining_chars = _clip_text_content(value, max_chars)
        if not truncated:
            return clipped
        return {
            "content": clipped,
            "truncated": True,
            "remaining_chars": remaining_chars,
            "remaining_tokens": _count_remaining_tokens(value, max_chars),
        }
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return repr(value)


def _playwright_timeout_seconds(body: dict, default: int = DEFAULT_PLAYWRIGHT_SESSION_TIMEOUT) -> int:
    try:
        requested = int(body.get("timeout") or default)
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, MAX_PLAYWRIGHT_SESSION_TIMEOUT))


def _resolve_file_list(value: Any) -> str | list[str]:
    if isinstance(value, list):
        return [_resolve_path(str(item)) for item in value]
    return _resolve_path(str(value))


async def _create_playwright_session(session_id: str, body: dict, timeout_ms: int) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    headless = _coerce_bool(body.get("headless"), True)
    user_data_dir = (body.get("user_data_dir") or "").strip()
    storage_state_path = (body.get("storage_state_path") or "").strip()

    pw = await async_playwright().start()
    browser = None
    context = None
    try:
        if user_data_dir:
            context = await pw.chromium.launch_persistent_context(
                _resolve_path(user_data_dir),
                headless=headless,
            )
            browser = context.browser
        else:
            browser = await pw.chromium.launch(headless=headless)
            context_kwargs = {}
            if storage_state_path:
                resolved_state = _resolve_path(storage_state_path)
                if os.path.exists(resolved_state):
                    context_kwargs["storage_state"] = resolved_state
            context = await browser.new_context(**context_kwargs)

        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)

        return {
            "session_id": session_id,
            "playwright": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "headless": headless,
            "user_data_dir": _resolve_path(user_data_dir) if user_data_dir else None,
            "storage_state_path": _resolve_path(storage_state_path) if storage_state_path else None,
            "persistent_context": bool(user_data_dir),
            "created_at": time.time(),
            "last_used_at": time.time(),
            "lock": asyncio.Lock(),
        }
    except Exception:
        with contextlib.suppress(Exception):
            if context is not None:
                await context.close()
        with contextlib.suppress(Exception):
            if browser is not None:
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()
        raise


async def _close_idle_playwright_sessions():
    cutoff = time.time() - PLAYWRIGHT_SESSION_IDLE_SECONDS
    for session_id, session in list(_PLAYWRIGHT_SESSIONS.items()):
        if session["last_used_at"] < cutoff:
            await _close_playwright_session(session_id)


async def close_all_playwright_sessions():
    for session_id in list(_PLAYWRIGHT_SESSIONS):
        await _close_playwright_session(session_id)


async def _get_playwright_session(session_id: str, body: dict, timeout_ms: int) -> tuple[dict[str, Any], bool]:
    await _close_idle_playwright_sessions()
    async with _PLAYWRIGHT_SESSIONS_LOCK:
        session = _PLAYWRIGHT_SESSIONS.get(session_id)
        if session is not None:
            session["last_used_at"] = time.time()
            return session, False

        session = await _create_playwright_session(session_id, body, timeout_ms)
        _PLAYWRIGHT_SESSIONS[session_id] = session
        return session, True


async def _close_playwright_session(session_id: str, save_storage_state: bool = True) -> dict:
    async with _PLAYWRIGHT_SESSIONS_LOCK:
        session = _PLAYWRIGHT_SESSIONS.pop(session_id, None)

    if session is None:
        return {"success": False, "error": f"session not found: {session_id}", "session_id": session_id}

    storage_state_path = session.get("storage_state_path")
    if save_storage_state and storage_state_path:
        with contextlib.suppress(Exception):
            await session["context"].storage_state(path=storage_state_path)

    errors = []
    try:
        await session["context"].close()
    except Exception as exc:
        errors.append(f"context: {exc}")

    if not session.get("persistent_context"):
        browser = session.get("browser")
        if browser is not None:
            try:
                await browser.close()
            except Exception as exc:
                errors.append(f"browser: {exc}")

    try:
        await session["playwright"].stop()
    except Exception as exc:
        errors.append(f"playwright: {exc}")

    return {
        "success": not errors,
        "session_id": session_id,
        "closed": True,
        "errors": errors,
    }


async def _wait_after_playwright_step(page, step: dict, timeout_ms: int) -> dict:
    waits: dict[str, Any] = {}

    wait_for = step.get("wait_for")
    if wait_for:
        await page.wait_for_selector(str(wait_for), timeout=timeout_ms)
        waits["selector"] = wait_for

    wait_for_text = step.get("wait_for_text")
    if wait_for_text:
        exact = _coerce_bool(step.get("exact"), False)
        await page.get_by_text(str(wait_for_text), exact=exact).wait_for(timeout=timeout_ms)
        waits["text"] = wait_for_text

    wait_for_url = step.get("wait_for_url")
    if wait_for_url:
        await page.wait_for_url(str(wait_for_url), timeout=timeout_ms)
        waits["url"] = wait_for_url

    wait_for_load_state = step.get("wait_for_load_state")
    if wait_for_load_state:
        await page.wait_for_load_state(str(wait_for_load_state), timeout=timeout_ms)
        waits["load_state"] = wait_for_load_state

    wait_ms = int(step.get("wait_ms") or 0)
    if wait_ms > 0:
        await page.wait_for_timeout(min(wait_ms, timeout_ms))
        waits["wait_ms"] = min(wait_ms, timeout_ms)

    return waits


async def _run_playwright_session_step(page, step: dict, body: dict, timeout_ms: int) -> dict:
    step_type = (step.get("type") or step.get("action") or "").strip().lower()
    if not step_type:
        return {"success": False, "error": "missing step type/action"}

    selector = step.get("selector") or body.get("selector") or ""
    value = step.get("value", body.get("value"))
    result: dict[str, Any] = {"success": True, "type": step_type}

    if step_type in {"goto", "navigate"}:
        url = (step.get("url") or body.get("url") or "").strip()
        if not url:
            return {"success": False, "type": step_type, "error": "url required"}
        response = await page.goto(url, timeout=timeout_ms)
        result.update({
            "url": page.url,
            "status": response.status if response else None,
        })

    elif step_type == "click":
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        await page.click(selector, timeout=timeout_ms)
        result["selector"] = selector

    elif step_type == "fill":
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        await page.fill(selector, "" if value is None else str(value), timeout=timeout_ms)
        result["selector"] = selector

    elif step_type == "check":
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        await page.check(selector, timeout=timeout_ms)
        result["selector"] = selector

    elif step_type == "uncheck":
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        await page.uncheck(selector, timeout=timeout_ms)
        result["selector"] = selector

    elif step_type in {"select", "select_option"}:
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        options = step.get("values", value)
        selected = await page.select_option(selector, options, timeout=timeout_ms)
        result.update({"selector": selector, "selected": selected})

    elif step_type == "press":
        key = step.get("key") or value
        if not key:
            return {"success": False, "type": step_type, "error": "key/value required"}
        target = selector or "body"
        await page.press(target, str(key), timeout=timeout_ms)
        result.update({"selector": target, "key": key})

    elif step_type in {"upload", "set_input_files"}:
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        files = step.get("file_paths") or step.get("files") or step.get("path") or value
        if not files:
            return {"success": False, "type": step_type, "error": "file path(s) required"}
        resolved_files = _resolve_file_list(files)
        await page.set_input_files(selector, resolved_files, timeout=timeout_ms)
        result.update({"selector": selector, "files": resolved_files})

    elif step_type == "wait_for_selector":
        if not selector:
            return {"success": False, "type": step_type, "error": "selector required"}
        state = step.get("state") or "visible"
        await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        result.update({"selector": selector, "state": state})

    elif step_type == "wait_for_text":
        text = step.get("text") or value
        if not text:
            return {"success": False, "type": step_type, "error": "text/value required"}
        exact = _coerce_bool(step.get("exact"), False)
        await page.get_by_text(str(text), exact=exact).wait_for(timeout=timeout_ms)
        result.update({"text": text, "exact": exact})

    elif step_type == "wait_for_url":
        url_pattern = step.get("url") or value
        if not url_pattern:
            return {"success": False, "type": step_type, "error": "url/value required"}
        await page.wait_for_url(str(url_pattern), timeout=timeout_ms)
        result["url_pattern"] = url_pattern

    elif step_type == "wait_for_load_state":
        state = step.get("state") or value or "load"
        await page.wait_for_load_state(str(state), timeout=timeout_ms)
        result["state"] = state

    elif step_type == "get_text":
        target = selector or "body"
        text = await page.inner_text(target, timeout=timeout_ms)
        result.update({
            "selector": target,
            "text": _serializable_preview(text, int(body.get("max_chars") or DEFAULT_PLAYWRIGHT_MAX_CHARS)),
        })

    elif step_type in {"snapshot", "inspect"}:
        result["snapshot"] = await _collect_playwright_snapshot(
            page=page,
            max_chars=int(body.get("max_chars") or DEFAULT_PLAYWRIGHT_MAX_CHARS),
            include_text=_coerce_bool(body.get("include_text"), True),
            include_elements=_coerce_bool(body.get("include_elements"), True),
            include_html=_coerce_bool(body.get("include_html"), False),
        )

    elif step_type == "screenshot":
        full_page = _coerce_bool(step.get("full_page", body.get("full_page")), True)
        result["screenshot"] = await _save_playwright_screenshot(page, {**body, **step}, full_page=full_page)

    elif step_type == "evaluate":
        script = step.get("script") or value
        if not script:
            return {"success": False, "type": step_type, "error": "script/value required"}
        evaluated = await page.evaluate(str(script), step.get("arg"))
        result["result"] = _serializable_preview(evaluated, int(body.get("max_chars") or DEFAULT_PLAYWRIGHT_MAX_CHARS))

    else:
        return {"success": False, "type": step_type, "error": f"unknown step type: {step_type}"}

    waits = await _wait_after_playwright_step(page, step, timeout_ms)
    if waits:
        result["waits"] = waits
    result["current_url"] = page.url
    return result


async def action_playwright_session(body: dict) -> dict:
    """
    Mantiene sesiones Playwright persistentes para flujos web largos.
    Requiere action, o actions para batch. session_id es opcional en start/goto/batch.
    """
    requested_action = (body.get("action") or ("batch" if body.get("actions") else "start")).strip().lower()
    timeout_s = _playwright_timeout_seconds(body)
    timeout_ms = timeout_s * 1000
    session_id = (body.get("session_id") or "").strip() or f"pw_{uuid.uuid4().hex[:12]}"

    if requested_action == "list":
        return {
            "success": True,
            "sessions": [
                {
                    "session_id": sid,
                    "url": data["page"].url,
                    "headless": data.get("headless"),
                    "created_at": data.get("created_at"),
                    "last_used_at": data.get("last_used_at"),
                    "user_data_dir": data.get("user_data_dir"),
                }
                for sid, data in _PLAYWRIGHT_SESSIONS.items()
            ],
        }

    if requested_action == "close":
        return await _close_playwright_session(
            session_id,
            save_storage_state=_coerce_bool(body.get("save_storage_state"), True),
        )

    try:
        session, created = await _get_playwright_session(session_id, body, timeout_ms)
        async with session["lock"]:
            page = session["page"]
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)

            if requested_action == "batch":
                raw_steps = body.get("actions") or []
                if not isinstance(raw_steps, list) or not raw_steps:
                    return {"success": False, "error": "actions must be a non-empty list", "session_id": session_id}
                steps = raw_steps
            elif requested_action == "start":
                steps = [{"type": "goto", "url": body["url"]}] if body.get("url") else []
            else:
                steps = [{**body, "type": requested_action}]

            step_results = []
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    step_results.append({"success": False, "index": index, "error": "step must be an object"})
                    break
                step_result = await _run_playwright_session_step(page, step, body, timeout_ms)
                step_result["index"] = index
                step_results.append(step_result)
                if not step_result.get("success"):
                    break

            storage_state_path = (body.get("storage_state_path") or session.get("storage_state_path") or "").strip()
            if storage_state_path and _coerce_bool(body.get("save_storage_state"), False):
                await session["context"].storage_state(path=_resolve_path(storage_state_path))

            include_snapshot = _coerce_bool(body.get("include_snapshot"), True)
            result = {
                "success": all(step.get("success") for step in step_results),
                "session_id": session_id,
                "created": created,
                "action": requested_action,
                "timeout": timeout_s,
                "url": page.url,
                "steps": step_results,
            }
            if include_snapshot:
                result["snapshot"] = await _collect_playwright_snapshot(
                    page=page,
                    max_chars=int(body.get("max_chars") or DEFAULT_PLAYWRIGHT_MAX_CHARS),
                    include_text=_coerce_bool(body.get("include_text"), True),
                    include_elements=_coerce_bool(body.get("include_elements"), True),
                    include_html=_coerce_bool(body.get("include_html"), False),
                )
            return result

    except Exception as e:
        return {"success": False, "error": str(e), "session_id": session_id, "action": requested_action}


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
    timeout_s = min(int(body.get("timeout") or 300), 600)
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
                result["text"], result["truncated"], result["remaining_chars"] = _clip_text_content(text, max_chars)
                result["remaining_tokens"] = _count_remaining_tokens(text, max_chars)

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
        response = await _get_openai_client().responses.create(
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
    return await _get_rag().execute_safe_query(sql, embed_text=embed_text)


def action_save_preference(body: dict):
    def clean(value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def exact_or_none(value):
        if value is None:
            return None
        text = str(value)
        return None if text.strip() == "" else text

    action = clean(body.get("action"))
    old_text = exact_or_none(body.get("old_text"))
    new_text_raw = body.get("new_text")
    new_text = str(new_text_raw) if new_text_raw is not None else None
    preference = clean(body.get("preference"))
    replace_all = bool(body.get("replace_all", False))
    path = _resolve_path(user_preferences_path)

    # Backward compatibility for previous calls where agents sent empty
    # old_text/new_text plus preference to mean "add this preference".
    if action is None:
        if preference:
            action = "add"
        elif old_text and new_text is not None:
            action = "delete" if new_text == "" else "replace"

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        if action == "add":
            if not preference:
                preference = clean(new_text)
            if not preference:
                return {"success": False, "error": "preference is required for action='add'", "path": path}

            prefix = ""
            if os.path.exists(path) and os.path.getsize(path) > 0:
                prefix = "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{prefix} -{preference}")

            return {
                "success": True,
                "action": "add",
                "path": path,
                "file_hash": _file_hash(path),
            }

        if action not in {"replace", "delete"}:
            return {
                "success": False,
                "error": "action must be one of: add, replace, delete",
                "path": path,
            }

        if not old_text:
            return {"success": False, "error": f"old_text is required for action='{action}'", "path": path}

        if not os.path.exists(path):
            return {"success": False, "error": f"preferences file not found: {path}", "path": path}

        if action == "delete":
            replacement_text = ""
        else:
            if new_text is None:
                return {"success": False, "error": "new_text is required for action='replace'", "path": path}
            replacement_text = new_text

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return {
                "success": False,
                "error": "old_text not found in preferences",
                "path": path,
            }

        if not replace_all and count > 1:
            return {
                "success": False,
                "error": f"old_text found {count} times — set replace_all=true or provide more context to make it unique",
                "path": path,
                "occurrences": count,
            }

        new_content = (
            content.replace(old_text, replacement_text)
            if replace_all
            else content.replace(old_text, replacement_text, 1)
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return {
            "success": True,
            "action": action,
            "path": path,
            "replacements": count if replace_all else 1,
            "file_hash": _file_hash(path),
        }
    except Exception as e:
        return {"success": False, "path": path, "error": str(e)}

# ── Dispatcher ───────────────────────────────────────────────────────

ticket_dispatcher: dict[str, callable] = {
    "edit_file": action_edit_file,
    "run_command": action_run_command,
    "web_fetch": action_web_fetch,
    "playwright_session": action_playwright_session,
    "playwright_navigate": action_playwright_navigate,
    "save_preference": action_save_preference,
    "memory_query": action_memory_query,
    "interpret_image": action_interpret_image,
}
