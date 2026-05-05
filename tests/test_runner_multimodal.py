import base64
import tempfile
import unittest
from pathlib import Path

from runner.agent_runner import AgentRunner


class RunnerMultimodalTests(unittest.TestCase):
    def test_text_message_still_normalizes(self):
        runner = object.__new__(AgentRunner)

        item = runner._normalize_context_item(
            {"type": "message", "role": "user", "content": "hola"}
        )

        self.assertEqual(
            item,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hola"}],
            },
        )

    def test_user_message_preserves_input_image_url(self):
        runner = object.__new__(AgentRunner)
        message = runner._build_user_message_item(
            "describe esto",
            images=[{"url": "https://example.com/image.png", "detail": "high"}],
        )

        item = runner._normalize_context_item(message)

        self.assertEqual(item["content"][0], {"type": "input_text", "text": "describe esto"})
        self.assertEqual(
            item["content"][1],
            {
                "type": "input_image",
                "image_url": "https://example.com/image.png",
                "detail": "high",
            },
        )

    def test_prepare_context_resolves_local_image_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "tiny.png"
            image_path.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADElEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
            ))
            message = AgentRunner._build_user_message_item(
                "mira",
                images=[{"path": str(image_path), "mime_type": "image/png"}],
            )

            prepared = AgentRunner._prepare_context_for_api([message])

        image_part = prepared[0]["content"][1]
        self.assertEqual(image_part["type"], "input_image")
        self.assertTrue(image_part["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(image_part["detail"], "auto")

    def test_memory_serialization_marks_images(self):
        runner = object.__new__(AgentRunner)
        context = {
            "ExecutorAgent": [
                AgentRunner._build_user_message_item(
                    "analiza",
                    images=[{"file_id": "file_123"}],
                )
            ]
        }

        memory_text = runner._serialize_context_for_memory(context)

        self.assertIn("[ExecutorAgent] user: analiza", memory_text)
        self.assertIn("[ExecutorAgent] user: [1 imagen(es) adjunta(s)]", memory_text)

    def test_retrieval_mode_normalization_is_conservative(self):
        self.assertEqual(AgentRunner._normalize_retrieval_mode("hybrid"), "hybrid")
        self.assertEqual(AgentRunner._normalize_retrieval_mode("keyword"), "keyword")
        self.assertEqual(AgentRunner._normalize_retrieval_mode("unknown"), "vector")


if __name__ == "__main__":
    unittest.main()
