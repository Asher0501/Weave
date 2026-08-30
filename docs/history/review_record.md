# Weave Code Review Record

**Reviewed:** `C:/Users/Asher/WorkSpace/05_Projects/13_weave/weave/` (13 subpackages, ~46 Python files)
**Review focus:** code quality, async patterns, error handling, test coverage
**Test run:** `pytest tests/test_weave.py` → **203 passed, 1 failed, 2 RuntimeWarnings** (101.7s)

---

## 0. Verdict

**needs_fix.** The architecture is clean and well-documented, but there are several critical
correctness bugs in core paths (sync `run()` in an async context, stuck `_is_running` flag on
error, non-functional streaming events, ephemeral memory namespaces), a 1-failing-test suite,
and significant unfinished code (ScheduledLoop, memory hooks, Chroma backend).

---

## 1. Test suite results

| Item | Result |
|---|---|
| Total tests (`tests/test_weave.py`) | 182 test methods |
| Passing | 203 (incl. parametrized) |
| Failing | 1 — `TestBakFileCleanup::test_no_bak_files_anywhere_in_project` |
| Warnings | 2 × `RuntimeWarning: coroutine 'to_thread' was never awaited` (iterative.py:183) |
| Duration | ~101s |

### 1.1 Failing test
`test_no_bak_files_anywhere_in_project` does `project_root.rglob("*.bak")` and asserts zero.
The repo contains `nexus/report.md.bak` and `nexus/review_record.md.bak`, so it fails. The test
scans the *entire* project tree (including `nexus/`, `.claude/`, `.obsidian/`) rather than just
source, making it brittle. This is an environment/artifact leak, but the test as written will
fail on any machine that has ever had a `.bak` written under the tree.

### 1.2 RuntimeWarnings (test-mock leak revealing a robustness gap)
`test_memory_updated_includes_tool_writes_when_present` builds `agent._config` as a `MagicMock`
without setting `loop.tool_timeout`. In `_execute_tool`:

```python
timeout = getattr(agent._config.loop, "tool_timeout", 30.0)
```

On a `MagicMock`, `getattr(..., default)` returns a `MagicMock` (not `30.0`), which is passed to
`asyncio.wait_for(coro, timeout=<MagicMock>)`. The coroutine `asyncio.to_thread(...)` is never
awaited → warning. Production impact: if a user configures a non-numeric `tool_timeout`, the same
un-awaited-coroutine leak occurs. `_execute_tool` should validate the timeout is a real number.

---

## 2. Critical bugs

### 2.1 `Weave.run()` is broken inside a running event loop (agent.py:75-88)
```python
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    return asyncio.run(self._run_impl(...))
else:
    return loop.run_until_complete(self._run_impl(...))
```
Verified with Python 3.13: calling `loop.run_until_complete()` on a loop that is already running
raises `RuntimeError: This event loop is already running`. The docstring explicitly claims support
for "Jupyter, FastAPI, 异步测试等" — precisely the contexts where a loop *is* already running.
There is no correct way to synchronously block on the running loop from the same thread; the
implementation should raise a clear error directing the caller to `arun()`, or run the coroutine
on a separate thread/loop. **This is a guaranteed crash for the documented use cases.**

### 2.2 `_is_running` sticks to `True` after any error (agent.py:170-196)
```python
self._is_running = True
self._memory.activate_scopes(scope_hints)
self._system_prompt = self._load_system_prompt(context)   # may raise
...
try:
    result = await self._loop.run(self, input)
finally:
    if tool_filter: ... restore ...
self._is_running = False          # skipped on exception
self._last_run = time.time()      # skipped on exception
```
If `_load_system_prompt` raises (no `prompts.system` configured) or `self._loop.run` raises
(LLM error, network error), `_is_running` is never reset and `_last_run` is never updated.
`status()` will permanently report `is_running: true`. The flag reset must be in a `finally`.

### 2.3 Streaming feature is non-functional end-to-end
`Weave.stream()` subscribes to `llm_token`, `tool_call`, `tool_result`, `run_complete`, `error`
(agent.py:125). But **no loop or LLM adapter ever emits these events** — grep confirms the only
`emit()` calls are `run_complete`/`error` inside `agent.stream()`'s `_run_and_emit`. The loops call
non-streaming `llm.chat()`, and the `chat_stream()` adapter methods are never invoked anywhere.
Consequences:
- `stream()` silently yields only `run_complete`/`error`, i.e., it is a wrapper over a
  non-streaming call. The "逐 token 产出" promise in the docstring is unmet.
- The WebSocket endpoint (`server/ws.py`) has the same limitation.
- If the consumer cancels early, the background `_run_task` is **never cancelled** — it keeps
  running (only a `wait_for` timeout, which also does not cancel).

### 2.4 Memory is ephemeral across runs by default (memory/manager.py:63-65)
```python
if not scopes:
    self._active_scopes = [("default", 0, scope_hints.get("default_id", str(uuid.uuid4())))]
```
Verified: two consecutive `activate_scopes(None)` calls produce different namespaces
(`default:<uuid1>:stream` vs `default:<uuid2>:stream`). Since `Weave.run()`/`arun()` call
`activate_scopes(scope_hints)` with no hints unless the caller supplies them, **every run writes
to a brand-new namespace and can never read prior runs' data**. Out of the box, the memory
feature is effectively non-persistent. Either the default should be a stable namespace (e.g.,
`default`), or this behavior must be clearly documented with a recommended pattern for passing
stable `scope_hints`.

### 2.5 Memory is never loaded by the default loops
`before_think()`/`after_think()` in `BaseLoop` are no-ops returning `{}`. `SimpleLoop`,
`IterativeLoop`, and `ScheduledLoop` call them but none override them. Therefore:
- `format_memory_context` and the memory-injection code are dead unless the user subclasses a loop.
- `SimpleLoop` fabricates `memory_updated={"stream": {ns: 0 ...}}` — a zero-count claim implying
  writes that never happened.
- `IterativeLoop` keeps `stream_writes`/`state_writes` as permanent empty dicts (with a `TODO`),
  so the `LoopResult.memory_updated` contract is unfulfilled by the shipped loops.

### 2.6 No concurrency safety on a shared `Weave` instance
`_system_prompt`, `_is_running`, `_tools`/`_tool_map` filtering, and memory scope activation are
all instance-level mutable state mutated in `_run_impl`. Two concurrent `arun()`/`stream()` calls
(such as concurrent requests against the FastAPI server) will clobber each other's system prompt,
tools, running flag, and memory scopes. There is no lock or per-run context isolation.

---

## 3. Major issues

### 3.1 `DELETE /agents/{name}/memory` does not clear memory (server/routes.py:47-51)
`weave._memory.close()` closes SQLite connections and clears the in-memory backend cache — it does
**not delete any data**. Backends are re-created lazily on next access and will re-read the same
DB files. The endpoint returns `{"status": "cleared"}` while data remains intact. Misleading API.

### 3.2 ScheduledLoop is unfinished (loop/scheduled.py)
- `self._current_task` is never assigned anywhere; `handle_shutdown()` cancels a task that is
  always `None` → dead code.
- `self._shutting_down` is set but never read.
- No cron scheduling loop exists; `run()` just delegates to `_execute_once`. The `schedule`
  config value (`cron` or `@on_data_change`) is entirely unused.
- The docstring ("持续运行直到 shutdown") does not match the implementation (one-shot call).

### 3.3 Blocking DB calls inside async methods stall the event loop
`SQLiteStreamMemory.append`, `SQLiteStateMemory.set`, `SQLiteKnowledgeMemory.search`, etc. are
`async def` but execute synchronous sqlite3 calls directly on the event-loop thread. Fine for a
single-user CLI; problematic for the FastAPI/WebSocket server under concurrency. These should be
plain `def` (called via `to_thread`) or run in an executor.

### 3.4 `stop_conditions` are effectively ignored by IterativeLoop
The loop unconditionally breaks on `not response.tool_calls` (the `no_tool_calls` behavior),
regardless of the configured `stop_conditions`. The `finish`-tool and `text_pattern` conditions
only apply if explicitly configured in `weave.yaml`; the default config ships
`stop_conditions: [{type: no_tool_calls}]`, which is redundant with hardcoded behavior.

### 3.5 Repository hygiene
- `tests/test_weave_backup.py` (111 KB, 149 tests) is a stale duplicate of the live test file.
- `_fix_test.py` is a script that patches `tests/test_weave.py` text — a sign of a fragile
  test-maintenance workflow.
- No `.gitignore`; `__pycache__/` and `.pytest_cache/` are present in the tree.
- `nexus/*.bak` files exist and trip the `.bak`-scanning test.
- `data/` is empty; `docs/issues/` exists but wasn't reviewed here.

---

## 4. Async pattern findings

| Area | Assessment |
|---|---|
| `EventBus.emit` backpressure | Good — `await q.put()` with unbounded queues; records per-subscriber failures via `gather(return_exceptions=True)` |
| `EventBus.subscribe` cleanup | Good — unsubscribe in `finally`, removes empty keys |
| `asyncio.wait_for` for tool timeout | Good idea; but timeout value is not type-validated (see 1.2) |
| `asyncio.to_thread` for sync tools | Correct approach; thread can't be cancelled on timeout (inherent) |
| `retry()` + typed errors | `retry` retries *all* `Exception` subclasses by default — including non-retryable `AuthError`/`BadRequestError`. Not integrated with the `RateLimitError`/`ServerError`/`NetworkError` retry taxonomy the module defines |
| `timeout()` util | Clean wrapper over `asyncio.timeout` (3.11+), typed `WeaveTimeoutError` |
| `fallback()` util | Correctly re-raises `CancelledError` — good |
| Client lifecycle | `AsyncOpenAI`/`AsyncAnthropic` client re-created on every `chat()`/`chat_stream()` call — no reuse, no `aclose()` |
| Cancellation | `Weave.stream()` leaks the background task on early consumer exit / timeout (no `task.cancel()`) |

---

## 5. Error handling findings

- **Good:** typed LLM error hierarchy (`RateLimitError`, `AuthError`, `ContextLengthError`,
  `ServerError`, `NetworkError`, `BadRequestError`) with `classify_http_error`; adapters map
  `APIStatusError`/`APIConnectionError` properly and wrap unknown exceptions in `WeaveLLMError`.
- **Good:** `load_claude_env` distinguishes `FileNotFoundError` (silent) from JSON/permission/
  other errors (logged) — no silent swallowing.
- **Good:** `_execute_tool` normalizes unknown-tool and tool exceptions into `ToolResult.error`
  instead of crashing the loop.
- **Bad:** `_run_impl` leaves the agent in a stuck state on error (2.2).
- **Bad:** `ChromaBackend.knowledge_search` has a bare `except Exception: pass` that silently
  swallows all query errors; `_get_collection` also swallows. Chroma is also **not wired into
  `MemoryManager`** at all — it is dead code.
- **Bad:** `FileBackend` uses `__import__("time")` inline and has no concurrency protection
  (acknowledged in docstring). It is also not wired into `MemoryManager`.
- **Minor:** `prompt_defense.sanitize` docstring documents `**llm_kwargs` that the signature
  doesn't have.
- **Minor:** `structured_call` retries on every failure type including non-retryable
  `BadRequestError`, feeding the same bad JSON back up to `max_retries` times.
- **Minor:** `routes.py agent_run` takes `request: dict` — a non-dict JSON body yields
  `AttributeError` → 500 instead of 422.

---

## 6. Code quality observations

### Strengths
- Clear package separation (llm / loop / memory / prompts / features / server / utils).
- Dataclasses with `slots=True`; `ABC` interfaces for LLM/loop/memory; consistent naming.
- Config is environment-driven with `${VAR:-default}` interpolation and no hardcoded model names
  (R3 satisfied). `_create_loop` raises `ValueError` on unknown loop type instead of silently
  falling back.
- `EventBus` design (blocking backpressure, generator-based subscribe, unsubscribe in `finally`)
  is solid.
- `_merge_consecutive_tool_messages` / `_unpack_tool_results` correctly encapsulate the
  Anthropic multi-tool-result protocol quirk.
- `_build_tool_schemas` derives JSON schema from `inspect.signature` incl. required params.
- `extract_json` has a sensible extraction cascade (fence → boundaries → fix trailing commas)
  and a `JsonExtractError.to_feedback()` for LLM self-correction loops.

### Weaknesses / style
- `agent.run()`/`arun()`/`stream()` duplicate the same 4-arg signature three times; the sync
  wrapper is broken (2.1).
- `LoopResult` is a `namedtuple` while everything else is a dataclass — inconsistent; its
  `memory_updated` docstring type (`access_type → namespace → count`) doesn't match the actual
  `tools: {tool_name: count}` key emitted by `IterativeLoop`.
- `Weave` has no `close()`/`aclose()`/context-manager protocol; `MemoryManager.close()` is never
  invoked by the agent lifecycle → DB connections leak across repeatedly-created agents.
- `SQLiteStreamMemory.last` merges only backends already instantiated this session; a DB file
  from a prior process/session is invisible until written to (compounds 2.4).
- `config.py` applies `_resolve_env` to the whole YAML text *and then* `_resolve_dict` to `llm`
  again — redundant double resolution; env values containing YAML-significant chars (e.g. `:`,
  newline) could corrupt parsing.
- `LazyRegistry.get` invokes the factory while holding the lock — a slow factory (e.g., network
  init) blocks all other lookups.
- Comments are mostly in Chinese while identifiers/errors are English — acceptable for a
  single-maintainer project, but inconsistent for an "SDK".

---

## 7. Test coverage assessment

**Coverage is broad but shallow and brittle.**

- 182 test methods in one 148 KB file. Many are **source-text assertions** (82 hits for
  `inspect.getsource` / reading `.py` files), e.g. `test_openai_py_no_dynamic_import_json`,
  `test_agent_code_uses_config_timeout`, `test_scheduled_loop_no_iterative_loop_import`.
  These verify *how code is written* rather than *what it does*, and break on any refactor.
- Notable behavioral gaps:
  - No test exercising `Weave.run()` inside a running event loop (the 2.1 bug is untested).
  - No test asserting `_is_running` resets after an LLM exception (2.2).
  - No test asserting `stream()` actually yields `llm_token` events (2.3).
  - No test verifying memory persistence across two `run()` calls (2.4).
  - No tests for `ScheduledLoop.handle_shutdown` or cron behavior (unfinished code).
  - No tests for `ChromaBackend`, `FileBackend`, `features/*` (prompt_defense,
    schema_validation, structured_call, two_stage) — the entire features package is untested.
  - No tests for `server/routes.py` or `server/ws.py`.
- The one failing test is an environment-artifact check (`*.bak` scan), not a product test —
  symptomatic of tests being written to lock in specific file-system states.
- `tests/test_weave_backup.py` (stale 149-test duplicate) is not run but inflates confusion.

---

## 8. Priority fix list

1. **P0** Rewrite `Weave.run()` to fail with a clear `RuntimeError` directing to `arun()` when a
   loop is running (or run the impl on a fresh thread-loop). Do not call
   `run_until_complete` on a running loop.
2. **P0** Wrap `_run_impl` body in `try/finally` to always reset `_is_running`/`_last_run` and
   deactivate scopes.
3. **P0** Wire real event emission (`llm_token`, `tool_call`, `tool_result`) into the loops (use
   `chat_stream` + emit), and cancel the background task in `stream()`/`ws.py` on early exit.
4. **P0** Fix default namespace stability so memory persists across runs (stable default id or
   require/derive stable scope ids).
5. **P1** Add real `before_think`/`after_think` memory read/write in the default loops (or remove
   the misleading `memory_updated` zeros).
6. **P1** Fix `DELETE /memory` to actually delete data; add agent `close()`/`aclose()` and call it.
7. **P1** Complete or explicitly mark ScheduledLoop as WIP; remove dead `_current_task`/`_shutting_down`.
8. **P1** Validate `tool_timeout` is a number in `_execute_tool`; add concurrency guard for
   shared-agent runs.
9. **P2** Remove stale `test_weave_backup.py` and `_fix_test.py`; add `.gitignore`; move/delete
   `nexus/*.bak`; relax the `.bak`-scan test to source dirs only.
10. **P2** Add behavioral tests for the P0/P1 fixes and for the `features/` package; reduce
    source-text assertions.

---

*Environment: Python 3.13.9 (Anaconda), Windows; pytest run captured 203 passed / 1 failed / 2 warnings.*
