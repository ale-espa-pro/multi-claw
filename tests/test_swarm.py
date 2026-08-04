import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from data.trace_view import build_agent_trace
from runner.agent_runner import AgentRunner
from runner.context import build_context_delta, normalize_full_context
from runner.execution import ExecutionContext
from runner.sandbox import write_root_scope
from runner.swarm import REPORT_FILENAME, SwarmError, SwarmRegistry
from tools.ticket_dispatcher import action_edit_file, action_run_command, action_write_file

SWARM_CONFIG = {
    "max_iterations": 5,
    "max_depth": 2,
    "max_concurrent": 2,
    "max_budget": 50,
    "report_max_chars": 20_000,
}


def _registry(taken_ids=None, **overrides):
    return SwarmRegistry({**SWARM_CONFIG, **overrides}, taken_ids=taken_ids)


class SwarmBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.registry = _registry()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _root(self, budget=10):
        return await self.registry.create_root(
            root_path=self.root, budget=budget, task="objetivo", name="enjambre"
        )

    async def test_root_takes_one_slot_of_the_total_budget(self):
        node = await self._root(budget=10)

        self.assertEqual(node.budget, 9)
        self.assertEqual(node.depth, 0)
        self.assertEqual(node.root_path, os.path.realpath(self.root))

    async def test_budget_outside_range_is_rejected(self):
        for budget in (0, -3, 51):
            with self.subTest(budget=budget), self.assertRaises(SwarmError):
                await self.registry.create_root(
                    root_path=self.root, budget=budget, task="objetivo", name=f"e{budget}"
                )

    async def test_spawning_a_child_costs_its_slot_plus_the_ceded_budget(self):
        root = await self._root(budget=10)

        child = await self.registry.spawn(
            parent_id=root.agent_id, name="Investigación Web", task="investiga", budget=3
        )

        self.assertEqual(child.budget, 3)
        self.assertEqual(root.budget, 9 - 4)
        self.assertEqual(child.agent_id, "enjambre/investigacion-web")
        self.assertEqual(child.root_path, os.path.join(root.root_path, "investigacion-web"))
        self.assertTrue(os.path.isdir(child.root_path))

    async def test_spawn_is_rejected_when_budget_does_not_cover_the_cost(self):
        root = await self._root(budget=3)

        with self.assertRaises(SwarmError) as ctx:
            await self.registry.spawn(parent_id=root.agent_id, name="hijo", task="t", budget=2)

        self.assertIn("presupuesto insuficiente", str(ctx.exception))
        self.assertEqual(root.budget, 2)

    async def test_unused_child_budget_returns_to_the_parent(self):
        root = await self._root(budget=10)
        child = await self.registry.spawn(
            parent_id=root.agent_id, name="hijo", task="t", budget=4
        )
        grandchild = await self.registry.spawn(
            parent_id=child.agent_id, name="nieto", task="t", budget=0
        )
        await self.registry.settle(grandchild, "completed")

        returned = await self.registry.settle(child, "completed")

        self.assertEqual(returned, 3)
        self.assertEqual(root.budget, 4 + 3)
        self.assertEqual(child.status, "completed")

    async def test_total_agents_never_exceed_the_initial_budget(self):
        budget = 12
        root = await self._root(budget=budget)

        # Reparto agresivo: cada nodo intenta gastar todo lo que tiene.
        pending = [root]
        while pending:
            parent = pending.pop()
            index = 0
            while parent.budget >= 1 and parent.depth + 1 <= SWARM_CONFIG["max_depth"]:
                child = await self.registry.spawn(
                    parent_id=parent.agent_id,
                    name=f"n{index}",
                    task="t",
                    budget=max(parent.budget - 1, 0),
                )
                pending.append(child)
                index += 1

        self.assertLessEqual(len(self.registry.nodes), budget)

    async def test_depth_limit_blocks_deeper_branches(self):
        root = await self._root()
        child = await self.registry.spawn(parent_id=root.agent_id, name="a", task="t", budget=3)
        grandchild = await self.registry.spawn(parent_id=child.agent_id, name="b", task="t", budget=1)

        with self.assertRaises(SwarmError) as ctx:
            await self.registry.spawn(parent_id=grandchild.agent_id, name="c", task="t")

        self.assertIn("profundidad máxima", str(ctx.exception))

    async def test_child_folder_cannot_escape_the_parent_folder(self):
        root = await self._root()

        for subpath in ("../fuera", "/tmp/otro-sitio", ".."):
            with self.subTest(subpath=subpath), self.assertRaises(SwarmError) as ctx:
                await self.registry.spawn(
                    parent_id=root.agent_id, name="fuga", task="t", subpath=subpath
                )
            self.assertIn("subcarpeta", str(ctx.exception))

    async def test_root_path_outside_allowed_roots_is_rejected(self):
        with self.assertRaises(SwarmError) as ctx:
            await self.registry.create_root(
                root_path="/etc/multi-claw-swarm", budget=5, task="t", name="malo"
            )

        self.assertIn("rutas permitidas", str(ctx.exception))

    async def test_confined_branch_cannot_grant_shell_access(self):
        root = await self._root()
        confined = await self.registry.spawn(
            parent_id=root.agent_id, name="confinado", task="t", budget=2
        )

        with self.assertRaises(SwarmError) as ctx:
            await self.registry.spawn(
                parent_id=confined.agent_id, name="shell", task="t", allow_shell=True
            )

        self.assertIn("allow_shell", str(ctx.exception))

    async def test_spawn_requires_an_existing_parent_node(self):
        with self.assertRaises(SwarmError) as ctx:
            await self.registry.spawn(parent_id="ExecutorAgent", name="huerfano", task="t")

        self.assertIn("deploy_swarm", str(ctx.exception))

    async def test_lane_ids_never_collide_with_lanes_from_previous_turns(self):
        taken = {"ExecutorAgent", "enjambre", "enjambre/hijo"}
        registry = _registry(taken_ids=taken)

        root = await registry.create_root(
            root_path=self.root, budget=5, task="t", name="enjambre"
        )
        child = await registry.spawn(parent_id=root.agent_id, name="hijo", task="t")

        self.assertEqual(root.agent_id, "enjambre#2")
        self.assertEqual(child.agent_id, "enjambre#2/hijo")
        # La carpeta sí se reutiliza: solo la identidad del carril es nueva.
        self.assertEqual(root.root_path, os.path.realpath(self.root))


class SandboxWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.sandbox = self.base / "agente"
        self.sandbox.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_inside_the_sandbox_is_allowed(self):
        target = self.sandbox / "REPORT.md"

        with write_root_scope(str(self.sandbox)):
            result = action_write_file({"path": str(target), "content": "informe"})

        self.assertTrue(result["success"], result)
        self.assertEqual(target.read_text(encoding="utf-8"), "informe")

    def test_write_above_the_sandbox_is_rejected_even_inside_allowed_roots(self):
        sibling = self.base / "vecino.txt"

        with write_root_scope(str(self.sandbox)):
            result = action_write_file({"path": str(sibling), "content": "fuga"})

        self.assertFalse(result["success"])
        self.assertIn("sandbox violation", result["error"])
        self.assertFalse(sibling.exists())

    def test_relative_traversal_out_of_the_sandbox_is_rejected(self):
        with write_root_scope(str(self.sandbox)):
            result = action_write_file(
                {"path": str(self.sandbox / ".." / "escape.txt"), "content": "fuga"}
            )

        self.assertFalse(result["success"])
        self.assertIn("sandbox violation", result["error"])

    def test_edit_above_the_sandbox_is_rejected(self):
        outside = self.base / "ajeno.txt"
        outside.write_text("original", encoding="utf-8")

        with write_root_scope(str(self.sandbox)):
            result = action_edit_file(
                {
                    "path": str(outside),
                    "old_text": "original",
                    "new_text": "pisado",
                    "expected_file_hash": "loquesea",
                }
            )

        self.assertFalse(result["success"])
        self.assertIn("sandbox violation", result["error"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "original")

    def test_run_command_workdir_cannot_leave_the_sandbox(self):
        with write_root_scope(str(self.sandbox)):
            result = asyncio.run(
                action_run_command({"command": "pwd", "workdir": str(self.base)})
            )

        self.assertFalse(result["success"])
        self.assertIn("sandbox violation", result["error"])

    def test_run_command_defaults_to_the_sandbox_folder(self):
        with write_root_scope(str(self.sandbox)):
            result = asyncio.run(action_run_command({"command": "pwd"}))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"].strip(), os.path.realpath(self.sandbox))

    def test_without_a_sandbox_the_global_roots_still_apply(self):
        result = action_write_file({"path": str(self.base / "libre.txt"), "content": "ok"})
        self.assertTrue(result["success"], result)

        denied = action_write_file({"path": "/etc/multi-claw-test", "content": "no"})
        self.assertFalse(denied["success"])
        self.assertIn("write not allowed outside", denied["error"])


class DynamicLanePersistenceTests(unittest.TestCase):
    def test_swarm_lanes_survive_normalization(self):
        context = {
            "ExecutorAgent": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hola"}]}
            ],
            "enjambre/investigacion": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hecho"}]}
            ],
        }

        normalized = normalize_full_context(context, {"ExecutorAgent"})

        self.assertIn("enjambre/investigacion", normalized)
        self.assertEqual(len(normalized["enjambre/investigacion"]), 1)

    def test_delta_includes_lanes_created_during_the_turn(self):
        before = {"ExecutorAgent": []}
        after = {
            "ExecutorAgent": [],
            "enjambre": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "listo"}]}
            ],
        }

        delta = build_context_delta(before, after, {"ExecutorAgent"})

        self.assertEqual(len(delta["enjambre"]), 1)


class TraceViewSwarmTests(unittest.TestCase):
    def test_spawn_calls_link_to_the_lane_of_the_created_agent(self):
        context = {
            "enjambre": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "spawn_agent",
                    "arguments": json.dumps({"name": "hijo", "task": "t"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": json.dumps({"success": True, "agent_id": "enjambre/hijo"}),
                },
            ],
            "enjambre/hijo": [],
        }

        trace = build_agent_trace(context)
        call = trace["lanes"]["enjambre"][0]

        self.assertEqual(call["kind"], "subagent_call")
        self.assertEqual(call["target_agent"], "enjambre/hijo")

    def test_plain_tool_calls_stay_unlinked(self):
        context = {
            "enjambre": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "write_file",
                    "arguments": json.dumps({"path": "/tmp/x"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": json.dumps({"success": True, "path": "/tmp/x"}),
                },
            ]
        }

        call = build_agent_trace(context)["lanes"]["enjambre"][0]

        self.assertEqual(call["kind"], "tool_call")
        self.assertIsNone(call["target_agent"])


class _FakeAgentBuilder:
    main_agent = "ExecutorAgent"
    agent_names = {"ExecutorAgent"}

    def __init__(self):
        from tools.ticket_dispatcher import ticket_dispatcher

        self.ticket_dispatcher = ticket_dispatcher

    def build_system_prompt(self, agent_name, session_id, conversation_type=None):
        return "Test agent"

    def get_tools_for_agent(self, agent_name):
        return []

    def get_runner_config(self):
        return {"max_iterations": 400}

    def get_swarm_config(self):
        return dict(SWARM_CONFIG)

    def get_swarm_params(self, model=None):
        return {
            "model": model or "test-model",
            "provider": None,
            "reasoning": {"effort": "low", "summary": "auto"},
            "parallel_tool_calls": True,
            "max_output_tokens": 16_000,
            "text": None,
        }

    def get_swarm_tools(self, allow_shell=False):
        return []

    def get_agent_max_iterations(self, agent_name):
        return 10

    def get_agent_params(self, agent_name):
        return self.get_swarm_params()


def _message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def _call(call_id, tool, /, **arguments):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": tool,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


class _ScriptedRunner(AgentRunner):
    """Runner con el proveedor sustituido por un guion por agente."""

    def __init__(self, script, delay=0.0):
        super().__init__(client=None, agent_builder=_FakeAgentBuilder())
        self.script = {name: list(turns) for name, turns in script.items()}
        self.delay = delay
        self.prompts = {}
        self.requests = []

    async def _request_agent(self, agent_name, curr_context, exec_ctx):
        if self.delay:
            await asyncio.sleep(self.delay)
        node = exec_ctx.swarm.get(agent_name)
        if node is not None and agent_name not in self.prompts:
            from runner import swarm as swarm_utils

            self.prompts[agent_name] = swarm_utils.build_worker_prompt(node, 2)
        self.requests.append(agent_name)
        turns = self.script.get(agent_name)
        if not turns:
            return [_message(f"{agent_name} sin guion")]
        return turns.pop(0)


class SwarmRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "enjambre")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _exec_ctx(self):
        return ExecutionContext("sesion-swarm", {"ExecutorAgent"}, swarm_config=SWARM_CONFIG)

    async def _deploy(self, runner, exec_ctx, budget=5, **extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return await runner._run_swarm_tool(
                "deploy_swarm",
                {"root_path": self.root, "budget": budget, "task": "haz el trabajo", **extra},
                "ExecutorAgent",
                exec_ctx,
            )

    async def test_tree_runs_confined_and_returns_the_child_report(self):
        report_body = "---\nstatus: completed\n---\n\ndescription: hecho\n\n## Resultado\nlisto"
        runner = _ScriptedRunner(
            {
                "enjambre": [
                    [_call("c1", "spawn_agent", name="analisis", task="analiza esto", budget=1)],
                    [_call("c2", "write_file", path=os.path.join(self.root, REPORT_FILENAME),
                           content="---\nstatus: completed\n---\n\ndescription: raiz")],
                    [_message("enjambre terminado")],
                ],
                "enjambre/analisis": [
                    [_call("h1", "write_file",
                           path=os.path.join(self.root, "analisis", REPORT_FILENAME),
                           content=report_body)],
                    [_message("analisis terminado")],
                ],
            }
        )
        exec_ctx = self._exec_ctx()

        result = await self._deploy(runner, exec_ctx, budget=5)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_id"], "enjambre")
        self.assertEqual(result["subagents"], ["enjambre/analisis"])
        self.assertIn("description: raiz", result["report"])

        child_lane = exec_ctx.context["enjambre/analisis"]
        self.assertTrue(child_lane)
        child_output = json.loads(
            next(item for item in child_lane if item["type"] == "function_call_output")["output"]
        )
        self.assertTrue(child_output["success"])
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "analisis", REPORT_FILENAME))
        )

    async def test_child_cannot_write_outside_its_own_folder(self):
        intruder = os.path.join(self.root, "otro-agente", "robado.txt")
        runner = _ScriptedRunner(
            {
                "enjambre": [
                    [_call("c1", "spawn_agent", name="ladron", task="escribe fuera")],
                    [_message("enjambre terminado")],
                ],
                "enjambre/ladron": [
                    [_call("h1", "write_file", path=intruder, content="mío")],
                    [_message("no pude")],
                ],
            }
        )
        exec_ctx = self._exec_ctx()

        await self._deploy(runner, exec_ctx)

        child_output = json.loads(
            next(
                item
                for item in exec_ctx.context["enjambre/ladron"]
                if item["type"] == "function_call_output"
            )["output"]
        )
        self.assertFalse(child_output["success"])
        self.assertIn("sandbox violation", child_output["error"])
        self.assertFalse(os.path.exists(intruder))

    async def test_missing_report_triggers_one_extra_turn_and_is_flagged(self):
        runner = _ScriptedRunner({"enjambre": [[_message("acabé sin informe")]]})
        exec_ctx = self._exec_ctx()

        result = await self._deploy(runner, exec_ctx)

        self.assertEqual(result["status"], "report_missing")
        self.assertTrue(result["report_missing"])
        self.assertNotIn("report", result)
        # Un turno inicial más el turno extra que se le concede para el informe.
        self.assertGreaterEqual(runner.requests.count("enjambre"), 2)
        nudges = [
            item
            for item in exec_ctx.context["enjambre"]
            if item["type"] == "message" and "REPORT.md" in json.dumps(item, ensure_ascii=False)
        ]
        self.assertTrue(nudges)

    async def test_budget_errors_are_returned_to_the_caller_as_tool_output(self):
        runner = _ScriptedRunner({})
        exec_ctx = self._exec_ctx()

        result = await self._deploy(runner, exec_ctx, budget=99)

        self.assertFalse(result["success"])
        self.assertIn("budget", result["error"])
        self.assertEqual(exec_ctx.swarm.nodes, {})

    async def test_parallel_siblings_keep_separate_sandboxes(self):
        """Dos ramas concurrentes no deben heredar ni pisarse el write-root.

        El sandbox viaja en un ContextVar y cada tool corre en su propia tarea,
        así que este es el punto donde un fallo de aislamiento se vería.
        """
        def child_script(own, sibling):
            return [
                [
                    _call(f"{own}-1", "write_file",
                          path=os.path.join(self.root, own, "propio.txt"), content=own),
                    _call(f"{own}-2", "write_file",
                          path=os.path.join(self.root, sibling, "intruso.txt"), content=own),
                ],
                [_call(f"{own}-3", "write_file",
                       path=os.path.join(self.root, own, REPORT_FILENAME),
                       content=f"description: {own}")],
                [_message(f"{own} listo")],
            ]

        runner = _ScriptedRunner(
            {
                "enjambre": [
                    [
                        _call("s1", "spawn_agent", name="alfa", task="trabaja"),
                        _call("s2", "spawn_agent", name="beta", task="trabaja"),
                    ],
                    [_call("r1", "write_file",
                           path=os.path.join(self.root, REPORT_FILENAME),
                           content="description: raiz")],
                    [_message("raiz lista")],
                ],
                "enjambre/alfa": child_script("alfa", "beta"),
                "enjambre/beta": child_script("beta", "alfa"),
            },
            delay=0.01,
        )
        exec_ctx = self._exec_ctx()

        result = await self._deploy(runner, exec_ctx, budget=5)

        self.assertEqual(result["status"], "completed")
        for own, sibling in (("alfa", "beta"), ("beta", "alfa")):
            # Los resultados se anexan en orden de finalización, así que se
            # localizan por call_id y no por posición.
            outputs = {
                item["call_id"]: json.loads(item["output"])
                for item in exec_ctx.context[f"enjambre/{own}"]
                if item["type"] == "function_call_output"
            }
            own_write, intrusion = outputs[f"{own}-1"], outputs[f"{own}-2"]
            self.assertTrue(own_write["success"], f"{own} no pudo escribir en su carpeta")
            self.assertFalse(intrusion["success"], f"{own} escribió en la carpeta de {sibling}")
            self.assertIn("sandbox violation", intrusion["error"])
            self.assertFalse(os.path.exists(os.path.join(self.root, sibling, "intruso.txt")))
            self.assertTrue(os.path.isfile(os.path.join(self.root, own, "propio.txt")))

    async def test_a_failing_branch_returns_its_budget_to_the_parent(self):
        class _Exploding(_ScriptedRunner):
            async def _request_agent(self, agent_name, curr_context, exec_ctx):
                if agent_name == "enjambre/roto":
                    raise RuntimeError("proveedor caído")
                return await super()._request_agent(agent_name, curr_context, exec_ctx)

        runner = _Exploding(
            {
                "enjambre": [
                    [_call("c1", "spawn_agent", name="roto", task="t", budget=2)],
                    [_call("r1", "write_file",
                           path=os.path.join(self.root, REPORT_FILENAME), content="description: x")],
                    [_message("listo")],
                ]
            }
        )
        exec_ctx = self._exec_ctx()

        await self._deploy(runner, exec_ctx, budget=8)

        self.assertEqual(exec_ctx.swarm.get("enjambre/roto").status, "failed")
        child_result = json.loads(
            next(
                item
                for item in exec_ctx.context["enjambre"]
                if item["type"] == "function_call_output" and item["call_id"] == "c1"
            )["output"]
        )
        self.assertFalse(child_result["success"])
        self.assertEqual(child_result["status"], "failed")
        # Los 2 cedidos vuelven al padre; solo se pierde la plaza del hijo.
        self.assertEqual(child_result["budget_returned"], 2)

    async def test_sandbox_is_released_after_the_swarm_finishes(self):
        runner = _ScriptedRunner(
            {
                "enjambre": [
                    [_call("r1", "write_file",
                           path=os.path.join(self.root, REPORT_FILENAME), content="description: x")],
                    [_message("listo")],
                ]
            }
        )
        exec_ctx = self._exec_ctx()

        await self._deploy(runner, exec_ctx)

        # El agente llamante no queda confinado por la rama que acaba de lanzar.
        outside = os.path.join(self._tmp.name, "fuera-del-enjambre.txt")
        self.assertTrue(action_write_file({"path": outside, "content": "ok"})["success"])

    async def test_worker_prompt_states_its_folder_and_budget(self):
        runner = _ScriptedRunner({"enjambre": [[_message("ok")]]})
        exec_ctx = self._exec_ctx()

        await self._deploy(runner, exec_ctx, budget=4)

        prompt = runner.prompts["enjambre"]
        self.assertIn(os.path.realpath(self.root), prompt)
        self.assertIn("Presupuesto de subagentes: 3", prompt)
        self.assertIn(REPORT_FILENAME, prompt)


if __name__ == "__main__":
    unittest.main()
