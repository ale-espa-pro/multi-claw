# agent_runner.py
import base64
import json
import copy
import asyncio
import mimetypes
import os
from time import time
from typing import Any, Optional
import inspect
from pricing.token_tracker import TokenUsageTracker
from agents.agent_builder import AgentBuilder
from data.conversation_store import PostgresConversationStore
from data.redis_manager import RedisSessionManager
from tools.memoryTools.RAG_memory import MemoryRag


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class AgentRunner:

    @staticmethod
    def _normalize_similarity_threshold(value: Any, default: float = 0.35) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, numeric))

    @staticmethod
    def _normalize_retrieval_limit(value: Any, default: int = 5) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, numeric)

    @staticmethod
    def _normalize_retrieval_mode(value: Any, default: str = "vector") -> str:
        mode = str(value or "").strip().lower()
        return mode if mode in {"vector", "keyword", "hybrid"} else default

    @staticmethod
    def _normalize_retrieval_weight(value: Any, default: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, numeric)

    def __init__(
        self,
        client,  # EXPECTED: openai.AsyncOpenAI
        agent_builder: AgentBuilder,
        redis_url: str | None = None,
        conversation_store: PostgresConversationStore | None = None,
        memory_rag: MemoryRag | None = None,
    ):
        self.client = client
        self.agent_builder = agent_builder
        self.main_agent = agent_builder.main_agent
        self.agent_names = agent_builder.agent_names

        self.max_messages = 120
        self.keep_after_reset = 10
        self.max_iterations = 400
        self.memory_retrieval_limit = self._normalize_retrieval_limit(
            os.getenv("MEMORY_RETRIEVAL_LIMIT", "5")
        )
        self.memory_min_similarity = self._normalize_similarity_threshold(
            os.getenv("MEMORY_MIN_SIMILARITY", "0.55")
        )
        self.memory_retrieval_mode = self._normalize_retrieval_mode(
            os.getenv("MEMORY_RETRIEVAL_MODE", "vector")
        )
        self.memory_vector_weight = self._normalize_retrieval_weight(
            os.getenv("MEMORY_VECTOR_WEIGHT", "0.7"),
            0.7,
        )
        self.memory_keyword_weight = self._normalize_retrieval_weight(
            os.getenv("MEMORY_KEYWORD_WEIGHT", "0.3"),
            0.3,
        )
        self.memory_query_max_chars = 12_000

        self.session_manager = RedisSessionManager(redis_url=redis_url)
        self.conversation_store = conversation_store
        self.memory_rag = memory_rag
        self._api_semaphore = asyncio.Semaphore(10)
        self._background_tasks: set[asyncio.Task] = set()

        self._session_locks: dict[str, asyncio.Lock] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
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
    def _normalize_message_content(content: Any, role: str) -> list[dict[str, Any]]:
        if isinstance(content, str):
            text_type = "input_text" if role == "user" else "output_text"
            return [{"type": text_type, "text": content}]

        if not isinstance(content, list):
            return []

        normalized_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "output_text"}:
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                normalized_parts.append({"type": part_type, "text": text})
            elif part_type == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                mapped_type = "input_text" if role == "user" else "output_text"
                normalized_parts.append({"type": mapped_type, "text": text})
            elif part_type == "input_image":
                normalized_image = AgentRunner._normalize_image_part(part)
                if normalized_image is not None:
                    normalized_parts.append(normalized_image)
        return normalized_parts

    @staticmethod
    def _normalize_image_detail(detail: Any) -> str:
        return detail if detail in {"low", "high", "auto"} else "auto"

    @staticmethod
    def _normalize_image_part(part: dict[str, Any]) -> Optional[dict[str, Any]]:
        detail = AgentRunner._normalize_image_detail(part.get("detail"))
        normalized: dict[str, Any] = {"type": "input_image", "detail": detail}

        for key in ("image_url", "file_id", "path", "mime_type"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()

        if "image_url" not in normalized and "file_id" not in normalized and "path" not in normalized:
            return None
        return normalized

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
                    image_count = sum(
                        1
                        for part in item.get("content", [])
                        if isinstance(part, dict) and part.get("type") == "input_image"
                    )
                    if image_count:
                        lines.append(f"[{agent_name}] {role}: [{image_count} imagen(es) adjunta(s)]")
                elif item_type == "function_call":
                    lines.append(
                        f"[{agent_name}] function_call {item.get('name', '')}: {item.get('arguments', '')}"
                    )
                elif item_type == "function_call_output":
                    output = item.get("output", "")
                    if isinstance(output, str) and output:
                        lines.append(f"[{agent_name}] function_output: {output}")
        return "\n".join(lines)

    def _build_context_delta(
        self,
        before_context: dict[str, Any],
        after_context: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        before = self._normalize_full_context(before_context)
        after = self._normalize_full_context(after_context)
        delta: dict[str, list[dict[str, Any]]] = {}

        for agent_name in self.agent_names:
            before_items = before.get(agent_name, [])
            after_items = after.get(agent_name, [])
            overlap = self._find_context_overlap(before_items, after_items)
            delta[agent_name] = copy.deepcopy(after_items[overlap:])

        return delta

    @staticmethod
    def _find_context_overlap(
        before_items: list[dict[str, Any]],
        after_items: list[dict[str, Any]],
    ) -> int:
        max_overlap = min(len(before_items), len(after_items))
        for overlap in range(max_overlap, 0, -1):
            if before_items[-overlap:] == after_items[:overlap]:
                return overlap
        return 0

    @staticmethod
    def _serialize_tool_result(result: Any) -> str:
        return json.dumps(result, ensure_ascii=False, default=str)

    @staticmethod
    def _serialize_user_payload(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _image_input_to_part(image: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(image, dict):
            return None

        detail = AgentRunner._normalize_image_detail(image.get("detail"))
        mime_type = (image.get("mime_type") or "").strip() or None

        file_id = (image.get("file_id") or "").strip()
        if file_id:
            return {"type": "input_image", "file_id": file_id, "detail": detail}

        url = (image.get("url") or "").strip()
        if url:
            return {"type": "input_image", "image_url": url, "detail": detail}

        data_url = (image.get("data_url") or "").strip()
        if data_url:
            return {"type": "input_image", "image_url": data_url, "detail": detail}

        path = (image.get("path") or "").strip()
        if path:
            part = {"type": "input_image", "path": path, "detail": detail}
            if mime_type:
                part["mime_type"] = mime_type
            return part

        raw_base64 = (image.get("base64") or "").strip()
        if raw_base64:
            compact_base64 = "".join(raw_base64.split())
            decoded = base64.b64decode(compact_base64, validate=True)
            if len(decoded) > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {len(decoded)}")
            mime = mime_type or "image/png"
            return {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{compact_base64}",
                "detail": detail,
            }

        return None

    @staticmethod
    def _build_user_message_item(text: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
        for image in images or []:
            try:
                image_part = AgentRunner._image_input_to_part(image)
            except Exception as exc:
                content.append({"type": "input_text", "text": f"[imagen omitida: {exc}]"})
                continue
            if image_part is not None:
                content.append(image_part)
        return {
            "type": "message",
            "role": "user",
            "content": content,
        }

    @staticmethod
    def _with_replaced_message_text(message_item: dict[str, Any], text: str) -> dict[str, Any]:
        item = copy.deepcopy(message_item)
        replaced = False
        for part in item.get("content", []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "input_text":
                part["text"] = text
                replaced = True
                break
        if not replaced:
            item.setdefault("content", []).insert(0, {"type": "input_text", "text": text})
        return item

    @staticmethod
    def _resolve_local_image_part(part: dict[str, Any]) -> dict[str, Any]:
        if "path" not in part:
            return part

        path = os.path.realpath(os.path.expandvars(os.path.expanduser(part["path"])))
        if not os.path.isfile(path):
            raise ValueError(f"image file not found: {path}")

        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {size}")

        ext = os.path.splitext(path)[1].lower()
        mime = part.get("mime_type") or IMAGE_EXT_TO_MIME.get(ext) or mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            image_url = f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"

        api_part = {"type": "input_image", "image_url": image_url, "detail": part.get("detail", "auto")}
        return api_part

    @classmethod
    def _prepare_context_for_api(cls, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for item in context:
            if not isinstance(item, dict) or item.get("type") != "message":
                prepared.append(item)
                continue

            api_item = copy.deepcopy(item)
            api_content = []
            for part in api_item.get("content", []):
                if isinstance(part, dict) and part.get("type") == "input_image":
                    try:
                        api_content.append(cls._resolve_local_image_part(part))
                    except Exception as exc:
                        api_content.append({
                            "type": "input_text",
                            "text": f"[imagen no disponible para enviar al modelo: {exc}]",
                        })
                else:
                    api_content.append(part)
            api_item["content"] = api_content
            prepared.append(api_item)
        return prepared

    def _prepare_memory_query(self, text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) <= self.memory_query_max_chars:
            return compact
        return compact[:self.memory_query_max_chars]

    def _format_retrieved_memory(self, chunks: list[dict[str, Any]]) -> str:
        lines = [
            "[EXTRA DE MEMORIA AUTOMATICA]",
            (
                "Estos fragmentos vienen de la base de memoria de conversaciones y son un extra "
                "potencialmente util para contextualizar la solicitud actual o decidir si conviene "
                "hacer una busqueda de memoria mas especifica. No forman parte literal del mensaje del usuario."
            ),
        ]

        for idx, chunk in enumerate(chunks, start=1):
            header = f"[{idx}] session_id={chunk.get('session_id', 'unknown')}"
            conversation_type = chunk.get("conversation_type")
            if conversation_type:
                header += f" | conversation_type={conversation_type}"
            score = chunk.get("score")
            if isinstance(score, (int, float)):
                header += f" | score={score:.4f}"
            method = chunk.get("retrieval_method")
            if method:
                header += f" | retrieval={method}"
            lines.append(header)
            lines.append(str(chunk.get("chunck", "")).strip())

        return "\n".join(lines)

    async def _augment_user_message_with_memory(
        self,
        user_text: str,
        exec_ctx: "ExecutionContext",
    ) -> str:
        if self.memory_rag is None:
            return user_text
        if self.memory_retrieval_limit <= 0:
            return user_text

        query_text = self._prepare_memory_query(user_text)
        if not query_text:
            return user_text

        try:
            chunks = await self.memory_rag.search_chunks(
                query=query_text,
                limit=self.memory_retrieval_limit,
                min_similarity=self.memory_min_similarity,
                mode=self.memory_retrieval_mode,
                vector_weight=self.memory_vector_weight,
                keyword_weight=self.memory_keyword_weight,
            )
        except Exception as exc:
            print(f"\033[1;31m  [MEMORY] Retrieval error: {exc}\033[0m")
            return user_text

        if not chunks:
            print(
                f"\033[1;35m  [MEMORY] 0 chunks para {exec_ctx.session_id} "
                f"(mode={self.memory_retrieval_mode}, k={self.memory_retrieval_limit}, "
                f"min_similarity={self.memory_min_similarity:.2f})\033[0m"
            )
            return user_text

        print(
            f"\033[1;35m  [MEMORY] {len(chunks)} chunks recuperados para "
            f"{exec_ctx.session_id} "
            f"(mode={self.memory_retrieval_mode}, k={self.memory_retrieval_limit}, "
            f"min_similarity={self.memory_min_similarity:.2f})\033[0m"
        )
        return f"{user_text}\n\n{self._format_retrieved_memory(chunks)}"

    def _schedule_semantic_memory_sync(
        self,
        session_id: str,
        context: dict[str, list[dict[str, Any]]],
        previous_context: dict[str, list[dict[str, Any]]] | None = None,
        conversation_type: str | None = None,
    ):
        if self.memory_rag is None:
            return

        context_snapshot = self._build_context_delta(previous_context or {}, context)
        memory_text = self._serialize_context_for_memory(context_snapshot)
        if not memory_text.strip():
            return

        async def _runner():
            try:
                lock = await self._get_memory_lock(session_id)
                async with lock:
                    await self.memory_rag.store_text_embeddings(
                        session_id=session_id,
                        text=memory_text,
                        conversation_type=conversation_type,
                        replace=False,
                    )
            except Exception as exc:
                print(f"\033[1;31m  [MEMORY] Sync error for {session_id}: {exc}\033[0m")

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

    async def _get_memory_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._memory_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._memory_locks[session_id] = lock
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
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        agent_name = agent_name or self.main_agent
        session_lock = await self._get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(session_id, self.agent_names, conversation_type)
            try:
                exec_ctx.context = await self._load_complete_context(session_id)
                previous_context = copy.deepcopy(exec_ctx.context)

                response = await self._chat(user_input, agent_name, exec_ctx, images=images)

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
        images: list[dict[str, Any]] | None = None,
    ) -> dict:
        agent_name = agent_name or self.main_agent
        session_lock = await self._get_session_lock(session_id)

        async with session_lock:
            exec_ctx = ExecutionContext(session_id, self.agent_names, conversation_type)
            try:
                exec_ctx.context = await self._load_complete_context(session_id)
                previous_context = copy.deepcopy(exec_ctx.context)

                response = await self._chat(user_input, agent_name, exec_ctx, images=images)

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
        messages = [{"role": "system", "content": system_prompt}] + self._prepare_context_for_api(curr_context)
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
                reasoning={"effort": "high", "summary": "auto"},
                **kwargs,
            )

        exec_ctx.token_tracker.accumulate(response, agent_name=agent_name)
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
            result = await self._run_subagent(tool_name, tool_arguments, exec_ctx)
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
        task_description: Any,
        exec_ctx: "ExecutionContext",
        max_iterations: int = 10,
    ):
        raw_user_text = self._serialize_user_payload(task_description)
        exec_ctx.context[agent_name].append(self._build_user_message_item(raw_user_text))
        self._truncate_context_if_needed(agent_name, exec_ctx)

        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = self._build_user_message_item(
            await self._augment_user_message_with_memory(raw_user_text, exec_ctx)
        )

        for _ in range(max_iterations):
            response = await self._request_agent(agent_name, working_context, exec_ctx)

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

            if final_message and not function_calls:
                print(f"\033[1;34m  └─ {agent_name} completado\033[0m")
                return final_message

            if function_calls:
                tool_results = await self._execute_tools_parallel(function_calls, agent_name, exec_ctx)
                for call_id, result in tool_results.items():
                    tool_output = {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": self._serialize_tool_result(result),
                    }
                    working_context.append(tool_output)
                    exec_ctx.context[agent_name].append(tool_output)

        return f"[{agent_name}] Máximo de iteraciones alcanzado"

    async def _chat(
        self,
        user_input: str,
        agent_name: str,
        exec_ctx: "ExecutionContext",
        images: list[dict[str, Any]] | None = None,
    ):
        user_message = self._build_user_message_item(user_input, images=images)
        exec_ctx.context[agent_name].append(user_message)

        self._truncate_context_if_needed(agent_name, exec_ctx)
        working_context = copy.deepcopy(exec_ctx.context[agent_name])
        working_context[-1] = self._with_replaced_message_text(
            working_context[-1],
            await self._augment_user_message_with_memory(user_input, exec_ctx),
        )

        for _ in range(self.max_iterations):
            response = await self._request_agent(agent_name, working_context, exec_ctx)

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

            if function_calls:
                tool_results = await self._execute_tools_parallel(function_calls, agent_name, exec_ctx)
                for call_id, result in tool_results.items():
                    tool_output = {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": self._serialize_tool_result(result),
                    }
                    working_context.append(tool_output)
                    exec_ctx.context[agent_name].append(tool_output)
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
            self._memory_locks.pop(session_id, None)

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
