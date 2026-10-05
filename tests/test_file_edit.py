import hashlib
import json
import multiprocessing
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from runner.context import serialize_tool_result
from runner.sandbox import write_root_scope
from tools import file_edit
from tools.file_edit import action_edit_file


def _edit_in_process(path, expected_hash, replacement, barrier, results):
    barrier.wait(timeout=10)
    results.put(action_edit_file({
        "path": path,
        "old_text": "original",
        "new_text": replacement,
        "expected_file_hash": expected_hash,
    }))


class FileEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "note.txt"
        self.path.write_text("original", encoding="utf-8")
        roots = patch.object(file_edit, "ALLOWED_WRITE_ROOTS", [str(self.root)])
        roots.start()
        self.addCleanup(roots.stop)

    def inspect(self, **kwargs):
        return action_edit_file({"path": str(self.path), **kwargs})

    def edit(self, expected_hash, **kwargs):
        return action_edit_file({
            "path": str(self.path),
            "old_text": "original",
            "new_text": "edited",
            "expected_file_hash": expected_hash,
            **kwargs,
        })

    def assert_only_one_editor_succeeded(self, results):
        self.assertEqual(sum(result["success"] for result in results), 1, results)
        rejected = next(result for result in results if not result["success"])
        self.assertEqual(rejected["error"], "file_conflict")
        self.assertIn(self.path.read_text(encoding="utf-8"), {"first", "second"})

    def test_inspect_then_edit_returns_hashes_of_the_actual_bytes(self):
        content = "línea uno\r\nlínea dos\n"
        self.path.write_bytes(content.encode("utf-8"))

        inspected = self.inspect(action="inspect")

        self.assertTrue(inspected["success"], inspected)
        self.assertEqual(inspected["content"], content)
        self.assertEqual(inspected["file_hash"]["algorithm"], "md5")
        self.assertEqual(
            inspected["file_hash"]["value"], hashlib.md5(content.encode("utf-8")).hexdigest()
        )

        edited = self.edit(
            inspected["file_hash"]["value"], action="edit", old_text="uno", new_text="tres"
        )

        self.assertTrue(edited["success"], edited)
        self.assertEqual(self.path.read_bytes(), "línea tres\r\nlínea dos\n".encode("utf-8"))
        self.assertEqual(edited["file_hash"]["value"], hashlib.md5(self.path.read_bytes()).hexdigest())

    def test_external_change_requires_inspection_before_retry(self):
        expected = self.inspect()["file_hash"]["value"]
        self.path.write_text("external update", encoding="utf-8")

        rejected = self.edit(expected, old_text="external update")

        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["error"], "file_conflict")
        self.assertIn("inspect", rejected["action_required"])
        self.assertNotIn("file_hash", rejected)
        self.assertNotIn("current_file_hash", rejected)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "external update")

        refreshed = self.inspect()
        edited = self.edit(refreshed["file_hash"]["value"], old_text="external update")
        self.assertTrue(edited["success"], edited)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "edited")

    def test_missing_hash_never_writes_or_returns_a_hash(self):
        for expected in (None, "", "  "):
            with self.subTest(expected=expected):
                result = self.edit(expected)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "read_required")
                self.assertNotIn("file_hash", result)
                self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_empty_old_text_and_non_string_replacements_are_rejected(self):
        expected = self.inspect()["file_hash"]["value"]
        for arguments in ({"old_text": ""}, {"old_text": None}, {"new_text": 12}):
            with self.subTest(arguments=arguments):
                result = self.edit(expected, **arguments)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid_arguments")
                self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_duplicate_matches_require_explicit_replace_all(self):
        self.path.write_text("original original", encoding="utf-8")
        expected = self.inspect()["file_hash"]["value"]

        rejected = self.edit(expected)

        self.assertFalse(rejected["success"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original original")
        edited = self.edit(expected, replace_all=True)
        self.assertTrue(edited["success"], edited)
        self.assertEqual(edited["replacements"], 2)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "edited edited")

    def test_missing_old_text_does_not_modify_the_file(self):
        result = self.edit(self.inspect()["file_hash"]["value"], old_text="absent")

        self.assertFalse(result["success"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_inspect_paginates_characters_but_hashes_the_whole_file(self):
        content = "á🙂bcdef"
        self.path.write_text(content, encoding="utf-8")

        first = self.inspect(offset=1, max_chars=3)
        last = self.inspect(offset=4, max_chars=3)
        past_end = self.inspect(offset=100, max_chars=3)

        self.assertTrue(first["success"], first)
        self.assertEqual(first["content"], "🙂bc")
        self.assertTrue(first["truncated"])
        self.assertEqual(first["remaining_chars"], 3)
        self.assertEqual(first["total_chars"], len(content))
        self.assertEqual(last["content"], "def")
        self.assertFalse(last["truncated"])
        self.assertEqual(last["remaining_chars"], 0)
        self.assertEqual(first["file_hash"]["value"], last["file_hash"]["value"])
        self.assertEqual(first["file_hash"]["value"], hashlib.md5(content.encode()).hexdigest())
        self.assertEqual(past_end["content"], "")
        self.assertFalse(past_end["truncated"])
        self.assertEqual(past_end["remaining_chars"], 0)

    def test_default_inspect_is_bounded_and_invalid_offsets_are_rejected(self):
        self.path.write_text("x" * 12_010, encoding="utf-8")

        result = self.inspect()

        self.assertEqual(len(result["content"]), 12_000)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["remaining_chars"], 10)
        for arguments in ({"offset": -1}, {"offset": "1"}, {"max_chars": 0}):
            with self.subTest(arguments=arguments):
                rejected = self.inspect(**arguments)
                self.assertFalse(rejected["success"])
                self.assertEqual(rejected["error"], "invalid_arguments")

    def test_large_inspection_keeps_hash_visible_after_runner_truncation(self):
        self.path.write_text("x" * 100_000, encoding="utf-8")
        inspected = self.inspect(max_chars=100_000)

        with patch.dict("os.environ", {
            "TOOL_OUTPUT_MAX_CHARS": "100000",
            "TOOL_OUTPUT_HARD_MAX_CHARS": "1000000",
        }):
            delivered = json.loads(serialize_tool_result(inspected, 100_000))

        self.assertTrue(delivered["truncated"])
        self.assertIn(inspected["file_hash"]["value"], delivered["content"])
        self.assertIn('"offset": 0', delivered["content"])
        self.assertIn('"total_chars": 100000', delivered["content"])

    def test_non_utf8_and_binary_files_are_never_rewritten(self):
        for content in (b"before\xffafter", b"before\x00after"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                inspected = self.inspect()
                edited = self.edit(hashlib.md5(content).hexdigest(), old_text="before")

                self.assertFalse(inspected["success"])
                self.assertEqual(inspected["error"], "unsupported_text")
                self.assertFalse(edited["success"])
                self.assertEqual(edited["error"], "unsupported_text")
                self.assertEqual(self.path.read_bytes(), content)

    def test_missing_file_requires_creation_through_terminal(self):
        self.path.unlink()

        inspected = self.inspect()
        edited = self.edit(hashlib.md5(b"").hexdigest())

        self.assertEqual(inspected["error"], "file_not_found")
        self.assertEqual(edited["error"], "file_not_found")
        self.assertFalse(self.path.exists())

    def test_replacement_preserves_executable_permissions_and_line_endings(self):
        self.path.write_bytes(b"#!/bin/sh\r\necho original\r\n# last\n")
        self.path.chmod(0o755)

        result = self.edit(self.inspect()["file_hash"]["value"])

        self.assertTrue(result["success"], result)
        self.assertEqual(self.path.read_bytes(), b"#!/bin/sh\r\necho edited\r\n# last\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o755)

    def test_failed_atomic_replace_leaves_original_intact_and_cleans_temporary_file(self):
        expected = self.inspect()["file_hash"]["value"]
        with patch("tools.file_edit.os.replace", side_effect=OSError("disk failure")):
            result = self.edit(expected)

        self.assertFalse(result["success"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_external_change_during_edit_is_detected_before_replace(self):
        expected = self.inspect()["file_hash"]["value"]
        atomic_replace = file_edit._atomic_replace

        def modify_then_replace(path, data, original):
            self.path.write_text("changed during edit", encoding="utf-8")
            return atomic_replace(path, data, original)

        with patch("tools.file_edit._atomic_replace", side_effect=modify_then_replace):
            result = self.edit(expected)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "file_conflict")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "changed during edit")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_global_write_roots_cannot_be_overridden_by_tool_arguments(self):
        expected = self.inspect()["file_hash"]["value"]
        allowed = self.root / "allowed"
        allowed.mkdir()

        with patch.object(file_edit, "ALLOWED_WRITE_ROOTS", [str(allowed)]):
            rejected = self.edit(expected, write_root=str(self.root))

        self.assertFalse(rejected["success"])
        self.assertIn("write not allowed", rejected["error"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_scoped_write_root_rejects_traversal_and_symlinks(self):
        sandbox = self.root / "sandbox"
        sandbox.mkdir()
        link = sandbox / "alias.txt"
        link.symlink_to(self.path)
        expected = self.inspect()["file_hash"]["value"]

        for path in (self.path, sandbox / ".." / self.path.name, link):
            with self.subTest(path=str(path)), write_root_scope(str(sandbox)):
                rejected = self.edit(expected, path=str(path))
                self.assertFalse(rejected["success"])
                self.assertIn("sandbox violation", rejected["error"])
                self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_concurrent_threads_cannot_both_edit_the_same_version(self):
        expected = self.inspect()["file_hash"]["value"]
        barrier = threading.Barrier(2)

        def edit_together(replacement):
            barrier.wait(timeout=10)
            return self.edit(expected, new_text=replacement)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit_together, ("first", "second")))

        self.assert_only_one_editor_succeeded(results)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires POSIX fork")
    def test_concurrent_processes_cannot_both_edit_the_same_version(self):
        expected = self.inspect()["file_hash"]["value"]
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results_queue = context.Queue()
        processes = [context.Process(
            target=_edit_in_process,
            args=(str(self.path), expected, replacement, barrier, results_queue),
        ) for replacement in ("first", "second")]

        try:
            for process in processes:
                process.start()
            results = [results_queue.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
            self.assert_only_one_editor_succeeded(results)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results_queue.close()
            results_queue.join_thread()


if __name__ == "__main__":
    unittest.main()
