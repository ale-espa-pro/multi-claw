# main.py
import asyncio
import logging
import os
import secrets

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from openai import DefaultAioHttpClient
from dotenv import load_dotenv

from agents.agent_builder import AgentBuilder
from data.conversation_store import TEMPORAL_CONVERSATION, PostgresConversationStore
from data.schemas import (
    ChatRequest, ChatResponse,
    ConversationSummary, ConversationDetail, ConversationTrace,
    serialize_conversation_summary, serialize_conversation_detail,
)
from data.trace_view import build_agent_trace
from runner.agent_runner import AgentRunner
from tools import ticket_dispatcher as ticket_dispatcher_module
from tools.memoryTools.RAG_memory import MemoryRag
from integrations.twilio.router import router as twilio_router, set_agent_runner

# ── Setup ──
load_dotenv()

logger = logging.getLogger(__name__)
app = FastAPI()
# La API da acceso a una terminal: sin CORS_ORIGINS solo se admite la propia
# máquina, para que una web cualquiera no pueda invocarla desde el navegador.
cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=None if cors_origins else r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Clients & Agent ──
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=5, timeout=1000,  http_client=DefaultAioHttpClient())
conversation_store = PostgresConversationStore()
memory_rag = MemoryRag(conversation_store=conversation_store)

# Las tools comparten el mismo pool de Postgres y cliente OpenAI que la app.
ticket_dispatcher_module.configure(memory_rag=memory_rag, openai_client=openai_client)

agent_builder = AgentBuilder()
agent_builder.load_agents()

runner = AgentRunner(
    client=openai_client,
    agent_builder=agent_builder,
    conversation_store=conversation_store,
    memory_rag=memory_rag,
)

CHAT_API_KEY = os.getenv("CHAT_API_KEY")


if not CHAT_API_KEY:
    logger.warning("CHAT_API_KEY no está definida: la API (y su terminal) queda sin autenticación")


def require_api_key(x_api_key: str | None = Header(default=None)):
    if CHAT_API_KEY and not secrets.compare_digest((x_api_key or "").encode(), CHAT_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


api = APIRouter(dependencies=[Depends(require_api_key)])

# ── Twilio ──
set_agent_runner(runner)
app.include_router(twilio_router)


# ── Lifecycle ──
@app.on_event("startup")
async def startup_event():
    max_attempts = int(os.getenv("MULTIAGENT_PG_STARTUP_ATTEMPTS", "6"))
    delay_seconds = float(os.getenv("MULTIAGENT_PG_STARTUP_RETRY_SECONDS", "2"))

    for attempt in range(1, max_attempts + 1):
        try:
            await conversation_store.connect()
            await conversation_store.init_schema()
            return
        except Exception:
            await conversation_store.close()
            if attempt == max_attempts:
                raise
            logger.warning(
                "Database startup attempt %s/%s failed; retrying in %.1f seconds",
                attempt,
                max_attempts,
                delay_seconds,
                exc_info=True,
            )
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 10)


@app.on_event("shutdown")
async def shutdown_event():
    await ticket_dispatcher_module.close_all_playwright_sessions()
    await runner.close()


# ── Endpoints ──
@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.get("/inspector")
async def serve_inspector():
    return FileResponse("inspector.html")


@api.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    temporal = request.conversation_type == TEMPORAL_CONVERSATION
    if not temporal:
        await conversation_store.ensure_conversation(
            session_id=request.session_id,
            username=request.username,
            metadata=request.metadata,
            conversation_type=request.conversation_type,
        )
    response = await runner.process_message(
        session_id=request.session_id,
        user_input=request.message,
        images=[image.model_dump(exclude_none=True) for image in request.images or []],
        conversation_type=request.conversation_type,
    )
    conversation = (
        None if temporal else await conversation_store.get_conversation(request.session_id, visible_only=True)
    )
    title = conversation["title"] if conversation is not None else None
    return ChatResponse(response=response, session_id=request.session_id, title=title)


@api.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(limit: int = Query(default=50, ge=1, le=500)):
    conversations = await conversation_store.list_conversations(limit=limit)
    return [serialize_conversation_summary(row) for row in conversations]


@api.get("/conversations/{session_id}", response_model=ConversationDetail)
async def get_conversation(session_id: str):
    conversation = await conversation_store.get_conversation(session_id, visible_only=True)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return serialize_conversation_detail(conversation)


@api.delete("/conversations/{session_id}")
async def delete_conversation(session_id: str):
    await runner.delete_session(session_id)
    return {"status": "deleted"}


@api.get("/conversations/{session_id}/trace", response_model=ConversationTrace)
async def get_conversation_trace(session_id: str):
    conversation = await conversation_store.get_conversation(session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    trace = build_agent_trace(conversation["context_jsonb"])
    return {"session_id": session_id, **trace}


app.include_router(api)
