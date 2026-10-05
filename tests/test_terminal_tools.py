import asyncio
import subprocess
import threading
import unittest
from unittest.mock import patch

from tools.ticket_dispatcher import action_run_command


class TerminalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_runs_inline_python(self):
        result = await action_run_command({
            "command": "python3 -c 'print(6 * 7)'",
            "workdir": "/tmp",
        })

        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"].strip(), "42")

    async def test_waiting_for_a_command_does_not_block_other_tasks(self):
        started = threading.Event()
        release = threading.Event()

        def waiting_command(*args, **kwargs):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("the event loop could not release the command")
            return subprocess.CompletedProcess(args[0], 0, stdout="done", stderr="")

        with patch("tools.ticket_dispatcher.subprocess.run", side_effect=waiting_command):
            task = asyncio.create_task(action_run_command({"command": "test-command"}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(task.done(), "the command blocked the event loop")
                release.set()
                result = await task
            finally:
                release.set()
                await task

        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"], "done")
