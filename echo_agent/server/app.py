from __future__ import annotations

import secrets
import time
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any

import uvicorn

from echo_agent.config import DispatcherSettings
from echo_agent.console_settings import public_dispatcher_settings, save_console_settings
from echo_agent.models import Project, ProjectStatus
from echo_agent.server.attachments import MAX_ATTACHMENT_BYTES
from echo_agent.server.project_workspace import ProjectWorkspace
from echo_agent.server.docker_runtime import DockerProjectRuntime, DockerRuntimeError
from echo_agent.server.schemas import (
    ConsoleSettingsUpdate,
    FactCreateRequest,
    IntentCreateRequest,
    ProjectBatchRunRequest,
    ProjectCreateRequest,
    ProjectDetailsUpdate,
    ProjectLogCreateRequest,
    ProjectRunRequest,
    ProjectSaveRequest,
    ProjectStatusRequest,
)
from echo_agent.server.dispatcher_runner import DispatcherRunner
from echo_agent.storage import ProjectNotFoundError, SQLiteProjectStore


class MissingServerDependencyError(RuntimeError):
    pass


def create_app(store: SQLiteProjectStore, auth_token: str | None = None) -> Any:
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        globals()["Request"] = Request
    except ModuleNotFoundError as exc:
        raise MissingServerDependencyError(
            "FastAPI is required to run the server. Install dependencies with: "
            "pip install -r requirements.txt"
        ) from exc

    app = FastAPI(title="EchoAgent Server")
    app.state.store = store
    data_dir = Path(store.database_path).parent
    app.state.attachment_root = data_dir / "uploads"
    app.state.workspace = ProjectWorkspace(data_dir / "workspace", legacy_uploads=app.state.attachment_root)
    app.state.workspace.bind_store(store)
    app.state.workspace.ensure_projects(store.list_projects())
    app.state.workspace.backfill_logs(store)
    store.refresh_boundary_facts()
    app.state.project_runtime = replace(
        DockerProjectRuntime.from_settings(),
        attachment_root=app.state.attachment_root,
        workspace=app.state.workspace,
    )
    app.state.project_runtime.prepare_shared_resources()
    app.state.dispatcher_runner = DispatcherRunner(store, runtime=app.state.project_runtime)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    if auth_token:
        @app.middleware("http")
        async def _require_auth_token(request, call_next):  # type: ignore[no-untyped-def]
            path = request.url.path
            if path in {"/", "/health"} or path.startswith("/static"):
                return await call_next(request)
            provided = request.headers.get("x-echo-agent-token", "")
            if not secrets.compare_digest(provided, auth_token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    def apply_project_status_transition(project: Project, old_project: Project) -> None:
        if old_project.status == project.status:
            return
        app.state.store.add_project_log(
            project.id,
            f"project status transition: {old_project.status.value} -> {project.status.value}",
            metadata={"old_status": old_project.status.value, "new_status": project.status.value},
        )
        app.state.store.add_project_log(
            project.id,
            f"project container lifecycle started for {project.status.value}",
            metadata={"docker": project.docker, "status": project.status.value},
        )
        try:
            app.state.project_runtime.apply_status_transition(
                project,
                old_project.status,
                log=lambda message, level="info", metadata=None: app.state.store.add_project_log(
                    project.id,
                    message,
                    level=level,
                    metadata=metadata or {},
                ),
            )
        except DockerRuntimeError as exc:
            app.state.store.add_project_log(
                project.id,
                f"project container lifecycle failed for {project.status.value}: {exc}",
                level="error",
                metadata={"docker": project.docker, "status": project.status.value},
            )
            raise
        app.state.store.add_project_log(
            project.id,
            f"project container lifecycle applied for {project.status.value}",
            metadata={"docker": project.docker, "status": project.status.value},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/projects")
    def list_projects() -> list[dict[str, Any]]:
        return [project.to_dict() for project in app.state.store.list_projects()]

    @app.post("/projects", status_code=HTTPStatus.CREATED)
    def create_project(data: ProjectCreateRequest) -> dict[str, Any]:
        try:
            project = app.state.store.create_project(
                name=data.name,
                project_id=data.id,
                docker=data.docker,
                target_url=data.target_url,
                hint=data.hint,
                objective=data.objective,
            )
            app.state.workspace.write_record(project)
            return project.to_dict()
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.put("/projects/{project_id}/attachment")
    async def upload_project_attachment(project_id: int, request: Request, name: str = "") -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        if project.status != ProjectStatus.INACTIVE:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="只能在项目启动前上传附件")
        filename = name.strip()
        if not filename:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="缺少附件文件名")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                announced = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="附件大小无效") from exc
            if announced > MAX_ATTACHMENT_BYTES:
                raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail="附件不能超过 128MB")
        content = await request.body()
        try:
            _host_path, container_path = app.state.workspace.save_upload(project, filename, content)
            project = app.state.store.set_project_target(project_id, container_path)
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        return project.to_dict()

    @app.get("/projects/runs")
    def list_project_runs() -> list[dict[str, Any]]:
        return [state.to_dict() for state in app.state.dispatcher_runner.list_states()]

    @app.post("/projects/runs", status_code=HTTPStatus.ACCEPTED)
    def start_project_runs(data: ProjectBatchRunRequest) -> dict[str, Any]:
        interval_seconds, max_workers = _run_limits(data.interval_seconds, data.max_workers)
        project_ids = data.project_ids
        if not project_ids:
            project_ids = [
                project.id
                for project in app.state.store.list_projects()
                if project.status != ProjectStatus.COMPLETED
            ]
        runs: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for project_id in project_ids:
            try:
                runs.append(
                    app.state.dispatcher_runner.start(
                        project_id,
                        interval_seconds=interval_seconds,
                        max_steps=data.max_steps,
                        max_workers=max_workers,
                    ).to_dict()
                )
            except ProjectNotFoundError:
                errors.append({"project_id": project_id, "detail": "project not found"})
            except ValueError as exc:
                errors.append({"project_id": project_id, "detail": str(exc)})
        return {"runs": runs, "errors": errors}

    @app.delete("/projects/runs")
    def stop_project_runs() -> dict[str, Any]:
        return {"runs": [state.to_dict() for state in app.state.dispatcher_runner.stop_all()]}

    @app.patch("/projects/{project_id}/details")
    def update_project_details(project_id: int, data: ProjectDetailsUpdate) -> dict[str, Any]:
        try:
            run_state = app.state.dispatcher_runner.status(project_id)
            if run_state.running or run_state.queued:
                raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="项目正在运行或排队，请先停止后再编辑")
            project = app.state.store.update_project_details(
                project_id,
                name=data.name,
                target_url=data.target_url,
                hint=data.hint,
                objective=data.objective,
            )
            app.state.workspace.prune_attachments(project)
            return project.to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/projects/{project_id}")
    def get_project(project_id: int) -> dict[str, Any]:
        try:
            return app.state.store.get_project(project_id).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: int) -> dict[str, Any]:
        try:
            run_state = app.state.dispatcher_runner.status(project_id)
            if run_state.queued:
                app.state.dispatcher_runner.stop(project_id)
            elif run_state.running:
                raise HTTPException(
                    status_code=HTTPStatus.CONFLICT,
                    detail="project dispatcher is running; stop it before deleting the project",
                )
            project = app.state.store.get_project(project_id)
            app.state.project_runtime.remove_project_container(project)
            deleted = app.state.store.delete_project(project_id).to_dict()
            app.state.workspace.remove(project_id)
            return deleted
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.put("/projects/{project_id}")
    def save_project(project_id: int, data: ProjectSaveRequest) -> dict[str, Any]:
        try:
            old_project = app.state.store.get_project(project_id)
            payload = data.model_dump(mode="json")
            payload["id"] = project_id
            project = Project.from_dict(payload)
            apply_project_status_transition(project, old_project)
            return app.state.store.save_project(project).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.patch("/projects/{project_id}/status")
    def update_project_status(project_id: int, data: ProjectStatusRequest) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
            old_project = Project.from_dict(project.to_dict())
            project.transition_to(data.status)
            apply_project_status_transition(project, old_project)
            return app.state.store.save_project(project).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/facts", status_code=HTTPStatus.CREATED)
    def add_fact(project_id: int, data: FactCreateRequest) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
            fact = project.add_fact(data.description, fact_id=data.id)
            app.state.store.save_project(project)
            return fact.to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/intents", status_code=HTTPStatus.CREATED)
    def add_intent(project_id: int, data: IntentCreateRequest) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
            intent = project.add_intent(
                description=data.description,
                from_fact_id=data.from_fact_id,
                to_fact_id=data.to_fact_id,
                intent_id=data.id,
            )
            app.state.store.save_project(project)
            return intent.to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/projects/{project_id}/logs")
    def list_project_logs(project_id: int, limit: int = 200, after_id: int = 0) -> list[dict[str, Any]]:
        try:
            return app.state.store.list_project_logs(project_id, limit=limit, after_id=max(0, after_id))
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.post("/projects/{project_id}/logs", status_code=HTTPStatus.CREATED)
    def add_project_log(project_id: int, data: ProjectLogCreateRequest) -> dict[str, Any]:
        try:
            return app.state.store.add_project_log(
                project_id,
                message=data.message,
                level=data.level,
                metadata=data.metadata,
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.delete("/projects/{project_id}/logs")
    def clear_project_logs(project_id: int) -> dict[str, Any]:
        try:
            return app.state.store.clear_project_logs(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.get("/projects/{project_id}/run")
    def get_project_run_status(project_id: int) -> dict[str, Any]:
        try:
            app.state.store.get_project(project_id)
            return app.state.dispatcher_runner.status(project_id).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.post("/projects/{project_id}/run", status_code=HTTPStatus.ACCEPTED)
    def start_project_run(project_id: int, data: ProjectRunRequest) -> dict[str, Any]:
        try:
            interval_seconds, max_workers = _run_limits(data.interval_seconds, data.max_workers)
            return app.state.dispatcher_runner.start(
                project_id,
                interval_seconds=interval_seconds,
                max_steps=data.max_steps,
                max_workers=max_workers,
            ).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.delete("/projects/{project_id}/run")
    def stop_project_run(project_id: int) -> dict[str, Any]:
        try:
            app.state.store.get_project(project_id)
            return app.state.dispatcher_runner.stop(project_id).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        try:
            return public_dispatcher_settings()
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.put("/settings")
    def put_settings(data: ConsoleSettingsUpdate) -> dict[str, Any]:
        try:
            saved = save_console_settings(
                max_concurrent_projects=data.max_concurrent_projects,
                codex_max_slots=data.codex_max_slots,
                explore_max_workers=data.explore_max_workers,
                base_url=data.base_url,
                model=data.model,
                api_key=data.api_key,
                clear_api_key=data.clear_api_key,
            )
            app.state.dispatcher_runner._promote()
            return saved
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    return app


def _run_limits(interval_seconds: float | None, max_workers: int | None) -> tuple[float, int]:
    settings = DispatcherSettings.from_env()
    interval = settings.interval_seconds if interval_seconds is None else interval_seconds
    workers = settings.explore_max_workers if max_workers is None else max_workers
    if interval < 0:
        raise ValueError("interval_seconds must be non-negative")
    if workers < 1:
        raise ValueError("max_workers must be at least 1")
    return interval, workers


class UvicornServerHandle:
    def __init__(self, host: str, port: int, app: Any) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)

    @property
    def server_port(self) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            port = self._bound_port()
            if port is not None:
                return port
            time.sleep(0.01)
        raise RuntimeError("server did not start within 5 seconds")

    def serve_forever(self) -> None:
        self._server.run()

    def shutdown(self) -> None:
        self._server.should_exit = True

    def server_close(self) -> None:
        return

    def _bound_port(self) -> int | None:
        servers = getattr(self._server, "servers", None)
        if not servers:
            return None
        sockets = getattr(servers[0], "sockets", None)
        if not sockets:
            return None
        return int(sockets[0].getsockname()[1])


def create_server(host: str, port: int, database_path: str, auth_token: str | None = None) -> UvicornServerHandle:
    return UvicornServerHandle(host, port, create_app(SQLiteProjectStore(database_path), auth_token=auth_token))
