import os
import re
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class PostgresConversationStore:
    """Persistencia simple de conversaciones y mensajes visibles para la web."""

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
    def messages_table(self) -> str:
        return f"{self.schema}.conversation_messages"

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
    def _build_title(text: str) -> str:
        compact = " ".join(text.strip().split())
        if not compact:
            return "Nueva conversacion"
        return compact[:60]

    @staticmethod
    def _build_preview(text: str) -> str:
        compact = " ".join(text.strip().split())
        return compact[:140]

    async def init_schema(self):
        pool = self._require_pool()
        async with pool.connection() as conn:
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
                        title TEXT NOT NULL DEFAULT 'Nueva conversacion',
                        preview TEXT NOT NULL DEFAULT '',
                        username TEXT,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """.format(conversations_table=self.conversations_table)
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS {messages_table} (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES {conversations_table}(session_id) ON DELETE CASCADE,
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """.format(
                        messages_table=self.messages_table,
                        conversations_table=self.conversations_table,
                    )
                )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                    ON {conversations_table} (updated_at DESC);
                    """.format(conversations_table=self.conversations_table)
                )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_id_id
                    ON {messages_table} (session_id, id);
                    """.format(messages_table=self.messages_table)
                )

    async def ensure_conversation(
        self,
        session_id: str,
        username: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        pool = self._require_pool()
        metadata = metadata or {}

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO {conversations_table} (session_id, username, metadata)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE
                    SET username = COALESCE(EXCLUDED.username, {conversations_table}.username),
                        metadata = {conversations_table}.metadata || EXCLUDED.metadata
                    """.format(conversations_table=self.conversations_table),
                    (session_id, username, Jsonb(metadata)),
                )

    async def append_message(self, session_id: str, role: str, content: str):
        pool = self._require_pool()
        preview = self._build_preview(content)
        title = self._build_title(content)

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO {conversations_table} (session_id)
                    VALUES (%s)
                    ON CONFLICT (session_id) DO NOTHING
                    """.format(conversations_table=self.conversations_table),
                    (session_id,),
                )
                await cur.execute(
                    """
                    INSERT INTO {messages_table} (session_id, role, content)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """.format(messages_table=self.messages_table),
                    (session_id, role, content),
                )
                row = await cur.fetchone()
                message_id = row[0]

                await cur.execute(
                    """
                    UPDATE {conversations_table}
                    SET preview = %s,
                        updated_at = now(),
                        title = CASE
                            WHEN %s = 'user'
                             AND {conversations_table}.title = 'Nueva conversacion'
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM {messages_table}
                                 WHERE session_id = %s
                                   AND role = 'user'
                                   AND id <> %s
                             )
                            THEN %s
                            ELSE {conversations_table}.title
                        END
                    WHERE {conversations_table}.session_id = %s
                    """.format(
                        conversations_table=self.conversations_table,
                        messages_table=self.messages_table,
                    ),
                    (preview, role, session_id, message_id, title, session_id),
                )

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        c.session_id,
                        c.title,
                        c.preview,
                        c.username,
                        c.metadata,
                        c.created_at,
                        c.updated_at,
                        COALESCE(m.message_count, 0) AS message_count
                    FROM {conversations_table} AS c
                    LEFT JOIN (
                        SELECT session_id, COUNT(*) AS message_count
                        FROM {messages_table}
                        GROUP BY session_id
                    ) AS m
                    ON m.session_id = c.session_id
                    ORDER BY c.updated_at DESC
                    LIMIT %s
                    """.format(
                        conversations_table=self.conversations_table,
                        messages_table=self.messages_table,
                    ),
                    (limit,),
                )
                return await cur.fetchall()

    async def get_conversation(self, session_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT session_id, title, preview, username, metadata, created_at, updated_at
                    FROM {conversations_table}
                    WHERE session_id = %s
                    """.format(conversations_table=self.conversations_table),
                    (session_id,),
                )
                conversation = await cur.fetchone()
                if conversation is None:
                    return None

                await cur.execute(
                    """
                    SELECT id, role, content, created_at
                    FROM {messages_table}
                    WHERE session_id = %s
                    ORDER BY id ASC
                    """.format(messages_table=self.messages_table),
                    (session_id,),
                )
                messages = await cur.fetchall()

        conversation["messages"] = messages
        conversation["message_count"] = len(messages)
        return conversation

    async def delete_conversation(self, session_id: str):
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {self.conversations_table} WHERE session_id = %s",
                    (session_id,),
                )

    async def build_agent_context(self, session_id: str) -> list[dict[str, Any]]:
        conversation = await self.get_conversation(session_id)
        if conversation is None:
            return []

        context: list[dict[str, Any]] = []
        for message in conversation["messages"]:
            role = message["role"]
            text_type = "input_text" if role == "user" else "output_text"
            context.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": text_type, "text": message["content"]}],
                }
            )
        return context
