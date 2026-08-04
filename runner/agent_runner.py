import asyncio
import contextlib
import copy
import inspect
import json
import os
from typing import Any, Optional

from agents.agent_builder import AgentBuilder
from data.conversation_store import PostgresConversationStore
from data.redis_manager import RedisSessionManager
from providers import AnthropicMessagesProvider, OpenAIResponsesProvider, resolve_provider_name
from runner import context as context_utils
from runner import images as image_utils
from runner import swarm as swarm_utils
from runner.execution import ExecutionContext, SessionLockRegistry
from runner.memory import MemoryRetrievalConfig, MemoryService
from runner.sandbox import write_root_scope
from runner.swarm import SwarmError, SwarmNode
from tools.memoryTools.RAG_memory import MemoryRag

SWARM_TOOLS = {"deploy_swarm", "spawn_agent"}


class AgentRunner:
    def __init__(
        self,
        client,
        agent_builder: AgentBuilder,
        redis_url: str | None = None,
        conversation_store: PostgresConversationStore | None = None,
        memory_rag: MemoryRag | None = None,
        anthropic_client=None,
    ):
        self.client = client
        self._providers = {
            "openai": OpenAIResponsesProvider(client),
            "anthropic": AnthropicMessagesProvider(anthropic_client),
        }
        self.agent_builder = agent_builder
        self.main_agent = agent_builder.main_agent
        self.agent_names = agent_builder.agent_names

        runner_config = agent_builder.get_runner_config()
        self.max_iterations = int(runner_config["max_iterations"])

        self.session_manager = RedisSessionManager(redis_url=redis_url)
        self.conversation_store = conversation_store
        self.memory_rag = memory_rag
        self._swarm_config = agent_builder.get_swarm_config()
        self._api_semaphore = asyncio.Semaphore(max(int(os.getenv("AGENT_API_CONCURRENCY", "10")), 1))
        self._background_tasks: set[asyncio.Task] = set()
        self._locks = SessionLockRegistry()
        self._memory_service = MemoryService(
            memory_rag=memory_rag,
            config=MemoryRetrievalConfig.from_env(),
            agent_names=self.agent_names,
            background_tasks=self._background_tasks,
            get_memory_lock=self._locks.get_memory_lock,
        )

    # ── Contexto persistido ──

    async def _load_complete_context(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        cached_context, context_exists = await self.session_manager.load_context(session_id, self.agent_names)
        normalized_cached = context_utils.normalize_full_context(cached_context, self.agent_names)
        if context_exists:
            return normalized_cached

        if self.conversation_store is None:
            return normalized_cached

        stored_context = await self.conversation_store.load_context(session_id)
        normalized_stored = context_utils.normalize_full_context(stored_context, self.agent_names)

        if any(normalized_stored.values()):
            await self.session_manager.save_context(session_id, normalized_stored)

        return normalized_stored

    async def _persist_complete_context(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        conversation_type: str | None = None,
    ):
        normalized_context = context_utils.normalize_full_context(context, self.agent_names)
        if self.conversation_store is not None:
            await self.conversation_store.save_context(
                session_id=session_id,
                context=normalized_context,
                conversation_type=conversation_type,
            )
        await self.session_manager.save_context(session_id, normalized_context)

    # ── Entrada principal ──

    async def process_message(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None = None,
        conversation_type: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        resolved_agent = agent_name or self.main_agent
        session_lock = await self._locks.get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(
                session_id, self.agent_names, conversation_type, swarm_config=self._swarm_config
            )
            try:
                exec_ctx.context = await self._load_complete_context(session_id)
                exec_ctx.adopt_lanes(exec_ctx.context)
                previous_context = copy.deepcopy(exec_ctx.context)

                response = await self._chat(user_input, resolved_agent, exec_ctx, images=images)

                await self._persist_complete_context(
                    session_id=session_id,
                    context=exec_ctx.context,
                    conversation_type=conversation_type,
                )
                self._memory_service.schedule_semantic_sync(
                    session_id=session_id,
                    context=exec_ctx.context,
                    previous_context=previous_context,
                    conversation_type=conversation_type,
                )
                exec_ctx.token_tracker.print_summary()
                return response
            except Exception:
                try:
                    await self._persist_complete_context(
                        session_id=session_id,
                        context=exec_ctx.context,
                        conversation_type=conversation_type,
                    )
                except Exception:
                    pass
                raise

    # ── Loop de agente ──

    async def _request_agent(
        self, agent_name: str, curr_context: list, exec_ctx: ExecutionContext
    ) -> list[dict[str, Any]]:
        node = exec_ctx.swarm.get(agent_name)
        system_prompt = exec_ctx.system_prompts.get(agent_name)
        if system_prompt is None:
            if node is not None:
                # Los nodos del enjambre no cargan preferencias, workflows ni crons:
                # su prompt es corto a propósito porque se paga por cada agente.
                system_prompt = swarm_utils.build_worker_prompt(
                    node, int(self._swarm_config.get("max_depth", 4))
                )
            else:
                # build_system_prompt hace I/O de disco (preferencias, workflows,
                # crons): se construye una sola vez por request y fuera del loop.
                system_prompt = await asyncio.to_thread(
                    self.agent_builder.build_system_prompt,
                    agent_name,
                    exec_ctx.session_id,
                    exec_ctx.conversation_type,
                )
            exec_ctx.system_prompts[agent_name] = system_prompt

        if node is not None:
            params = self.agent_builder.get_swarm_params(node.model)
            tools = self.agent_builder.get_swarm_tools(node.allow_shell)
        else:
            params = self.agent_builder.get_agent_params(agent_name)
            tools = self.agent_builder.get_tools_for_agent(agent_name)
        provider = self._providers[resolve_provider_name(params)]

        async with contextlib.AsyncExitStack() as stack:
            if node is not None:
                # Tope de concurrencia del enjambre. Se sostiene solo durante la
                # llamada al proveedor y nunca mientras un padre espera a sus
                # hijos, así que un árbol profundo no puede autobloquearse.
                await stack.enter_async_context(exec_ctx.swarm.request_semaphore)
            await stack.enter_async_context(self._api_semaphore)
            result = await provider.request(
                system_prompt=system_prompt,
                context=curr_context,
                tools=tools,
                params=params,
            )

        exec_ctx.token_tracker.accumulate(result.usage, agent_name=agent_name)
        return result.items

    async def _execute_tool(self, tool_call: dict, caller_agent: str, exec_ctx: ExecutionContext):
        tool_name = tool_call["name"]
        call_id = tool_call["call_id"]

        try:
            tool_arguments = json.loads(tool_call["arguments"])
        except json.JSONDecodeError:
            return call_id, "Error: Invalid JSON arguments"

        if tool_name in self.agent_names:
            print(
                f"\033[1;35m  [{caller_agent}] -> AGENT CALL -> {tool_name}: \033[0m"
                f"{json.dumps(tool_arguments, ensure_ascii=False)}"
            )
            result = await self._run_subagent(tool_name, tool_arguments, exec_ctx)
            return call_id, result

        if tool_name in SWARM_TOOLS:
            print(
                f"\033[1;36m  [{caller_agent}] -> {tool_name.upper()}: \033[0m"
                f"{json.dumps(tool_arguments, ensure_ascii=False)[:400]}"
            )
            result = await self._run_swarm_tool(tool_name, tool_arguments, caller_agent, exec_ctx)
            return call_id, result

        dispatcher = self.agent_builder.ticket_dispatcher
        if tool_name not in dispatcher:
            return call_id, f"Error: Tool '{tool_name}' not found"

        print(f"\033[1;33m  [{caller_agent}] TOOL -> {tool_name}: \033[0m{tool_arguments}")
        func = dispatcher[tool_name]

        if inspect.iscoroutinefunction(func):
            result = await func(tool_arguments)
        else:
            result = await asyncio.to_thread(func, tool_arguments)
        print(result)
        return call_id, result

    async def _execute_tools_parallel(self, function_calls: list, caller_agent: str, exec_ctx: ExecutionContext):
        if len(function_calls) > 1:
            print(f"\033[1;36m  [{caller_agent}] Ejecutando {len(function_calls)} llamadas en paralelo...\033[0m")

        results: dict[str, Any] = {}

        async with asyncio.TaskGroup() as tg:
            async def _run(tc):
                # Una excepción inesperada en una tool no debe cancelar el resto
                # del batch ni tumbar la request: se devuelve como resultado de
                # error para que el modelo pueda verlo y reaccionar.
                try:
                    call_id, result = await self._execute_tool(tc, caller_agent, exec_ctx)
                except Exception as exc:
                    call_id = tc["call_id"]
                    result = f"Error: tool '{tc.get('name')}' raised {type(exc).__name__}: {exc}"
                    print(f"\033[1;31m  [{caller_agent}] TOOL ERROR -> {tc.get('name')}: {exc}\033[0m")
                results[call_id] = result

            for tc in function_calls:
                tg.create_task(_run(tc))

        return results

    async def _run_subagent(
        self,
        agent_name: str,
        task_description: Any,
        exec_ctx: ExecutionContext,
        max_iterations: int | None = None,
    ):
        max_iterations = max_iterations or self.agent_builder.get_agent_max_iterations(agent_name)
        raw_user_text = context_utils.serialize_user_payload(task_description)
        exec_ctx.context[agent_name].append(image_utils.build_user_message_item(raw_user_text))

        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = image_utils.build_user_message_item(
            await self._memory_service.augment_user_message(raw_user_text, exec_ctx.session_id)
        )

        return await self._run_agent_loop(agent_name, working_context, exec_ctx, max_iterations, subagent=True)

    # ── Enjambre ──

    async def _run_swarm_tool(
        self,
        tool_name: str,
        arguments: Any,
        caller_agent: str,
        exec_ctx: ExecutionContext,
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"success": False, "error": f"{tool_name} espera un objeto de argumentos"}

        try:
            if tool_name == "deploy_swarm":
                node = await exec_ctx.swarm.create_root(
                    root_path=arguments.get("root_path"),
                    budget=arguments.get("budget"),
                    task=arguments.get("task"),
                    name=arguments.get("name"),
                    extra_prompt=arguments.get("extra_prompt") or "",
                    model=arguments.get("model"),
                    max_iterations=arguments.get("max_iterations"),
                    allow_shell=bool(arguments.get("allow_shell", False)),
                )
            else:
                node = await exec_ctx.swarm.spawn(
                    parent_id=caller_agent,
                    name=arguments.get("name"),
                    task=arguments.get("task"),
                    budget=arguments.get("budget", 0),
                    extra_prompt=arguments.get("extra_prompt") or "",
                    subpath=arguments.get("subpath"),
                    model=arguments.get("model"),
                    max_iterations=arguments.get("max_iterations"),
                    allow_shell=bool(arguments.get("allow_shell", False)),
                )
        except SwarmError as exc:
            return {"success": False, "error": str(exc)}
        except OSError as exc:
            return {"success": False, "error": f"no se pudo preparar la carpeta de trabajo: {exc}"}

        return await self._run_swarm_agent(node, exec_ctx)

    async def _run_swarm_agent(self, node: SwarmNode, exec_ctx: ExecutionContext) -> dict[str, Any]:
        """Ejecuta un nodo del árbol confinado a su carpeta y devuelve su informe.

        El nodo es efímero: arranca con contexto limpio y su continuidad es el
        REPORT.md. El carril sí se conserva en exec_ctx.context para la traza.
        """
        exec_ctx.register_lane(node.agent_id)
        print(
            f"\033[1;35m  ├─ SPAWN {node.agent_id} \033[0m"
            f"(budget={node.budget}, depth={node.depth}) -> {node.root_path}"
        )

        user_message = image_utils.build_user_message_item(node.task)
        working_context = [user_message]
        exec_ctx.context[node.agent_id].append(user_message)

        # El write-root se hereda por copia de contexto en cada tarea que se cree
        # dentro del bloque, así que cubre también a los subagentes de esta rama.
        try:
            with write_root_scope(node.root_path):
                final_message = await self._run_agent_loop(
                    node.agent_id, working_context, exec_ctx, node.max_iterations, subagent=True
                )
                report = swarm_utils.read_report(node, exec_ctx.swarm.report_max_chars)

                if report is None:
                    nudge = image_utils.build_user_message_item(swarm_utils.REPORT_NUDGE)
                    working_context.append(nudge)
                    exec_ctx.context[node.agent_id].append(nudge)
                    final_message = await self._run_agent_loop(
                        node.agent_id,
                        working_context,
                        exec_ctx,
                        swarm_utils.REPORT_NUDGE_ITERATIONS,
                        subagent=True,
                    ) or final_message
                    report = swarm_utils.read_report(node, exec_ctx.swarm.report_max_chars)
        except Exception as exc:
            # Sin esto el presupuesto del nodo se quedaría bloqueado en el padre.
            returned = await exec_ctx.swarm.settle(node, "failed")
            print(f"\033[1;31m  └─ {node.agent_id} falló: {type(exc).__name__}: {exc}\033[0m")
            return {
                "success": False,
                "agent_id": node.agent_id,
                "path": node.root_path,
                "status": "failed",
                "budget_returned": returned,
                "error": f"{type(exc).__name__}: {exc}",
            }

        status = "completed" if report else "report_missing"
        returned = await exec_ctx.swarm.settle(node, status)

        result: dict[str, Any] = {
            "success": True,
            "agent_id": node.agent_id,
            "path": node.root_path,
            "status": status,
            "budget_granted": node.granted,
            "budget_returned": returned,
            "final_message": final_message,
        }
        if node.spawned:
            result["subagents"] = node.spawned
        if report is not None:
            result.update(report)
        else:
            result["report_missing"] = True
            result["hint"] = (
                f"El subagente no dejó {swarm_utils.REPORT_FILENAME} pese a pedírselo dos veces. "
                "Trata final_message como no verificado o vuelve a delegar la tarea."
            )
        return result

    async def _chat(
        self,
        user_input: str,
        agent_name: str,
        exec_ctx: ExecutionContext,
        images: list[dict[str, Any]] | None = None,
    ):
        user_message = image_utils.build_user_message_item(user_input, images=images)
        exec_ctx.context[agent_name].append(user_message)

        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = image_utils.with_replaced_message_text(
            working_context[-1],
            await self._memory_service.augment_user_message(user_input, exec_ctx.session_id),
        )

        return await self._run_agent_loop(agent_name, working_context, exec_ctx, self.max_iterations)

    async def _run_agent_loop(
        self,
        agent_name: str,
        working_context: list[dict[str, Any]],
        exec_ctx: ExecutionContext,
        max_iterations: int,
        subagent: bool = False,
    ):
        for _ in range(max_iterations):
            items = await self._request_agent(agent_name, working_context, exec_ctx)
            function_calls, final_message = self._collect_response_items(items, agent_name, working_context, exec_ctx)

            if final_message and not function_calls:
                if subagent:
                    print(f"\033[1;34m  └─ {agent_name} completado\033[0m")
                return final_message

            if function_calls:
                await self._append_tool_results(function_calls, agent_name, working_context, exec_ctx)

        return f"[{agent_name}] Máximo de iteraciones alcanzado"

    def _collect_response_items(
        self,
        items: list[dict[str, Any]],
        agent_name: str,
        working_context: list[dict[str, Any]],
        exec_ctx: ExecutionContext,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Añade al contexto los items ya normalizados por el provider
        (incluidos los de reasoning) y clasifica tool calls y mensaje final."""
        function_calls = []
        final_message: Optional[str] = None

        for task in items:
            working_context.append(task)
            exec_ctx.context[agent_name].append(task)

            if task["type"] == "function_call":
                function_calls.append(task)
            elif task["type"] == "message":
                extracted_text = context_utils.extract_message_text(task)
                if extracted_text:
                    final_message = extracted_text

        return function_calls, final_message

    async def _append_tool_results(
        self,
        function_calls: list[dict[str, Any]],
        agent_name: str,
        working_context: list[dict[str, Any]],
        exec_ctx: ExecutionContext,
    ):
        tool_results = await self._execute_tools_parallel(function_calls, agent_name, exec_ctx)
        for call_id, result in tool_results.items():
            tool_call = next(call for call in function_calls if call["call_id"] == call_id)
            try:
                requested_chars = json.loads(tool_call["arguments"]).get("max_chars")
                requested_chars = int(requested_chars) if requested_chars is not None else None
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                requested_chars = None
            tool_output = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": context_utils.serialize_tool_result(result, requested_chars),
            }
            working_context.append(tool_output)
            exec_ctx.context[agent_name].append(tool_output)

    # ── Ciclo de vida ──

    async def delete_session(self, session_id: str):
        await self.session_manager.delete_session(session_id)
        if self.conversation_store is not None:
            await self.conversation_store.delete_conversation(session_id)
        await self._locks.clear_session(session_id)

    async def close(self):
        await self.session_manager.close()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self.memory_rag is not None:
            await self.memory_rag.close()
        if self.conversation_store is not None:
            await self.conversation_store.close()
        for provider in self._providers.values():
            await provider.close()
