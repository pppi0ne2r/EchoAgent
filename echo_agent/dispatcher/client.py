from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from echo_agent.crypto_files import MAX_INPUT_BYTES
from echo_agent.crypto_verification import DEFAULT_VERIFY_SECONDS
from echo_agent.models import Project


class ProjectAPIError(RuntimeError):
    pass


class ProjectClient:
    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token or None
        self.dispatcher_id = uuid4().hex

    def list_projects(self) -> list[Project]:
        return [Project.from_dict(item) for item in self._request("GET", "/projects")]

    def get_project(self, project_id: int) -> Project:
        return Project.from_dict(self._request("GET", f"/projects/{project_id}"))

    def add_token_usage(self, project_id: int, usage: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/token-usage", usage)

    def get_fact(self, project_id: int, fact_id: int) -> dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}/facts/{fact_id}")

    def verify_crypto_artifact(self, project_id: int, reference: dict[str, Any]) -> None:
        result = self._request("POST", f"/projects/{project_id}/artifacts/verify", reference)
        if result.get("verified") is not True:
            raise ProjectAPIError("crypto artifact verification did not succeed")

    def verify_crypto_candidate(self, project_id: int, candidate_fact_id: int, *,
                                verification_id: str | None = None,
                                timeout_seconds: float | None = None) -> dict[str, Any]:
        verification_id = verification_id or uuid4().hex
        timeout_seconds = DEFAULT_VERIFY_SECONDS if timeout_seconds is None else timeout_seconds
        query = urlencode({"verification_id": verification_id, "timeout_seconds": timeout_seconds})
        return self._request(
            "POST",
            f"/projects/{project_id}/candidates/{candidate_fact_id}/verify?{query}",
            {},
            timeout=float(timeout_seconds) + 5,
            retry=False,
        )

    def cancel_crypto_verification(self, project_id: int, verification_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/projects/{project_id}/verifications/{quote(verification_id, safe='')}/cancel",
            {},
            timeout=10,
            retry=False,
        )

    def accept_crypto_candidate(self, project_id: int, candidate_fact_id: int, operator: str,
                                note: str = "") -> dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/acceptances",
                             {"candidate_fact_id": candidate_fact_id, "operator": operator, "note": note})

    def delete_project(self, project_id: int) -> Project:
        return Project.from_dict(self._request("DELETE", f"/projects/{project_id}"))

    def create_project(
        self,
        name: str | None = None,
        project_id: int | None = None,
        docker: str | None = None,
        target_url: str | None = None,
        hint: str = "",
        objective: str = "",
        task_type: str = "general",
        crypto: dict[str, Any] | None = None,
    ) -> Project:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if project_id is not None:
            payload["id"] = project_id
        if docker is not None:
            payload["docker"] = docker
        if target_url is not None:
            payload["target_url"] = target_url
        if hint:
            payload["hint"] = hint
        if objective:
            payload["objective"] = objective
        if task_type != "general":
            payload["task_type"] = task_type
        if crypto:
            payload["crypto"] = crypto
        return Project.from_dict(self._request("POST", "/projects", payload))

    def save_project(self, project: Project) -> Project:
        if project.task_type == "crypto" and project.is_completed:
            saved = Project.from_dict(self._request("PUT", f"/projects/{project.id}", project.to_dict(),
                                                   timeout=45, retry=False))
        else:
            saved = Project.from_dict(self._request("PUT", f"/projects/{project.id}", project.to_dict()))
        project.revision = saved.revision
        project.project_uid = saved.project_uid
        return saved

    def upload_input_file(
        self,
        project_id: int,
        path: str | Path,
        *,
        role: str = "input",
        input_id: str | None = None,
        number_radix: int | None = None,
        byte_order: str | None = None,
        byte_length: int | None = None,
    ) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise ValueError(f"input file does not exist: {source}")
        if source.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("input exceeds 32 MiB limit")
        body = source.read_bytes()
        if len(body) > MAX_INPUT_BYTES:
            raise ValueError("input exceeds 32 MiB limit")
        query = {"filename": source.name, "role": role}
        for key, value in (("input_id", input_id), ("number_radix", number_radix),
                           ("byte_order", byte_order), ("byte_length", byte_length)):
            if value is not None:
                query[key] = str(value)
        reference = self._request(
            "POST", f"/projects/{project_id}/inputs?{urlencode(query)}",
            raw_data=body, content_type="application/octet-stream", retry=False,
        )
        if reference["sha256"] != hashlib.sha256(body).hexdigest() or reference["size_bytes"] != len(body):
            raise ProjectAPIError("uploaded input hash or size differs from local file")
        return reference

    def update_project_status(self, project_id: int, status: str) -> Project:
        if status in {"stoped", "stopped"}:
            return Project.from_dict(self._request("PATCH", f"/projects/{project_id}/status", {"status": status},
                                                   timeout=45, retry=False))
        return Project.from_dict(self._request("PATCH", f"/projects/{project_id}/status", {"status": status}))

    def activate_project(self, project_id: int) -> Project:
        return self.update_project_status(project_id, "active")

    def add_fact(self, project_id: int, description: str, fact_id: int | None = None, crypto: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"description": description}
        if fact_id is not None:
            payload["id"] = fact_id
        if crypto:
            payload["crypto"] = crypto
        return self._request("POST", f"/projects/{project_id}/facts", payload)

    def add_intent(
        self,
        project_id: int,
        description: str = "",
        from_fact_id: int | None = None,
        to_fact_id: int | None = None,
        intent_id: int | None = None,
        crypto: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"description": description}
        if intent_id is not None:
            payload["id"] = intent_id
        if from_fact_id is not None:
            payload["from"] = from_fact_id
        if to_fact_id is not None:
            payload["to"] = to_fact_id
        if crypto:
            payload["crypto"] = crypto
        return self._request("POST", f"/projects/{project_id}/intents", payload)

    def list_project_logs(self, project_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._request("GET", f"/projects/{project_id}/logs?limit={limit}")

    def clear_project_logs(self, project_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/projects/{project_id}/logs")

    def add_project_log(
        self,
        project_id: int,
        message: str,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/projects/{project_id}/logs",
            {
                "level": level,
                "message": message,
                "metadata": metadata or {},
            },
        )

    def get_project_run_status(self, project_id: int) -> dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}/run")

    def start_project_run(
        self,
        project_id: int,
        interval_seconds: float = 3.0,
        max_steps: int | None = None,
        max_workers: int = 4,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/projects/{project_id}/run",
            {
                "interval_seconds": interval_seconds,
                "max_steps": max_steps,
                "max_workers": max_workers,
            },
        )

    def stop_project_run(self, project_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/projects/{project_id}/run", timeout=45, retry=False)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *,
        raw_data: bytes | None = None, content_type: str = "application/json", retry: bool = True,
        timeout: float = 10,
    ) -> Any:
        body = raw_data if raw_data is not None else (None if payload is None else json.dumps(payload).encode("utf-8"))
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": content_type},
        )
        if self.auth_token:
            request.add_header("X-Echo-Agent-Token", self.auth_token)
        request.add_header("X-Echo-Agent-Dispatcher", self.dispatcher_id)
        last_error: URLError | None = None
        for attempt in range(3 if retry else 1):
            try:
                with urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raw = exc.read().decode("utf-8")
                try:
                    data = json.loads(raw)
                    detail = data.get("error", data.get("detail", raw))
                except json.JSONDecodeError:
                    detail = raw
                raise ProjectAPIError(f"{method} {path} failed: {exc.code} {detail}") from exc
            except URLError as exc:
                last_error = exc
                if not retry or attempt >= 2 or not _is_transient_url_error(exc):
                    break
                time.sleep(0.2 * (attempt + 1))
        reason = last_error.reason if last_error is not None else "request failed"
        raise ProjectAPIError(f"{method} {path} failed: {reason}") from last_error


def _is_transient_url_error(exc: URLError) -> bool:
    reason = exc.reason
    text = f"{exc} {reason} {getattr(reason, 'errno', '')}".lower()
    markers = (
        "timed out",
        "timeout",
        "connection refused",
        "connection reset",
        "10048",
        "address already in use",
        "只允许使用一次",
    )
    return any(marker in text for marker in markers)
