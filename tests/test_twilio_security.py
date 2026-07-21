import unittest

from integrations.twilio import router


class TwilioSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_redis_url = router.REDIS_URL
        router.REDIS_URL = None
        router._local_rate_buckets.clear()
        router._local_seen_messages.clear()

    def tearDown(self):
        router.REDIS_URL = self._original_redis_url

    def test_split_words_caps_chunks(self):
        text = " ".join(f"w{i}" for i in range(501))

        chunks = router._split_words(text, max_words=250)

        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0].split()), 250)
        self.assertEqual(len(chunks[1].split()), 250)
        self.assertEqual(len(chunks[2].split()), 1)

    def test_allowed_sender_defaults_open_when_no_allowlist(self):
        original = router.TWILIO_ALLOWED_FROM
        try:
            router.TWILIO_ALLOWED_FROM = set()

            self.assertTrue(router._is_allowed_sender("whatsapp:+34123456789"))
        finally:
            router.TWILIO_ALLOWED_FROM = original

    def test_allowed_sender_rejects_unknown_when_configured(self):
        original = router.TWILIO_ALLOWED_FROM
        try:
            router.TWILIO_ALLOWED_FROM = {"whatsapp:+34111111111"}

            self.assertTrue(router._is_allowed_sender("whatsapp:+34111111111"))
            self.assertFalse(router._is_allowed_sender("whatsapp:+34222222222"))
        finally:
            router.TWILIO_ALLOWED_FROM = original

    async def test_local_message_idempotency(self):
        self.assertFalse(await router._mark_message_seen("SM123"))
        self.assertTrue(await router._mark_message_seen("SM123"))

    async def test_process_and_reply_sends_chunks_via_rest(self):
        class _FakeRunner:
            async def process_message(self, session_id, user_input):
                return " ".join(f"w{i}" for i in range(router.TWILIO_MAX_REPLY_WORDS + 1))

        class _FakeMessages:
            def __init__(self):
                self.sent = []

            def create(self, to, from_, body):
                self.sent.append({"to": to, "from_": from_, "body": body})

        class _FakeRestClient:
            def __init__(self):
                self.messages = _FakeMessages()

        fake_client = _FakeRestClient()
        original_runner = router._agent_runner
        original_get_client = router._get_twilio_rest_client
        try:
            router._agent_runner = _FakeRunner()
            router._get_twilio_rest_client = lambda: fake_client

            await router._process_and_reply(
                session_id="34123456789",
                user_message="hola",
                sender="whatsapp:+34123456789",
                receiver="whatsapp:+34999999999",
            )
        finally:
            router._agent_runner = original_runner
            router._get_twilio_rest_client = original_get_client

        self.assertEqual(len(fake_client.messages.sent), 2)
        for sent in fake_client.messages.sent:
            self.assertEqual(sent["to"], "whatsapp:+34123456789")
            self.assertEqual(sent["from_"], "whatsapp:+34999999999")

    async def test_process_and_reply_reports_agent_errors(self):
        class _BrokenRunner:
            async def process_message(self, session_id, user_input):
                raise RuntimeError("boom")

        class _FakeMessages:
            def __init__(self):
                self.sent = []

            def create(self, to, from_, body):
                self.sent.append(body)

        class _FakeRestClient:
            def __init__(self):
                self.messages = _FakeMessages()

        fake_client = _FakeRestClient()
        original_runner = router._agent_runner
        original_get_client = router._get_twilio_rest_client
        try:
            router._agent_runner = _BrokenRunner()
            router._get_twilio_rest_client = lambda: fake_client

            await router._process_and_reply(
                session_id="34123456789",
                user_message="hola",
                sender="whatsapp:+34123456789",
                receiver="whatsapp:+34999999999",
            )
        finally:
            router._agent_runner = original_runner
            router._get_twilio_rest_client = original_get_client

        self.assertEqual(fake_client.messages.sent, ["Error procesando tu mensaje."])


if __name__ == "__main__":
    unittest.main()
