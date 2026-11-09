from typing import Any
from pydantic import BaseModel, Field, field_validator


class ChatImage(BaseModel):
    url: str | None = None
    path: str | None = None
    data_url: str | None = None
    file_id: str | None = None
    base64: str | None = None
    mime_type: str | None = None
    detail: str | None = None


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str
    images: list[ChatImage] | None = None
    username: str | None = None
    metadata: dict[str, Any] | None = None
    conversation_type: str | None = None  # (None, cron, temporal)

    @field_validator("session_id")
    @classmethod
    def session_id_is_one_path_segment(cls, value: str) -> str:
        # Da nombre a la carpeta de trabajo de la sesión.
        if value in {".", ".."} or any(char in value for char in "/\\\0"):
            raise ValueError("session_id cannot contain path separators")
        return value


class ChatResponse(BaseModel):
    response: str
    session_id: str
    title: str | None = None


class ConversationSummary(BaseModel):
    session_id: str
    title: str
    preview: str
    username: str | None = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    message_count: int


class ConversationMessage(BaseModel):
    id: int
    role: str
    content: str
    data: dict[str, Any] | None = None
    created_at: str


class ConversationDetail(BaseModel):
    session_id: str
    title: str
    preview: str
    username: str | None = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    message_count: int
    messages: list[ConversationMessage]


def serialize_conversation_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "title": row["title"],
        "preview": row["preview"],
        "username": row["username"],
        "metadata": row["metadata"] or {},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "message_count": row["message_count"],
    }


class TraceLaneItem(BaseModel):
    kind: str
    role: str | None = None
    text: str | None = None
    provider: str | None = None
    name: str | None = None
    call_id: str | None = None
    arguments: Any | None = None
    result: Any | None = None
    target_agent: str | None = None


class ConversationTrace(BaseModel):
    session_id: str
    agents: list[str]
    lanes: dict[str, list[TraceLaneItem]]


def serialize_conversation_detail(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "title": row["title"],
        "preview": row["preview"],
        "username": row["username"],
        "metadata": row["metadata"] or {},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "message_count": row["message_count"],
        "messages": [
            {
                "id": message["id"],
                "role": message["role"],
                "content": message["content"],
                "data": message.get("data"),
                "created_at": message["created_at"].isoformat(),
            }
            for message in row["messages"]
        ],
    }
