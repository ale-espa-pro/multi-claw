import base64
import tempfile
import unittest
from pathlib import Path

from runner.context import (
    build_context_delta,
    normalize_context_item,
    serialize_context_for_memory,
)
from runner.images import build_user_message_item, prepare_context_for_api
from runner.memory import normalize_retrieval_mode


class RunnerMultimodalTests(unittest.TestCase):
    def test_text_message_still_normalizes(self):
        item = normalize_context_item(
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
        message = build_user_message_item(
            "describe esto",
            images=[{"url": "https://example.com/image.png", "detail": "high"}],
        )

        item = normalize_context_item(message)

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
            message = build_user_message_item(
                "mira",
                images=[{"path": str(image_path), "mime_type": "image/png"}],
            )

            prepared = prepare_context_for_api([message])

        image_part = prepared[0]["content"][1]
        self.assertEqual(image_part["type"], "input_image")
        self.assertTrue(image_part["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(image_part["detail"], "auto")

    def test_memory_serialization_marks_images(self):
        context = {
            "ExecutorAgent": [
                build_user_message_item(
                    "analiza",
                    images=[{"file_id": "file_123"}],
                )
            ]
        }

        memory_text = serialize_context_for_memory(context)

        self.assertIn("[ExecutorAgent] user: analiza", memory_text)
        self.assertIn("[ExecutorAgent] user: [1 imagen(es) adjunta(s)]", memory_text)

    def test_context_delta_extracts_only_appended_items(self):
        agent_names = {"ExecutorAgent"}
        before = {
            "ExecutorAgent": [
                build_user_message_item("antiguo 1"),
                build_user_message_item("antiguo 2"),
            ]
        }
        after = {
            "ExecutorAgent": [
                build_user_message_item("antiguo 1"),
                build_user_message_item("antiguo 2"),
                build_user_message_item("nuevo"),
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": '{"tool": "resultado completo"}',
                },
            ]
        }

        delta = build_context_delta(before, after, agent_names)
        memory_text = serialize_context_for_memory(delta)

        self.assertNotIn("antiguo 1", memory_text)
        self.assertNotIn("antiguo 2", memory_text)
        self.assertIn("[ExecutorAgent] user: nuevo", memory_text)
        self.assertIn('{"tool": "resultado completo"}', memory_text)

    def test_retrieval_mode_normalization_is_conservative(self):
        self.assertEqual(normalize_retrieval_mode("hybrid"), "hybrid")
        self.assertEqual(normalize_retrieval_mode("keyword"), "keyword")
        self.assertEqual(normalize_retrieval_mode("unknown"), "vector")


if __name__ == "__main__":
    unittest.main()
