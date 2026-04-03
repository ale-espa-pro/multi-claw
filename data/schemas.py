from typing import Any
from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str
    username: str | None = None
    metadata: dict[str, Any] | None = None
    conversation_type: str | None = None  # (None, cron, temporal)


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
