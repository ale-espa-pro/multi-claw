import asyncio
import copy
import inspect
import json
from time import time
from typing import Any, Optional

from agents.agent_builder import AgentBuilder
from data.conversation_store import PostgresConversationStore
from data.redis_manager import RedisSessionManager
from runner import context as context_utils
from runner import images as image_utils
from runner.execution import ExecutionContext, SessionLockRegistry
from runner.memory import (
    MemoryRetrievalConfig,
    MemoryService,
    normalize_retrieval_limit,
    normalize_retrieval_mode,
    normalize_retrieval_weight,
    normalize_similarity_threshold,
)
from tools.memoryTools.RAG_memory import MemoryRag


class AgentRunner:
    def __init__(
        self,
        client,
        agent_builder: AgentBuilder,
        redis_url: str | None = None,
        conversation_store: PostgresConversationStore | None = None,
        memory_rag: MemoryRag | None = None,
    ):
        self.client = client
        self.agent_builder = agent_builder
        self.main_agent = agent_builder.main_agent
        self.agent_names = agent_builder.agent_names

        runner_config = agent_builder.get_runner_config()
        self.max_iterations = int(runner_config["max_iterations"])

        self.session_manager = RedisSessionManager(redis_url=redis_url)
        self.conversation_store = conversation_store
        self.memory_rag = memory_rag
        self._api_semaphore = asyncio.Semaphore(10)
        self._background_tasks: set[asyncio.Task] = set()
        self._locks = SessionLockRegistry()
        self._memory_service = MemoryService(
            memory_rag=memory_rag,
            config=MemoryRetrievalConfig.from_env(),
            background_tasks=self._background_tasks,
            get_memory_lock=self._locks.get_memory_lock,
            build_context_delta=self._build_context_delta,
            serialize_context_for_memory=self._serialize_context_for_memory,
        )

    _normalize_similarity_threshold = staticmethod(normalize_similarity_threshold)
    _normalize_retrieval_limit = staticmethod(normalize_retrieval_limit)
    _normalize_retrieval_mode = staticmethod(normalize_retrieval_mode)
    _normalize_retrieval_weight = staticmethod(normalize_retrieval_weight)
    _extract_message_text = staticmethod(context_utils.extract_message_text)
    _normalize_message_content = staticmethod(context_utils.normalize_message_content)
    _normalize_image_detail = staticmethod(image_utils.normalize_image_detail)
    _normalize_image_part = staticmethod(image_utils.normalize_image_part)
    _serialize_tool_result = staticmethod(context_utils.serialize_tool_result)
    _serialize_user_payload = staticmethod(context_utils.serialize_user_payload)
    _image_input_to_part = staticmethod(image_utils.image_input_to_part)
    _build_user_message_item = staticmethod(image_utils.build_user_message_item)
    _with_replaced_message_text = staticmethod(image_utils.with_replaced_message_text)
    _resolve_local_image_part = staticmethod(image_utils.resolve_local_image_part)
    _prepare_context_for_api = staticmethod(image_utils.prepare_context_for_api)

    def _normalize_context_item(self, item: Any) -> Optional[dict[str, Any]]:
        return context_utils.normalize_context_item(item)

    def _normalize_full_context(self, context: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        return context_utils.normalize_full_context(context, self.agent_names)

    def _serialize_context_for_memory(self, context: dict[str, list[dict[str, Any]]]) -> str:
        return context_utils.serialize_context_for_memory(context)

    def _build_context_delta(
        self,
        before_context: dict[str, Any],
        after_context: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        return context_utils.build_context_delta(before_context, after_context, self.agent_names)

    def _prepare_memory_query(self, text: str) -> str:
        return self._memory_service.prepare_query(text)

    def _format_retrieved_memory(self, chunks: list[dict[str, Any]]) -> str:
        return self._memory_service.format_retrieved_memory(chunks)

    async def _augment_user_message_with_memory(
        self,
        user_text: str,
        exec_ctx: ExecutionContext,
    ) -> str:
        return await self._memory_service.augment_user_message(user_text, exec_ctx.session_id)

    def _schedule_semantic_memory_sync(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        previous_context: dict[str, list[dict[str, Any]]] | None = None,
        conversation_type: str | None = None,
    ):
        self._memory_service.schedule_semantic_sync(
            session_id=session_id,
            context=context,
            previous_context=previous_context,
            conversation_type=conversation_type,
        )

    async def _load_complete_context(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        cached_context, context_exists = await self.session_manager.load_context(session_id, self.agent_names)
        normalized_cached = self._normalize_full_context(cached_context)
        if context_exists:
            return normalized_cached

        if self.conversation_store is None:
            return normalized_cached

        stored_context = await self.conversation_store.load_context(session_id)
        normalized_stored = self._normalize_full_context(stored_context)

        if any(normalized_stored.values()):
            await self.session_manager.save_context(session_id, normalized_stored)

        return normalized_stored

    async def _persist_complete_context(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        conversation_type: str | None = None,
    ):
        normalized_context = self._normalize_full_context(context)
        if self.conversation_store is not None:
            await self.conversation_store.save_context(
                session_id=session_id,
                context=normalized_context,
                conversation_type=conversation_type,
            )
        await self.session_manager.save_context(session_id, normalized_context)

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        return await self._locks.get_session_lock(session_id)

    async def _get_memory_lock(self, session_id: str) -> asyncio.Lock:
        return await self._locks.get_memory_lock(session_id)

    async def _cleanup_session_lock(self, session_id: str):
        await self._locks.cleanup_session_lock(session_id)

    async def process_message(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None = None,
        conversation_type: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        result = await self._process_message(
            session_id=session_id,
            user_input=user_input,
            agent_name=agent_name,
            conversation_type=conversation_type,
            images=images,
        )
        result["exec_ctx"].token_tracker.print_summary()
        return result["response"]

    async def process_message_with_usage(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None = None,
        conversation_type: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> dict:
        result = await self._process_message(
            session_id=session_id,
            user_input=user_input,
            agent_name=agent_name,
            conversation_type=conversation_type,
            images=images,
        )
        return {
            "response": result["response"],
            "token_usage": result["exec_ctx"].token_tracker.get_usage(),
        }

    async def _process_message(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None,
        conversation_type: str | None,
        images: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        resolved_agent = agent_name or self.main_agent
        session_lock = await self._get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(session_id, self.agent_names, conversation_type)
            try:
                exec_ctx.context = await self._load_complete_context(session_id)
                previous_context = copy.deepcopy(exec_ctx.context)

                response = await self._chat(user_input, resolved_agent, exec_ctx, images=images)

                await self._persist_complete_context(
                    session_id=session_id,
                    context=exec_ctx.context,
                    conversation_type=conversation_type,
                )
                self._schedule_semantic_memory_sync(
                    session_id=session_id,
                    context=exec_ctx.context,
                    previous_context=previous_context,
                    conversation_type=conversation_type,
                )
                return {"response": response, "exec_ctx": exec_ctx}
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
            finally:
                await self._cleanup_session_lock(session_id)

    async def _request_agent(self, agent_name: str, curr_context: list, exec_ctx: ExecutionContext):
        system_prompt = self.agent_builder.build_system_prompt(
            agent_name, exec_ctx.session_id, exec_ctx.conversation_type
        )
        messages = [{"role": "system", "content": system_prompt}] + self._prepare_context_for_api(curr_context)
        curr_agent_tools = self.agent_builder.get_tools_for_agent(agent_name)
        kwargs = self.agent_builder.get_response_create_kwargs(agent_name)

        async with self._api_semaphore:
            response = await self.client.responses.create(
                input=messages,
                tools=curr_agent_tools,
                **kwargs,
            )

        exec_ctx.token_tracker.accumulate(response, agent_name=agent_name)
        return response

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
                call_id, result = await self._execute_tool(tc, caller_agent, exec_ctx)
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
        raw_user_text = self._serialize_user_payload(task_description)
        exec_ctx.context[agent_name].append(self._build_user_message_item(raw_user_text))

        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = self._build_user_message_item(
            await self._augment_user_message_with_memory(raw_user_text, exec_ctx)
        )

        return await self._run_agent_loop(agent_name, working_context, exec_ctx, max_iterations, subagent=True)

    async def _chat(
        self,
        user_input: str,
        agent_name: str,
        exec_ctx: ExecutionContext,
        images: list[dict[str, Any]] | None = None,
    ):
        user_message = self._build_user_message_item(user_input, images=images)
        exec_ctx.context[agent_name].append(user_message)

        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = self._with_replaced_message_text(
            working_context[-1],
            await self._augment_user_message_with_memory(user_input, exec_ctx),
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
            response = await self._request_agent(agent_name, working_context, exec_ctx)
            function_calls, final_message = self._collect_response_items(response, agent_name, working_context, exec_ctx)

            if final_message and not function_calls:
                if subagent:
                    print(f"\033[1;34m  └─ {agent_name} completado\033[0m")
                return final_message

            if function_calls:
                await self._append_tool_results(function_calls, agent_name, working_context, exec_ctx)

        return f"[{agent_name}] Máximo de iteraciones alcanzado"

    def _collect_response_items(
        self,
        response: Any,
        agent_name: str,
        working_context: list[dict[str, Any]],
        exec_ctx: ExecutionContext,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        function_calls = []
        final_message: Optional[str] = None

        for item in response.output:
            task = self._normalize_context_item(item.model_dump())
            if task is None:
                continue
            working_context.append(task)
            exec_ctx.context[agent_name].append(task)

            if task["type"] == "function_call":
                function_calls.append(task)
            elif task["type"] == "message":
                extracted_text = self._extract_message_text(task)
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
                "output": self._serialize_tool_result(result, requested_chars),
            }
            working_context.append(tool_output)
            exec_ctx.context[agent_name].append(tool_output)

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
        close_fn = getattr(self.client, "close", None)
        if close_fn is not None:
            res = close_fn()
            if asyncio.iscoroutine(res):
                await res

    async def run_loop(self, session_id: str = "console_session"):
        while True:
            user_input = await asyncio.to_thread(input, f"\n\033[1;37m[{self.main_agent}] Tu mensaje: \033[0m")
            start_time = time()
            response = await self.process_message(session_id, user_input)
            elapsed = time() - start_time
            print(f"\n\033[1;32m{self.main_agent} ({elapsed:.3f} seg): \033[0m{response}")
