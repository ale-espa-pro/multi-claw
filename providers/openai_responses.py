"""Proveedor OpenAI (Responses API).

El formato interno del runner ya es casi el de la Responses API, así que la
traducción se limita a resolver imágenes locales y a gestionar los items de
reasoning (se conservan los propios y se omiten los de otros proveedores).
"""

import asyncio
from typing import Any

from runner import images as image_utils
from providers.result import ProviderResult


def context_to_input(system_prompt: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for item in image_utils.prepare_context_for_api(context):
        if item.get("type") == "reasoning":
            if item.get("provider") == "openai":
                items.append({k: v for k, v in item.items() if k != "provider"})
            continue
        items.append(item)
    return items


def items_from_response(response: Any) -> list[dict[str, Any]]:
    from runner import context as context_utils

    items: list[dict[str, Any]] = []
    for raw in response.output:
        data = raw.model_dump()
        if data.get("type") == "reasoning":
            reasoning = {k: v for k, v in data.items() if v is not None and k != "status"}
            items.append({**reasoning, "provider": "openai"})
            continue
        normalized = context_utils.normalize_context_item(data)
        if normalized is not None:
            items.append(normalized)
    return items


def usage_from_response(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    details = getattr(usage, "input_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
    }


class OpenAIResponsesProvider:
    def __init__(self, client):
        self.client = client

    async def request(
        self,
        system_prompt: str,
        context: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> ProviderResult:
        kwargs: dict[str, Any] = {
            "model": params["model"],
            "reasoning": params["reasoning"],
            "parallel_tool_calls": params["parallel_tool_calls"],
        }
        if params.get("text"):
            kwargs["text"] = params["text"]

        response = await self.client.responses.create(
            input=context_to_input(system_prompt, context),
            tools=tools,
            **kwargs,
        )
        return ProviderResult(
            items=items_from_response(response),
            usage=usage_from_response(response),
        )

    async def close(self):
        close_fn = getattr(self.client, "close", None)
        if close_fn is not None:
            res = close_fn()
            if asyncio.iscoroutine(res):
                await res
