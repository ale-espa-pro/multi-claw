import asyncio
import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from data.conversation_store import PostgresConversationStore
from tools.memoryTools.semantic_splitter import (
    EMBED_MODEL,
    MAX_TOKENS_PER_INPUT,
    _get_encoder,
    count_tokens,
    semantic_split,
    split_text_by_tokens,
)

MAX_OUTPUT_CHARS = 200_000
MAX_OUTPUT_WORDS = 20_000

BLOCKED_SQL_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "COPY", "EXECUTE", "CALL",
}


def _truncate_output(data: Any) -> dict:
    """Serialize data and truncate to 200K chars / 20K words."""
    output = json.dumps(data, default=str, ensure_ascii=False)
    original_output = output
    original_chars = len(output)
    original_words = len(output.split())
    truncated = False
    truncated_chars = 0
    truncated_words = 0

    if len(output) > MAX_OUTPUT_CHARS:
        truncated_chars = len(output) - MAX_OUTPUT_CHARS
        output = output[:MAX_OUTPUT_CHARS]
        truncated = True

    if len(output.split()) > MAX_OUTPUT_WORDS:
        words = output.split()
        truncated_words = len(words) - MAX_OUTPUT_WORDS
        output = " ".join(words[:MAX_OUTPUT_WORDS])
        truncated = True

    if truncated:
        return {
            "truncated": True,
            "remaining_chars": max(original_chars - len(output), truncated_chars, 0),
            "remaining_words": max(original_words - len(output.split()), truncated_words, 0),
            "remaining_tokens": max(count_tokens(original_output) - count_tokens(output), 0),
            "result": output,
        }
    return {
        "truncated": False,
        "remaining_chars": 0,
        "remaining_words": 0,
        "remaining_tokens": 0,
        "result": data,
    }

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
        safe_input = split_text_by_tokens(
            text.strip() or " ",
            _get_encoder(),
            MAX_TOKENS_PER_INPUT,
        )[0]
        response = self.client.embeddings.create(
            model=EMBED_MODEL,
            input=safe_input,
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
        query: str,
        session_id: str | None = None,
        limit: int = 5,
        conversation_type: str | None = None,
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        await self.connect()
        query_embedding = await asyncio.to_thread(self.embed_query, query)
        return await self.conversation_store.search_similar_chunks(
            query_embedding=query_embedding,
            session_id=session_id,
            conversation_type=conversation_type,
            limit=limit,
            min_similarity=min_similarity,
        )

    async def search_keyword_chunks(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
        conversation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.connect()
        return await self.conversation_store.search_keyword_chunks(
            query=query,
            session_id=session_id,
            conversation_type=conversation_type,
            limit=limit,
        )

    @staticmethod
    def _merge_ranked_chunks(
        vector_results: list[dict[str, Any]],
        keyword_results: list[dict[str, Any]],
        limit: int,
        vector_weight: float = 0.7,
        keyword_weight: float = 0.3,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[str, int], dict[str, Any]] = {}

        def add_result(
            result: dict[str, Any],
            rank: int,
            method: str,
            weight: float,
        ):
            key = (str(result.get("session_id")), int(result.get("message_order", -1)))
            current = merged.setdefault(key, dict(result))
            current["score"] = float(current.get("score") or 0.0)
            current["hybrid_score"] = float(current.get("hybrid_score") or 0.0)
            current["hybrid_score"] += weight / (rrf_k + rank)

            methods = set(str(current.get("retrieval_method") or "").split("+"))
            methods.discard("")
            methods.add(method)
            current["retrieval_method"] = "+".join(sorted(methods))

            if method == "vector":
                current["vector_rank"] = rank
                if "score" in result:
                    current["vector_score"] = result["score"]
                if "distance" in result:
                    current["distance"] = result["distance"]
            else:
                current["keyword_rank"] = rank
                current["keyword_score"] = result.get("keyword_score", result.get("score"))

            current["score"] = current["hybrid_score"]

        for rank, result in enumerate(vector_results, start=1):
            add_result(result, rank, "vector", vector_weight)
        for rank, result in enumerate(keyword_results, start=1):
            add_result(result, rank, "keyword", keyword_weight)

        return sorted(
            merged.values(),
            key=lambda item: float(item.get("hybrid_score") or 0.0),
            reverse=True,
        )[:limit]

    async def search_chunks(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
        conversation_type: str | None = None,
        min_similarity: float | None = None,
        mode: str = "vector",
        vector_weight: float = 0.7,
        keyword_weight: float = 0.3,
    ) -> list[dict[str, Any]]:
        normalized_mode = (mode or "vector").strip().lower()
        if normalized_mode == "keyword":
            return await self.search_keyword_chunks(
                query=query,
                session_id=session_id,
                limit=limit,
                conversation_type=conversation_type,
            )
        if normalized_mode != "hybrid":
            return await self.search_similar_chunks(
                query=query,
                session_id=session_id,
                limit=limit,
                conversation_type=conversation_type,
                min_similarity=min_similarity,
            )

        expanded_limit = max(limit * 4, limit)
        vector_results, keyword_results = await asyncio.gather(
            self.search_similar_chunks(
                query=query,
                session_id=session_id,
                limit=expanded_limit,
                conversation_type=conversation_type,
                min_similarity=min_similarity,
            ),
            self.search_keyword_chunks(
                query=query,
                session_id=session_id,
                limit=expanded_limit,
                conversation_type=conversation_type,
            ),
        )
        return self._merge_ranked_chunks(
            vector_results=vector_results,
            keyword_results=keyword_results,
            limit=limit,
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
        )

    async def execute_safe_query(self, sql: str, embed_text: str | None = None) -> dict:
        """Execute a read-only SQL query with optional embedding injection and output truncation."""
        await self.connect()

        # Inject embedding if requested
        if embed_text and "$EMBEDDING$" in sql:
            embedding = await asyncio.to_thread(self.embed_query, embed_text)
            vector_literal = self.conversation_store._vector_literal(embedding)
            sql = sql.replace("$EMBEDDING$", f"'{vector_literal}'")
        elif "$EMBEDDING$" in sql and not embed_text:
            return {"success": False, "error": "La query usa $EMBEDDING$ pero no se proporcionó embed_text"}

        normalized = " ".join(sql.strip().split()).upper()

        if not (
            normalized.startswith("SELECT")
            or normalized.startswith("WITH")
            or normalized.startswith("EXPLAIN")
        ):
            return {
                "success": False,
                "error": "Solo se permiten consultas de lectura (SELECT / WITH / EXPLAIN)",
            }

        for kw in BLOCKED_SQL_KEYWORDS:
            if re.search(rf"\b{kw}\b", normalized):
                return {"success": False, "error": f"Operación no permitida: {kw}"}

        try:
            rows = await self.conversation_store.execute_readonly_query(sql)
            out = _truncate_output(rows)
            return {
                "success": True,
                "row_count": len(rows),
                "truncated": out["truncated"],
                "remaining_chars": out["remaining_chars"],
                "remaining_words": out["remaining_words"],
                "remaining_tokens": out["remaining_tokens"],
                "data": out["result"],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
