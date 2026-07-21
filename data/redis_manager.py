import json
import os
import redis.asyncio as redis


class RedisSessionManager:
    """Cache de contexto de sesión en Redis."""

    def __init__(self, redis_url: str | None = None, context_ttl: int | None = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.redis_client = None
        configured_ttl = context_ttl if context_ttl is not None else os.getenv(
            "REDIS_CONTEXT_TTL_SECONDS", "86400"
        )
        self.context_ttl = max(int(configured_ttl), 60)

    async def get_client(self):
        if self.redis_client is None:
            self.redis_client = await redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self.redis_client

    def _get_context_key(self, session_id: str) -> str:
        return f"agent_session:{session_id}:context"

    async def load_context(self, session_id: str, agent_names: set) -> tuple[dict, bool]:
        """Devuelve (contexto, existía_previamente)."""
        r = await self.get_client()
        data = await r.get(self._get_context_key(session_id))
        if data:
            context = json.loads(data)
            for name in agent_names:
                if name not in context:
                    context[name] = []
            return context, True

        return {name: [] for name in agent_names}, False

    async def save_context(self, session_id: str, context: dict):
        r = await self.get_client()
        await r.set(
            self._get_context_key(session_id),
            json.dumps(context, ensure_ascii=False),
            ex=self.context_ttl
        )

    async def delete_session(self, session_id: str):
        r = await self.get_client()
        # Se borran también las claves legacy (:state y la base) de versiones previas.
        await r.delete(
            f"agent_session:{session_id}",
            self._get_context_key(session_id),
            f"agent_session:{session_id}:state",
        )

    async def close(self):
        if self.redis_client:
            await self.redis_client.aclose()
            self.redis_client = None
