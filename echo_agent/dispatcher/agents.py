from __future__ import annotations

import inspect
import json
import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Mapping, Sequence

from echo_agent.dispatcher.parsing import (
    ReasonFormatError,
    extract_mode_json_output,
    parse_reason_result,
    parse_worker_result,
    repair_reason_output,
    strip_result_json_text,
    substantive_worker_output,
)
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import (
    AgentAdapter,
    AgentLogCallback,
    AgentRunResult,
    DispatchMode,
    ReasonResult,
    WorkerResult,
)
from echo_agent.models import Intent, Project


TRANSIENT_UPSTREAM_MARKERS = (
    "high demand",
    "overloaded",
    "stream disconnected",
    "reconnecting",
    "rate limit",
    "too many requests",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)



PYGHIDRA_MCP_CONFIG = """[mcp_servers.pyghidra-mcp]
url = "http://127.0.0.1:8000/mcp"
startup_timeout_sec = 180
tool_timeout_sec = 300
"""


def _ensure_codex_mcp_config(codex_home: str) -> str:
    config_path = f"{codex_home}/config.toml"
    marker = "mcp_servers.pyghidra-mcp"
    write_config = (
        f"if [ ! -f {shlex.quote(config_path)} ] || ! grep -q {shlex.quote(marker)} {shlex.quote(config_path)}; then "
        f"printf %s {shlex.quote(PYGHIDRA_MCP_CONFIG)} >> {shlex.quote(config_path)}; "
        "fi"
    )
    return f"flock -w 10 {shlex.quote(f'{codex_home}/config.toml.lock')} sh -c {shlex.quote(write_config)};"


class CodexDockerAgent:
    def __init__(
        self,
        codex_command: Sequence[str] | None = None,
        timeout_seconds: int = 1800,
        workspace: str = "/home/kali/workspace",
        model: str | None = None,
        mode_models: Mapping[DispatchMode | str, str] | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        provider_name: str = "echo_agent_proxy",
        provider_supports_websockets: bool = False,
        request_max_retries: int = 2,
        stream_max_retries: int = 2,
        stream_idle_timeout_ms: int = 60_000,
        no_output_timeout_seconds: int = 900,
        mode_no_output_timeout_seconds: Mapping[DispatchMode | str, int] | None = None,
        mode_timeout_seconds: Mapping[DispatchMode | str, int] | None = None,
    ) -> None:
        self.codex_command = tuple(
            codex_command
            or (
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-",
            )
        )
        self.timeout_seconds = timeout_seconds
        self.workspace = workspace
        self.model = model
        self.mode_models = {self._mode_key(key): value for key, value in (mode_models or {}).items()}
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url
        self.provider_name = provider_name
        self.provider_supports_websockets = provider_supports_websockets
        self.request_max_retries = request_max_retries
        self.stream_max_retries = stream_max_retries
        self.stream_idle_timeout_ms = stream_idle_timeout_ms
        self.no_output_timeout_seconds = no_output_timeout_seconds
        self.mode_no_output_timeout_seconds = {
            self._mode_key(key): int(value)
            for key, value in (mode_no_output_timeout_seconds or {}).items()
        }
        self.mode_timeout_seconds = {
            self._mode_key(key): int(value)
            for key, value in (mode_timeout_seconds or {}).items()
        }
        self._preflight_lock = threading.Lock()
        self._preflight_ready: set[str] = set()
        self._process_lock = threading.Lock()
        self._processes: list[subprocess.Popen[str]] = []
        self._run_state = threading.local()

    def run(
        self,
        project: Project,
        prompt: str,
        mode: DispatchMode,
        log: AgentLogCallback | None = None,
        intent_id: int | None = None,
        runtime_key: str | None = None,
    ) -> AgentRunResult:
        if not project.docker:
            raise RuntimeError("project.docker is required to run codex in explore/reason mode")

        runtime_key = self._runtime_key(intent_id=intent_id, runtime_key=runtime_key)
        self._run_preflight(project, log=log)
        if runtime_key is not None:
            self._prepare_runtime(project, runtime_key, log=log)
        command = self._docker_command(project, mode, runtime_key=runtime_key)
        env = self._subprocess_env()
        workspace = self.workspace if runtime_key is None else self.runtime_paths(runtime_key)[1]
        self._log(
            log,
            f"codex agent process starting in {mode.value} mode",
            metadata={
                "docker": project.docker,
                "workspace": workspace,
                "intent_id": intent_id,
                "runtime_key": runtime_key,
                "command": self._safe_command(command),
                "model": self.mode_models.get(mode.value, self.model),
                "timeout_seconds": self._timeout_for_mode(mode),
                "no_output_timeout_seconds": self._no_output_timeout_for_mode(mode),
                "prompt_chars": len(prompt),
            },
        )
        output = self._run_process(command, prompt, mode, env=env, log=log)
        self._log(log, f"codex agent process finished in {mode.value} mode", metadata={"output_chars": len(output)})
        state = self._run_state
        return AgentRunResult(
            output=output,
            salvaged=bool(getattr(state, "salvaged", False)),
            timed_out=bool(getattr(state, "timed_out", False)),
            raw_output=str(getattr(state, "raw_output", "") or output),
        )

    def intent_runtime_paths(self, intent_id: int) -> tuple[str, str]:
        return self.runtime_paths(f"intents/{intent_id}")

    def runtime_paths(self, runtime_key: str) -> tuple[str, str]:
        root = self.workspace.rstrip("/") or "/home/kali/workspace"
        parent = root.rsplit("/", 1)[0] if "/" in root else "/home/kali"
        if runtime_key.startswith("intents/"):
            intent_id = runtime_key.split("/", 1)[1]
            return f"{parent}/.codex/intents/{intent_id}", f"{root}/intents/{intent_id}"
        safe_key = runtime_key.strip("/") or "session"
        return f"{parent}/.codex/{safe_key}", f"{root}/{safe_key}"

    @staticmethod
    def _runtime_key(intent_id: int | None, runtime_key: str | None) -> str | None:
        if intent_id is not None:
            return f"intents/{intent_id}"
        if runtime_key:
            return runtime_key.strip("/") or None
        return None

    def _docker_command(
        self,
        project: Project,
        mode: DispatchMode,
        runtime_key: str | None = None,
    ) -> list[str]:
        command = ["docker", "exec", "-i"]
        if self.openai_api_key:
            command.extend(["-e", f"OPENAI_API_KEY={self.openai_api_key}"])
        if self.openai_base_url:
            command.extend(["-e", f"OPENAI_BASE_URL={self.openai_base_url}"])
        workspace = self.workspace
        if runtime_key is not None:
            codex_home, workspace = self.runtime_paths(runtime_key)
            command.extend(
                [
                    "-e",
                    f"CODEX_HOME={codex_home}",
                    "-e",
                    f"TMUX_TMPDIR={codex_home}/tmux",
                ]
            )
        command.extend(["-w", workspace, project.docker, *self._codex_command_for_mode(mode)])
        return command

    def _prepare_runtime(
        self,
        project: Project,
        runtime_key: str,
        log: AgentLogCallback | None,
    ) -> None:
        codex_home, workspace = self.runtime_paths(runtime_key)
        shared = self.workspace.rstrip("/") or "/home/kali/workspace"
        script = " ".join(
            [
                "set -eu;",
                "mkdir -p",
                shlex.quote(codex_home),
                shlex.quote(f"{codex_home}/tmux"),
                shlex.quote(workspace),
                ";",
                "if [ -e",
                shlex.quote(f"{shared}/AGENTS.md"),
                "]; then ln -sfn",
                shlex.quote(f"{shared}/AGENTS.md"),
                shlex.quote(f"{workspace}/AGENTS.md"),
                "; fi;",
                "if [ -d",
                shlex.quote(f"{shared}/.agents"),
                "]; then ln -sfn",
                shlex.quote(f"{shared}/.agents"),
                shlex.quote(f"{workspace}/.agents"),
                "; fi;",
                _ensure_codex_mcp_config(codex_home),
            ]
        )
        command = ["docker", "exec", "-i", project.docker, "sh", "-lc", script]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=self._subprocess_env(),
        )
        if completed.returncode != 0:
            detail = "\n".join(
                line for line in [completed.stdout.strip(), completed.stderr.strip()] if line
            )
            raise RuntimeError(f"failed to prepare codex runtime: {detail}")
        self._log(log, "codex runtime prepared", metadata={"runtime_key": runtime_key, "workspace": workspace})

    def _preflight_command(self, project: Project) -> list[str]:
        command = ["docker", "exec", "-i"]
        if self.openai_api_key:
            command.extend(["-e", f"OPENAI_API_KEY={self.openai_api_key}"])
        if self.openai_base_url:
            command.extend(["-e", f"OPENAI_BASE_URL={self.openai_base_url}"])
        script = (
            'echo "workspace=$(pwd)"; '
            'echo "codex_path=$(command -v codex || true)"; '
            'codex --version; '
            'if [ -n "$OPENAI_API_KEY" ]; then echo "openai_api_key=present"; else echo "openai_api_key=missing"; fi; '
            'if [ -n "$OPENAI_BASE_URL" ]; then echo "openai_base_url=$OPENAI_BASE_URL"; else echo "openai_base_url=missing"; fi'
        )
        command.extend(["-w", self.workspace, project.docker, "sh", "-lc", script])
        return command

    def _run_preflight(self, project: Project, log: AgentLogCallback | None) -> None:
        container = project.docker or ""
        with self._preflight_lock:
            if container in self._preflight_ready:
                return
            self._run_preflight_once(project, log)
            self._preflight_ready.add(container)

    def _run_preflight_once(self, project: Project, log: AgentLogCallback | None) -> None:
        command = self._preflight_command(project)
        self._log(log, "codex preflight started", metadata={"command": self._safe_command(command)})
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=self._subprocess_env(),
        )
        output = "\n".join(
            line for line in [completed.stdout.strip(), completed.stderr.strip()] if line
        )
        if output:
            for line in output.splitlines():
                self._log(log, f"codex preflight: {self._truncate(line)}")
        if completed.returncode != 0:
            raise RuntimeError(f"codex preflight failed with exit code {completed.returncode}: {output}")
        self._log(log, "codex preflight finished")

    def _subprocess_env(self) -> dict[str, str] | None:
        if not self.openai_api_key and not self.openai_base_url:
            return None
        env = os.environ.copy()
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        if self.openai_base_url:
            env["OPENAI_BASE_URL"] = self.openai_base_url
        return env

    def _run_process(
        self,
        command: Sequence[str],
        prompt: str,
        mode: DispatchMode,
        env: dict[str, str] | None,
        log: AgentLogCallback | None,
    ) -> str:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self._register_process(process)
        try:
            return self._consume_process(process, prompt, mode, log)
        finally:
            self._unregister_process(process)

    def _consume_process(
        self,
        process: subprocess.Popen[str],
        prompt: str,
        mode: DispatchMode,
        log: AgentLogCallback | None,
    ) -> str:
        self._remember_run(salvaged=False, timed_out=False, raw_output="")
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(prompt)
        process.stdin.close()
        self._log(log, "codex prompt written to stdin", metadata={"prompt_chars": len(prompt)})

        output_lines: list[str] = []
        stderr_lines: list[str] = []
        recent_output: deque[str] = deque(maxlen=40)
        stdout_log_lines: list[str] = []
        stderr_log_lines: list[str] = []
        output_lock = threading.Lock()
        total_activity_count = 0

        def pop_log_block(lines: list[str]) -> str | None:
            if not lines:
                return None
            block = "\n".join(lines)
            lines.clear()
            return block

        def should_flush_log_block(lines: list[str]) -> bool:
            return len(lines) >= 20 or sum(len(line) for line in lines) >= 4000

        def flush_log_blocks() -> None:
            with output_lock:
                stdout_block = pop_log_block(stdout_log_lines)
                stderr_block = pop_log_block(stderr_log_lines)
            if stdout_block:
                self._log(log, f"codex result:\n{self._truncate(stdout_block, limit=5000)}")
            if stderr_block:
                self._log(log, f"codex output:\n{self._truncate(stderr_block, limit=5000)}")

        def read_stdout() -> None:
            nonlocal total_activity_count
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                flush_needed = False
                with output_lock:
                    total_activity_count += 1
                    output_lines.append(line)
                    recent_output.append(f"stdout: {line}")
                    stdout_log_lines.append(line)
                    flush_needed = should_flush_log_block(stdout_log_lines)
                if flush_needed:
                    flush_log_blocks()

        def read_stderr() -> None:
            nonlocal total_activity_count
            for raw_line in process.stderr:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                flush_needed = False
                with output_lock:
                    total_activity_count += 1
                    stderr_lines.append(line)
                    recent_output.append(f"stderr: {line}")
                    stderr_log_lines.append(line)
                    flush_needed = should_flush_log_block(stderr_log_lines)
                if flush_needed:
                    flush_log_blocks()

        stdout_reader = threading.Thread(target=read_stdout, daemon=True)
        stderr_reader = threading.Thread(target=read_stderr, daemon=True)
        stdout_reader.start()
        stderr_reader.start()
        started_at = time.monotonic()
        last_activity_at = started_at
        last_heartbeat_at = started_at
        last_log_flush_at = started_at
        last_activity_count = 0
        activity_count = 0
        no_output_timeout_seconds = self._no_output_timeout_for_mode(mode)
        timeout_seconds = self._timeout_for_mode(mode)
        while process.poll() is None:
            elapsed = time.monotonic() - started_at
            with output_lock:
                current_activity_count = total_activity_count
            if current_activity_count != activity_count:
                activity_count = current_activity_count
                last_activity_at = time.monotonic()
            if elapsed > timeout_seconds:
                self._kill_process(process)
                process.wait()
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                flush_log_blocks()
                process.stdout.close()
                process.stderr.close()
                with output_lock:
                    output_count = len(output_lines)
                    combined = "\n".join(part for part in ("\n".join(output_lines), "\n".join(stderr_lines)) if part).strip()
                    detail = "\n".join(recent_output).strip()
                salvaged = self._salvaged_output(combined, mode)
                if salvaged is not None:
                    self._log(
                        log,
                        f"codex agent timed out in {mode.value} mode, salvaged JSON output",
                        "warning",
                        {"timeout_seconds": timeout_seconds, "output_lines": output_count},
                    )
                    self._remember_run(salvaged=True, timed_out=True, raw_output=combined)
                    return salvaged
                self._log(
                    log,
                    f"codex agent timed out in {mode.value} mode",
                    "error",
                    {"timeout_seconds": timeout_seconds, "output_lines": output_count, "recent_output": detail},
                )
                raise RuntimeError(self._timeout_message(
                    f"codex agent timed out in {mode.value} mode after {timeout_seconds}s",
                    detail or combined,
                ))
            idle_seconds = time.monotonic() - last_activity_at
            if no_output_timeout_seconds > 0 and idle_seconds > no_output_timeout_seconds:
                self._kill_process(process)
                process.wait()
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                flush_log_blocks()
                process.stdout.close()
                process.stderr.close()
                with output_lock:
                    output_count = len(output_lines)
                    combined = "\n".join(part for part in ("\n".join(output_lines), "\n".join(stderr_lines)) if part).strip()
                    detail = "\n".join(recent_output).strip()
                salvaged = self._salvaged_output(combined, mode)
                if salvaged is not None:
                    self._log(
                        log,
                        f"codex agent no-output timed out in {mode.value} mode, salvaged JSON output",
                        "warning",
                        {
                            "no_output_timeout_seconds": no_output_timeout_seconds,
                            "elapsed_seconds": int(elapsed),
                            "idle_seconds": int(idle_seconds),
                            "output_lines": output_count,
                        },
                    )
                    self._remember_run(salvaged=True, timed_out=True, raw_output=combined)
                    return salvaged
                self._log(
                    log,
                    f"codex agent no-output timed out in {mode.value} mode",
                    "error",
                    {
                        "no_output_timeout_seconds": no_output_timeout_seconds,
                        "elapsed_seconds": int(elapsed),
                        "idle_seconds": int(idle_seconds),
                        "output_lines": output_count,
                        "recent_output": detail,
                    },
                )
                raise RuntimeError(self._timeout_message(
                    f"codex agent no-output timed out in {mode.value} mode after "
                    f"{no_output_timeout_seconds}s without stdout/stderr",
                    detail or combined,
                ))
            if time.monotonic() - last_heartbeat_at >= 60:
                with output_lock:
                    output_count = len(output_lines)
                    heartbeat_activity_count = total_activity_count
                idle_seconds = time.monotonic() - last_activity_at
                remaining_idle_seconds = (
                    max(0, no_output_timeout_seconds - int(idle_seconds))
                    if no_output_timeout_seconds > 0
                    else None
                )
                if heartbeat_activity_count == last_activity_count:
                    self._log(
                        log,
                        "codex agent still running with no new output",
                        metadata={
                            "elapsed_seconds": int(elapsed),
                            "idle_seconds": int(idle_seconds),
                            "remaining_idle_seconds": remaining_idle_seconds,
                            "output_lines": output_count,
                        },
                    )
                else:
                    self._log(
                        log,
                        "codex agent still running",
                        metadata={"elapsed_seconds": int(elapsed), "output_lines": output_count},
                    )
                last_activity_count = heartbeat_activity_count
                last_heartbeat_at = time.monotonic()
            if time.monotonic() - last_log_flush_at >= 2:
                flush_log_blocks()
                last_log_flush_at = time.monotonic()
            time.sleep(1)
        return_code = process.returncode
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        flush_log_blocks()
        process.stdout.close()
        process.stderr.close()

        with output_lock:
            output = "\n".join(output_lines).strip()
            stderr_output = "\n".join(stderr_lines).strip()
            detail = "\n".join(recent_output).strip()
        if return_code != 0:
            combined = "\n".join(item for item in [output, stderr_output] if item)
            salvaged_output = self._salvaged_output(combined, mode)
            if salvaged_output is not None:
                self._log(
                    log,
                    f"codex agent exited with code {return_code} in {mode.value} mode, salvaged JSON output",
                    "warning",
                    {"recent_output": detail},
                )
                self._remember_run(salvaged=True, timed_out=False, raw_output=combined)
                return salvaged_output
            if any(marker in combined.lower() for marker in TRANSIENT_UPSTREAM_MARKERS):
                self._log(
                    log,
                    f"codex agent transient upstream failure in {mode.value} mode "
                    f"(exit code {return_code})",
                    "warning",
                    {"recent_output": detail},
                )
                raise RuntimeError(
                    f"codex agent transient upstream failure in {mode.value} mode "
                    f"with exit code {return_code}: {detail}"
                )
            self._log(
                log,
                f"codex agent failed in {mode.value} mode with exit code {return_code}",
                "error",
                {"recent_output": detail},
            )
            raise RuntimeError(f"codex agent failed in {mode.value} mode with exit code {return_code}: {detail}")
        self._remember_run(salvaged=False, timed_out=False, raw_output=output)
        return output

    def _remember_run(self, *, salvaged: bool, timed_out: bool, raw_output: str) -> None:
        self._run_state.salvaged = salvaged
        self._run_state.timed_out = timed_out
        self._run_state.raw_output = raw_output

    @classmethod
    def _salvaged_output(cls, text: str, mode: DispatchMode) -> str | None:
        if mode == DispatchMode.REASON:
            return extract_mode_json_output(text, "reason")
        if mode not in {DispatchMode.FIRST_RUN, DispatchMode.EXPLORE}:
            return None
        return substantive_worker_output(text)

    @classmethod
    def _extract_mode_json_output(cls, text: str, mode: DispatchMode) -> str | None:
        return cls._salvaged_output(text, mode)

    @staticmethod
    def _timeout_message(message: str, tail: str) -> str:
        cleaned = tail.strip()
        if len(cleaned) > 4000:
            cleaned = cleaned[-4000:]
        if not cleaned:
            return message
        return f"{message}\noutput_tail:\n{cleaned}"

    def _no_output_timeout_for_mode(self, mode: DispatchMode) -> int:
        return self.mode_no_output_timeout_seconds.get(mode.value, self.no_output_timeout_seconds)

    def _timeout_for_mode(self, mode: DispatchMode) -> int:
        return self.mode_timeout_seconds.get(mode.value, self.timeout_seconds)

    def cancel_running(self) -> bool:
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            self._kill_process(process)
        return bool(processes)

    def _register_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._processes.append(process)

    def _unregister_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._processes = [item for item in self._processes if item is not process]

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()

    def _codex_command_for_mode(self, mode: DispatchMode) -> tuple[str, ...]:
        model = self.mode_models.get(mode.value, self.model)
        command = list(self.codex_command)

        extra_args: list[str] = []
        if self.openai_base_url and not self._command_already_sets_provider(command):
            # OPENAI_BASE_URL is not a reliable Codex CLI provider override. In
            # particular, recent Codex releases may still use the built-in
            # api.openai.com WebSocket endpoint. Configure an explicit provider
            # so proxy-backed runs use the requested Responses API endpoint.
            provider = self.provider_name
            extra_args.extend(
                [
                    "-c",
                    f"model_provider={json.dumps(provider)}",
                    "-c",
                    f"model_providers.{provider}.name={json.dumps('EchoAgent Proxy')}",
                    "-c",
                    f"model_providers.{provider}.base_url={json.dumps(self.openai_base_url)}",
                    "-c",
                    f"model_providers.{provider}.env_key={json.dumps('OPENAI_API_KEY')}",
                    "-c",
                    f"model_providers.{provider}.wire_api={json.dumps('responses')}",
                    "-c",
                    f"model_providers.{provider}.supports_websockets={str(self.provider_supports_websockets).lower()}",
                    "-c",
                    f"model_providers.{provider}.request_max_retries={self.request_max_retries}",
                    "-c",
                    f"model_providers.{provider}.stream_max_retries={self.stream_max_retries}",
                    "-c",
                    f"model_providers.{provider}.stream_idle_timeout_ms={self.stream_idle_timeout_ms}",
                ]
            )
        if model and not self._command_already_sets_model():
            extra_args.extend(["--model", model])

        if extra_args:
            if command and command[-1] == "-":
                command[-1:-1] = extra_args
            else:
                command.extend(extra_args)
        return tuple(command)

    def _command_already_sets_model(self) -> bool:
        return "--model" in self.codex_command or "-m" in self.codex_command

    @staticmethod
    def _command_already_sets_provider(command: Sequence[str]) -> bool:
        return any(
            item.startswith("model_provider=") or item.startswith("model_providers.")
            for item in command
        )

    @staticmethod
    def _mode_key(mode: DispatchMode | str) -> str:
        if isinstance(mode, DispatchMode):
            return mode.value
        return mode

    @staticmethod
    def _safe_command(command: Sequence[str]) -> list[str]:
        return ["<env>" if item.startswith("OPENAI_") else item for item in command]

    @staticmethod
    def _truncate(value: str, limit: int = 1000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit - 3]}..."

    @staticmethod
    def _log(
        log: AgentLogCallback | None,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> None:
        if log is not None:
            log(message, level, metadata or {})


class CodexWorker:
    def __init__(self, agent: AgentAdapter, prompt_builder: PromptBuilder | None = None) -> None:
        self.agent = agent
        self.prompt_builder = prompt_builder or PromptBuilder()

    def execute_intent(
        self,
        project: Project,
        intent: Intent,
        mode: DispatchMode = DispatchMode.EXPLORE,
        log: AgentLogCallback | None = None,
    ) -> WorkerResult:
        if mode == DispatchMode.FIRST_RUN:
            prompt = self.prompt_builder.build_first_run_prompt(project, intent)
        else:
            prompt = self.prompt_builder.build_explore_prompt(project, intent)
        prompt = f"{prompt}\n\n{self._isolation_note(intent.id)}"
        result = _invoke_agent(self.agent, project, prompt, mode, log, intent_id=intent.id)
        parsed = parse_worker_result(result.output)
        parsed.timed_out = bool(getattr(result, "timed_out", False))
        raw_output = getattr(result, "raw_output", "") or result.output
        if parsed.answer:
            parsed.answer_source = answer_source_context(raw_output, parsed.answer)
        for prior in parsed.prior_results:
            if prior.answer and not prior.answer_source:
                prior.answer_source = answer_source_context(raw_output, prior.answer)
        return parsed

    @staticmethod
    def _isolation_note(intent_id: int) -> str:
        return (
            f"本次执行已隔离到 intent {intent_id} 的独立工作目录和 CODEX_HOME。"
            f"tmux 必须使用 `tmux -L intent-{intent_id}`。"
            "产物只写当前目录。不要杀死其他进程，不要绑定固定的公共端口。"
        )


class CodexReasoner:
    def __init__(self, agent: AgentAdapter, prompt_builder: PromptBuilder | None = None) -> None:
        self.agent = agent
        self.prompt_builder = prompt_builder or PromptBuilder()

    def plan(self, project: Project, log: AgentLogCallback | None = None) -> ReasonResult:
        prompt = self.prompt_builder.build_reason_prompt(project)
        prompt = (
            f"{prompt}\n\n"
            "本次规划使用独立 CODEX_HOME 和工作目录。"
            "不要杀死其他进程，不要修改其他 intent 目录。"
        )
        result = _invoke_agent(self.agent, project, prompt, DispatchMode.REASON, log, runtime_key="reason")
        try:
            parsed = parse_reason_result(result.output)
        except ReasonFormatError as exc:
            repaired = repair_reason_output(exc.output or result.output)
            if repaired is None:
                raise
            parsed = parse_reason_result(repaired)
        parsed.salvaged = bool(getattr(result, "timed_out", False))
        return parsed



_ANSWER_SOURCE_CONTEXT_CHARS = 240


def answer_source_context(raw_output: str, answer: str | None) -> str | None:
    """从去掉结果 JSON 后的原始输出里截取答案上下文。只出现在结果 JSON 中则返回 None。"""
    text = (answer or "").strip()
    if not text or not raw_output:
        return None
    cleaned = strip_result_json_text(raw_output)
    index = cleaned.find(text)
    if index < 0:
        return None
    start = max(0, index - _ANSWER_SOURCE_CONTEXT_CHARS)
    end = min(len(cleaned), index + len(text) + _ANSWER_SOURCE_CONTEXT_CHARS)
    snippet = " ".join(cleaned[start:end].split())
    return snippet or None


def _invoke_agent(
    agent: AgentAdapter,
    project: Project,
    prompt: str,
    mode: DispatchMode,
    log: AgentLogCallback | None,
    **extra: object,
) -> AgentRunResult:
    run = agent.run
    try:
        parameters = inspect.signature(run).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, object] = {}
    if "log" in parameters:
        kwargs["log"] = log
    for key, value in extra.items():
        if key in parameters and value is not None:
            kwargs[key] = value
    return run(project, prompt, mode, **kwargs)  # type: ignore[arg-type]


class NoopWorker:
    def execute_intent(self, project: Project, intent: Intent) -> WorkerResult:
        return WorkerResult(fact_description="intent executed without adapter output")


class NoopReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=False, next_intent_descriptions=[])
