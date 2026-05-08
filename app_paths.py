import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKING_DIR_NAME = "working-dir"


def _resolve(path: str | os.PathLike) -> str:
    expanded = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return str(expanded.resolve())


def get_working_path() -> str:
    return _resolve(os.getenv("WORKING_PATH") or PROJECT_ROOT / DEFAULT_WORKING_DIR_NAME)


def get_sessions_path(working_path: str | os.PathLike | None = None) -> str:
    if working_path is None and os.getenv("SESSIONS_PATH"):
        return _resolve(os.environ["SESSIONS_PATH"])
    return _resolve(Path(working_path or get_working_path()) / "sessions")


def get_crons_path(working_path: str | os.PathLike | None = None) -> str:
    if working_path is None and os.getenv("CRONS_PATH"):
        return _resolve(os.environ["CRONS_PATH"])
    return _resolve(Path(working_path or get_working_path()) / "crons")


def get_workflows_path(working_path: str | os.PathLike | None = None) -> str:
    if working_path is None and os.getenv("WORKFLOW_PATH"):
        return _resolve(os.environ["WORKFLOW_PATH"])
    return _resolve(Path(working_path or get_working_path()) / "workflows")


def get_user_preferences_path(working_path: str | os.PathLike | None = None) -> str:
    if working_path is None and os.getenv("USER_PREFERENCES_PATH"):
        return _resolve(os.environ["USER_PREFERENCES_PATH"])
    return _resolve(Path(working_path or get_working_path()) / "memory" / "user_preferences.txt")


def get_playwright_output_dir() -> str:
    return _resolve(os.getenv("PLAYWRIGHT_OUTPUT_DIR") or Path(get_working_path()) / "playwright")


def get_allowed_write_roots() -> list[str]:
    configured = os.getenv("ALLOWED_WRITE_ROOTS")
    if configured:
        return [_resolve(item.strip()) for item in configured.split(",") if item.strip()]

    roots = [
        get_working_path(),
        Path.home() / "Downloads",
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path("/tmp"),
    ]
    return [_resolve(root) for root in roots]
