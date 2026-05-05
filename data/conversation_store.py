import os
import re
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class PostgresConversationStore:
    """Persistencia minima del snapshot completo de una conversacion."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self.host = host or os.getenv("MULTIAGENT_PG_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("MULTIAGENT_PG_PORT", "5432"))
        self.database = database or os.getenv("MULTIAGENT_PG_DB", "web")
        self.user = user or os.getenv("MULTIAGENT_PG_USER", "admin")
        self.password = password or os.getenv("MULTIAGENT_PG_PASSWORD")
        raw_schema = os.getenv("MULTIAGENT_PG_SCHEMA", "multiagente")
        self.schema = re.sub(r"[^a-zA-Z0-9_]", "", raw_schema) or "multiagente"
        self.pool: AsyncConnectionPool | None = None
        self.vector_enabled = False

    @property
    def conninfo(self) -> str:
        return (
            f"host={self.host} "
            f"port={self.port} "
            f"dbname={self.database} "
            f"user={self.user} "
            f"password={self.password}"
        )

    @property
    def conversations_table(self) -> str:
        return f"{self.schema}.conversations"

    @property
    def conversation_chunks_table(self) -> str:
        return f"{self.schema}.conversation_chunks"

    async def connect(self):
        if self.pool is None:
            self.pool = AsyncConnectionPool(
                conninfo=self.conninfo,
                min_size=1,
                max_size=5,
                open=False,
            )
            await self.pool.open()

    async def close(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> AsyncConnectionPool:
        if self.pool is None:
            raise RuntimeError("PostgresConversationStore is not connected")
        return self.pool

    @staticmethod
    def _extract_text_from_item(item: dict[str, Any]) -> str:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                    return part["text"]
            return ""
        if item_type == "function_call":
            return f'{item.get("name", "")}({item.get("arguments", "")})'
        if item_type == "function_call_output":
            output = item.get("output", "")
            return output[:500] if isinstance(output, str) else ""
        return ""

    @staticmethod
    def _build_title(text: str) -> str:
        compact = " ".join(text.strip().split())
        if not compact:
            return "Nueva conversacion"
        return compact[:60]

    @staticmethod
    def _build_preview(text: str) -> str:
        compact = " ".join(text.strip().split())
        return compact[:140]

    @staticmethod
    def _normalize_context(context_jsonb: Any) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(context_jsonb, dict):
            return {}

        normalized: dict[str, list[dict[str, Any]]] = {}
        for agent_name, items in context_jsonb.items():
            if not isinstance(agent_name, str):
                continue
            if not isinstance(items, list):
                normalized[agent_name] = []
                continue
            normalized[agent_name] = [item for item in items if isinstance(item, dict)]
        return normalized

    def _flatten_context_items(self, context_jsonb: Any) -> list[dict[str, Any]]:
        normalized_context = self._normalize_context(context_jsonb)
        flattened: list[dict[str, Any]] = []
        for agent_name, items in normalized_context.items():
            for item in items:
                flattened.append({"agent_name": agent_name, **item})
        return flattened

    def _select_primary_conversation_items(self, context_jsonb: Any) -> list[dict[str, Any]]:
        normalized_context = self._normalize_context(context_jsonb)
        best_items: list[dict[str, Any]] = []
        best_score = -1

        for agent_name, items in normalized_context.items():
            visible_items = []
            score = 0
            for item in items:
                if item.get("type") != "message":
                    continue
                role = item.get("role")
                if role not in {"user", "assistant"}:
                    continue
                visible_items.append({"agent_name": agent_name, **item})
                score += 1

            if score > best_score:
                best_score = score
                best_items = visible_items

        return best_items

    @staticmethod
    def _vector_literal(embedding: list[float]) -> str:
        if len(embedding) != 3072:
            raise ValueError(f"Expected embedding dimension 3072, got {len(embedding)}")
        return "[" + ",".join(format(float(value), ".12g") for value in embedding) + "]"

    def _is_vector_enabled(self) -> bool:
        return bool(getattr(self, "vector_enabled", False))

    def _embedding_column_type(self) -> str:
        return "halfvec(3072)" if self._is_vector_enabled() else "JSONB"

    def _embedding_sql_cast(self) -> str:
        return "halfvec(3072)" if self._is_vector_enabled() else "JSONB"

    def _embedding_index_ops(self) -> str:
        return "halfvec_cosine_ops"

    @staticmethod
    def _normalize_bm25_query(query: str) -> str:
        return " ".join(str(query or "").split())

    def _build_snapshot_view(
        self,
        session_id: str,
        context_jsonb: Any,
        created_at,
        updated_at,
        username: str | None = None,
        metadata: dict[str, Any] | None = None,
        conversation_type: str | None = None,
        archived_at=None,
    ) -> dict[str, Any]:
        visible_items = self._select_primary_conversation_items(context_jsonb)
        messages: list[dict[str, Any]] = []
        first_user_text = ""
        last_text = ""

        for idx, item in enumerate(visible_items, start=1):
            role = item.get("role", "unknown")
            content = self._extract_text_from_item(item)
            if role == "user" and content and not first_user_text:
                first_user_text = content
            if content:
                last_text = content

            messages.append(
                {
                    "id": idx,
                    "role": role if isinstance(role, str) else "unknown",
                    "content": content,
                    "data": item,
                    "created_at": updated_at,
                }
            )

        return {
            "session_id": session_id,
            "title": self._build_title(first_user_text),
            "preview": self._build_preview(last_text),
            "username": username,
            "metadata": metadata or {},
            "conversation_type": conversation_type,
            "created_at": created_at,
            "updated_at": updated_at,
            "archived_at": archived_at,
            "message_count": len(messages),
            "messages": messages,
            "context_jsonb": self._normalize_context(context_jsonb),
        }

    async def init_schema(self):
        pool = self._require_pool()
        async with pool.connection() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    self.vector_enabled = True
            except (errors.UndefinedFile, errors.InsufficientPrivilege):
                self.vector_enabled = False
                await conn.rollback()

            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    CREATE SCHEMA IF NOT EXISTS {self.schema};
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS {conversations_table} (
                        session_id TEXT PRIMARY KEY,
                        context_jsonb JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        username TEXT,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        conversation_type TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        archived_at TIMESTAMPTZ
                    );
                    """.format(conversations_table=self.conversations_table)
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS {conversation_chunks_table} (
                        session_id TEXT NOT NULL REFERENCES {conversations_table}(session_id) ON DELETE CASCADE,
                        message_order INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        conversation_type TEXT,
                        chunck TEXT NOT NULL DEFAULT '',
                        embedding {embedding_type},
                        PRIMARY KEY (session_id, message_order)
                    );
                    """.format(
                        conversation_chunks_table=self.conversation_chunks_table,
                        conversations_table=self.conversations_table,
                        embedding_type=self._embedding_column_type(),
                    )
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS context_jsonb JSONB NOT NULL DEFAULT '{{}}'::jsonb"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS username TEXT"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS conversation_type TEXT"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversations_table} ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS conversation_type TEXT"
                )
                await cur.execute(
                    f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS chunck TEXT NOT NULL DEFAULT ''"
                )
                if self._is_vector_enabled():
                    await cur.execute(
                        f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS embedding halfvec(3072)"
                    )
                else:
                    await cur.execute(
                        f"ALTER TABLE {self.conversation_chunks_table} ADD COLUMN IF NOT EXISTS embedding JSONB"
                    )
                await cur.execute(
                    """
                    SELECT udt_name
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = %s
                      AND column_name = 'embedding'
                    """,
                    (self.schema, "conversation_chunks"),
                )
                embedding_column = await cur.fetchone()
                embedding_udt_name = embedding_column[0] if embedding_column else None
                if self._is_vector_enabled() and embedding_udt_name != "halfvec":
                    await cur.execute(
                        """
                        ALTER TABLE {conversation_chunks_table}
                        ALTER COLUMN embedding TYPE halfvec(3072)
                        USING CASE
                            WHEN embedding IS NULL THEN NULL
                            ELSE embedding::text::halfvec(3072)
                        END
                        """.format(conversation_chunks_table=self.conversation_chunks_table)
                    )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                    ON {conversations_table} (updated_at DESC);
                    """.format(conversations_table=self.conversations_table)
                )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conversation_chunks_updated_at
                    ON {conversation_chunks_table} (updated_at DESC);
                    """.format(conversation_chunks_table=self.conversation_chunks_table)
                )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conversation_chunks_chunck_fts
                    ON {conversation_chunks_table}
                    USING gin (to_tsvector('simple', coalesce(chunck, '')));
                    """.format(conversation_chunks_table=self.conversation_chunks_table)
                )
                if self._is_vector_enabled():
                    await cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_conversation_chunks_embedding_hnsw
                        ON {conversation_chunks_table}
                        USING hnsw (embedding {embedding_index_ops});
                        """.format(
                            conversation_chunks_table=self.conversation_chunks_table,
                            embedding_index_ops=self._embedding_index_ops(),
                        )
                    )

    async def ensure_conversation(
        self,
        session_id: str,
        username: str | None = None,
        metadata: dict[str, Any] | None = None,
        conversation_type: str | None = None,
    ):
        pool = self._require_pool()
        metadata = metadata or {}

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO {conversations_table} (
                        session_id,
                        username,
                        metadata,
                        conversation_type
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE
                    SET username = COALESCE(EXCLUDED.username, {conversations_table}.username),
                        metadata = {conversations_table}.metadata || EXCLUDED.metadata,
                        conversation_type = COALESCE(
                            EXCLUDED.conversation_type,
                            {conversations_table}.conversation_type
                        )
                    """.format(conversations_table=self.conversations_table),
                    (session_id, username, Jsonb(metadata), conversation_type),
                )

    async def save_context(
        self,
        session_id: str,
        context: dict[str, Any],
        username: str | None = None,
        metadata: dict[str, Any] | None = None,
        conversation_type: str | None = None,
        archived: bool = False,
    ):
        pool = self._require_pool()
        metadata = metadata or {}
        normalized_context = self._normalize_context(context)

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO {conversations_table} (
                        session_id,
                        context_jsonb,
                        username,
                        metadata,
                        conversation_type,
                        archived_at
                    )
                    VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)
                    ON CONFLICT (session_id) DO UPDATE
                    SET context_jsonb = EXCLUDED.context_jsonb,
                        username = COALESCE(EXCLUDED.username, {conversations_table}.username),
                        metadata = {conversations_table}.metadata || EXCLUDED.metadata,
                        conversation_type = COALESCE(
                            EXCLUDED.conversation_type,
                            {conversations_table}.conversation_type
                        ),
                        updated_at = now(),
                        archived_at = CASE
                            WHEN %s THEN now()
                            ELSE {conversations_table}.archived_at
                        END
                    """.format(conversations_table=self.conversations_table),
                    (
                        session_id,
                        Jsonb(normalized_context),
                        username,
                        Jsonb(metadata),
                        conversation_type,
                        archived,
                        archived,
                    ),
                )

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        session_id,
                        context_jsonb,
                        username,
                        metadata,
                        conversation_type,
                        created_at,
                        updated_at,
                        archived_at
                    FROM {conversations_table}
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """.format(conversations_table=self.conversations_table),
                    (limit,),
                )
                rows = await cur.fetchall()

        return [
            self._build_snapshot_view(
                session_id=row["session_id"],
                context_jsonb=row["context_jsonb"],
                username=row.get("username"),
                metadata=row.get("metadata"),
                conversation_type=row.get("conversation_type"),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                archived_at=row.get("archived_at"),
            )
            for row in rows
        ]

    async def get_conversation(self, session_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        session_id,
                        context_jsonb,
                        username,
                        metadata,
                        conversation_type,
                        created_at,
                        updated_at,
                        archived_at
                    FROM {conversations_table}
                    WHERE session_id = %s
                    """.format(conversations_table=self.conversations_table),
                    (session_id,),
                )
                row = await cur.fetchone()

        if row is None:
            return None

        return self._build_snapshot_view(
            session_id=row["session_id"],
            context_jsonb=row["context_jsonb"],
            username=row.get("username"),
            metadata=row.get("metadata"),
            conversation_type=row.get("conversation_type"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row.get("archived_at"),
        )

    async def delete_conversation(self, session_id: str):
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {self.conversations_table} WHERE session_id = %s",
                    (session_id,),
                )

    async def execute_query(self, query: str) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query)
                return await cur.fetchall()

    async def execute_readonly_query(self, sql: str) -> list[dict[str, Any]]:
        """Execute a SQL query inside a READ ONLY transaction for safety."""
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET TRANSACTION READ ONLY")
                    await cur.execute(sql)
                    return await cur.fetchall()

    async def load_context(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT context_jsonb
                    FROM {conversations_table}
                    WHERE session_id = %s
                    """.format(conversations_table=self.conversations_table),
                    (session_id,),
                )
                row = await cur.fetchone()

        if row is None:
            return {}

        return self._normalize_context(row.get("context_jsonb"))

    async def delete_chunks(self, session_id: str):
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {self.conversation_chunks_table} WHERE session_id = %s",
                    (session_id,),
                )

    async def get_next_chunk_order(self, session_id: str) -> int:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(MAX(message_order) + 1, 0) AS next_order
                    FROM {conversation_chunks_table}
                    WHERE session_id = %s
                    """.format(conversation_chunks_table=self.conversation_chunks_table),
                    (session_id,),
                )
                row = await cur.fetchone()
        return int(row["next_order"]) if row is not None else 0

    async def save_chunks(
        self,
        session_id: str,
        chunks: list[dict[str, Any]],
        conversation_type: str | None = None,
        replace: bool = False,
    ) -> list[dict[str, Any]]:
        pool = self._require_pool()
        normalized_chunks = []

        for idx, chunk in enumerate(chunks):
            text = str(chunk.get("chunck", "")).strip()
            embedding = chunk.get("embedding")
            if not text or not isinstance(embedding, list):
                continue
            normalized_chunks.append(
                {
                    "message_order": int(chunk.get("message_order", idx)),
                    "chunck": text,
                    "embedding": embedding,
                }
            )

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                if replace:
                    await cur.execute(
                        f"DELETE FROM {self.conversation_chunks_table} WHERE session_id = %s",
                        (session_id,),
                    )
                    next_order = 0
                else:
                    await cur.execute(
                        """
                        SELECT COALESCE(MAX(message_order) + 1, 0)
                        FROM {conversation_chunks_table}
                        WHERE session_id = %s
                        """.format(conversation_chunks_table=self.conversation_chunks_table),
                        (session_id,),
                    )
                    row = await cur.fetchone()
                    next_order = int(row[0]) if row and row[0] is not None else 0

                saved_chunks: list[dict[str, Any]] = []
                for offset, chunk in enumerate(normalized_chunks):
                    message_order = next_order + offset
                    if self._is_vector_enabled():
                        await cur.execute(
                            """
                        INSERT INTO {conversation_chunks_table} (
                            session_id,
                            message_order,
                            conversation_type,
                            chunck,
                            embedding
                        )
                        VALUES (%s, %s, %s, %s, %s::{embedding_sql_cast})
                        ON CONFLICT (session_id, message_order) DO UPDATE
                        SET conversation_type = COALESCE(
                                EXCLUDED.conversation_type,
                                {conversation_chunks_table}.conversation_type
                            ),
                                chunck = EXCLUDED.chunck,
                                embedding = EXCLUDED.embedding,
                                updated_at = now()
                        """.format(
                            conversation_chunks_table=self.conversation_chunks_table,
                            embedding_sql_cast=self._embedding_sql_cast(),
                        ),
                        (
                            session_id,
                            message_order,
                            conversation_type,
                            chunk["chunck"],
                                self._vector_literal(chunk["embedding"]),
                            ),
                        )
                    else:
                        await cur.execute(
                            """
                            INSERT INTO {conversation_chunks_table} (
                                session_id,
                                message_order,
                                conversation_type,
                                chunck,
                                embedding
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (session_id, message_order) DO UPDATE
                            SET conversation_type = COALESCE(
                                    EXCLUDED.conversation_type,
                                    {conversation_chunks_table}.conversation_type
                                ),
                                chunck = EXCLUDED.chunck,
                                embedding = EXCLUDED.embedding,
                                updated_at = now()
                            """.format(conversation_chunks_table=self.conversation_chunks_table),
                            (
                                session_id,
                                message_order,
                                conversation_type,
                                chunk["chunck"],
                                Jsonb(chunk["embedding"]),
                            ),
                        )
                    saved_chunks.append(
                        {
                            "session_id": session_id,
                            "message_order": message_order,
                            "conversation_type": conversation_type,
                            "chunck": chunk["chunck"],
                        }
                    )

        return saved_chunks

    async def search_similar_chunks(
        self,
        query_embedding: list[float],
        session_id: str | None = None,
        conversation_type: str | None = None,
        limit: int = 5,
        min_similarity: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self._is_vector_enabled():
            return []

        pool = self._require_pool()
        filters: list[str] = ["embedding IS NOT NULL"]
        query_vector = self._vector_literal(query_embedding)
        params: list[Any] = [query_vector]

        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)

        if conversation_type is not None:
            filters.append("conversation_type = %s")
            params.append(conversation_type)

        if min_similarity is not None:
            filters.append("(1 - (embedding <=> %s::{embedding_sql_cast})) >= %s".format(
                embedding_sql_cast=self._embedding_sql_cast(),
            ))
            params.extend([query_vector, min_similarity])

        params.append(limit)
        where_clause = " AND ".join(filters)

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        session_id,
                        message_order,
                        created_at,
                        updated_at,
                        conversation_type,
                        chunck,
                        embedding <=> %s::{embedding_sql_cast} AS distance
                    FROM {conversation_chunks_table}
                    WHERE {where_clause}
                    ORDER BY embedding <=> %s::{embedding_sql_cast}
                    LIMIT %s
                    """.format(
                        conversation_chunks_table=self.conversation_chunks_table,
                        where_clause=where_clause,
                        embedding_sql_cast=self._embedding_sql_cast(),
                    ),
                    [params[0], *params[1:-1], params[0], params[-1]],
                )
                rows = await cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            distance = float(row["distance"])
            results.append(
                {
                    "session_id": row["session_id"],
                    "message_order": row["message_order"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "conversation_type": row.get("conversation_type"),
                    "chunck": row["chunck"],
                    "distance": distance,
                    "score": 1.0 - distance,
                }
            )
        return results

    async def search_keyword_chunks(
        self,
        query: str,
        session_id: str | None = None,
        conversation_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        normalized_query = self._normalize_bm25_query(query)
        if not normalized_query:
            return []

        pool = self._require_pool()
        filters: list[str] = [
            "to_tsvector('simple', coalesce(chunck, '')) @@ q.query"
        ]
        params: list[Any] = [normalized_query]

        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)

        if conversation_type is not None:
            filters.append("conversation_type = %s")
            params.append(conversation_type)

        params.append(limit)
        where_clause = " AND ".join(filters)

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH q AS (
                        SELECT websearch_to_tsquery('simple', %s) AS query
                    )
                    SELECT
                        session_id,
                        message_order,
                        created_at,
                        updated_at,
                        conversation_type,
                        chunck,
                        ts_rank_cd(
                            to_tsvector('simple', coalesce(chunck, '')),
                            q.query
                        ) AS keyword_score
                    FROM {conversation_chunks_table}, q
                    WHERE {where_clause}
                    ORDER BY keyword_score DESC, updated_at DESC
                    LIMIT %s
                    """.format(
                        conversation_chunks_table=self.conversation_chunks_table,
                        where_clause=where_clause,
                    ),
                    params,
                )
                rows = await cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            keyword_score = float(row["keyword_score"] or 0.0)
            results.append(
                {
                    "session_id": row["session_id"],
                    "message_order": row["message_order"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "conversation_type": row.get("conversation_type"),
                    "chunck": row["chunck"],
                    "keyword_score": keyword_score,
                    "score": keyword_score,
                    "retrieval_method": "keyword",
                }
            )
        return results
