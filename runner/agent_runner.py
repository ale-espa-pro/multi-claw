# agent_runner.py
import json
import asyncio
from time import time
from typing import Any, Optional
import inspect
from pricing.token_tracker import TokenUsageTracker
from agents.agent_builder import AgentBuilder
from data.conversation_store import PostgresConversationStore
from data.redis_manager import RedisSessionManager
from tools.memoryTools.RAG_memory import MemoryRag


class AgentRunner:

    def __init__(
        self,
        client,  # EXPECTED: openai.AsyncOpenAI
        agent_builder: AgentBuilder,
        redis_url: str = "redis://localhost:6379",
        conversation_store: PostgresConversationStore | None = None,
        memory_rag: MemoryRag | None = None,
    ):
        self.client = client
        self.agent_builder = agent_builder
        self.main_agent = agent_builder.main_agent
        self.agent_names = agent_builder.agent_names

        self.max_messages = 15
        self.keep_after_reset = 7
        self.max_iterations = 120

        self.session_manager = RedisSessionManager(redis_url=redis_url)
        self.conversation_store = conversation_store
        self.memory_rag = memory_rag
        self._api_semaphore = asyncio.Semaphore(5)
        self._background_tasks: set[asyncio.Task] = set()

        self._session_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    # ── Context normalization ──

    @staticmethod
    def _extract_message_text(message_item: dict[str, Any]) -> Optional[str]:
        for part in reversed(message_item.get("content", [])):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type not in {"output_text", "input_text", "text"}:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        return None

    @staticmethod
    def _normalize_message_content(content: Any, role: str) -> list[dict[str, str]]:
        if isinstance(content, str):
            text_type = "input_text" if role == "user" else "output_text"
            return [{"type": text_type, "text": content}]

        if not isinstance(content, list):
            return []

        normalized_parts: list[dict[str, str]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            text = part.get("text")
            if not isinstance(text, str):
                continue
            if part_type in {"input_text", "output_text"}:
                normalized_parts.append({"type": part_type, "text": text})
            elif part_type == "text":
                mapped_type = "input_text" if role == "user" else "output_text"
                normalized_parts.append({"type": mapped_type, "text": text})
        return normalized_parts

    def _normalize_context_item(self, item: Any) -> Optional[dict[str, Any]]:
        if not isinstance(item, dict):
            return None

        item_type = item.get("type")

        if item_type == "message":
            role = item.get("role")
            if role not in {"user", "assistant", "developer", "system"}:
                return None
            content = self._normalize_message_content(item.get("content"), role)
            if not content:
                return None
            return {"type": "message", "role": role, "content": content}

        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not all(isinstance(v, str) for v in (call_id, name, arguments)):
                return None
            return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}

        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                return None
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            return {"type": "function_call_output", "call_id": call_id, "output": output}

        return None

    def _normalize_full_context(self, context: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(context, dict):
            context = {}
        normalized_context: dict[str, list[dict[str, Any]]] = {}
        for agent_name in self.agent_names:
            raw_items = context.get(agent_name, [])
            if not isinstance(raw_items, list):
                raw_items = []
            normalized_context[agent_name] = [
                n for item in raw_items if (n := self._normalize_context_item(item)) is not None
            ]
        return normalized_context

    def _serialize_context_for_memory(self, context: dict[str, list[dict[str, Any]]]) -> str:
        lines: list[str] = []
        for agent_name in sorted(context.keys()):
            for item in context.get(agent_name, []):
                item_type = item.get("type")
                if item_type == "message":
                    role = item.get("role", "unknown")
                    text = self._extract_message_text(item)
                    if text:
                        lines.append(f"[{agent_name}] {role}: {text}")
                elif item_type == "function_call":
                    lines.append(
                        f"[{agent_name}] function_call {item.get('name', '')}: {item.get('arguments', '')}"
                    )
                elif item_type == "function_call_output":
                    output = item.get("output", "")
                    if isinstance(output, str) and output:
                        lines.append(f"[{agent_name}] function_output: {output}")
        return "\n".join(lines)

    @staticmethod
    def _serialize_tool_result(result: Any) -> str:
        return json.dumps(result, ensure_ascii=False, default=str)

    def _schedule_semantic_memory_sync(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        conversation_type: str | None = None,
    ):
        if self.memory_rag is None:
            return

        context_snapshot = self._normalize_full_context(context)
        memory_text = self._serialize_context_for_memory(context_snapshot)
        if not memory_text.strip():
            return

        async def _runner():
            try:
                await self.memory_rag.store_text_embeddings(
                    session_id=session_id,
                    text=memory_text,
                    conversation_type=conversation_type,
                    replace=True,
                )
            except Exception:
                pass

        task = asyncio.create_task(_runner())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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

    # ── Session locking ──

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    async def _cleanup_session_lock(self, session_id: str):
        async with self._locks_lock:
            lock = self._session_locks.get(session_id)
            if lock is not None and not lock.locked():
                del self._session_locks[session_id]

    # ── Public API ──

    async def process_message(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None = None,
        conversation_type: str | None = None,
    ) -> str:
        agent_name = agent_name or self.main_agent
        session_lock = await self._get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(session_id, self.agent_names, conversation_type)
            try:
                exec_ctx.context = await self._load_complete_context(session_id)

                response = await self._chat(user_input, agent_name, exec_ctx)

                await self._persist_complete_context(
                    session_id=session_id,
                    context=exec_ctx.context,
                    conversation_type=conversation_type,
                )
                self._schedule_semantic_memory_sync(
                    session_id=session_id,
                    context=exec_ctx.context,
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
            finally:
                await self._cleanup_session_lock(session_id)

    async def process_message_with_usage(
        self,
        session_id: str,
        user_input: str,
        agent_name: str | None = None,
        conversation_type: str | None = None,
    ) -> dict:
        agent_name = agent_name or self.main_agent
        session_lock = await self._get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(session_id, self.agent_names, conversation_type)
            try:
                exec_ctx.context = await self._load_complete_context(session_id)

                response = await self._chat(user_input, agent_name, exec_ctx)

                await self._persist_complete_context(
                    session_id=session_id,
                    context=exec_ctx.context,
                    conversation_type=conversation_type,
                )
                self._schedule_semantic_memory_sync(
                    session_id=session_id,
                    context=exec_ctx.context,
                    conversation_type=conversation_type,
                )
                return {"response": response, "token_usage": exec_ctx.token_tracker.get_usage()}
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

    # ── Agent execution ──

    async def _request_agent(self, agent_name: str, curr_context: list, exec_ctx: "ExecutionContext"):
        system_prompt = self.agent_builder.build_system_prompt(
            agent_name, exec_ctx.session_id, exec_ctx.conversation_type
        )
        messages = [{"role": "system", "content": system_prompt}] + curr_context
        kwargs: dict[str, Any] = {}

        if agent_name in ["web_search_agent", "ResearchAgent", "WebSearchAgent"]:
            curr_agent_tools = [{"type": "web_search"}]
        else:
            curr_agent_tools = self.agent_builder.get_tools_for_agent(agent_name)
            kwargs["parallel_tool_calls"] = False

            if self.agent_builder.uses_json_response(agent_name):
                kwargs["text"] = {"format": {"type": "json_object"}}
                kwargs["parallel_tool_calls"] = True

        async with self._api_semaphore:
            response = await self.client.responses.create(
                model="gpt-5.4",
                input=messages,
                tools=curr_agent_tools,
                reasoning={"effort": "medium", "summary": "auto"},
                **kwargs,
            )

        exec_ctx.token_tracker.accumulate(response)
        return response

    async def _execute_tool(self, tool_call: dict, caller_agent: str, exec_ctx: "ExecutionContext"):
        tool_name = tool_call["name"]
        call_id = tool_call["call_id"]

        try:
            tool_arguments = json.loads(tool_call["arguments"])
        except json.JSONDecodeError:
            return call_id, "Error: Invalid JSON arguments"

        if tool_name in self.agent_names:
            print(f"\033[1;35m  [{caller_agent}] → AGENT CALL → {tool_name}: \033[0m"
                  f"{json.dumps(tool_arguments, ensure_ascii=False)}")
            result = await self._run_subagent(tool_name, json.dumps(tool_arguments, ensure_ascii=False), exec_ctx)
            return call_id, result

        dispatcher = self.agent_builder.ticket_dispatcher
        if tool_name not in dispatcher:
            return call_id, f"Error: Tool '{tool_name}' not found"

        print(f"\033[1;33m  [{caller_agent}] TOOL → {tool_name}: \033[0m{tool_arguments}")
        func = dispatcher[tool_name]

        if inspect.iscoroutinefunction(func):
            result = await func(tool_arguments)
        else:
            result = await asyncio.to_thread(func, tool_arguments)
        print(result)
        return call_id, result

    async def _execute_tools_parallel(self, function_calls: list, caller_agent: str, exec_ctx: "ExecutionContext"):
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
        task_description: str,
        exec_ctx: "ExecutionContext",
        max_iterations: int = 10,
    ):
        exec_ctx.context[agent_name].append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": json.dumps(task_description, ensure_ascii=False)}],
        })
        working_context = exec_ctx.context[agent_name].copy()

        for _ in range(max_iterations):
            response = await self._request_agent(agent_name, working_context, exec_ctx)

            function_calls = []
            final_message: Optional[str] = None

            for item in response.output:
                task = self._normalize_context_item(item.model_dump())
                if task is None:
                    continue
                working_context.append(task)

                if task["type"] == "function_call":
                    function_calls.append(task)
                elif task["type"] == "message":
                    extracted_text = self._extract_message_text(task)
                    if extracted_text:
                        final_message = extracted_text

            if final_message and not function_calls:
                print(f"\033[1;34m  └─ {agent_name} completado\033[0m")
                exec_ctx.context[agent_name] = working_context
                return final_message

            if function_calls:
                tool_results = await self._execute_tools_parallel(function_calls, agent_name, exec_ctx)
                for call_id, result in tool_results.items():
                    working_context.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": self._serialize_tool_result(result),
                        }
                    )

        exec_ctx.context[agent_name] = working_context
        return f"[{agent_name}] Máximo de iteraciones alcanzado"

    async def _chat(self, user_input: str, agent_name: str, exec_ctx: "ExecutionContext"):
        exec_ctx.context[agent_name].append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": user_input}],
        })

        self._truncate_context_if_needed(agent_name, exec_ctx)

        for _ in range(self.max_iterations):
            response = await self._request_agent(agent_name, exec_ctx.context[agent_name], exec_ctx)

            function_calls = []
            final_message: Optional[str] = None

            for item in response.output:
                task = self._normalize_context_item(item.model_dump())
                if task is None:
                    continue
                exec_ctx.context[agent_name].append(task)

                if task["type"] == "function_call":
                    function_calls.append(task)
                elif task["type"] == "message":
                    extracted_text = self._extract_message_text(task)
                    if extracted_text:
                        final_message = extracted_text

            if function_calls:
                tool_results = await self._execute_tools_parallel(function_calls, agent_name, exec_ctx)
                for call_id, result in tool_results.items():
                    exec_ctx.context[agent_name].append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": self._serialize_tool_result(result),
                        }
                    )
                continue

            if final_message:
                return final_message

        return f"[{agent_name}] Máximo de iteraciones alcanzado"

    def _truncate_context_if_needed(self, agent_name: str, exec_ctx: "ExecutionContext") -> bool:
        context = exec_ctx.context[agent_name]
        user_msg_indices = [
            i for i, msg in enumerate(context)
            if msg.get("type") == "message" and msg.get("role") == "user"
        ]
        if len(user_msg_indices) > self.max_messages:
            cut_index = user_msg_indices[-self.keep_after_reset]
            exec_ctx.context[agent_name] = context[cut_index:]
            print(f"\033[1;33m  [CONTEXT] Reset: {len(user_msg_indices)} → {self.keep_after_reset} mensajes\033[0m")
            return True
        return False

    # ── Cleanup ──

    async def delete_session(self, session_id: str):
        await self.session_manager.delete_session(session_id)
        if self.conversation_store is not None:
            await self.conversation_store.delete_conversation(session_id)
        async with self._locks_lock:
            self._session_locks.pop(session_id, None)

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


class ExecutionContext:
    """Per-request isolated execution context."""

    def __init__(self, session_id: str, agent_names: set[str], conversation_type: str | None = None):
        self.session_id = session_id
        self.conversation_type = conversation_type
        self.context: dict[str, list] = {name: [] for name in agent_names}
        self.token_tracker = TokenUsageTracker()
