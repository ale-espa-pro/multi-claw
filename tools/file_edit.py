"""Inspect UTF-8 files and edit an explicitly observed version of their bytes."""

import contextlib
import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app_paths import get_allowed_write_roots
from runner.sandbox import get_write_root, is_within


DEFAULT_MAX_CHARS = 12_000
ALLOWED_WRITE_ROOTS = get_allowed_write_roots()


class _FileChangedError(Exception):
    """The path or its contents changed while processing an operation."""


class _UnsupportedTextError(ValueError):
    """The file is not a regular UTF-8 text file."""


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    metadata: os.stat_result

    def file_hash(self) -> dict[str, Any]:
        return {
            "algorithm": "md5",
            "value": hashlib.md5(self.data).hexdigest(),
            "bytes": len(self.data),
            "mtime_ns": self.metadata.st_mtime_ns,
        }


def _resolve_path(path: str) -> str:
    return os.path.realpath(os.path.expandvars(os.path.expanduser(path)))


def _write_denial(path: str) -> str | None:
    write_root = get_write_root()
    if write_root is not None and not is_within(path, write_root):
        return f"sandbox violation: este agente solo puede escribir dentro de {write_root}"
    if not any(is_within(path, root) for root in ALLOWED_WRITE_ROOTS):
        return f"write not allowed outside: {ALLOWED_WRITE_ROOTS}"
    return None


@contextlib.contextmanager
def _path_lock(path: str) -> Iterator[None]:
    """Use a stable lock inode across atomic replacements and server workers.

    Each call opens its own descriptor, so Linux flock also serializes threads.
    Lock files must remain in place: unlinking one could split waiting callers
    across different inodes. Shell commands do not participate in this lock.
    """
    directory = os.path.join(tempfile.gettempdir(), f"multi-claw-file-edit-{os.getuid()}")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    metadata = os.lstat(directory)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PermissionError("file edit lock directory must be private and owned by this user")

    name = hashlib.sha256(os.fsencode(path)).hexdigest() + ".lock"
    descriptor = os.open(
        os.path.join(directory, name),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        # Closing releases the lock even if flock or the operation raised.
        os.close(descriptor)


def _version(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
    )


def _read_snapshot(path: str) -> _Snapshot:
    """Read and hash the same bytes, rejecting a file modified during the read."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise _UnsupportedTextError("only regular UTF-8 text files are supported")
        data = source.read()
        after = os.fstat(source.fileno())
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as error:
        raise _FileChangedError from error
    if _version(before) != _version(after) or _version(after) != _version(current):
        raise _FileChangedError
    return _Snapshot(data, after)


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise _UnsupportedTextError("binary files containing NUL bytes cannot be edited")
    try:
        # Decoding bytes directly preserves CRLF and mixed line endings.
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _UnsupportedTextError("file must contain valid UTF-8 text") from error


def _conflict(path: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "file_conflict",
        "path": path,
        "message": "The file changed since it was inspected; no edit was applied.",
        "action_required": (
            "Call edit_file with action='inspect', review the current content, "
            "then retry action='edit' with the hash returned by that inspection."
        ),
    }


def _atomic_replace(path: str, data: bytes, original: _Snapshot) -> _Snapshot:
    """Keep mode and untouched bytes; recheck external changes before replace."""
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".agent-edit-", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fchmod(destination.fileno(), stat.S_IMODE(original.metadata.st_mode))
            os.fsync(destination.fileno())
            updated = _Snapshot(data, os.fstat(destination.fileno()))

        try:
            current = _read_snapshot(path)
        except FileNotFoundError as error:
            raise _FileChangedError from error
        if (
            current.data != original.data
            or _version(current.metadata) != _version(original.metadata)
        ):
            raise _FileChangedError
        denial = _write_denial(path)
        if denial:
            raise PermissionError(denial)
        # Non-cooperating external writers still have a small final race window.
        os.replace(temporary_path, path)
        return updated
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_path)


def _invalid(message: str) -> dict[str, Any]:
    return {"success": False, "error": "invalid_arguments", "message": message}


def action_edit_file(body: dict[str, Any]) -> dict[str, Any]:
    """Inspect a file, or replace exact text using its previously observed hash.

    ``inspect`` accepts a character ``offset`` and ``max_chars``. Its hash always
    covers the complete file. ``edit`` requires ``old_text``, ``new_text`` and
    ``expected_file_hash``; use ``replace_all`` to replace multiple occurrences.
    This tool never creates a file. Creation belongs to the terminal.
    """
    if not isinstance(body, dict):
        return _invalid("arguments must be an object")
    supplied_path = body.get("path")
    if not isinstance(supplied_path, str) or not supplied_path or "\x00" in supplied_path:
        return _invalid("path must be a non-empty string without NUL bytes")

    default_action = "edit" if "old_text" in body or "new_text" in body else "inspect"
    action = body.get("action", default_action)
    if action not in ("inspect", "edit"):
        return _invalid("action must be 'inspect' or 'edit'")
    path = _resolve_path(supplied_path)

    offset = body.get("offset", 0)
    max_chars = body.get("max_chars", DEFAULT_MAX_CHARS)
    if action == "inspect":
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return _invalid("offset must be an integer greater than or equal to zero")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            return _invalid("max_chars must be a positive integer")
    else:
        denial = _write_denial(path)
        if denial:
            return {"success": False, "error": denial, "path": path}
        expected_hash = body.get("expected_file_hash")
        if expected_hash is None or (isinstance(expected_hash, str) and not expected_hash.strip()):
            return {
                "success": False,
                "error": "read_required",
                "path": path,
                "action_required": (
                    "Call edit_file with action='inspect' and review the content first. "
                    "Pass its file_hash.value as expected_file_hash when editing."
                ),
            }
        if not isinstance(expected_hash, str):
            return _invalid("expected_file_hash must be the MD5 string returned by inspect")
        expected_hash = expected_hash.strip().lower()
        if len(expected_hash) != 32 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            return _invalid("expected_file_hash must be a 32-character MD5 hexadecimal string")
        old_text, new_text = body.get("old_text"), body.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            return _invalid("old_text must be a non-empty string")
        if not isinstance(new_text, str):
            return _invalid("new_text must be a string")
        if "\x00" in old_text or "\x00" in new_text:
            return _invalid("old_text and new_text must not contain NUL bytes")
        replace_all = body.get("replace_all", False)
        if not isinstance(replace_all, bool):
            return _invalid("replace_all must be a boolean")

    try:
        with _path_lock(path):
            snapshot = _read_snapshot(path)
            file_hash = snapshot.file_hash()
            if action == "edit" and file_hash["value"] != expected_hash:
                return _conflict(path)
            content = _decode_text(snapshot.data)
            if action == "inspect":
                end = min(offset + max_chars, len(content))
                remaining = max(len(content) - end, 0)
                return {
                    "success": True,
                    "action": "inspect",
                    "path": path,
                    "file_hash": file_hash,
                    "offset": offset,
                    "max_chars": max_chars,
                    "total_chars": len(content),
                    "truncated": remaining > 0,
                    "remaining_chars": remaining,
                    # Keep the observed version visible if the runner clips a large page.
                    "content": content[offset:end],
                }

            count = content.count(old_text)
            if count == 0:
                return {"success": False, "error": "old_text not found in file", "path": path}
            if count > 1 and not replace_all:
                return {
                    "success": False,
                    "error": (
                        f"old_text found {count} times; set replace_all=true or provide "
                        "more context to make it unique"
                    ),
                    "path": path,
                    "occurrences": count,
                }
            updated_content = content.replace(old_text, new_text, -1 if replace_all else 1)
            updated = _atomic_replace(path, updated_content.encode("utf-8"), snapshot)
            return {
                "success": True,
                "action": "edit",
                "path": path,
                "replacements": count if replace_all else 1,
                "file_hash": updated.file_hash(),
            }
    except _FileChangedError:
        return _conflict(path)
    except _UnsupportedTextError as error:
        return {"success": False, "error": "unsupported_text", "message": str(error), "path": path}
    except UnicodeEncodeError:
        return _invalid("new_text must be encodable as valid UTF-8")
    except FileNotFoundError:
        return {
            "success": False,
            "error": "file_not_found",
            "path": path,
            "message": "File does not exist. Create new files through the terminal.",
        }
    except OSError as error:
        return {"success": False, "error": str(error), "path": path}
