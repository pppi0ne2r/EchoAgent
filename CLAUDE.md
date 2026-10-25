# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What EchoAgent is

A CTF / penetration-testing agent orchestrator. A task is modeled as a graph: **facts** are nodes, **intents** are edges. A FastAPI server owns the graph in SQLite; a dispatcher drives Codex CLI workers that run inside **one Kali Docker container per project**. Docs: `README.md` (usage), `docs/design.md` (original design note, Chinese), `docs/project-structure.md` (layout).

`container/AGENTS.md` and `container/.agents/skills/` are **not** repo guidance for you — they are copied into the worker image (`/home/kali/workspace/`) and read by the Codex agent inside the container.

## Commands

```bash
python -m pip install -r requirements.txt     # or: python -m pip install -e .
cp echo_agent.toml.example echo_agent.toml    # local config (git-ignored, holds the proxy API key)

# Server (web UI at http://127.0.0.1:8000/)
python -m echo_agent server --host 127.0.0.1 --port 8000 --db .echo_agent/echo_agent.db
uvicorn echo_agent.server.main:app --host 127.0.0.1 --port 8000   # alternative

# Dispatcher CLI (talks HTTP to a running server)
python -m echo_agent dispatcher create-project --target-url https://target.example --objective "get flag" --activate
python -m echo_agent dispatcher run-once 1 --max-workers 4
python -m echo_agent dispatcher run-loop 1 --interval 3 --max-steps 10 --max-workers 4

# Tests (from the repo root; no Docker or network needed)
python -m unittest
python -m unittest tests.test_dispatcher.DispatcherFactInsertionTests.test_first_run_fact_completes_initial_intent_without_auto_bridge
node --check echo_agent/server/static/app.js      # syntax-check the web UI
```

Two tests fail on Windows for platform reasons only: `test_nonzero_reason_process_salvages_json_from_stderr` (subprocess `text=True` decodes stderr with the GBK locale → UnicodeDecodeError) and `test_prepare_shared_resources_extracts_single_executable_file` (chmod `+x` is a no-op on NTFS). Both pass on Linux.

## Architecture

### Two processes, one source of truth

The server owns all state; the dispatcher only mutates projects through a client. `ProjectClient` (`dispatcher/client.py`) is the HTTP path — `Dispatcher` never touches SQLite directly. When a run is started from the web UI, `server/dispatcher_runner.py` runs the same `Dispatcher` in a background thread using `StoreDispatcherClient`, which writes to the store in-process but still mirrors the HTTP semantics (status-transition logging + Docker lifecycle on every `save_project`). If you change one client, change the other.

Persistence is `SQLiteProjectStore` (`storage.py`). Every `save_project` deletes and re-inserts **all** facts and intents for the project, so partial updates are not possible; intents carry foreign keys to facts, so a fact cannot be dropped while an intent still references it.

### Graph model (`models.py`)

- Fact `0` = `start`, fact `1` = `goal`; both are auto-created on `create_project` and hold `target_url` / `hint` / `objective`. On completion, fact 1 is overwritten with `result\nanswer: ...` and an edge from the source fact to fact 1 is added.
- Fact/intent ids are `max(existing) + 1`, or caller-supplied.
- Status machines are enforced: `ProjectStatus` `inactive → active → running → stoped → running → completed`, `IntentStatus` `active → running → completed`. Invalid transitions raise `StateTransitionError`. Note the deliberate misspelling `stoped` (`coerce()` accepts `"stopped"` and normalizes it) — keep it when writing status literals or REST payloads.
- `Project.is_first_run` is simply "has no intents".

### Dispatch loop (`dispatcher/core.py`)

`_run_project_step` picks exactly one mode per step:

1. **first_run** — only when the project has no intents. Creates the initial intent, tries to solve directly, and stores one result fact (or a failure fact, so reason can re-plan).
2. **explore** — when active intents exist. All of them run in a `ThreadPoolExecutor` (bounded by `explore_max_workers`), each marking its intent `running` up front. Each finished intent is saved and logged immediately, not at the end of the batch.
3. **reason** — when no active intent remains. Asks the model to judge completion and plan new intents; a non-empty plan is required each round, otherwise a fallback intent is synthesized from the newest leaf fact. `completed=true` sets fact 1's result and marks the project `completed`.

Status is the mutex: `_enter_running` transitions active/stoped → running at step start, `_release_project` returns running → stoped at step end. Concurrent steps on the same project are prevented by the server-side runner, not by the graph.

The worker/reasoner adapters are probed with `callable_accepts_mode` (`dispatcher/utils.py`) so tests can inject fakes with narrower signatures — keep that duck-typing when adding adapter methods.

### Codex execution (`dispatcher/agents.py`)

`CodexDockerAgent.run()` shells out to `docker exec -i -w /home/kali/workspace <container> codex exec … -` with the prompt written to stdin; `OPENAI_API_KEY` / `OPENAI_BASE_URL` are injected via `docker exec -e` and the process env. A preflight `docker exec` checks the workspace, `codex --version`, and that the key/URL arrived.

Because Codex CLI does not reliably honor `OPENAI_BASE_URL`, a non-empty `base_url` also emits `-c model_provider=…` / `model_providers.<name>.*` overrides (skipped if the configured command already sets a provider or `--model`). Each mode can override the model and the no-output timeout.

Watchdog: a wall-clock timeout plus a per-mode *no-output* timeout kill the whole process group (`start_new_session` + `os.killpg`), and every line of stdout/stderr is mirrored into project logs in batches. On non-zero exit, a JSON object matching the mode's expected shape is salvaged from stdout/stderr before failing.

Prompts (`dispatcher/prompts.py`, Chinese) demand a single JSON object and no markdown. `parsing.py` tolerates extra output by taking the *last* JSON object, and drops placeholder answers/facts (literally the example strings from the prompt) so echoed templates never become graph nodes.

### Docker lifecycle (`server/docker_runtime.py`)

Container state is a pure function of the project status transition, applied on every save: `active` → create/start with mounts, `running` → start if needed (creating it first if missing), `stoped` → stop, `completed` → remove. The container name is `project.docker` (default `echo-agent-task-<id>`) and it just runs `sleep infinity`; work happens through `docker exec`. "Stop" in the UI is a force stop (`docker stop -t 0`) plus a running → stoped transition.

Shared resources come from `[resources]`: raised once at server startup when `root` has no `{project_id}`/`{project_name}` placeholder, otherwise per project at container creation. Downloads are skipped when the target already exists (non-empty dir / any file), `archive` extracts into the resource path (single root dir stripped), `archive_single_file` extracts the first file to exactly `path`, `executable` chmods `+x`, and archive paths are checked against traversal.

### Configuration (`config.py`)

Precedence is **environment > `echo_agent.toml` (or `$ECHO_AGENT_CONFIG`) > `echo_agent.toml.example`** — the example file is always loaded first as the defaults layer, so a required key may live only there. Relative paths in config resolve against the **config file's directory**, not the cwd. `ECHO_AGENT_DATA_DIR` relocates both the DB and the resource root. A handful of keys (`codex.provider_name`, `supports_websockets`, `*_max_retries`, `codex.stream_idle_timeout_ms`, the `[docker]` and `[resources]` essentials) are required and raise if absent from every layer.

`echo_agent.toml` is git-ignored and contains a live proxy API key — never print it, commit it, or copy it into docs, logs, or tests.

## Gotchas seen in this codebase

- `docker_runtime._container_state` / `_image_exists` match `"No such object"` / `"No such image"` **case-sensitively**; Docker 29.x prints lowercase, which turns a missing container/image into a hard `DockerRuntimeError` instead of "create it".
- `core._run_project_step` logs `"project entered {mode} mode"` using the mode from `_enter_running`, which is only ever `first_run`/`explore` — a step that actually runs reason still logs `entered explore mode`. Trust `DispatchResult.mode` and the per-step logs, not that line.
- Reason output is model self-report: `completed=true` is accepted without independent verification. Check the real `docker exec` output in `project_logs` before treating a project as solved.
