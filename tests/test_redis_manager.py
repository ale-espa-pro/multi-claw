import json
import os
import unittest
from unittest.mock import patch

from data.redis_manager import RedisSessionManager


class RedisSessionManagerTests(unittest.TestCase):
    def test_context_ttl_defaults_to_one_day(self):
        with patch.dict(os.environ, {"REDIS_CONTEXT_TTL_SECONDS": "86400"}):
            manager = RedisSessionManager()

        self.assertEqual(manager.context_ttl, 24 * 60 * 60)

    def test_context_ttl_is_configurable_and_has_safe_minimum(self):
        with patch.dict(os.environ, {"REDIS_CONTEXT_TTL_SECONDS": "3600"}):
            configured = RedisSessionManager()

        self.assertEqual(configured.context_ttl, 3600)
        self.assertEqual(RedisSessionManager(context_ttl=1).context_ttl, 60)


class _FlakyRedis:
    def __init__(self, stored):
        self.stored = json.dumps(stored)
        self.available = True
        self.writable = True

    async def get(self, key):
        if not self.available:
            raise ConnectionError("redis down")
        return self.stored

    async def set(self, key, value, ex=None):
        if not self.writable:
            raise ConnectionError("redis down")
        self.stored = value


class RedisFailureTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, stored):
        manager = RedisSessionManager(redis_url="redis://unused", context_ttl=60)
        manager.redis_client = _FlakyRedis(stored)
        return manager, manager.redis_client

    async def test_unreachable_redis_is_a_cache_miss(self):
        manager, client = self._manager({"A": ["cached"]})
        client.available = False

        self.assertEqual(await manager.load_context("s", {"A"}), ({"A": []}, False))

    async def test_corrupt_cache_entry_is_a_cache_miss(self):
        manager, client = self._manager({})
        client.stored = "{not json"

        self.assertEqual(await manager.load_context("s", {"A"}), ({"A": []}, False))

    async def test_failed_write_stops_serving_the_stale_entry(self):
        manager, client = self._manager({"A": ["turno antiguo"]})

        client.writable = False
        await manager.save_context("s", {"A": ["turno nuevo"]})
        self.assertEqual(await manager.load_context("s", {"A"}), ({"A": []}, False))

        client.writable = True
        await manager.save_context("s", {"A": ["turno nuevo"]})
        self.assertEqual(await manager.load_context("s", {"A"}), ({"A": ["turno nuevo"]}, True))


if __name__ == "__main__":
    unittest.main()
