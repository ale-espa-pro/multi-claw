import unittest
from datetime import datetime, timezone

from data.conversation_store import PostgresConversationStore
from runner.images import build_user_message_item


class ConversationSnapshotTests(unittest.TestCase):
    def test_snapshot_prefers_main_agent_over_noisier_subagent(self):
        store = PostgresConversationStore()
        now = datetime.now(timezone.utc)
        context = {
            "ExecutorAgent": [
                build_user_message_item("hola executor"),
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "respuesta visible"}],
                },
            ],
            "DeviceManagerAgent": [
                build_user_message_item('{"query": "json interno 1"}'),
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                },
                build_user_message_item('{"query": "json interno 2"}'),
            ],
        }

        snapshot = store._build_snapshot_view(
            session_id="s1",
            context_jsonb=context,
            created_at=now,
            updated_at=now,
        )

        self.assertEqual(snapshot["title"], "hola executor")
        self.assertEqual(snapshot["message_count"], 2)
        self.assertEqual(snapshot["messages"][0]["data"]["agent_name"], "ExecutorAgent")


if __name__ == "__main__":
    unittest.main()
