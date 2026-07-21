from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    """Respuesta de un proveedor ya traducida al formato interno."""

    items: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
