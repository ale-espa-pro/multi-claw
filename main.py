# main.py
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI
import os
from dotenv import load_dotenv

from qdrant_client import QdrantClient

from agents.agent_prompts import system_prompts
from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import ticket_dispatcher
from data.conversation_store import PostgresConversationStore
from runner.agent_runner import AgentRunner
from integrations.twilio.router import router as twilio_router, set_agent_runner

# -------------------------
# Setup
# -------------------------
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Clients
# -------------------------
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=5, timeout=600)
qdrant = QdrantClient(url="http://localhost:6333")
conversation_store = PostgresConversationStore()

# -------------------------
# Agent
# -------------------------


agent_tools = {
    #"PlannerAgent": ["ExecutorAgent"], DEMOEMNTO SIN USO
    "ExecutorAgent": ["WebSearchAgent", "DeviceManagerAgent", "save_preference"],
    "WebSearchAgent": ["web_search"],
    "DeviceManagerAgent": ["read_file", "run_python", "search_files", "run_command", "write_file", "WebSearchAgent"],
    "CronosAgent":[]
}


runner = AgentRunner(
    client=openai_client,
    system_prompts=system_prompts,
    agent_tools=agent_tools,
    dict_total_tools=dict_total_tools,
    ticket_dispatcher=ticket_dispatcher,
    main_agent="ExecutorAgent",
    conversation_store=conversation_store,
)

# -------------------------
# Twilio setup
# -------------------------
set_agent_runner(runner)
app.include_router(twilio_router)


# -------------------------
# API REST
# -------------------------
class ChatRequest(BaseModel):
    session_id: str
    message: str
    username: str | None = None
    metadata: dict[str, Any] | None = None


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


def _serialize_conversation_summary(row: dict[str, Any]) -> dict[str, Any]:
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


def _serialize_conversation_detail(row: dict[str, Any]) -> dict[str, Any]:
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
                "created_at": message["created_at"].isoformat(),
            }
            for message in row["messages"]
        ],
    }


@app.on_event("startup")
async def startup_event():
    await conversation_store.connect()
    await conversation_store.init_schema()


@app.on_event("shutdown")
async def shutdown_event():
    await runner.close()


@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    await conversation_store.ensure_conversation(
        session_id=request.session_id,
        username=request.username,
        metadata=request.metadata,
    )
    await conversation_store.append_message(
        session_id=request.session_id,
        role="user",
        content=request.message,
    )
    response = await runner.process_message(
        session_id=request.session_id,
        user_input=request.message,
    )
    await conversation_store.append_message(
        session_id=request.session_id,
        role="assistant",
        content=response,
    )
    conversation = await conversation_store.get_conversation(request.session_id)
    title = conversation["title"] if conversation is not None else None
    return ChatResponse(response=response, session_id=request.session_id, title=title)


@app.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(limit: int = 50):
    conversations = await conversation_store.list_conversations(limit=limit)
    return [_serialize_conversation_summary(row) for row in conversations]


@app.get("/conversations/{session_id}", response_model=ConversationDetail)
async def get_conversation(session_id: str):
    conversation = await conversation_store.get_conversation(session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _serialize_conversation_detail(conversation)


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    await runner.delete_session(session_id)
    return {"status": "deleted"}


@app.delete("/conversations/{session_id}")
async def delete_conversation(session_id: str):
    await runner.delete_session(session_id)
    return {"status": "deleted"}
