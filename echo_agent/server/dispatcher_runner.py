from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from echo_agent.config import DispatcherSettings
from echo_agent.dispatcher import Dispatcher
from echo_agent.dispatcher.core import step_consumes_budget, step_requests_trailing_explore
from echo_agent.models import Project, ProjectStatus
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_service import CryptoProjectService
from echo_agent.server.docker_runtime import DockerProjectRuntime, DockerRuntimeError
from echo_agent.storage import SQLiteProjectStore, ProjectConflictError


@dataclass(slots=True)
class DispatcherRunState:
    project_id: int
    running: bool
    stop_requested: bool = False
    last_message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "last_message": self.last_message,
        }


class DispatcherRunner:
    def __init__(self, store: SQLiteProjectStore, runtime: DockerProjectRuntime | None = None) -> None:
        self.store = store
        self.runtime = runtime or DockerProjectRuntime.from_settings(
            project_data_root=store.database_path.parent / "projects"
        )
        self._lock = threading.Lock()
        self._runs: dict[int, _RunHandle] = {}

    def start(
        self,
        project_id: int,
        interval_seconds: float = 3.0,
        max_steps: int | None = None,
        max_workers: int = 4,
    ) -> DispatcherRunState:
        project = self.store.get_project(project_id)
        if project.status == ProjectStatus.INACTIVE:
            raise ValueError("project must be activated before it can run")
        if project.status == ProjectStatus.COMPLETED:
            raise ValueError("completed project cannot run")
        with self._lock:
            existing = self._runs.get(project_id)
            if existing and existing.thread.is_alive():
                return existing.state
            if project.status == ProjectStatus.RUNNING and project.run_owner:
                raise ProjectConflictError("project is already owned by a dispatcher; stop it before starting another run")

            stop_event = threading.Event()
            state = DispatcherRunState(project_id=project_id, running=True)
            thread = threading.Thread(
                target=self._run_project,
                args=(project_id, interval_seconds, max_steps, max_workers, stop_event, state),
                daemon=True,
                name=f"echo-agent-dispatcher-{project_id}",
            )
            self._runs[project_id] = _RunHandle(thread=thread, stop_event=stop_event, state=state)
            thread.start()
            return state

    def stop(self, project_id: int, *, join_timeout: float = 5.0) -> DispatcherRunState:
        with self._lock:
            handle = self._runs.get(project_id)
            if handle is None:
                state = DispatcherRunState(project_id=project_id, running=False, last_message="not running")
                dispatcher = None
                thread = None
            else:
                handle.stop_event.set()
                handle.state.stop_requested = True
                handle.state.last_message = "stop requested; running codex processes cancelled"
                state = handle.state
                dispatcher = handle.dispatcher
                thread = handle.thread
        try:
            if dispatcher is not None:
                dispatcher.request_stop()
            if thread is not None and thread.is_alive():
                thread.join(timeout=join_timeout)
                if thread.is_alive():
                    self._force_stop_container(project_id)
                    thread.join(timeout=join_timeout)
        finally:
            # A failed cancel/force-stop must still release ownership, otherwise the project
            # stays "running" and can never be started again.
            try:
                self._release_idle_project(project_id)
            finally:
                state.running = thread is not None and thread.is_alive()
        return state

    def _force_stop_container(self, project_id: int) -> None:
        project = self.store.get_project(project_id)
        try:
            self.runtime.force_stop_project_container(
                project,
                log=lambda message, level="info", metadata=None: self.store.add_project_log(
                    project.id,
                    message,
                    level=level,
                    metadata=metadata or {},
                ),
            )
        except (DockerRuntimeError, OSError) as exc:
            self.store.add_project_log(
                project.id,
                f"forced container stop failed: {exc}",
                level="error",
                metadata={"docker": project.docker},
            )

    def _release_idle_project(self, project_id: int) -> None:
        with self.store.project_lock(project_id):
            self._release_idle_project_locked(project_id)

    def _release_idle_project_locked(self, project_id: int) -> None:
        project = self.store.get_project(project_id)
        if project.status != ProjectStatus.RUNNING:
            return
        old_status = project.status
        project.stop()
        self.runtime.apply_status_transition(
            project, old_status,
            log=lambda message, level="info", metadata=None: self.store.add_project_log(
                project.id,
                message,
                level=level,
                metadata=metadata or {},
            ),
        )
        self.store.add_project_log(
            project.id,
            "project status transition: running -> stoped",
            metadata={"old_status": "running", "new_status": "stoped", "forced": False},
        )
        self.store.save_project(project, force_stop=True)

    def status(self, project_id: int) -> DispatcherRunState:
        with self._lock:
            handle = self._runs.get(project_id)
            if handle is None:
                return DispatcherRunState(project_id=project_id, running=False, last_message="not running")
            handle.state.running = handle.thread.is_alive()
            return handle.state

    def _run_project(
        self,
        project_id: int,
        interval_seconds: float,
        max_steps: int | None,
        max_workers: int,
        stop_event: threading.Event,
        state: DispatcherRunState,
    ) -> None:
        client = StoreDispatcherClient(self.store, self.runtime)
        settings = DispatcherSettings.from_env()
        dispatcher = Dispatcher(
            client,
            max_explore_workers=max_workers,
            max_codex_slots=settings.codex_max_slots,
            stop_event=stop_event,
            codex_model=settings.codex_model,
            codex_mode_models=settings.codex_mode_models,
            codex_workspace=settings.codex_workspace,
            codex_timeout_seconds=settings.codex_timeout_seconds,
            codex_no_output_timeout_seconds=settings.codex_no_output_timeout_seconds,
            codex_mode_no_output_timeout_seconds=settings.codex_mode_no_output_timeout_seconds,
        codex_mode_timeout_seconds=settings.codex_mode_timeout_seconds,
            openai_api_key=settings.openai_api_key,
            openai_base_url=settings.openai_base_url,
            codex_command=settings.codex_command,
            codex_provider_name=settings.codex_provider_name,
            codex_provider_supports_websockets=settings.codex_provider_supports_websockets,
            codex_request_max_retries=settings.codex_request_max_retries,
            codex_stream_max_retries=settings.codex_stream_max_retries,
            codex_stream_idle_timeout_ms=settings.codex_stream_idle_timeout_ms,
        )
        with self._lock:
            handle = self._runs.get(project_id)
            if handle is not None:
                handle.dispatcher = dispatcher
        if stop_event.is_set():
            dispatcher.request_stop()
        steps = 0
        observed = 0
        try:
            client.add_project_log(
                project_id,
                "server dispatcher started",
                metadata={
                    "max_steps": max_steps,
                    "codex_model": settings.codex_model,
                    "codex_mode_models": settings.codex_mode_models,
                },
            )
            consecutive_failures = 0
            while not stop_event.is_set() and (max_steps is None or steps < max_steps):
                try:
                    result = dispatcher.run_once(project_id, release_project=False)
                except Exception as exc:
                    consecutive_failures += 1
                    state.last_message = f"{type(exc).__name__}: {exc}"
                    client.add_project_log(project_id, state.last_message, level="error")
                    if consecutive_failures >= 3:
                        break
                    stop_event.wait(interval_seconds)
                    continue
                consecutive_failures = 0
                observed += 1
                if step_consumes_budget(result):
                    steps += 1
                state.last_message = result.message
                client.add_project_log(
                    project_id,
                    f"server dispatcher step {observed} finished",
                    metadata={
                        "mode": result.mode.value,
                        "status": result.status.value,
                        "message": result.message,
                        "budget_steps": steps,
                    },
                )
                if result.mode.value in {"completed", "skipped"}:
                    break
                if step_consumes_budget(result) and max_steps is not None and steps >= max_steps:
                    if step_requests_trailing_explore(result) and not stop_event.is_set():
                        try:
                            trailing = dispatcher.run_once(project_id, release_project=False)
                        except Exception as exc:
                            state.last_message = f"{type(exc).__name__}: {exc}"
                            client.add_project_log(project_id, state.last_message, level="error")
                        else:
                            observed += 1
                            state.last_message = trailing.message
                            client.add_project_log(
                                project_id,
                                f"server dispatcher step {observed} finished",
                                metadata={
                                    "mode": trailing.mode.value,
                                    "status": trailing.status.value,
                                    "message": trailing.message,
                                    "budget_steps": steps,
                                    "trailing_explore": True,
                                },
                            )
                        break
                    break
                stop_event.wait(interval_seconds)
        except Exception as exc:
            state.last_message = f"{type(exc).__name__}: {exc}"
            client.add_project_log(project_id, state.last_message, level="error")
        finally:
            try:
                dispatcher._release_project_if_running(project_id)
            except ProjectConflictError:
                pass
            state.running = False
            client.add_project_log(
                project_id,
                "server dispatcher stopped",
                metadata={"steps": observed, "budget_steps": steps},
            )

class StoreDispatcherClient:
    def __init__(self, store: SQLiteProjectStore, runtime: DockerProjectRuntime | None = None) -> None:
        self.store = store
        self.dispatcher_id = uuid4().hex
        self.runtime = runtime or DockerProjectRuntime.from_settings(
            project_data_root=store.database_path.parent / "projects"
        )
        self.crypto_service = CryptoProjectService(
            CryptoProjectFiles(store.database_path.parent / "projects"),
            aes_image=getattr(self.runtime, "image", None),
        )

    def get_project(self, project_id: int) -> Project:
        return self.store.get_project(project_id)

    def verify_crypto_artifact(self, project_id: int, reference: dict) -> None:
        if self.store.get_project(project_id).task_type != "crypto":
            raise ValueError("artifact verification requires a crypto project")
        self.crypto_service.files.verify_ref(project_id, reference, "artifact")

    def verify_crypto_candidate(self, project_id: int, candidate_fact_id: int, *,
                                verification_id: str | None = None,
                                timeout_seconds: float | None = None) -> dict:
        return self.crypto_service.verify(
            self.store.get_project(project_id),
            candidate_fact_id,
            verification_id=verification_id,
            timeout_seconds=timeout_seconds,
        )

    def cancel_crypto_verification(self, project_id: int, verification_id: str) -> dict:
        return {"cancelled": self.crypto_service.cancel(verification_id)}

    def save_project(self, project: Project) -> Project:
        with self.store.project_lock(project.id):
            return self._save_project_locked(project)

    def _save_project_locked(self, project: Project) -> Project:
        old_project = self.store.get_project(project.id)
        if old_project.task_type == "crypto":
            preserved = old_project.crypto.get("acceptances")
            if preserved and project.crypto.get("acceptances") != preserved:
                project.crypto["acceptances"] = preserved
        self.store.check_write(project, dispatcher_id=self.dispatcher_id)
        self.crypto_service.validate_save(old_project, project)
        old_status = old_project.status
        if old_status != project.status:
            self.add_project_log(
                project.id,
                f"project status transition: {old_status.value} -> {project.status.value}",
                metadata={"old_status": old_status.value, "new_status": project.status.value},
            )
        try:
            self.runtime.apply_status_transition(
                project,
                old_status,
                log=lambda message, level="info", metadata=None: self.add_project_log(
                    project.id,
                    message,
                    level=level,
                    metadata=metadata or {},
                ),
            )
        except DockerRuntimeError as exc:
            self.add_project_log(
                project.id,
                f"project container lifecycle failed for {project.status.value}: {exc}",
                level="error",
                metadata={"docker": project.docker, "status": project.status.value},
            )
            raise
        if old_status != project.status:
            self.add_project_log(
                project.id,
                f"project container lifecycle applied for {project.status.value}",
                metadata={"docker": project.docker, "status": project.status.value},
            )
        return self.store.save_project(project, dispatcher_id=self.dispatcher_id)

    def add_project_log(
        self,
        project_id: int,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.store.add_project_log(project_id, message, level=level, metadata=metadata)


@dataclass(slots=True)
class _RunHandle:
    thread: threading.Thread
    stop_event: threading.Event
    state: DispatcherRunState
    dispatcher: Dispatcher | None = None
