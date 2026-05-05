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


if __name__ == "__main__":
    unittest.main()
