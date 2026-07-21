import asyncio

from pricing.token_tracker import TokenUsageTracker


class ExecutionContext:
    """Per-request isolated execution context."""

    def __init__(self, session_id: str, agent_names: set[str], conversation_type: str | None = None):
        self.session_id = session_id
        self.conversation_type = conversation_type
        self.context: dict[str, list] = {name: [] for name in agent_names}
        self.token_tracker = TokenUsageTracker()
        # System prompts construidos una vez por request (evita I/O de disco
        # en cada iteración del loop del agente).
        self.system_prompts: dict[str, str] = {}


class SessionLockRegistry:
    def __init__(self):
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    async def get_session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    async def get_memory_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._memory_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._memory_locks[session_id] = lock
            return lock

    async def clear_session(self, session_id: str):
        async with self._locks_lock:
            self._session_locks.pop(session_id, None)
            self._memory_locks.pop(session_id, None)
