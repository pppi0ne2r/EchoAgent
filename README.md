# EchoAgent

EchoAgent is an experimental CTF / penetration-testing agent orchestrator. It keeps a task as a graph of facts and intents, uses Codex CLI workers inside per-project Docker containers, and provides a FastAPI web UI for observing the reasoning/exploration process.

## Acknowledgement

This project is inspired by and borrows ideas from [oritera/Cairn](https://github.com/oritera/Cairn), especially the idea of representing task progress as a graph-like reasoning process. EchoAgent is an independent implementation focused on Docker-isolated Codex workers, a local web UI, and configurable shared security resources.

## Features

- Fact / intent graph for task state.
- First-run, explore, and reason phases.
- Parallel explore execution for multiple active intents.
- Per-project Docker container isolation.
- Shared resource mounting for nuclei templates, PoCs, tools, and knowledge bases.
- Configurable Codex command, model, provider, retries, and timeout settings.
- Web UI for project status, graph inspection, and runtime logs.

## Requirements

- Python 3.11+
- Docker
- Codex CLI available in the worker image
- Python dependencies from `requirements.txt`

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Or install the package in editable mode:

```bash
python -m pip install -e .
```

## Configuration

Copy the example config:

```bash
cp echo_agent.toml.example echo_agent.toml
```

EchoAgent reads `echo_agent.toml` from the current working directory. You can use another file with:

```bash
export ECHO_AGENT_CONFIG=/path/to/echo_agent.toml
```

Configuration priority is:

```text
environment variables > .echo_agent/console_settings.toml > echo_agent.toml > echo_agent.toml.example
```

Important sections:

- `[server]`: web UI host, port, database path.
- `[dispatcher]`: loop interval, per-project Codex slots, and how many projects may run together.
- `[codex]`: Codex CLI command, models, provider, retries, timeouts.
- `[docker]`: worker image build/run settings.
- `[resources]`: host directory for the extension catalog. The tool list itself lives in the Extensions page.

Use `echo_agent.toml` for local configuration and keep real secrets out of commits. `echo_agent.toml` is ignored by git; commit `echo_agent.toml.example` as the shared template.

## Shared Resources

Tools, knowledge bases, and skills are configured on the web UI's Extensions page. EchoAgent stores that catalog in `extensions.json` and downloads the files into `[resources].root`.

Built-in entries are seeded on first start:

- Tools on `PATH`: katana, nuclei, dalfox, cloudfox, mounted at `/home/kali/.local/bin`.
- `ysoserial` at `/home/kali/tools/ysoserial.jar`.
- `nuclei-templates` at `/home/kali/.local/nuclei-templates`.
- Shared directories: `/home/kali/tools`, `/home/kali/knowledges`, `/home/kali/pocs`, and skills at `/home/kali/.agents/skills`.

The server downloads missing files when it starts. A project run checks them again and stops if a download fails. Use Update on the Extensions page to download the current URL again and replace an existing file. Add or remove entries from the Extensions page; do not keep a second list in `echo_agent.toml`.

`[resources].root` is only the host directory for those files. `items` stays empty.

## Start the Web Server

```bash
python -m echo_agent server --host 127.0.0.1 --port 8000 --db .echo_agent/echo_agent.db
```

Open:

```text
http://127.0.0.1:8000/
```

You can also run FastAPI directly:

```bash
uvicorn echo_agent.server.main:app --host 127.0.0.1 --port 8000
```

## Dispatcher CLI

Create and activate a project:

```bash
python -m echo_agent dispatcher create-project \
  --target-url https://target.example \
  --hint "optional hint" \
  --objective "get flag" \
  --activate
```

Run one step:

```bash
python -m echo_agent dispatcher run-once 1 --max-workers 4
```

Run a loop:

```bash
python -m echo_agent dispatcher run-loop 1 --interval 3 --max-steps 10 --max-workers 4
```

## Docker Lifecycle

Each project uses one Docker container, usually named:

```text
echo-agent-task-<project_id>
```

Status behavior:

- `active`: create/start the project container and prepare mounts.
- `running`: ensure the project container is running before Codex executes.
- `stoped`: stop the project container.
- `completed`: remove the project container.

Clicking Stop in the web UI force-stops the project container with:

```bash
docker stop -t 0 <container>
```

## Tests

```bash
python -m unittest
node --check echo_agent/server/static/app.js
```
