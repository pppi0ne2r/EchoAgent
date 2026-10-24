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
- `[resources]`: shared resources prepared on startup and mounted into containers.

Use `echo_agent.toml` for local configuration and keep real secrets out of commits. `echo_agent.toml` is ignored by git; commit `echo_agent.toml.example` as the shared template.

## Shared Resources

Resources are configured in `echo_agent.toml`:

```toml
[resources]
root = ".echo_agent/resources"
items = [
  { name = "nuclei-templates", url = "https://github.com/projectdiscovery/nuclei-templates/archive/refs/heads/main.zip", archive = true, path = "nuclei-templates", container_path = "/home/kali/.local/nuclei-templates", read_only = false },
  { name = "katana", url = "https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_amd64.zip", archive = true, archive_single_file = true, executable = true, path = "local-bin/katana", container_path = "/home/kali/.local/bin/katana", read_only = true },
  { name = "nuclei", url = "https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip", archive = true, archive_single_file = true, executable = true, path = "local-bin/nuclei", container_path = "/home/kali/.local/bin/nuclei", read_only = true },
  { name = "dalfox", url = "https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-amd64.tar.gz", archive = true, archive_single_file = true, executable = true, path = "local-bin/dalfox", container_path = "/home/kali/.local/bin/dalfox", read_only = true },
  { name = "cloudfox", url = "https://github.com/BishopFox/cloudfox/releases/download/v2.0.1/cloudfox-linux-amd64.zip", archive = true, archive_single_file = true, executable = true, path = "local-bin/cloudfox", container_path = "/home/kali/.local/bin/cloudfox", read_only = true },
  { name = "kerbrute", url = "https://github.com/ropnop/kerbrute/releases/download/v1.0.3/kerbrute_linux_amd64", executable = true, path = "local-bin/kerbrute", container_path = "/home/kali/.local/bin/kerbrute", read_only = true },
  { name = "pocs", path = "pocs", container_path = "/home/kali/pocs", read_only = false },
  { name = "tools", path = "tools", container_path = "/home/kali/tools", read_only = false },
  { name = "knowledges", path = "knowledges", container_path = "/home/kali/knowledges", read_only = false },
]
```

When the server starts, EchoAgent prepares shared resources:

- If `url` is absent, it creates the directory.
- If `url` is present, it downloads the file once.
- If `archive = true`, it extracts zip/tar archives into the resource path.
- If `archive_single_file = true`, it extracts the first file in the archive to exactly `path`.
- If `executable = true`, it applies executable permissions to the downloaded/extracted file(s).
- Existing non-empty directories/files are reused.

When a project container is created, these resources are mounted at their configured `container_path`.

Inside the worker container, the default paths are:

```text
/home/kali/.local/nuclei-templates
/home/kali/.local/bin/katana
/home/kali/.local/bin/nuclei
/home/kali/.local/bin/dalfox
/home/kali/.local/bin/cloudfox
/home/kali/.local/bin/kerbrute
/home/kali/pocs
/home/kali/tools
/home/kali/knowledges
```

`/home/kali/.local/bin` is already in the worker container PATH, so these mounted tools can be run directly.

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
