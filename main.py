# main.py
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
from dotenv import load_dotenv

from agents.agent_builder import AgentBuilder
from data.conversation_store import PostgresConversationStore
from data.schemas import (
    ChatRequest, ChatResponse,
    ConversationSummary, ConversationDetail,
    serialize_conversation_summary, serialize_conversation_detail,
)
from runner.agent_runner import AgentRunner
from tools.memoryTools.RAG_memory import MemoryRag
from integrations.twilio.router import router as twilio_router, set_agent_runner

# ── Setup ──
load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Clients & Agent ──
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=5, timeout=1000)
conversation_store = PostgresConversationStore()
memory_rag = MemoryRag(conversation_store=conversation_store)

agent_builder = AgentBuilder()
agent_builder.load_agents()

runner = AgentRunner(
    client=openai_client,
    agent_builder=agent_builder,
    conversation_store=conversation_store,
    memory_rag=memory_rag,
)

# ── Twilio ──
set_agent_runner(runner)
app.include_router(twilio_router)


# ── Lifecycle ──
@app.on_event("startup")
async def startup_event():
    await conversation_store.connect()
    await conversation_store.init_schema()


@app.on_event("shutdown")
async def shutdown_event():
    await runner.close()


# ── Endpoints ──
@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
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
    conversation = await conversation_store.get_conversation(request.session_id)
    title = conversation["title"] if conversation is not None else None
    return ChatResponse(response=response, session_id=request.session_id, title=title)


@app.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(limit: int = 50):
    conversations = await conversation_store.list_conversations(limit=limit)
    return [serialize_conversation_summary(row) for row in conversations]


@app.get("/conversations/{session_id}", response_model=ConversationDetail)
async def get_conversation(session_id: str):
    conversation = await conversation_store.get_conversation(session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return serialize_conversation_detail(conversation)


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    await runner.delete_session(session_id)
    return {"status": "deleted"}


@app.delete("/conversations/{session_id}")
async def delete_conversation(session_id: str):
    await runner.delete_session(session_id)
    return {"status": "deleted"}
