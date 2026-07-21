import json
import unittest

from providers import resolve_provider_name
from providers import anthropic_messages as anthropic
from providers import openai_responses as openai_provider
from runner.context import normalize_full_context


class _FakeBlock:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeUsage:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _FakeResponse:
    def __init__(self, content=None, output=None, usage=None):
        self.content = content
        self.output = output
        self.usage = usage


class ProviderResolutionTests(unittest.TestCase):
    def test_provider_inferred_from_model_prefix(self):
        self.assertEqual(resolve_provider_name({"model": "claude-sonnet-5"}), "anthropic")
        self.assertEqual(resolve_provider_name({"model": "gpt-5.5"}), "openai")

    def test_explicit_provider_wins_over_prefix(self):
        self.assertEqual(
            resolve_provider_name({"model": "gpt-5.5", "provider": "anthropic"}),
            "anthropic",
        )


class AnthropicTranslationTests(unittest.TestCase):
    def test_context_groups_blocks_into_alternating_turns(self):
        context = [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hola"}]},
            {"type": "reasoning", "provider": "anthropic",
             "block": {"type": "thinking", "thinking": "pensando", "signature": "sig1"}},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "voy a usar una tool"}]},
            {"type": "function_call", "call_id": "toolu_1", "name": "read_file",
             "arguments": '{"path": "/tmp/a.txt"}'},
            {"type": "function_call_output", "call_id": "toolu_1", "output": "contenido"},
        ]

        messages = anthropic.context_to_messages(context)

        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        assistant_blocks = messages[1]["content"]
        self.assertEqual(
            [b["type"] for b in assistant_blocks],
            ["thinking", "text", "tool_use"],
        )
        self.assertEqual(assistant_blocks[0]["signature"], "sig1")
        self.assertEqual(assistant_blocks[2]["input"], {"path": "/tmp/a.txt"})
        self.assertEqual(
            messages[2]["content"][0],
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "contenido"},
        )

    def test_openai_reasoning_items_are_skipped(self):
        context = [
            {"type": "reasoning", "provider": "openai", "id": "rs_1", "summary": []},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hola"}]},
        ]
        messages = anthropic.context_to_messages(context)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")

    def test_data_url_image_becomes_base64_source(self):
        context = [{
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "mira"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD", "detail": "auto"},
            ],
        }]
        blocks = anthropic.context_to_messages(context)[0]["content"]
        self.assertEqual(blocks[1], {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        })

    def test_tools_are_converted_to_input_schema(self):
        tools = [
            {"type": "function", "name": "read_file", "description": "lee",
             "parameters": {"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False}},
            {"type": "web_search"},
        ]
        converted = anthropic.tools_to_anthropic(tools)
        self.assertEqual(converted[0]["name"], "read_file")
        self.assertIn("input_schema", converted[0])
        self.assertNotIn("parameters", converted[0])
        self.assertEqual(converted[1], {"type": "web_search_20260209", "name": "web_search"})

    def test_request_kwargs_use_adaptive_thinking_and_effort(self):
        params = {
            "model": "claude-sonnet-5",
            "reasoning": {"effort": "high", "summary": "auto"},
            "parallel_tool_calls": False,
            "max_output_tokens": 8_000,
        }
        tools = [{"type": "function", "name": "t", "description": "",
                  "parameters": {"type": "object", "properties": {}}}]

        kwargs = anthropic.build_request_kwargs("system", [], tools, params)

        self.assertEqual(kwargs["model"], "claude-sonnet-5")
        self.assertEqual(kwargs["max_tokens"], 8_000)
        self.assertEqual(kwargs["system"], "system")
        self.assertEqual(kwargs["thinking"], {"type": "adaptive"})
        self.assertEqual(kwargs["output_config"], {"effort": "high"})
        self.assertEqual(
            kwargs["tool_choice"],
            {"type": "auto", "disable_parallel_tool_use": True},
        )

    def test_request_kwargs_without_tools_have_no_tool_choice(self):
        params = {"model": "claude-sonnet-5", "reasoning": {"effort": "minimal"}}
        kwargs = anthropic.build_request_kwargs("s", [], [], params)
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertEqual(kwargs["output_config"], {"effort": "low"})
        self.assertEqual(kwargs["max_tokens"], anthropic.DEFAULT_MAX_OUTPUT_TOKENS)

    def test_response_blocks_become_internal_items(self):
        response = _FakeResponse(
            content=[
                _FakeBlock(type="thinking", thinking="analizo", signature="sig9"),
                _FakeBlock(type="text", text="respuesta"),
                _FakeBlock(type="tool_use", id="toolu_9", name="web_fetch",
                           input={"url": "https://x.test"}),
            ],
            usage=_FakeUsage(input_tokens=100, output_tokens=20, cache_read_input_tokens=60),
        )

        items = anthropic.items_from_response(response)
        usage = anthropic.usage_from_response(response)

        self.assertEqual(items[0]["type"], "reasoning")
        self.assertEqual(items[0]["provider"], "anthropic")
        self.assertEqual(items[0]["block"]["signature"], "sig9")
        self.assertEqual(items[1]["content"][0]["text"], "respuesta")
        self.assertEqual(items[2]["type"], "function_call")
        self.assertEqual(json.loads(items[2]["arguments"]), {"url": "https://x.test"})
        self.assertEqual(
            usage,
            {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 60},
        )


class OpenAITranslationTests(unittest.TestCase):
    def test_reasoning_items_are_kept_and_tagged(self):
        response = _FakeResponse(
            output=[
                _FakeBlock(type="reasoning", id="rs_1", summary=[], encrypted_content=None,
                           status="completed"),
                _FakeBlock(type="message", role="assistant",
                           content=[{"type": "output_text", "text": "hola"}]),
            ],
            usage=None,
        )

        items = openai_provider.items_from_response(response)

        self.assertEqual(items[0], {"type": "reasoning", "id": "rs_1", "summary": [],
                                    "provider": "openai"})
        self.assertEqual(items[1]["type"], "message")

    def test_input_resends_own_reasoning_and_skips_anthropic(self):
        context = [
            {"type": "reasoning", "provider": "openai", "id": "rs_1", "summary": []},
            {"type": "reasoning", "provider": "anthropic",
             "block": {"type": "thinking", "thinking": "x", "signature": "s"}},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hola"}]},
        ]

        input_items = openai_provider.context_to_input("system", context)

        self.assertEqual(input_items[0], {"role": "system", "content": "system"})
        self.assertEqual(input_items[1], {"type": "reasoning", "id": "rs_1", "summary": []})
        self.assertEqual(input_items[2]["type"], "message")
        self.assertEqual(len(input_items), 3)


class ReasoningPersistenceTests(unittest.TestCase):
    def test_reasoning_items_survive_context_normalization(self):
        context = {
            "ExecutorAgent": [
                {"type": "reasoning", "provider": "openai", "id": "rs_1", "summary": []},
                {"type": "reasoning", "provider": "anthropic",
                 "block": {"type": "thinking", "thinking": "x", "signature": "s"}},
                {"type": "reasoning"},  # sin provider: se descarta
            ]
        }

        normalized = normalize_full_context(context, {"ExecutorAgent"})

        items = normalized["ExecutorAgent"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["provider"], "openai")
        self.assertEqual(items[1]["block"]["signature"], "s")


if __name__ == "__main__":
    unittest.main()
