import base64
import tempfile
import unittest
from pathlib import Path

from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import _resolve_image_source


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


if __name__ == "__main__":
    unittest.main()
