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


if __name__ == "__main__":
    unittest.main()
