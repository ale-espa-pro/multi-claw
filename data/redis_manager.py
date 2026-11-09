import json
import logging
import os

import redis.asyncio as redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Un contexto puede ocupar varios MB, pero Redis no debe retener una petición.
SOCKET_TIMEOUT_SECONDS = 5
_CACHE_ERRORS = (RedisError, OSError, ValueError)


class RedisSessionManager:
    """Cache de contexto de sesión en Redis.

    Postgres es la fuente de verdad: un fallo de Redis equivale a un fallo de
    caché y nunca debe tumbar una petición.
    """

    def __init__(self, redis_url: str | None = None, context_ttl: int | None = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.redis_client = None
        configured_ttl = context_ttl if context_ttl is not None else os.getenv(
            "REDIS_CONTEXT_TTL_SECONDS", "86400"
        )
        self.context_ttl = max(int(configured_ttl), 60)
        # Sesiones cuya última escritura no llegó a Redis: lo que siga allí está
        # obsoleto y leerlo descartaría turnos ya guardados en Postgres.
        self._stale_sessions: set[str] = set()

    async def get_client(self):
        if self.redis_client is None:
            self.redis_client = await redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
            )
        return self.redis_client

    def _get_context_key(self, session_id: str) -> str:
        return f"agent_session:{session_id}:context"

    async def load_context(self, session_id: str, agent_names: set) -> tuple[dict, bool]:
        """Devuelve (contexto, existía_previamente)."""
        empty = {name: [] for name in agent_names}
        if session_id in self._stale_sessions:
            return empty, False

        try:
            r = await self.get_client()
            data = await r.get(self._get_context_key(session_id))
            context = json.loads(data) if data else None
        except _CACHE_ERRORS as exc:
            logger.warning("Redis no disponible al leer %s: %s", session_id, exc)
            return empty, False

        if context is None:
            return empty, False
        for name in agent_names:
            context.setdefault(name, [])
        return context, True

    async def save_context(self, session_id: str, context: dict):
        try:
            r = await self.get_client()
            await r.set(
                self._get_context_key(session_id),
                json.dumps(context, ensure_ascii=False),
                ex=self.context_ttl
            )
        except _CACHE_ERRORS as exc:
            logger.warning("Redis no disponible al guardar %s: %s", session_id, exc)
            self._stale_sessions.add(session_id)
        else:
            self._stale_sessions.discard(session_id)

    async def delete_session(self, session_id: str):
        try:
            r = await self.get_client()
            # Se borran también las claves legacy (:state y la base) de versiones previas.
            await r.delete(
                f"agent_session:{session_id}",
                self._get_context_key(session_id),
                f"agent_session:{session_id}:state",
            )
        except _CACHE_ERRORS as exc:
            logger.warning("Redis no disponible al borrar %s: %s", session_id, exc)
            self._stale_sessions.add(session_id)
        else:
            self._stale_sessions.discard(session_id)

    async def close(self):
        if self.redis_client:
            await self.redis_client.aclose()
            self.redis_client = None
