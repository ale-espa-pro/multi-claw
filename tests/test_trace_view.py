import unittest

from data.trace_view import build_agent_trace
from runner.images import build_user_message_item


def assistant_message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(name, call_id, arguments="{}"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def function_call_output(call_id, output):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


class TraceViewTests(unittest.TestCase):
    def test_agents_are_sorted_lane_names(self):
        trace = build_agent_trace({"ExecutorAgent": [], "DeviceManagerAgent": []})
        self.assertEqual(trace["agents"], ["DeviceManagerAgent", "ExecutorAgent"])
        self.assertEqual(set(trace["lanes"].keys()), {"ExecutorAgent", "DeviceManagerAgent"})

    def test_plain_tool_call_is_merged_with_its_result(self):
        context = {
            "ExecutorAgent": [
                build_user_message_item("hola"),
                function_call("write_file", "call_1", '{"path": "/tmp/a.txt"}'),
                function_call_output("call_1", '{"success": true}'),
                assistant_message("listo"),
            ],
        }

        lane = build_agent_trace(context)["lanes"]["ExecutorAgent"]

        self.assertEqual([item["kind"] for item in lane], ["message", "tool_call", "message"])
        tool_call = lane[1]
        self.assertEqual(tool_call["name"], "write_file")
        self.assertEqual(tool_call["arguments"], {"path": "/tmp/a.txt"})
        self.assertEqual(tool_call["result"], {"success": True})
        self.assertIsNone(tool_call["target_agent"])

    def test_call_to_another_lane_is_classified_as_subagent_call(self):
        context = {
            "ExecutorAgent": [
                function_call("DeviceManagerAgent", "call_2", '{"query": "lista archivos"}'),
                function_call_output("call_2", "listo"),
            ],
            "DeviceManagerAgent": [
                build_user_message_item("lista archivos"),
                assistant_message("listo"),
            ],
        }

        lane = build_agent_trace(context)["lanes"]["ExecutorAgent"]

        self.assertEqual(lane[0]["kind"], "subagent_call")
        self.assertEqual(lane[0]["target_agent"], "DeviceManagerAgent")

    def test_reasoning_text_extraction_per_provider(self):
        context = {
            "ExecutorAgent": [
                {
                    "type": "reasoning",
                    "provider": "openai",
                    "summary": [{"type": "summary_text", "text": "revisando el pedido"}],
                },
                {
                    "type": "reasoning",
                    "provider": "anthropic",
                    "block": {"type": "thinking", "thinking": "creo que hace falta X"},
                },
                {
                    "type": "reasoning",
                    "provider": "anthropic",
                    "block": {"type": "redacted_thinking", "data": "opaque"},
                },
            ],
        }

        lane = build_agent_trace(context)["lanes"]["ExecutorAgent"]

        self.assertEqual(lane[0]["text"], "revisando el pedido")
        self.assertEqual(lane[1]["text"], "creo que hace falta X")
        self.assertIn("redactado", lane[2]["text"])

    def test_non_list_or_missing_lane_values_do_not_crash(self):
        trace = build_agent_trace({"ExecutorAgent": None})
        self.assertEqual(trace["lanes"]["ExecutorAgent"], [])

    def test_non_dict_context_returns_empty_trace(self):
        trace = build_agent_trace(None)
        self.assertEqual(trace, {"agents": [], "lanes": {}})


if __name__ == "__main__":
    unittest.main()
