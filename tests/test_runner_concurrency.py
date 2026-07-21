import asyncio
import copy
import contextlib
import io
import os
import re
import time
import unittest

import httpx

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import main as api_main
from runner.agent_runner import AgentRunner
from runner.execution import ExecutionContext


class _FakeAgentBuilder:
    main_agent = "ExecutorAgent"
    agent_names = {"ExecutorAgent"}
    ticket_dispatcher = {}

    def build_system_prompt(self, agent_name, session_id, conversation_type=None):
        return "Test agent"

    def get_tools_for_agent(self, agent_name):
        return []

    def get_runner_config(self):
        return {
            "max_iterations": 400,
        }

    def get_agent_max_iterations(self, agent_name):
        return 10

    def get_agent_params(self, agent_name):
        return {
            "model": "test-model",
            "provider": None,
            "reasoning": {"effort": "low", "summary": "auto"},
            "parallel_tool_calls": False,
            "max_output_tokens": 16_000,
            "text": None,
        }


class _InMemorySessionManager:
    def __init__(self):
        self.contexts = {}
        self.lock = asyncio.Lock()

    async def load_context(self, session_id, agent_names):
        async with self.lock:
            context = copy.deepcopy(self.contexts.get(session_id))
        if context is None:
            return {name: [] for name in agent_names}, False
        for name in agent_names:
            context.setdefault(name, [])
        return context, True

    async def save_context(self, session_id, context):
        async with self.lock:
            self.contexts[session_id] = copy.deepcopy(context)

    async def delete_session(self, session_id):
        async with self.lock:
            self.contexts.pop(session_id, None)

    async def close(self):
        return None


class _InMemoryConversationStore:
    def __init__(self):
        self.conversations = {}
        self.lock = asyncio.Lock()

    async def ensure_conversation(
        self,
        session_id,
        username=None,
        metadata=None,
        conversation_type=None,
    ):
        async with self.lock:
            self.conversations.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "title": session_id,
                    "username": username,
                    "metadata": metadata,
                    "conversation_type": conversation_type,
                },
            )

    async def get_conversation(self, session_id):
        async with self.lock:
            conversation = self.conversations.get(session_id)
            return copy.deepcopy(conversation) if conversation is not None else None

    async def delete_conversation(self, session_id):
        async with self.lock:
            self.conversations.pop(session_id, None)


class _FakeResponseItem:
    def __init__(self, text):
        self.text = text

    def model_dump(self):
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": self.text}],
        }


class _FakeResponse:
    def __init__(self, text):
        self.output = [_FakeResponseItem(text)]
        self.usage = None


class _FakeResponsesClient:
    def __init__(self, delay_seconds):
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def create(self, model, input, tools, reasoning, **kwargs):
        async with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

        try:
            await asyncio.sleep(self.delay_seconds)
            user_text = input[-1]["content"][0]["text"]
            match = re.search(r"n[uú]mero\s+(\d+)", user_text)
            return _FakeResponse(match.group(1))
        finally:
            async with self.lock:
                self.active -= 1


class _FakeClient:
    def __init__(self, delay_seconds):
        self.responses = _FakeResponsesClient(delay_seconds)


class RunnerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_runner = api_main.runner
        self.original_conversation_store = api_main.conversation_store
        self.original_chat_api_key = api_main.CHAT_API_KEY

    async def asyncTearDown(self):
        api_main.runner = self.original_runner
        api_main.conversation_store = self.original_conversation_store
        api_main.CHAT_API_KEY = self.original_chat_api_key

    async def test_chat_endpoint_runs_distinct_sessions_concurrently_without_context_overlap(self):
        delay_seconds = 0.05
        client = _FakeClient(delay_seconds)
        runner = AgentRunner(client=client, agent_builder=_FakeAgentBuilder())
        runner.session_manager = _InMemorySessionManager()
        api_main.runner = runner
        api_main.conversation_store = _InMemoryConversationStore()
        api_main.CHAT_API_KEY = None

        transport = httpx.ASGITransport(app=api_main.app)

        async def ask(http_client, number):
            session_id = f"concurrency-session-{number}"
            prompt = f"responde unicamente el número {number}"
            response = await http_client.post(
                "/chat",
                json={"session_id": session_id, "message": prompt},
            )
            response.raise_for_status()
            payload = response.json()
            return session_id, number, prompt, payload

        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
                results = await asyncio.gather(*(ask(http_client, number) for number in range(1, 21)))
                elapsed = time.perf_counter() - started

        self.assertLess(
            elapsed,
            delay_seconds * 10,
            f"20 requests a /chat tardaron {elapsed:.3f}s; parece ejecucion secuencial",
        )
        self.assertEqual(client.responses.max_active, 10)

        for session_id, number, prompt, payload in results:
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["response"], str(number))
            context = runner.session_manager.contexts[session_id]["ExecutorAgent"]
            user_texts = [
                part["text"]
                for item in context
                if item.get("role") == "user"
                for part in item.get("content", [])
                if part.get("type") == "input_text"
            ]
            assistant_texts = [
                part["text"]
                for item in context
                if item.get("role") == "assistant"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ]

            self.assertEqual(user_texts, [prompt])
            self.assertEqual(assistant_texts, [str(number)])

    async def test_context_is_not_truncated_after_120_messages(self):
        runner = AgentRunner(client=_FakeClient(0), agent_builder=_FakeAgentBuilder())
        runner.session_manager = _InMemorySessionManager()
        session_id = "long-running-session"

        with contextlib.redirect_stdout(io.StringIO()):
            for number in range(1, 122):
                await runner.process_message(
                    session_id=session_id,
                    user_input=f"responde unicamente el número {number}",
                )

        context = runner.session_manager.contexts[session_id]["ExecutorAgent"]
        user_messages = [
            item for item in context
            if item.get("type") == "message" and item.get("role") == "user"
        ]
        assistant_messages = [
            item for item in context
            if item.get("type") == "message" and item.get("role") == "assistant"
        ]

        self.assertEqual(len(user_messages), 121)
        self.assertEqual(len(assistant_messages), 121)

    async def test_one_failing_tool_does_not_cancel_the_batch(self):
        runner = AgentRunner(client=_FakeClient(0), agent_builder=_FakeAgentBuilder())
        runner.session_manager = _InMemorySessionManager()

        async def ok_tool(body):
            await asyncio.sleep(0.01)
            return {"success": True}

        def boom_tool(body):
            raise RuntimeError("boom")

        runner.agent_builder.ticket_dispatcher = {"ok_tool": ok_tool, "boom_tool": boom_tool}

        calls = [
            {"type": "function_call", "call_id": "c1", "name": "boom_tool", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "ok_tool", "arguments": "{}"},
        ]
        exec_ctx = ExecutionContext("failing-tool-session", {"ExecutorAgent"})

        with contextlib.redirect_stdout(io.StringIO()):
            results = await runner._execute_tools_parallel(calls, "ExecutorAgent", exec_ctx)

        self.assertEqual(results["c2"], {"success": True})
        self.assertIn("boom", results["c1"])
        self.assertIn("RuntimeError", results["c1"])


if __name__ == "__main__":
    unittest.main()
