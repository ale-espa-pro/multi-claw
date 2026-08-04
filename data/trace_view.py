"""Pure helpers to turn a conversation's context_jsonb into per-agent trace lanes.

Cada agente con su propia clave en context_jsonb es un "carril". Una función
solo se clasifica como subagent_call cuando su nombre coincide con otro carril
(las tools normales nunca tienen su propia clave en context_jsonb).
"""

import json
from typing import Any


def _extract_message_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    image_count = 0
    for part in item.get("content", []) or []:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif part_type == "input_image":
            image_count += 1

    text = "\n".join(parts)
    if image_count:
        suffix = f"[{image_count} imagen(es) adjunta(s)]"
        text = f"{text}\n{suffix}" if text else suffix
    return text


def _extract_reasoning_text(item: dict[str, Any]) -> str:
    provider = item.get("provider")

    if provider == "anthropic":
        block = item.get("block") or {}
        block_type = block.get("type")
        if block_type == "thinking":
            return str(block.get("thinking", ""))
        if block_type == "redacted_thinking":
            return "[razonamiento redactado por el proveedor]"
        return json.dumps(block, ensure_ascii=False)

    if provider == "openai":
        summary = item.get("summary")
        if isinstance(summary, list):
            texts = [
                part.get("text", "")
                for part in summary
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)
        return "[resumen de razonamiento no disponible]"

    return json.dumps(item, ensure_ascii=False)


def _parse_arguments(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _resolve_target_agent(name: str, result: Any, lane_names: set[str]) -> str | None:
    """Carril al que apunta una función, si es que invoca a otro agente.

    Los agentes estáticos se llaman por su nombre, así que la tool coincide con
    el carril. Los del enjambre se crean con deploy_swarm/spawn_agent y su carril
    solo aparece en el agent_id que devuelve la llamada.
    """
    if name in lane_names:
        return name
    if isinstance(result, dict):
        agent_id = result.get("agent_id")
        if isinstance(agent_id, str) and agent_id in lane_names:
            return agent_id
    return None


def _build_lane(items: list[dict[str, Any]], lane_names: set[str]) -> list[dict[str, Any]]:
    outputs_by_call_id: dict[str, Any] = {}
    for item in items:
        if item.get("type") == "function_call_output":
            outputs_by_call_id[item.get("call_id")] = item.get("output")

    lane: list[dict[str, Any]] = []
    for item in items:
        item_type = item.get("type")

        if item_type == "message":
            lane.append({
                "kind": "message",
                "role": item.get("role", "unknown"),
                "text": _extract_message_text(item),
            })

        elif item_type == "reasoning":
            lane.append({
                "kind": "reasoning",
                "provider": item.get("provider", "unknown"),
                "text": _extract_reasoning_text(item),
            })

        elif item_type == "function_call":
            name = item.get("name", "")
            call_id = item.get("call_id")
            result = _parse_arguments(outputs_by_call_id.get(call_id))
            target_agent = _resolve_target_agent(name, result, lane_names)
            lane.append({
                "kind": "subagent_call" if target_agent else "tool_call",
                "name": name,
                "call_id": call_id,
                "arguments": _parse_arguments(item.get("arguments")),
                "result": result,
                "target_agent": target_agent,
            })

        # function_call_output ya se fusiona en su function_call correspondiente.

    return lane


def build_agent_trace(context_jsonb: dict[str, Any]) -> dict[str, Any]:
    """Construye los carriles por agente para la vista de inspección.

    Devuelve {"agents": [...nombres ordenados...], "lanes": {agente: [items]}}.
    """
    if not isinstance(context_jsonb, dict):
        context_jsonb = {}

    lane_names = set(context_jsonb.keys())

    return {
        "agents": sorted(lane_names),
        "lanes": {
            agent_name: _build_lane(items if isinstance(items, list) else [], lane_names)
            for agent_name, items in context_jsonb.items()
        },
    }
