# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run the FastAPI server (port 8000):
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

There is no test suite, linter config, build script, or `requirements.txt` in the repo. Dependencies are installed ad-hoc into the active Python env (WSL2/Ubuntu). Runtime expects Postgres, Redis, and an OpenAI API key to be reachable — see the `.env` keys below.

External services used at startup:
- **PostgreSQL**: `MULTIAGENT_PG_HOST/PORT/DB/USER/PASSWORD`, schema `MULTIAGENT_PG_SCHEMA` (default `multiagente`). `pgvector` extension is detected at runtime; when available, embeddings are stored as `halfvec(3072)` and indexed with HNSW, otherwise fallback to `JSONB` with no vector search.
- **Redis**: `REDIS_URL` (session context cache, 120s TTL).
- **OpenAI**: `OPENAI_API_KEY` (Responses API + embeddings).
- **Twilio**: `TWILIO_ACCOUNT_SID/AUTH_TOKEN` for the WhatsApp webhook.
- **User/env paths**: `WORKING_PATH`, `SESSIONS_PATH`, `CRONS_PATH`, `WORKFLOW_PATH`, `USER_PREFERENCES_PATH`, `USER_SYSTEM`, `USER_PHONE`, `USER_EMAIL`, `USER_WINDOWS_PATH1/2` — these are injected into every agent's system prompt.

## Architecture

Multi-agent orchestration over the OpenAI **Responses API** (model id hardcoded as `gpt-5.5` in [runner/agent_runner.py](runner/agent_runner.py)). Entry point is [main.py](main.py); the brains are in [runner/agent_runner.py](runner/agent_runner.py) and [agents/agent_builder.py](agents/agent_builder.py).

### Request flow

1. HTTP `/chat` (or Twilio `/twilio/webhook`) → `AgentRunner.process_message(session_id, user_input, conversation_type)`.
2. An `asyncio.Lock` keyed by `session_id` serializes concurrent calls for the same session ([agent_runner.py:321](runner/agent_runner.py#L321)).
3. `_load_complete_context` reads context from Redis first, falls back to Postgres `context_jsonb`, and rehydrates Redis on hit.
4. `_chat` drives the main agent loop (up to `max_iterations=400` for the main agent): call Responses API → walk `response.output`, splitting into `function_call`s and assistant messages → execute tools in parallel via `asyncio.TaskGroup` → append outputs → loop until a final message with no pending tool calls.
5. On the **first** user message of each turn, `_augment_user_message_with_memory` injects a block of RAG-retrieved chunks (env-tunable via `MEMORY_RETRIEVAL_LIMIT` default `5`, `MEMORY_MIN_SIMILARITY` default `0.45`). The augmented text is only placed in the *working copy* of the context — the persisted context keeps the raw user text.
6. After the loop, context is persisted to both Postgres and Redis, and a **background task** re-embeds the full conversation via `MemoryRag.store_text_embeddings(replace=True)`. The task is tracked in `_background_tasks` and awaited in `AgentRunner.close()`.

### Agents as tools (key invariant)

Each agent is a dict of `{base_prompt, tools, json_response}` registered from [agents/agent_config.json](agents/agent_config.json). An agent listed in another agent's `tools` array becomes a **callable sub-agent**: when the parent emits a `function_call` whose name matches an `agent_name`, `_execute_tool` dispatches to `_run_subagent` instead of the tool dispatcher ([agent_runner.py:460](runner/agent_runner.py#L460)). Sub-agent context lives in its own slot of the per-session context dict and runs up to 10 iterations.

Current wiring (see `agent_config.json`):
- **ExecutorAgent** (main) → WebSearchAgent, DeviceManagerAgent, save_preference, read_file, memory_query, interpret_image.
- **WebSearchAgent** → web_search (OpenAI native). Hardcoded override in `_request_agent` swaps its tool list for `[{"type": "web_search"}]` regardless of config.
- **DeviceManagerAgent** → file ops, run_python, search_files, run_command, WebSearchAgent (recursive), memory_query, browsing tools, interpret_image (vision).
- **CronosAgent** → memory_query only (prompt/tools intentionally minimal).
- Non-`WebSearchAgent` agents use `parallel_tool_calls=False` by default, except when `json_response=true` (where parallel calls are re-enabled and `text.format = json_object` is requested).

### Tool registration

Adding a tool requires **two** places:
1. Schema entry in [tools/local_tools.py](tools/local_tools.py) `total_tools` list (the `dict_total_tools` dict is the lookup used by `AgentBuilder.get_tools_for_agent`).
2. Implementation callable in [tools/ticket_dispatcher.py](tools/ticket_dispatcher.py) `ticket_dispatcher` dict. Functions may be sync or `async`; the runner picks the right call path with `inspect.iscoroutinefunction`.

Tools are forwarded their args as a single `body: dict`. Agents that appear in `_AGENTS` get a `_passthrough_agent` dispatcher — their "tool call" is really just JSON relayed as the sub-agent's first user message.

### Security boundaries in tools

- `action_write_file` / `action_edit_file` restrict paths to `ALLOWED_WRITE_ROOTS` (`~/Downloads`, `~/Documents`, `~/Desktop`, `~`, `/mnt/d`, `/mnt/c`, `/tmp`) — check this before adding paths.
- `action_run_command` blocks `sudo`/`su`/`doas` and a pattern list (`BLOCKED_COMMANDS`), caps timeout to 30s.
- `action_run_python` runs in a forked process with `resource.setrlimit` (RLIMIT_AS/CPU/NOFILE/CORE) and a whitelisted `SAFE_BUILTINS` — **no `import`, no `open`**. For filesystem writes, agents are expected to call `write_file` instead.
- `action_memory_query` routes through `MemoryRag.execute_safe_query`, which enforces SELECT/WITH/EXPLAIN only and strips DDL/DML keywords. It also supports a `$EMBEDDING$` placeholder that is substituted with the vector literal of `embed_text` before execution.
- `action_read_file` treats unknown extensions as binary and returns metadata only (`preview_omitted: true`) — no base64 preview is ever returned inline. Text extensions recognized: `.txt .md .py .json .csv .log .yaml .yml .sql`.
- `action_interpret_image` accepts a local path, http(s) URL, data URI, or raw base64 string; caps at 20 MB; proxies the image to the Responses API as `input_image` for vision analysis. Local paths go through `_resolve_path` — they are not restricted to `ALLOWED_WRITE_ROOTS` (read-only).
- `ChatRequest.images` can pass images directly to the main Responses call as `input_image` parts (`url`, `path`, `data_url`, `file_id`, or base64). Local paths are resolved into data URLs only when preparing the API request.
- `playwright_navigate` returns compact page snapshots by default. Screenshots are saved as PNG files and return file metadata/path unless `screenshot_mode` explicitly requests base64.

### Context shape & normalization

The Responses-API context is a `dict[agent_name, list[item]]` where each item is one of `{type: "message", role, content: [...]}`, `{type: "function_call", call_id, name, arguments}`, or `{type: "function_call_output", call_id, output}`. Role→type mapping: `user` messages get `input_text` and optional `input_image` parts, assistant messages get `output_text`. `_normalize_context_item` drops anything that doesn't fit this schema — if you add new item types (reasoning blocks, computer calls, etc.), extend that method or they will be silently filtered on load/save.

`_truncate_context_if_needed` resets an agent's context when user-message count exceeds `max_messages=120`, keeping the last `keep_after_reset=10` user messages and everything after them. (Sub-agents still cap at `max_iterations=10` per invocation.)

### Memory / RAG

[tools/memoryTools/RAG_memory.py](tools/memoryTools/RAG_memory.py) wraps [tools/memoryTools/semantic_splitter.py](tools/memoryTools/semantic_splitter.py) (tiktoken `cl100k_base`, threshold-percentile splitting, `text-embedding-3-large`/3072-dim). Chunks are stored in `multiagente.conversation_chunks` with the schema documented inside the `memory_query` tool description — keep that description in sync if you change the table. `store_text_embeddings(replace=True)` wipes all chunks for a session before writing, so background sync failures leave the prior version intact only until the next successful run.

### Persistence model

- `multiagente.conversations` holds one row per session with the **entire** multi-agent context in `context_jsonb`. There is no per-message table — the Postgres view is a snapshot.
- `_build_snapshot_view` picks the agent whose context has the most user/assistant messages as the "primary" one and derives `title`/`preview`/`messages` from it; non-message items are dropped from the view but remain in `context_jsonb`.
- `init_schema` is idempotent and runs `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for every column, so schema changes should be additive.

### Conversation types

`conversation_type` flows from the `/chat` request into both persistence and prompt building. Known values:
- `None` (normal) → working dir `{WORKING_PATH}/sessions/{session_id}`.
- `"cron"` → working dir `{WORKING_PATH}/crons/{session_id}`.
- `"temporal"` → no working dir, prompt tells the agent nothing will be stored (but context is still persisted today — honor the contract if you touch this path).

The ExecutorAgent prompt instructs agents to self-invoke via HTTP `POST http://127.0.0.1:8000/chat` with `conversation_type: "cron" | "workflow"` for scheduled/autonomous tasks — cron directories and workflow directories are listed back into every prompt via `_crons_section` / `_worflows_section`.
