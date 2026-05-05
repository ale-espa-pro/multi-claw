import base64
import tempfile
import unittest
from pathlib import Path

from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import (
    action_edit_file,
    action_file_hash,
    action_read_file,
    action_write_file,
    _resolve_image_source,
)


class ToolContractTests(unittest.TestCase):
    def test_playwright_schema_defaults_to_compact_outputs(self):
        tool = dict_total_tools["playwright_navigate"]
        properties = tool["parameters"]["properties"]

        self.assertIn("snapshot", properties["action"]["enum"])
        self.assertIn("inspect", properties["action"]["enum"])
        self.assertEqual(properties["screenshot_mode"]["enum"], ["path", "base64", "both"])
        self.assertIn("include_html", properties)
        self.assertIn("max_chars", properties)

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
            })
            self.assertTrue(edited["success"])
            self.assertIn("file_hash", edited)
            self.assertNotEqual(edited["file_hash"]["value"], first_hash)


if __name__ == "__main__":
    unittest.main()
