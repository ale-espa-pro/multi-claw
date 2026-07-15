import copy
import json
from typing import Any, Optional

from runner.images import normalize_image_part


def extract_message_text(message_item: dict[str, Any]) -> Optional[str]:
    for part in reversed(message_item.get("content", [])):
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type not in {"output_text", "input_text", "text"}:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            return text
    return None


def normalize_message_content(content: Any, role: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        text_type = "input_text" if role == "user" else "output_text"
        return [{"type": text_type, "text": content}]

    if not isinstance(content, list):
        return []

    normalized_parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text"}:
            text = part.get("text")
            if isinstance(text, str):
                normalized_parts.append({"type": part_type, "text": text})
        elif part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                mapped_type = "input_text" if role == "user" else "output_text"
                normalized_parts.append({"type": mapped_type, "text": text})
        elif part_type == "input_image":
            normalized_image = normalize_image_part(part)
            if normalized_image is not None:
                normalized_parts.append(normalized_image)
    return normalized_parts


def normalize_context_item(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    item_type = item.get("type")

    if item_type == "message":
        role = item.get("role")
        if role not in {"user", "assistant", "developer", "system"}:
            return None
        content = normalize_message_content(item.get("content"), role)
        if not content:
            return None
        return {"type": "message", "role": role, "content": content}

    if item_type == "function_call":
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if not all(isinstance(v, str) for v in (call_id, name, arguments)):
            return None
        return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}

    if item_type == "function_call_output":
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            return None
        output = item.get("output", "")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    return None


def normalize_full_context(
    context: dict[str, Any],
    agent_names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(context, dict):
        context = {}
    normalized_context: dict[str, list[dict[str, Any]]] = {}
    for agent_name in agent_names:
        raw_items = context.get(agent_name, [])
        if not isinstance(raw_items, list):
            raw_items = []
        normalized_context[agent_name] = [
            normalized for item in raw_items if (normalized := normalize_context_item(item)) is not None
        ]
    return normalized_context


def serialize_context_for_memory(context: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    for agent_name in sorted(context.keys()):
        for item in context.get(agent_name, []):
            item_type = item.get("type")
            if item_type == "message":
                role = item.get("role", "unknown")
                text = extract_message_text(item)
                if text:
                    lines.append(f"[{agent_name}] {role}: {text}")
                image_count = sum(
                    1
                    for part in item.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "input_image"
                )
                if image_count:
                    lines.append(f"[{agent_name}] {role}: [{image_count} imagen(es) adjunta(s)]")
            elif item_type == "function_call":
                lines.append(
                    f"[{agent_name}] function_call {item.get('name', '')}: {item.get('arguments', '')}"
                )
            elif item_type == "function_call_output":
                output = item.get("output", "")
                if isinstance(output, str) and output:
                    lines.append(f"[{agent_name}] function_output: {output}")
    return "\n".join(lines)


def find_context_overlap(
    before_items: list[dict[str, Any]],
    after_items: list[dict[str, Any]],
) -> int:
    max_overlap = min(len(before_items), len(after_items))
    for overlap in range(max_overlap, 0, -1):
        if before_items[-overlap:] == after_items[:overlap]:
            return overlap
    return 0


def build_context_delta(
    before_context: dict[str, Any],
    after_context: dict[str, Any],
    agent_names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    before = normalize_full_context(before_context, agent_names)
    after = normalize_full_context(after_context, agent_names)
    delta: dict[str, list[dict[str, Any]]] = {}

    for agent_name in agent_names:
        before_items = before.get(agent_name, [])
        after_items = after.get(agent_name, [])
        overlap = find_context_overlap(before_items, after_items)
        delta[agent_name] = copy.deepcopy(after_items[overlap:])

    return delta


def serialize_tool_result(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def serialize_user_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)
