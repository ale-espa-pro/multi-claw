"""Confinamiento de escritura por agente.

El write-root viaja en un ContextVar y no en el body de la tool: los argumentos
los redacta el modelo, así que un límite transportado por ahí sería falsificable.
`asyncio.to_thread` y `TaskGroup.create_task` copian el contexto vigente, de modo
que el valor que fija el runner antes de despachar llega intacto a la tool y lo
heredan las llamadas anidadas de los subagentes de esa rama.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar

current_write_root: ContextVar[str | None] = ContextVar("current_write_root", default=None)


@contextmanager
def write_root_scope(root: str | None):
    """Fija el sandbox de escritura para todo lo que ocurra dentro del bloque."""
    token = current_write_root.set(root)
    try:
        yield
    finally:
        current_write_root.reset(token)


def get_write_root() -> str | None:
    return current_write_root.get()


def is_within(real_path: str, root: str) -> bool:
    root_path = os.path.realpath(root)
    real_path = os.path.realpath(real_path)
    return real_path == root_path or real_path.startswith(root_path + os.sep)
