import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from runner.context import serialize_tool_result
from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import (
    action_save_preference,
    action_web_fetch,
    ticket_dispatcher,
    _resolve_image_source,
    _clip_text_content,
)
from tools.memoryTools import RAG_memory


class ToolContractTests(unittest.TestCase):
    def test_only_configured_tools_are_exposed_with_responses_contracts(self):
        config_path = Path(__file__).parents[1] / "agents" / "agent_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        configured_tools = {
            tool
            for agent_name, agent_config in config.items()
            if not agent_name.startswith("_")
            for tool in agent_config.get("tools", [])
            if isinstance(tool, str)
        }
        # Los nodos del enjambre son dinámicos: su toolset vive en _swarm.
        swarm_config = config.get("_swarm") or {}
        configured_tools |= {
            tool
            for key in ("tools", "shell_tools")
            for tool in swarm_config.get(key, [])
            if isinstance(tool, str)
        }

        self.assertEqual(set(dict_total_tools), configured_tools)
        self.assertNotIn("MemoryAgent", dict_total_tools)
        self.assertNotIn("MemoryAgent", ticket_dispatcher)

        allowed_keys = {"type", "name", "description", "parameters", "strict"}
        for name, tool in dict_total_tools.items():
            with self.subTest(tool=name):
                self.assertEqual(set(tool) - allowed_keys, set())
                self.assertEqual(tool["type"], "function")
                self.assertIs(tool["strict"], False)
                self.assertNotIn("required", tool)
                parameters = tool["parameters"]
                self.assertEqual(parameters["type"], "object")
                self.assertIs(parameters["additionalProperties"], False)
                self.assertIn("max_chars", parameters["properties"])
                self.assertLessEqual(
                    set(parameters["required"]),
                    set(parameters["properties"]),
                )

    def test_terminal_replaces_removed_file_and_python_tools(self):
        removed_tools = {"run_python", "read_file", "write_file", "file_hash"}

        self.assertTrue(removed_tools.isdisjoint(dict_total_tools))
        self.assertTrue(removed_tools.isdisjoint(ticket_dispatcher))
        self.assertIn("run_command", dict_total_tools)
        self.assertIn("run_command", ticket_dispatcher)

    def test_edit_file_schema_exposes_inspect_and_guarded_edit(self):
        parameters = dict_total_tools["edit_file"]["parameters"]
        properties = parameters["properties"]

        self.assertEqual(set(properties["action"]["enum"]), {"inspect", "edit"})
        self.assertIn("expected_file_hash", properties)
        self.assertIn("offset", properties)
        self.assertEqual(set(parameters["required"]), {"path", "action"})
        self.assertIn("edit_file", ticket_dispatcher)

    def test_playwright_schema_defaults_to_compact_outputs(self):
        tool = dict_total_tools["playwright_navigate"]
        properties = tool["parameters"]["properties"]

        self.assertIn("snapshot", properties["action"]["enum"])
        self.assertIn("inspect", properties["action"]["enum"])
        self.assertEqual(properties["screenshot_mode"]["enum"], ["path", "base64", "both"])
        self.assertIn("include_html", properties)
        self.assertIn("max_chars", properties)

    def test_playwright_session_tool_and_agent_are_registered(self):
        tool = dict_total_tools["playwright_session"]
        properties = tool["parameters"]["properties"]

        self.assertIn("batch", properties["action"]["enum"])
        self.assertIn("upload", properties["action"]["enum"])
        self.assertIn("close", properties["action"]["enum"])
        self.assertIn("actions", properties)
        self.assertIn("playwright_session", ticket_dispatcher)
        self.assertIn("PlaywrightSessionAgent", dict_total_tools)

    def test_interpret_image_resolves_local_path_as_data_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "tiny.png"
            image_path.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADElEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
            ))

            data_url = _resolve_image_source(str(image_path), "image/png")

        self.assertTrue(data_url.startswith("data:image/png;base64,"))

    def test_interpret_image_accepts_url_without_inlining(self):
        url = "https://example.com/image.png"

        self.assertEqual(_resolve_image_source(url, None), url)

    def test_global_tool_output_limit_returns_counts(self):
        import unittest.mock

        with unittest.mock.patch.dict("os.environ", {
            "TOOL_OUTPUT_MAX_CHARS": "20",
            "TOOL_OUTPUT_HARD_MAX_CHARS": "30",
        }):
            output = json.loads(serialize_tool_result({"content": "x" * 100}))

        self.assertTrue(output["truncated"])
        self.assertEqual(output["shown_chars"], 20)
        self.assertEqual(output["remaining_chars"], output["total_chars"] - 20)
        self.assertFalse(output["limit_clamped"])

    def test_requested_tool_limit_is_clamped_to_hard_limit(self):
        import unittest.mock

        with unittest.mock.patch.dict("os.environ", {
            "TOOL_OUTPUT_MAX_CHARS": "20",
            "TOOL_OUTPUT_HARD_MAX_CHARS": "30",
        }):
            output = json.loads(serialize_tool_result({"content": "x" * 100}, 50))

        self.assertEqual(output["requested_chars"], 50)
        self.assertEqual(output["effective_limit_chars"], 30)
        self.assertTrue(output["limit_clamped"])

    def test_save_preference_can_append_replace_and_delete(self):
        import tools.ticket_dispatcher as dispatcher

        original_path = dispatcher.user_preferences_path
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher.user_preferences_path = str(Path(tmpdir) / "preferences.txt")
            try:
                appended = action_save_preference({
                    "action": "add",
                    "preference": "Usar respuestas breves",
                })
                self.assertTrue(appended["success"])
                self.assertEqual(appended["action"], "add")
                self.assertIn("file_hash", appended)

                replaced = action_save_preference({
                    "action": "replace",
                    "old_text": "respuestas breves",
                    "new_text": "respuestas detalladas",
                })
                self.assertTrue(replaced["success"])
                self.assertEqual(replaced["action"], "replace")

                deleted = action_save_preference({
                    "action": "delete",
                    "old_text": " -Usar respuestas detalladas",
                })
                self.assertTrue(deleted["success"])
                self.assertEqual(deleted["action"], "delete")
                self.assertEqual(Path(dispatcher.user_preferences_path).read_text(encoding="utf-8"), "")
            finally:
                dispatcher.user_preferences_path = original_path

    def test_save_preference_treats_empty_old_text_with_preference_as_add(self):
        import tools.ticket_dispatcher as dispatcher

        original_path = dispatcher.user_preferences_path
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher.user_preferences_path = str(Path(tmpdir) / "preferences.txt")
            try:
                result = action_save_preference({
                    "old_text": "",
                    "new_text": "",
                    "replace_all": False,
                    "preference": "Rutas de fotos del DNI guardadas bajo confirmacion literal.",
                })

                self.assertTrue(result["success"])
                self.assertEqual(result["action"], "add")
                content = Path(dispatcher.user_preferences_path).read_text(encoding="utf-8")
                self.assertIn("Rutas de fotos del DNI", content)
            finally:
                dispatcher.user_preferences_path = original_path

    def test_truncate_text_returns_remaining_chars(self):
        content, truncated, remaining = _clip_text_content("abcdef", 2)

        self.assertEqual(content, "ab")
        self.assertTrue(truncated)
        self.assertEqual(remaining, 4)

    def test_memory_output_truncation_returns_remaining_counts(self):
        original_max_chars = RAG_memory.MAX_OUTPUT_CHARS
        original_max_words = RAG_memory.MAX_OUTPUT_WORDS
        try:
            RAG_memory.MAX_OUTPUT_CHARS = 12
            RAG_memory.MAX_OUTPUT_WORDS = 20_000

            result = RAG_memory._truncate_output([{"text": "abcdefghijklmnopqrstuvwxyz"}])

            self.assertTrue(result["truncated"])
            self.assertGreater(result["remaining_chars"], 0)
            self.assertEqual(result["remaining_words"], 0)
            self.assertGreater(result["remaining_tokens"], 0)
        finally:
            RAG_memory.MAX_OUTPUT_CHARS = original_max_chars
            RAG_memory.MAX_OUTPUT_WORDS = original_max_words


class AsyncToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_web_fetch_returns_remaining_chars(self):
        class FakeResponse:
            is_success = True
            url = "https://example.test/data"
            status_code = 200
            headers = {"content-type": "text/plain"}
            text = "abcdef"

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, headers=None):
                return FakeResponse()

        import unittest.mock

        with unittest.mock.patch("httpx.AsyncClient", FakeAsyncClient):
            result = await action_web_fetch({"url": "https://example.test/data", "max_chars": 2})

        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "ab")
        self.assertEqual(result["remaining_chars"], 4)
        self.assertGreater(result["remaining_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
