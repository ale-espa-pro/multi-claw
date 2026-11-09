import os
import unittest

import httpx

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import main as api_main


class _Store:
    def __init__(self):
        self.ensured = []

    async def list_conversations(self, limit=50):
        return []

    async def ensure_conversation(self, session_id, **kwargs):
        self.ensured.append(session_id)

    async def get_conversation(self, session_id, visible_only=False):
        raise AssertionError("a temporal conversation must not be read from Postgres")


class _Runner:
    async def process_message(self, session_id, user_input, images=None, conversation_type=None):
        return "ok"


class ApiAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original = (api_main.runner, api_main.conversation_store, api_main.CHAT_API_KEY)
        self.store = _Store()
        api_main.runner = _Runner()
        api_main.conversation_store = self.store
        api_main.CHAT_API_KEY = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        api_main.runner, api_main.conversation_store, api_main.CHAT_API_KEY = self.original

    async def test_api_key_is_enforced_when_configured(self):
        api_main.CHAT_API_KEY = "secreta"

        missing = await self.client.get("/conversations")
        wrong = await self.client.get("/conversations", headers={"X-API-Key": "otra"})
        valid = await self.client.get("/conversations", headers={"X-API-Key": "secreta"})

        self.assertEqual((missing.status_code, wrong.status_code, valid.status_code), (401, 401, 200))

    async def test_chat_pages_stay_public_so_the_key_can_be_entered(self):
        api_main.CHAT_API_KEY = "secreta"

        self.assertEqual((await self.client.get("/inspector")).status_code, 200)

    async def test_session_id_cannot_leave_the_sessions_folder(self):
        for session_id in ("../../etc", "a/b", "a\\b", "..", ""):
            with self.subTest(session_id=session_id):
                response = await self.client.post("/chat", json={"session_id": session_id, "message": "hola"})
                self.assertEqual(response.status_code, 422)

    async def test_conversation_limit_is_bounded(self):
        self.assertEqual((await self.client.get("/conversations", params={"limit": 0})).status_code, 422)
        self.assertEqual((await self.client.get("/conversations", params={"limit": 501})).status_code, 422)

    async def test_cors_only_admits_local_origins_by_default(self):
        async def preflight(origin):
            return await self.client.options("/chat", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            })

        local = await preflight("http://localhost:3000")
        foreign = await preflight("https://evil.example")

        self.assertEqual(local.headers.get("access-control-allow-origin"), "http://localhost:3000")
        self.assertNotIn("access-control-allow-origin", foreign.headers)

    async def test_temporal_chat_skips_the_database(self):
        response = await self.client.post(
            "/chat", json={"session_id": "t", "message": "hola", "conversation_type": "temporal"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.ensured, [])
        self.assertIsNone(response.json()["title"])


if __name__ == "__main__":
    unittest.main()
