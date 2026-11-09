import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from providers.result import ProviderResult
from runner import context as context_utils
from runner.agent_runner import AgentRunner
from runner.execution import ExecutionContext

AGENT = "ExecutorAgent"


class _Builder:
    main_agent = AGENT
    agent_names = {AGENT}

    def __init__(self, tools=()):
        self.ticket_dispatcher = {}
        self.tools = [{"name": name} for name in tools]

    def build_system_prompt(self, agent_name, session_id, conversation_type=None):
        return "Test agent"

    def get_tools_for_agent(self, agent_name):
        return self.tools

    def get_runner_config(self):
        return {"max_iterations": 400}

    def get_swarm_config(self):
        return {"max_iterations": 5, "max_depth": 2, "max_concurrent": 2, "max_budget": 50}

    def get_agent_params(self, agent_name):
        return {"model": "test-model", "provider": None, "reasoning": {}, "parallel_tool_calls": False, "text": None}


class _Cache:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = {}

    async def load_context(self, session_id, agent_names):
        if self.fail:
            raise ConnectionError("cache down")
        context = copy.deepcopy(self.saved.get(session_id))
        return (context, True) if context is not None else ({name: [] for name in agent_names}, False)

    async def save_context(self, session_id, context):
        self.saved[session_id] = copy.deepcopy(context)


class _Store:
    def __init__(self, context=None):
        self.context = context or {}
        self.saves = []
        self.usage = []

    async def load_context(self, session_id):
        return copy.deepcopy(self.context)

    async def save_context(self, session_id, context, conversation_type=None):
        self.saves.append(copy.deepcopy(context))

    async def add_token_usage(self, session_id, usage):
        self.usage.append(usage)


class _Provider:
    def __init__(self, respond):
        self.respond = respond
        self.contexts = []

    async def request(self, system_prompt, context, tools, params):
        self.contexts.append(list(context))
        return await self.respond(len(self.contexts))

    async def close(self):
        pass


def _text(text="ok"):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def _user(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _call(call_id, name, arguments="{}"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def _output(call_id, output):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _result(*items):
    return ProviderResult(items=list(items), usage={"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0})


def _runner(respond, store=None, cache=None, tools=()):
    runner = AgentRunner(client=None, agent_builder=_Builder(tools), conversation_store=store)
    runner.session_manager = cache or _Cache()
    provider = _Provider(respond)
    runner._providers = {"openai": provider, "anthropic": provider}
    return runner, provider


async def _process(runner, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return await runner.process_message(*args, **kwargs)


class FailureHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_context_load_does_not_overwrite_the_conversation(self):
        store = _Store({AGENT: [_user("historial valioso")]})
        async def respond(_):
            return _result(_text())
        runner, provider = _runner(respond, store=store, cache=_Cache(fail=True))

        with self.assertRaises(ConnectionError):
            await _process(runner, "s", "hola")

        self.assertEqual(store.saves, [])
        self.assertEqual(provider.contexts, [])

    async def test_cancelled_run_keeps_the_work_done_so_far(self):
        store = _Store()
        started = asyncio.Event()
        async def respond(_):
            started.set()
            await asyncio.sleep(30)
        runner, _ = _runner(respond, store=store)

        task = asyncio.create_task(_process(runner, "s", "tarea larga"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(store.saves[-1][AGENT][0]["content"][0]["text"], "tarea larga")

    async def test_response_without_output_stops_after_one_retry(self):
        store = _Store()
        async def respond(number):
            return _result({"type": "reasoning", "provider": "openai", "id": f"rs_{number}", "summary": []})
        runner, provider = _runner(respond, store=store)

        answer = await _process(runner, "s", "hola")

        self.assertEqual(len(provider.contexts), 2)
        self.assertIn("no devolvió", answer)
        self.assertEqual([item["type"] for item in store.saves[-1][AGENT]], ["message"])

    def test_refusal_counts_as_message_text(self):
        item = context_utils.normalize_context_item(
            {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "no puedo"}]}
        )

        self.assertEqual(context_utils.extract_message_text(item), "no puedo")

    async def test_tool_not_offered_to_the_agent_is_refused(self):
        tool = MagicMock(return_value={"success": True})
        async def respond(_):
            return _result(_text())
        runner, _ = _runner(respond, tools=("allowed",))
        runner.agent_builder.ticket_dispatcher = {"allowed": tool, "hidden": tool}
        exec_ctx = ExecutionContext("s", {AGENT})

        _, result = await runner._execute_tool(_call("c", "hidden"), AGENT, exec_ctx)

        self.assertIn("not available", result["error"])
        tool.assert_not_called()


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporal_conversation_only_reaches_the_cache(self):
        store = _Store()
        cache = _Cache()
        async def respond(_):
            return _result(_text())
        runner, _ = _runner(respond, store=store, cache=cache)
        runner._memory_service.schedule_semantic_sync = MagicMock()

        await _process(runner, "s", "secreto", conversation_type="temporal")

        self.assertEqual(store.saves, [])
        self.assertEqual(store.usage, [])
        runner._memory_service.schedule_semantic_sync.assert_not_called()
        self.assertIn("secreto", json.dumps(cache.saved["s"]))

    async def test_long_run_is_checkpointed_between_iterations(self):
        store = _Store()
        async def tool(body):
            return {"success": True}
        async def respond(number):
            return _result(_call("c1", "tool")) if number == 1 else _result(_text())
        runner, _ = _runner(respond, store=store, tools=("tool",))
        runner.agent_builder.ticket_dispatcher = {"tool": tool}
        runner._checkpoint_seconds = 0

        await _process(runner, "s", "hola")

        self.assertEqual([item["type"] for item in store.saves[0][AGENT]], [
            "message", "function_call", "function_call_output",
        ])
        self.assertEqual(len(store.saves), 2)

    async def test_interrupted_call_gets_a_result_when_loaded(self):
        store = _Store({AGENT: [_user("hola"), _call("c1", "tool")]})
        async def respond(_):
            return _result(_text())
        runner, provider = _runner(respond, store=store)

        await _process(runner, "s", "sigue")

        sent = provider.contexts[0]
        self.assertEqual(sent[2]["type"], "function_call_output")
        self.assertEqual(sent[2]["call_id"], "c1")
        self.assertIn("interrupted", sent[2]["output"])

    async def test_token_usage_is_recorded_per_turn(self):
        store = _Store()
        async def respond(_):
            return _result(_text())
        runner, _ = _runner(respond, store=store)

        await _process(runner, "s", "hola")

        self.assertEqual(store.usage[0]["input_tokens"], 10)
        self.assertEqual(store.usage[0]["requests"], 1)


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def _lane(self, big):
        return [
            _user("turno 1"), _call("a", "tool", json.dumps({"content": big})), _output("a", big), _text("r1"),
            _user("turno 2"), _call("b", "tool"), _output("b", big), _text("r2"),
            _user("turno actual"),
        ]

    def test_only_turns_older_than_the_previous_one_are_compacted(self):
        big = "x" * 5_000
        lane = self._lane(big)
        original = copy.deepcopy(lane)

        compacted = context_utils.compact_previous_turns(lane)

        self.assertLess(len(compacted[2]["output"]), 600)
        self.assertIn("memory_query", compacted[2]["output"])
        self.assertIsInstance(json.loads(compacted[1]["arguments"]), dict)
        self.assertEqual(compacted[6]["output"], big)
        self.assertEqual(lane, original, "the persisted lane must stay intact")

    def test_short_outputs_and_recent_lanes_are_left_alone(self):
        lane = self._lane("corto")
        self.assertEqual(context_utils.compact_previous_turns(lane), lane)

        recent = self._lane("x" * 5_000)[4:]
        self.assertEqual(context_utils.compact_previous_turns(recent), recent)

    def test_compaction_can_be_disabled(self):
        lane = self._lane("x" * 5_000)
        with patch.dict("os.environ", {"CONTEXT_COMPACT_OVER_CHARS": "0"}):
            self.assertEqual(context_utils.compact_previous_turns(lane), lane)

    async def test_request_is_compacted_but_the_history_is_saved_complete(self):
        big = "x" * 5_000
        store = _Store({AGENT: self._lane(big)[:-1]})
        async def respond(_):
            return _result(_text())
        runner, provider = _runner(respond, store=store)

        await _process(runner, "s", "turno actual")

        self.assertLess(len(provider.contexts[0][2]["output"]), 600)
        self.assertEqual(store.saves[-1][AGENT][2]["output"], big)


if __name__ == "__main__":
    unittest.main()
