from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from echo_agent.config import DispatcherSettings
from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.models import Project
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.server.project_workspace import ProjectWorkspace, project_directory_name
from echo_agent.storage import SQLiteProjectStore

WRITEUP_INPUT_DIR = "writeup-input"
WRITEUP_FILENAME = "writeup.md"
RECORD_FILENAME = "wp.md"
MAX_LOG_CHARS = 400_000
MAX_LINE_CHARS = 4_000
_DROPPED_PREFIXES = (
    "codex agent still running",
    "codex preflight",
    "codex prompt written",
    "codex runtime prepared",
    "docker container inspect",
)


def condense_runtime_log(text: str, limit: int = MAX_LOG_CHARS) -> str:
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "")
        if message.startswith(_DROPPED_PREFIXES):
            continue
        if len(message) > MAX_LINE_CHARS:
            message = f"{message[:MAX_LINE_CHARS]}\n...[truncated]"
        item["message"] = message
        kept.append(json.dumps(item, ensure_ascii=False))
    blob = "\n".join(kept)
    if len(blob) <= limit:
        return f"{blob}\n" if blob else ""
    tail = blob[-limit:]
    if "\n" in tail:
        tail = tail.split("\n", 1)[1]
    return f"{tail}\n" if tail else ""


def writeup_download_name(project: Project) -> str:
    return f"{project_directory_name(project.name, project.id)}-wp.md"


def writeup_output_candidates(work_dir: Path) -> tuple[Path, Path]:
    return (work_dir / WRITEUP_FILENAME, work_dir / "writeup" / WRITEUP_FILENAME)


def find_writeup_file(work_dir: Path) -> Path | None:
    for path in writeup_output_candidates(work_dir):
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def make_writeup_agent(settings: DispatcherSettings | None = None) -> CodexDockerAgent:
    settings = settings or DispatcherSettings.from_env()
    return CodexDockerAgent(
        codex_command=settings.codex_command,
        model=settings.codex_model,
        mode_models=settings.codex_mode_models,
        workspace=settings.codex_workspace,
        timeout_seconds=settings.codex_timeout_seconds,
        no_output_timeout_seconds=settings.codex_no_output_timeout_seconds,
        mode_no_output_timeout_seconds=settings.codex_mode_no_output_timeout_seconds,
        mode_timeout_seconds=settings.codex_mode_timeout_seconds,
        openai_api_key=settings.openai_api_key,
        openai_base_url=settings.openai_base_url,
        provider_name=settings.codex_provider_name,
        provider_supports_websockets=settings.codex_provider_supports_websockets,
        request_max_retries=settings.codex_request_max_retries,
        stream_max_retries=settings.codex_stream_max_retries,
        stream_idle_timeout_ms=settings.codex_stream_idle_timeout_ms,
    )


@dataclass(slots=True)
class WriteupJob:
    project_id: int
    status: str = "idle"
    error: str = ""
    filename: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "error": self.error,
            "filename": self.filename,
        }


class WriteupService:
    def __init__(
        self,
        store: SQLiteProjectStore,
        workspace: ProjectWorkspace,
        runtime: DockerProjectRuntime,
        agent_factory=make_writeup_agent,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.runtime = runtime
        self.agent_factory = agent_factory
        self.prompt_builder = prompt_builder or PromptBuilder()
        self._lock = threading.Lock()
        self._jobs: dict[int, WriteupJob] = {}
        self._threads: dict[int, threading.Thread] = {}

    def forget(self, project_id: int) -> None:
        with self._lock:
            thread = self._threads.get(project_id)
            if thread is not None and thread.is_alive():
                return
            self._jobs.pop(project_id, None)
            self._threads.pop(project_id, None)

    def status(self, project_id: int) -> dict[str, object]:
        project = self.store.get_project(project_id)
        with self._lock:
            job = self._jobs.get(project_id)
        if job is not None and job.status == "running":
            return job.to_dict()
        if job is not None and job.status == "error":
            return job.to_dict()
        path = self.record_path(project)
        if path.is_file() and path.stat().st_size > 0:
            filename = writeup_download_name(project)
            return WriteupJob(project_id, status="ready", filename=filename).to_dict()
        if job is not None:
            return job.to_dict()
        return WriteupJob(project_id).to_dict()

    def record_path(self, project: Project) -> Path:
        return self.workspace.ensure(project) / "records" / RECORD_FILENAME

    def start(self, project_id: int) -> dict[str, object]:
        project = self.store.get_project(project_id)
        with self._lock:
            current = self._threads.get(project_id)
            if current is not None and current.is_alive():
                return self._jobs[project_id].to_dict()
            job = WriteupJob(project_id, status="running", filename=writeup_download_name(project))
            self._jobs[project_id] = job
            thread = threading.Thread(
                target=self._run,
                args=(project.id,),
                name=f"writeup-{project.id}",
                daemon=True,
            )
            self._threads[project_id] = thread
            thread.start()
        return job.to_dict()

    def _run(self, project_id: int) -> None:
        try:
            self._generate(project_id)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._log(project_id, f"生成 WP 失败：{message}", level="error")
            with self._lock:
                job = self._jobs.get(project_id)
                if job is not None:
                    job.status = "error"
                    job.error = message

    def _generate(self, project_id: int) -> None:
        project = self.store.get_project(project_id)
        self._log(project_id, "开始生成 WP")
        self.workspace.write_record(project)
        work_dir = self.prepare_inputs(project)
        for output_path in writeup_output_candidates(work_dir):
            if output_path.exists():
                output_path.unlink()
        self.runtime.ensure_project_container(
            project,
            log=lambda message, level="info", metadata=None: self._log(
                project_id,
                message,
                level=level,
            ),
        )
        agent = self.agent_factory()
        try:
            agent.on_token_usage = self.store.add_token_usage
        except (AttributeError, TypeError):
            pass
        prompt = self.prompt_builder.build_writeup_prompt(project)
        agent.run(
            project,
            prompt,
            DispatchMode.WRITEUP,
            log=lambda message, level="info", metadata=None: self._log(project_id, message, level=level),
            runtime_key="writeup",
        )
        output_path = find_writeup_file(work_dir)
        if output_path is None:
            raise RuntimeError("Codex 没有写入 writeup.md")
        record_path = self.record_path(project)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output_path, record_path)
        filename = writeup_download_name(project)
        with self._lock:
            job = self._jobs.get(project_id)
            if job is not None:
                job.status = "ready"
                job.error = ""
                job.filename = filename
        self._log(project_id, "WP 已生成", level="success")

    def prepare_inputs(self, project: Project) -> Path:
        directory = self.workspace.ensure(project)
        work_dir = self.workspace.work_dir(project)
        source = work_dir / WRITEUP_INPUT_DIR
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True, exist_ok=True)
        record = directory / "records" / "project.json"
        if record.is_file():
            shutil.copyfile(record, source / "project.json")
        log_path = directory / "logs" / "runtime.jsonl"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        (source / "runtime.jsonl").write_text(condense_runtime_log(log_text), encoding="utf-8")
        return work_dir

    def _log(self, project_id: int, message: str, level: str = "info") -> None:
        try:
            self.store.add_project_log(project_id, message, level=level)
        except Exception:
            return
