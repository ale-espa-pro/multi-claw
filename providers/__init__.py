"""Proveedores de modelos: traducen entre el formato interno del runner y cada API.

El formato interno es el de runner/context.py (items message / function_call /
function_call_output / reasoning). Cada proveedor implementa:

    async def request(system_prompt, context, tools, params) -> ProviderResult
    async def close()
"""

from dataclasses import dataclass
from typing import Any

from providers.anthropic_messages import AnthropicMessagesProvider
from providers.openai_responses import OpenAIResponsesProvider
from providers.result import ProviderResult


def resolve_provider_name(params: dict[str, Any]) -> str:
    """Proveedor explícito en config, o inferido por el prefijo del modelo."""
    explicit = params.get("provider")
    if explicit:
        return str(explicit)
    model = params.get("model") or ""
    return "anthropic" if model.startswith("claude") else "openai"


__all__ = [
    "AnthropicMessagesProvider",
    "OpenAIResponsesProvider",
    "ProviderResult",
    "resolve_provider_name",
]
