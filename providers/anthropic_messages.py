"""Proveedor Anthropic (Messages API).

Traducción entre el formato interno del runner y la Messages API:
- message user/assistant  <->  turnos con bloques text/image
- function_call           <->  bloque tool_use (turno assistant)
- function_call_output    <->  bloque tool_result (turno user)
- reasoning (anthropic)   <->  bloque thinking (debe reenviarse intacto)

El system prompt va en el parámetro `system` y el razonamiento se controla
con thinking adaptativo + output_config.effort (modelos claude 4.6+).
"""

import json
from typing import Any, Optional

from runner import images as image_utils
from providers.result import ProviderResult

DEFAULT_MAX_OUTPUT_TOKENS = 16_000

# Valores de effort de OpenAI -> Anthropic (low|medium|high|xhigh|max).
EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


# ── Formato interno -> request Anthropic ────────────────────────────

def _image_block(part: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        part = image_utils.resolve_local_image_part(part)
    except Exception:
        return None

    url = part.get("image_url")
    if not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header.removeprefix("data:").removesuffix(";base64") or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _message_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in item.get("content", []):
        part_type = part.get("type")
        if part_type in {"input_text", "output_text"} and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part_type == "input_image":
            block = _image_block(part)
            if block is not None:
                blocks.append(block)
    return blocks


def _tool_arguments(arguments: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def context_to_messages(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convierte items internos en turnos user/assistant, agrupando bloques
    consecutivos del mismo rol (los tool_result deben ir juntos en un turno user)."""
    messages: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]):
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for item in context:
        item_type = item.get("type")
        if item_type == "message":
            role = "assistant" if item.get("role") == "assistant" else "user"
            append(role, _message_blocks(item))
        elif item_type == "function_call":
            append("assistant", [{
                "type": "tool_use",
                "id": item["call_id"],
                "name": item["name"],
                "input": _tool_arguments(item.get("arguments")),
            }])
        elif item_type == "function_call_output":
            append("user", [{
                "type": "tool_result",
                "tool_use_id": item["call_id"],
                "content": item.get("output", ""),
            }])
        elif item_type == "reasoning" and item.get("provider") == "anthropic":
            append("assistant", [item["block"]])

    return messages


def tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type == "function":
            converted.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool["parameters"],
            })
        elif tool_type == "web_search":
            converted.append({"type": "web_search_20260209", "name": "web_search"})
        else:
            converted.append(tool)
    return converted


def build_request_kwargs(
    system_prompt: str,
    context: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": params["model"],
        "max_tokens": int(params.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
        "system": system_prompt,
        "messages": context_to_messages(context),
        "thinking": {"type": "adaptive"},
    }

    effort = EFFORT_MAP.get((params.get("reasoning") or {}).get("effort"))
    if effort:
        kwargs["output_config"] = {"effort": effort}

    anthropic_tools = tools_to_anthropic(tools)
    if anthropic_tools:
        kwargs["tools"] = anthropic_tools
        if not params.get("parallel_tool_calls", False):
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    return kwargs


# ── Respuesta Anthropic -> formato interno ──────────────────────────

def items_from_response(response: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type in {"thinking", "redacted_thinking"}:
            items.append({"type": "reasoning", "provider": "anthropic", "block": block.model_dump()})
        elif block_type == "text":
            items.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": block.text}],
            })
        elif block_type == "tool_use":
            items.append({
                "type": "function_call",
                "call_id": block.id,
                "name": block.name,
                "arguments": json.dumps(block.input, ensure_ascii=False),
            })
    return items


def usage_from_response(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cached_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


class AnthropicMessagesProvider:
    def __init__(self, client=None):
        # Lazy: solo se necesita ANTHROPIC_API_KEY si algún agente usa claude-*.
        self._client = client

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(max_retries=5)
        return self._client

    async def request(
        self,
        system_prompt: str,
        context: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> ProviderResult:
        kwargs = build_request_kwargs(system_prompt, context, tools, params)
        response = await self._get_client().messages.create(**kwargs)
        return ProviderResult(
            items=items_from_response(response),
            usage=usage_from_response(response),
        )

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None
