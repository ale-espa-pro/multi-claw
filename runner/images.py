import base64
import copy
import mimetypes
import os
from typing import Any, Optional


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def normalize_image_detail(detail: Any) -> str:
    return detail if detail in {"low", "high", "auto"} else "auto"


def normalize_image_part(part: dict[str, Any]) -> Optional[dict[str, Any]]:
    detail = normalize_image_detail(part.get("detail"))
    normalized: dict[str, Any] = {"type": "input_image", "detail": detail}

    for key in ("image_url", "file_id", "path", "mime_type"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()

    if "image_url" not in normalized and "file_id" not in normalized and "path" not in normalized:
        return None
    return normalized


def image_input_to_part(image: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(image, dict):
        return None

    detail = normalize_image_detail(image.get("detail"))
    mime_type = (image.get("mime_type") or "").strip() or None

    file_id = (image.get("file_id") or "").strip()
    if file_id:
        return {"type": "input_image", "file_id": file_id, "detail": detail}

    url = (image.get("url") or "").strip()
    if url:
        return {"type": "input_image", "image_url": url, "detail": detail}

    data_url = (image.get("data_url") or "").strip()
    if data_url:
        return {"type": "input_image", "image_url": data_url, "detail": detail}

    path = (image.get("path") or "").strip()
    if path:
        part = {"type": "input_image", "path": path, "detail": detail}
        if mime_type:
            part["mime_type"] = mime_type
        return part

    raw_base64 = (image.get("base64") or "").strip()
    if raw_base64:
        compact_base64 = "".join(raw_base64.split())
        decoded = base64.b64decode(compact_base64, validate=True)
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {len(decoded)}")
        mime = mime_type or "image/png"
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{compact_base64}",
            "detail": detail,
        }

    return None


def build_user_message_item(text: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for image in images or []:
        try:
            image_part = image_input_to_part(image)
        except Exception as exc:
            content.append({"type": "input_text", "text": f"[imagen omitida: {exc}]"})
            continue
        if image_part is not None:
            content.append(image_part)
    return {
        "type": "message",
        "role": "user",
        "content": content,
    }


def with_replaced_message_text(message_item: dict[str, Any], text: str) -> dict[str, Any]:
    item = copy.deepcopy(message_item)
    replaced = False
    for part in item.get("content", []):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "input_text":
            part["text"] = text
            replaced = True
            break
    if not replaced:
        item.setdefault("content", []).insert(0, {"type": "input_text", "text": text})
    return item


def resolve_local_image_part(part: dict[str, Any]) -> dict[str, Any]:
    if "path" not in part:
        return part

    path = os.path.realpath(os.path.expandvars(os.path.expanduser(part["path"])))
    if not os.path.isfile(path):
        raise ValueError(f"image file not found: {path}")

    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {size}")

    ext = os.path.splitext(path)[1].lower()
    mime = part.get("mime_type") or IMAGE_EXT_TO_MIME.get(ext) or mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        image_url = f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"

    return {"type": "input_image", "image_url": image_url, "detail": part.get("detail", "auto")}


def prepare_context_for_api(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in context:
        if not isinstance(item, dict) or item.get("type") != "message":
            prepared.append(item)
            continue

        api_item = copy.deepcopy(item)
        api_content = []
        for part in api_item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "input_image":
                try:
                    api_content.append(resolve_local_image_part(part))
                except Exception as exc:
                    api_content.append({
                        "type": "input_text",
                        "text": f"[imagen no disponible para enviar al modelo: {exc}]",
                    })
            else:
                api_content.append(part)
        api_item["content"] = api_content
        prepared.append(api_item)
    return prepared
