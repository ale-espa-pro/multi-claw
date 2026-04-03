import asyncio
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from data.conversation_store import PostgresConversationStore
from tools.memoryTools.semantic_splitter import EMBED_MODEL, semantic_split

load_dotenv()


class MemoryRag:
    """RAG minimo para guardar y buscar chunks conversacionales."""

    def __init__(self, conversation_store: PostgresConversationStore | None = None):
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=5, timeout=1000)
        self.conversation_store = conversation_store or PostgresConversationStore()
        self._owns_store = conversation_store is None
        self._connected = False

    async def connect(self):
        if self._connected:
            return
        await self.conversation_store.connect()
        await self.conversation_store.init_schema()
        self._connected = True

    async def close(self):
        if self._owns_store and self._connected:
            await self.conversation_store.close()
        self._connected = False

    def embed_text(self, text: str):
        return semantic_split(
            texts=[text],
            client=self.client,
            threshold_percentile=25,
            min_tokens=50,
            max_tokens=400,
            overlap_tokens=20,
        )[0]

    def embed_query(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=EMBED_MODEL,
            input=text.strip() or " ",
        )
        return list(response.data[0].embedding)

    async def store_text_embeddings(
        self,
        session_id: str,
        text: str,
        conversation_type: str | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        await self.connect()
        await self.conversation_store.ensure_conversation(
            session_id=session_id,
            conversation_type=conversation_type,
        )

        if not text.strip():
            if replace:
                await self.conversation_store.delete_chunks(session_id)
            return {
                "session_id": session_id,
                "stored_chunks": 0,
                "conversation_type": conversation_type,
                "chunks": [],
            }

        split_result = await asyncio.to_thread(self.embed_text, text)
        chunks = [
            {
                "chunck": chunk_text,
                "embedding": chunk_embedding.tolist(),
            }
            for chunk_text, chunk_embedding in zip(
                split_result.chunk_texts,
                split_result.chunk_embeddings,
            )
        ]

        saved_chunks = await self.conversation_store.save_chunks(
            session_id=session_id,
            chunks=chunks,
            conversation_type=conversation_type,
            replace=replace,
        )

        return {
            "session_id": session_id,
            "stored_chunks": len(saved_chunks),
            "conversation_type": conversation_type,
            "chunks": saved_chunks,
        }

    async def search_similar_chunks(
        self,
        session_id: str,
        query: str,
        limit: int = 5,
        conversation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.connect()
        query_embedding = await asyncio.to_thread(self.embed_query, query)
        return await self.conversation_store.search_similar_chunks(
            query_embedding=query_embedding,
            session_id=session_id,
            conversation_type=conversation_type,
            limit=limit,
        )
