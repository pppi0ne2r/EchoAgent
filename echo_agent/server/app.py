from __future__ import annotations

import secrets
import hashlib
import re
import tempfile
import time
from dataclasses import is_dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from starlette.requests import Request

from echo_agent.config import DispatcherSettings
from echo_agent.console_settings import public_dispatcher_settings, save_console_settings
from echo_agent.crypto_contract import artifact_ref, input_ref, project_crypto
from echo_agent.crypto_files import CryptoProjectFiles, MAX_INPUT_BYTES, safe_input_filename
from echo_agent.crypto_service import CryptoProjectService
from echo_agent.crypto_verification import VerificationBusy
from echo_agent.crypto_verify import candidate_digest, goal_sha256
from echo_agent.models import FactKind, Project, ProjectStatus
from echo_agent.server.attachments import MAX_ATTACHMENT_BYTES
from echo_agent.server.project_workspace import ProjectWorkspace
from echo_agent.server.docker_runtime import DockerProjectRuntime, DockerRuntimeError
from echo_agent.server.schemas import (
    AcceptanceRequest,
    ConsoleSettingsUpdate,
    FactCreateRequest,
    IntentCreateRequest,
    ProjectBatchRunRequest,
    ProjectCreateRequest,
    ProjectDetailsUpdate,
    ProjectLogCreateRequest,
    ExtensionCreateRequest,
    ProjectRunRequest,
    ProjectSaveRequest,
    ProjectStatusRequest,
)
from echo_agent.server.dispatcher_runner import DispatcherRunner
from echo_agent.server.extensions import ExtensionCatalog, ExtensionResolveError
from echo_agent.server.writeup import WriteupService
from echo_agent.storage import ProjectNotFoundError, ProjectConflictError, SQLiteProjectStore


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
    @app.exception_handler(ProjectConflictError)
    async def project_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=HTTPStatus.CONFLICT)
    app.state.store = store
    data_dir = Path(store.database_path).parent
    app.state.attachment_root = data_dir / "uploads"
    app.state.workspace = ProjectWorkspace(data_dir / "workspace", legacy_uploads=app.state.attachment_root)
    app.state.workspace.bind_store(store)
    app.state.workspace.ensure_projects(store.list_projects())
    app.state.workspace.backfill_logs(store)
    store.refresh_boundary_facts()
    runtime = DockerProjectRuntime.from_settings()
    if is_dataclass(runtime) and not isinstance(runtime, type):
        runtime = replace(
            runtime,
            attachment_root=app.state.attachment_root,
            workspace=app.state.workspace,
        )
    else:
        # Test doubles may not be dataclasses; attach the workspace when possible.
        for attribute, value in (
            ("attachment_root", app.state.attachment_root),
            ("workspace", app.state.workspace),
        ):
            try:
                setattr(runtime, attribute, value)
            except (AttributeError, TypeError):
                pass
    resource_root = getattr(runtime, "resource_root", data_dir / "resources")
    app.state.extensions = ExtensionCatalog(data_dir / "extensions.json", resource_root)
    if is_dataclass(runtime) and not isinstance(runtime, type):
        runtime = replace(runtime, extensions=app.state.extensions)
    else:
        try:
            setattr(runtime, "extensions", app.state.extensions)
        except (AttributeError, TypeError):
            pass
    app.state.project_runtime = runtime
    app.state.project_runtime.prepare_shared_resources()
    prepare_extensions = getattr(app.state.project_runtime, "prepare_extensions", None)
    if prepare_extensions is not None:
        prepare_extensions()
    app.state.crypto_service = CryptoProjectService(
        CryptoProjectFiles(store.database_path.parent / "projects"),
        aes_image=getattr(app.state.project_runtime, "image", None),
    )
    app.state.dispatcher_runner = DispatcherRunner(store, runtime=app.state.project_runtime)
    app.state.writeup_service = WriteupService(
        store,
        app.state.workspace,
        app.state.project_runtime,
    )
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
            if data.task_type == "crypto" and data.crypto.get("inputs"):
                raise ValueError("upload crypto input bytes after creating the project")
            project = app.state.store.create_project(
                name=data.name,
                project_id=data.id,
                docker=data.docker,
                target_url=data.target_url,
                hint=data.hint,
                objective=data.objective,
                task_type=data.task_type,
                crypto=data.crypto,
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

    @app.get("/projects/{project_id}/facts/{fact_id}")
    def get_project_fact(project_id: int, fact_id: int) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        fact = project.facts.get(fact_id)
        if fact is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="fact not found")
        return fact.to_dict()

    @app.post("/projects/{project_id}/inputs", status_code=HTTPStatus.CREATED)
    async def upload_project_input(
        project_id: int,
        request: Request,
        filename: str,
        role: str = "input",
        input_id: str | None = None,
        number_radix: int | None = None,
        byte_order: str | None = None,
        byte_length: int | None = None,
    ) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
            if project.task_type != "crypto":
                raise ValueError("inputs can only be uploaded to crypto projects")
            if project.status.value not in {"inactive", "active", "stoped"}:
                raise ValueError("inputs can only be added to an inactive, active or stopped crypto project; completed projects are immutable")
            safe_input_filename(filename)
            if not role.strip() or len(role) > 64 or any(ord(char) < 32 for char in role):
                raise ValueError("invalid input role")
            if input_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", input_id):
                raise ValueError("invalid input id")
            ref_id = input_id or f"input-{uuid4().hex}"
            relative_name = f"inputs/{uuid4().hex}-{filename}"
            reference: dict[str, Any] = {
                "id": ref_id, "role": role, "filename": filename,
                "path": relative_name, "sha256": "0" * 64, "size_bytes": 0,
            }
            for key, value in (("number_radix", number_radix), ("byte_order", byte_order), ("byte_length", byte_length)):
                if value is not None:
                    reference[key] = value
            input_ref(reference)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise HTTPException(status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE, detail="upload raw file bytes as application/octet-stream")
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > MAX_INPUT_BYTES:
            raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail="input exceeds 32 MiB limit")
        files = CryptoProjectFiles(store.database_path.parent / "projects")
        inputs, _ = files.ensure_layout(project_id)
        final_path = inputs / relative_name.split("/", 1)[1]
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".upload-", dir=inputs.parent, delete=False) as output:
                temporary_path = Path(output.name)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_INPUT_BYTES:
                        raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail="input exceeds 32 MiB limit")
                    digest.update(chunk)
                    output.write(chunk)
            reference["sha256"] = digest.hexdigest()
            reference["size_bytes"] = size
            input_ref(reference)
            temporary_path.replace(final_path)
            temporary_path = None
            return app.state.store.add_crypto_input(project_id, reference)
        except ProjectNotFoundError as exc:
            final_path.unlink(missing_ok=True)
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except ValueError as exc:
            final_path.unlink(missing_ok=True)
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @app.post("/projects/{project_id}/artifacts/verify")
    def verify_crypto_artifact(project_id: int, reference: dict[str, Any]) -> dict[str, bool]:
        try:
            project = app.state.store.get_project(project_id)
            if project.task_type != "crypto":
                raise ValueError("artifact verification requires a crypto project")
            CryptoProjectFiles(store.database_path.parent / "projects").verify_ref(
                project_id, artifact_ref(reference), "artifact"
            )
            return {"verified": True}
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/candidates/{candidate_fact_id}/verify")
    def verify_crypto_candidate(project_id: int, candidate_fact_id: int,
                                verification_id: str | None = None,
                                timeout_seconds: float | None = None) -> dict[str, Any]:
        try:
            project = app.state.store.get_project(project_id)
            return app.state.crypto_service.verify(
                project,
                candidate_fact_id,
                verification_id=verification_id,
                timeout_seconds=timeout_seconds,
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except VerificationBusy as exc:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/verifications/{verification_id}/cancel")
    def cancel_crypto_verification(project_id: int, verification_id: str) -> dict[str, Any]:
        try:
            app.state.store.get_project(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        return {"cancelled": app.state.crypto_service.cancel(verification_id)}

    @app.post("/projects/{project_id}/acceptances", status_code=HTTPStatus.CREATED)
    def record_crypto_acceptance(project_id: int, data: AcceptanceRequest) -> dict[str, Any]:
        with app.state.store.project_lock(project_id):
            try:
                project = app.state.store.get_project(project_id)
                if project.task_type != "crypto":
                    raise ValueError("acceptances require a crypto project")
                if project.status == ProjectStatus.RUNNING:
                    raise ValueError("stop the project before recording an acceptance")
                fact = project.facts.get(data.candidate_fact_id)
                if fact is None or fact.kind != FactKind.CANDIDATE:
                    raise ValueError("candidate_fact_id must name a candidate fact")
                candidate = fact.crypto.get("candidate")
                if (not isinstance(candidate, dict) or not isinstance(candidate.get("answer"), str)
                        or not candidate["answer"]):
                    raise ValueError("candidate fact needs a structured answer")
                record = {
                    "candidate_fact_id": fact.id,
                    "candidate_sha256": candidate_digest(fact),
                    "answer": candidate["answer"],
                    "goal_sha256": goal_sha256(project),
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                    "operator": data.operator,
                    "note": data.note,
                }
                acceptances = project.crypto.setdefault("acceptances", [])
                if not any(
                    item.get("candidate_sha256") == record["candidate_sha256"]
                    and item.get("answer") == record["answer"]
                    and item.get("goal_sha256") == record["goal_sha256"]
                    for item in acceptances
                ):
                    acceptances.append(record)
                project.crypto = project_crypto(project.crypto)
                app.state.store.save_project(project)
                return record
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
            except (KeyError, ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: int) -> dict[str, Any]:
        with app.state.store.project_lock(project_id):
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
                if project.status.value == "running":
                    raise ProjectConflictError("stop the project before deleting it")
                remove_container = getattr(app.state.project_runtime, "remove_project_container", None)
                if callable(remove_container):
                    remove_container(project)
                deleted = app.state.store.delete_project(project_id).to_dict()
                app.state.workspace.remove(project_id)
                return deleted
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
            except DockerRuntimeError as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.put("/projects/{project_id}")
    def save_project(project_id: int, data: ProjectSaveRequest, request: Request) -> dict[str, Any]:
        with app.state.store.project_lock(project_id):
            try:
                old_project = app.state.store.get_project(project_id)
                if old_project.task_type == "crypto":
                    if not {"task_type", "crypto"}.issubset(data.model_fields_set):
                        raise ValueError("crypto project save requires task_type and crypto metadata")
                    for field_name, old_items in (("facts", old_project.facts), ("intents", old_project.intents)):
                        for item in getattr(data, field_name):
                            old_item = old_items.get(item.get("id"))
                            if old_item is not None and old_item.crypto and "crypto" not in item:
                                raise ValueError(f"crypto metadata missing for {field_name} id {old_item.id}")
                payload = data.model_dump(mode="json")
                if old_project.task_type == "crypto" and isinstance(payload.get("crypto"), dict):
                    # Receipts are server-owned: a normal project save cannot add or remove them.
                    payload["crypto"].pop("acceptances", None)
                    preserved = old_project.crypto.get("acceptances")
                    if preserved:
                        payload["crypto"]["acceptances"] = preserved
                payload["id"] = project_id
                project = Project.from_dict(payload)
                dispatcher_id = request.headers.get("x-echo-agent-dispatcher")
                app.state.store.check_write(project, dispatcher_id=dispatcher_id)
                app.state.crypto_service.validate_save(old_project, project)
                apply_project_status_transition(project, old_project)
                return app.state.store.save_project(project, dispatcher_id=dispatcher_id).to_dict()
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
            except VerificationBusy as exc:
                raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
            except (KeyError, ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
            except DockerRuntimeError as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.patch("/projects/{project_id}/status")
    def update_project_status(project_id: int, data: ProjectStatusRequest) -> dict[str, Any]:
        # Joining while holding project_lock would block the dispatcher's final save.
        try:
            if ProjectStatus.coerce(data.status) == ProjectStatus.STOPED:
                project = app.state.store.get_project(project_id)
                if project.status != ProjectStatus.STOPED:
                    project.transition_to(ProjectStatus.STOPED)
                app.state.dispatcher_runner.stop(project_id)
                return app.state.store.get_project(project_id).to_dict()
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        except (ValueError, DockerRuntimeError) as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        with app.state.store.project_lock(project_id):
            try:
                project = app.state.store.get_project(project_id)
                old_project = Project.from_dict(project.to_dict())
                project.transition_to(data.status)
                app.state.store.check_write(project, force_stop=project.status.value == "stoped")
                app.state.crypto_service.validate_save(old_project, project)
                apply_project_status_transition(project, old_project)
                return app.state.store.save_project(project, force_stop=project.status.value == "stoped").to_dict()
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
            except VerificationBusy as exc:
                raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc)) from exc
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
            except DockerRuntimeError as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/facts", status_code=HTTPStatus.CREATED)
    def add_fact(project_id: int, data: FactCreateRequest) -> dict[str, Any]:
        with app.state.store.project_lock(project_id):
            try:
                project = app.state.store.get_project(project_id)
                app.state.store.check_write(project)
                fact = project.add_fact(data.description, fact_id=data.id, crypto=data.crypto)
                app.state.store.save_project(project)
                return fact.to_dict()
            except ProjectNotFoundError as exc:
                raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/intents", status_code=HTTPStatus.CREATED)
    def add_intent(project_id: int, data: IntentCreateRequest) -> dict[str, Any]:
        with app.state.store.project_lock(project_id):
            try:
                project = app.state.store.get_project(project_id)
                app.state.store.check_write(project)
                intent = project.add_intent(
                    description=data.description,
                    from_fact_id=data.from_fact_id,
                    to_fact_id=data.to_fact_id,
                    intent_id=data.id,
                    crypto=data.crypto,
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

    @app.post("/projects/{project_id}/writeup", status_code=HTTPStatus.ACCEPTED)
    def start_writeup(project_id: int) -> dict[str, Any]:
        try:
            return app.state.writeup_service.start(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.get("/projects/{project_id}/writeup")
    def get_writeup(project_id: int) -> dict[str, Any]:
        try:
            return app.state.writeup_service.status(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc

    @app.get("/projects/{project_id}/writeup/download")
    def download_writeup(project_id: int) -> FileResponse:
        try:
            project = app.state.store.get_project(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="project not found") from exc
        path = app.state.writeup_service.record_path(project)
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="writeup not found")
        return FileResponse(
            path,
            media_type="text/markdown; charset=utf-8",
            filename=app.state.writeup_service.status(project_id).get("filename") or "wp.md",
        )

    @app.get("/extensions")
    def list_extensions() -> list[dict[str, Any]]:
        return app.state.extensions.public_items(app.state.project_runtime._resource_exists)

    @app.post("/extensions", status_code=HTTPStatus.CREATED)
    def create_extension(data: ExtensionCreateRequest) -> dict[str, Any]:
        try:
            link = data.url.strip() or data.repo.strip()
            if data.name.strip() and data.kind != "tool":
                item = app.state.extensions.add(data.kind, data.name, data.url)
            else:
                item = app.state.extensions.add_from_link(data.kind, link)
        except (ValueError, ExtensionResolveError) as exc:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)) from exc
        return next(entry for entry in app.state.extensions.public_items(app.state.project_runtime._resource_exists) if entry["id"] == item.id)

    @app.post("/extensions/update")
    def update_extensions(kind: str | None = None) -> dict[str, Any]:
        if kind is not None and kind not in {"knowledge", "tool", "skill"}:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="kind must be knowledge, tool, or skill")
        failures = app.state.project_runtime.update_extensions(kind=kind)
        items = app.state.extensions.public_items(app.state.project_runtime._resource_exists)
        if failures:
            detail = "、".join(f"{item['name']}（{item['error']}）" for item in failures)
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=f"扩展更新失败：{detail}")
        return {"updated": len(items) if kind is None else sum(item["kind"] == kind for item in items), "items": items}

    @app.post("/extensions/{item_id}/update")
    def update_extension(item_id: str) -> dict[str, Any]:
        try:
            failures = app.state.project_runtime.update_extensions(item_id=item_id)
        except KeyError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="extension not found") from exc
        if failures:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=failures[0]["error"])
        return next(entry for entry in app.state.extensions.public_items(app.state.project_runtime._resource_exists) if entry["id"] == item_id)

    @app.delete("/extensions/{item_id}")
    def delete_extension(item_id: str) -> dict[str, Any]:
        try:
            app.state.extensions.delete(item_id)
        except KeyError as exc:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="extension not found") from exc
        return {"deleted": item_id}

    @app.post("/containers/restart")
    def restart_containers() -> dict[str, Any]:
        stopped = [state.to_dict() for state in app.state.dispatcher_runner.stop_all()]
        restarted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for project in app.state.store.list_projects():
            def log(message: str, level: str = "info", metadata: dict[str, Any] | None = None, project_id: int = project.id) -> None:
                app.state.store.add_project_log(project_id, message, level=level, metadata=metadata or {})

            try:
                recreated = app.state.project_runtime.recreate_existing_container(project, log=log)
            except DockerRuntimeError as exc:
                failed.append({"id": project.id, "name": project.name, "error": str(exc)})
                app.state.store.add_project_log(project.id, f"docker container restart failed: {exc}", level="error")
                continue
            entry = {"id": project.id, "name": project.name, "docker": project.docker}
            if recreated:
                restarted.append(entry)
            else:
                skipped.append({**entry, "reason": "not created"})
        return {"stopped": stopped, "restarted": restarted, "skipped": skipped, "failed": failed}

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
