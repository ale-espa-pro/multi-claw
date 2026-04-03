import json
import redis.asyncio as redis


class RedisSessionManager:
    """Manejador de sesiones y contexto persistido en Redis."""
    
    def __init__(self, redis_url: str = "redis://localhost:6379", context_ttl: int = 120):
        self.redis_url = redis_url
        self.redis_client = None
        self.context_ttl = context_ttl  # 24 horas por defecto
    
    async def get_client(self):
        """Obtiene o crea conexión a Redis (thread-safe con connection pool)."""
        if self.redis_client is None:
            self.redis_client = await redis.from_url(
                self.redis_url, 
                encoding="utf-8", 
                decode_responses=True
            )
        return self.redis_client
    
    def _get_session_key(self, session_id: str) -> str:
        """Genera la clave Redis para una sesión."""
        return f"agent_session:{session_id}"

    def _get_context_key(self, session_id: str) -> str:
        """Genera la clave Redis para el contexto de una sesión."""
        return f"agent_session:{session_id}:context"

    def _get_state_key(self, session_id: str) -> str:
        """Genera la clave Redis para el estado operativo de una sesión."""
        return f"agent_session:{session_id}:state"
    
    async def load_context(self, session_id: str, agent_names: set) -> tuple[dict, bool]:
        """
        Carga el contexto desde Redis.
        
        Args:
            session_id: ID de la sesión
            agent_names: Set con nombres de agentes para inicializar
        
        Returns:
            Tupla (contexto, existía_previamente)
        """
        r = await self.get_client()
        key = self._get_context_key(session_id)
        
        data = await r.get(key)
        if data:
            context = json.loads(data)
            # Asegurar que todos los agentes tengan entrada
            for name in agent_names:
                if name not in context:
                    context[name] = []
            return context, True
        
        # Sesión nueva: inicializar contexto vacío
        context = {name: [] for name in agent_names}
        return context, False
    
    async def save_context(self, session_id: str, context: dict):
        """Guarda el contexto en Redis."""
        r = await self.get_client()
        key = self._get_context_key(session_id)
        
        await r.set(
            key, 
            json.dumps(context, ensure_ascii=False),
            ex=self.context_ttl
        )

    async def load_state(self, session_id: str) -> dict:
        """Carga el estado operativo de una sesión."""
        r = await self.get_client()
        key = self._get_state_key(session_id)
        data = await r.get(key)
        if not data:
            return {}

        try:
            state = json.loads(data)
        except json.JSONDecodeError:
            return {}

        return state if isinstance(state, dict) else {}

    async def save_state(self, session_id: str, state: dict):
        """Guarda el estado operativo de una sesión."""
        r = await self.get_client()
        key = self._get_state_key(session_id)

        await r.set(
            key,
            json.dumps(state, ensure_ascii=False),
            ex=self.context_ttl
        )

    async def clear_state(self, session_id: str):
        """Elimina el estado operativo de una sesión."""
        r = await self.get_client()
        key = self._get_state_key(session_id)
        await r.delete(key)
    
    async def delete_session(self, session_id: str):
        """Elimina una sesión de Redis (tanto datos como contexto)."""
        r = await self.get_client()
        session_key = self._get_session_key(session_id)
        context_key = self._get_context_key(session_id)
        state_key = self._get_state_key(session_id)
        await r.delete(session_key, context_key, state_key)
    
    async def session_exists(self, session_id: str) -> bool:
        """Verifica si una sesión existe."""
        r = await self.get_client()
        key = self._get_context_key(session_id)
        return await r.exists(key) > 0
    
    async def get_session_ttl(self, session_id: str) -> int:
        """Obtiene el TTL restante de una sesión en segundos."""
        r = await self.get_client()
        key = self._get_context_key(session_id)
        return await r.ttl(key)
    
    async def refresh_session(self, session_id: str):
        """Renueva el TTL de una sesión."""
        r = await self.get_client()
        key = self._get_context_key(session_id)
        await r.expire(key, self.context_ttl)
    
    async def close(self):
        """Cierra la conexión a Redis."""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None
