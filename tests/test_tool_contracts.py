import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from runner.context import serialize_tool_result
from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import (
    action_edit_file,
    action_file_hash,
    action_read_file,
    action_save_preference,
    action_search_files,
    action_web_fetch,
    ticket_dispatcher,
    action_write_file,
    _resolve_image_source,
    _truncate_text,
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

    def test_file_hash_tool_is_registered(self):
        tool = dict_total_tools["file_hash"]

        self.assertEqual(tool["name"], "file_hash")
        self.assertIn("compare_to", tool["parameters"]["properties"])
        self.assertNotIn("algorithm", tool["parameters"]["properties"])

    def test_file_tools_return_file_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "note.txt")

            written = action_write_file({"path": path, "content": "hola"})
            self.assertTrue(written["success"])
            self.assertIn("file_hash", written)
            self.assertEqual(written["file_hash"]["algorithm"], "md5")
            first_hash = written["file_hash"]["value"]

            read = action_read_file({"path": path})
            self.assertTrue(read["success"])
            self.assertEqual(read["file_hash"]["value"], first_hash)

            unchanged = action_file_hash({"path": path, "compare_to": first_hash})
            self.assertTrue(unchanged["success"])
            self.assertFalse(unchanged["changed"])

            edited = action_edit_file({
                "path": path,
                "old_text": "hola",
                "new_text": "adios",
                "expected_file_hash": first_hash,
            })
            self.assertTrue(edited["success"])
            self.assertIn("file_hash", edited)
            self.assertNotEqual(edited["file_hash"]["value"], first_hash)

    def test_edit_file_rejects_a_stale_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.txt"
            created = action_write_file({"path": str(path), "content": "first"})
            stale_hash = created["file_hash"]["value"]
            path.write_text("changed elsewhere", encoding="utf-8")

            result = action_edit_file({
                "path": str(path),
                "old_text": "changed elsewhere",
                "new_text": "agent edit",
                "expected_file_hash": stale_hash,
            })

            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "file_conflict")
            self.assertEqual(path.read_text(encoding="utf-8"), "changed elsewhere")
            self.assertNotEqual(result["current_file_hash"], stale_hash)

    def test_write_file_requires_current_hash_for_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.txt"
            action_write_file({"path": str(path), "content": "original"})

            rejected = action_write_file({"path": str(path), "content": "replacement"})

            self.assertFalse(rejected["success"])
            self.assertEqual(rejected["error"], "file_conflict")
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

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

    def test_read_file_returns_remaining_chars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "long.txt")
            Path(path).write_text("abcdef", encoding="utf-8")

            read = action_read_file({"path": path, "max_chars": 2})

            self.assertTrue(read["success"])
            self.assertTrue(read["truncated"])
            self.assertEqual(read["content"], "ab")
            self.assertEqual(read["remaining_chars"], 4)
            self.assertGreater(read["remaining_tokens"], 0)

    def test_read_file_remaining_chars_is_zero_when_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "short.txt")
            Path(path).write_text("hola", encoding="utf-8")

            read = action_read_file({"path": path, "max_chars": 10})

            self.assertTrue(read["success"])
            self.assertFalse(read["truncated"])
            self.assertEqual(read["remaining_chars"], 0)
            self.assertEqual(read["remaining_tokens"], 0)

    def test_search_files_returns_remaining_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "match-alpha.txt").write_text("a", encoding="utf-8")
            Path(tmpdir, "match-beta.txt").write_text("b", encoding="utf-8")
            Path(tmpdir, "other.txt").write_text("c", encoding="utf-8")

            result = action_search_files({"root": tmpdir, "query": "match", "limit": 1})

            self.assertTrue(result["success"])
            self.assertEqual(result["limit"], 1)
            self.assertGreaterEqual(result["total_matches"], 2)
            self.assertGreaterEqual(result["remaining_results"], 1)
            self.assertEqual(len(result["results"]), 1)

    def test_truncate_text_returns_remaining_chars(self):
        content, truncated, remaining = _truncate_text("abcdef", 2)

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
