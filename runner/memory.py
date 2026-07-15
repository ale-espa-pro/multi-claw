import asyncio
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tools.memoryTools.RAG_memory import MemoryRag


def normalize_similarity_threshold(value: Any, default: float = 0.35) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, numeric))


def normalize_retrieval_limit(value: Any, default: int = 5) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, numeric)


def normalize_retrieval_mode(value: Any, default: str = "vector") -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"vector", "keyword", "hybrid"} else default


def normalize_retrieval_weight(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, numeric)


@dataclass(frozen=True)
class MemoryRetrievalConfig:
    limit: int
    min_similarity: float
    mode: str
    vector_weight: float
    keyword_weight: float
    query_max_chars: int = 12_000

    @classmethod
    def from_env(cls) -> "MemoryRetrievalConfig":
        return cls(
            limit=normalize_retrieval_limit(os.getenv("MEMORY_RETRIEVAL_LIMIT", "5")),
            min_similarity=normalize_similarity_threshold(os.getenv("MEMORY_MIN_SIMILARITY", "0.55")),
            mode=normalize_retrieval_mode(os.getenv("MEMORY_RETRIEVAL_MODE", "vector")),
            vector_weight=normalize_retrieval_weight(os.getenv("MEMORY_VECTOR_WEIGHT", "0.7"), 0.7),
            keyword_weight=normalize_retrieval_weight(os.getenv("MEMORY_KEYWORD_WEIGHT", "0.3"), 0.3),
        )


class MemoryService:
    def __init__(
        self,
        memory_rag: MemoryRag | None,
        config: MemoryRetrievalConfig,
        background_tasks: set[asyncio.Task],
        get_memory_lock: Callable[[str], Awaitable[asyncio.Lock]],
        build_context_delta: Callable[[dict[str, Any], dict[str, Any]], dict[str, list[dict[str, Any]]]],
        serialize_context_for_memory: Callable[[dict[str, list[dict[str, Any]]]], str],
    ):
        self.memory_rag = memory_rag
        self.config = config
        self.background_tasks = background_tasks
        self.get_memory_lock = get_memory_lock
        self.build_context_delta = build_context_delta
        self.serialize_context_for_memory = serialize_context_for_memory

    def prepare_query(self, text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) <= self.config.query_max_chars:
            return compact
        return compact[:self.config.query_max_chars]

    @staticmethod
    def format_retrieved_memory(chunks: list[dict[str, Any]]) -> str:
        lines = [
            "[EXTRA DE MEMORIA AUTOMATICA]",
            (
                "Estos fragmentos vienen de la base de memoria de conversaciones y son un extra "
                "potencialmente util para contextualizar la solicitud actual o decidir si conviene "
                "hacer una busqueda de memoria mas especifica. No forman parte literal del mensaje del usuario."
            ),
        ]

        for idx, chunk in enumerate(chunks, start=1):
            header = f"[{idx}] session_id={chunk.get('session_id', 'unknown')}"
            conversation_type = chunk.get("conversation_type")
            if conversation_type:
                header += f" | conversation_type={conversation_type}"
            score = chunk.get("score")
            if isinstance(score, (int, float)):
                header += f" | score={score:.4f}"
            method = chunk.get("retrieval_method")
            if method:
                header += f" | retrieval={method}"
            lines.append(header)
            lines.append(str(chunk.get("chunck", "")).strip())

        return "\n".join(lines)

    async def augment_user_message(self, user_text: str, session_id: str) -> str:
        if self.memory_rag is None or self.config.limit <= 0:
            return user_text

        query_text = self.prepare_query(user_text)
        if not query_text:
            return user_text

        try:
            chunks = await self.memory_rag.search_chunks(
                query=query_text,
                limit=self.config.limit,
                min_similarity=self.config.min_similarity,
                mode=self.config.mode,
                vector_weight=self.config.vector_weight,
                keyword_weight=self.config.keyword_weight,
            )
        except Exception as exc:
            print(f"\033[1;31m  [MEMORY] Retrieval error: {exc}\033[0m")
            return user_text

        if not chunks:
            print(
                f"\033[1;35m  [MEMORY] 0 chunks para {session_id} "
                f"(mode={self.config.mode}, k={self.config.limit}, "
                f"min_similarity={self.config.min_similarity:.2f})\033[0m"
            )
            return user_text

        print(
            f"\033[1;35m  [MEMORY] {len(chunks)} chunks recuperados para "
            f"{session_id} "
            f"(mode={self.config.mode}, k={self.config.limit}, "
            f"min_similarity={self.config.min_similarity:.2f})\033[0m"
        )
        return f"{user_text}\n\n{self.format_retrieved_memory(chunks)}"

    def schedule_semantic_sync(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        previous_context: dict[str, list[dict[str, Any]]] | None = None,
        conversation_type: str | None = None,
    ):
        if self.memory_rag is None:
            return

        context_snapshot = self.build_context_delta(previous_context or {}, context)
        memory_text = self.serialize_context_for_memory(context_snapshot)
        if not memory_text.strip():
            return

        async def _runner():
            try:
                lock = await self.get_memory_lock(session_id)
                async with lock:
                    await self.memory_rag.store_text_embeddings(
                        session_id=session_id,
                        text=memory_text,
                        conversation_type=conversation_type,
                        replace=False,
                    )
            except Exception as exc:
                print(f"\033[1;31m  [MEMORY] Sync error for {session_id}: {exc}\033[0m")

        task = asyncio.create_task(_runner())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
