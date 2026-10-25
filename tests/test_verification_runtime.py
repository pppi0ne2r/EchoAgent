"""R06: verification deadlines, cancellation, concurrency limits and records."""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_service import CryptoProjectService
from echo_agent.crypto_verification import (DEFAULT_VERIFY_SECONDS, VerificationBusy,
                                            VerificationRegistry)
from echo_agent.crypto_verify import verify_candidate
from echo_agent.dispatcher.client import ProjectClient
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import DispatchMode
from echo_agent.models import Project, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore
from tests.test_crypto_schemes import manifest_project, Runtime


def _project(root: Path):
    digest = hashlib.sha256(b"x").hexdigest()
    return manifest_project(root, {"algorithm": "hash", "hash": "sha256", "digest": digest}, "x")


class RegistryTests(unittest.TestCase):
    def test_same_project_is_exclusive_and_cancellable(self) -> None:
        registry = VerificationRegistry(max_concurrent=2)
        lease = registry.acquire(7, "v1", 5)
        with self.assertRaises(VerificationBusy):
            registry.acquire(7, "v2", 5)
        self.assertTrue(registry.cancel("v1"))
        self.assertTrue(lease.cancel_event.is_set())
        self.assertFalse(registry.cancel("missing"))
        registry.release(7, "v1")
        self.assertEqual(registry.active(), [])
        registry.release(7, registry.acquire(7, "v3", 5).verification_id)

    def test_global_limit(self) -> None:
        registry = VerificationRegistry(max_concurrent=1)
        registry.acquire(7, "a", 5)
        with self.assertRaises(VerificationBusy):
            registry.acquire(8, "b", 5)
        registry.release(7, "a")
        registry.release(8, registry.acquire(8, "b", 5).verification_id)

    def test_timeout_bounds(self) -> None:
        registry = VerificationRegistry()
        with self.assertRaises(ValueError):
            registry.acquire(7, "bad", 0)
        with self.assertRaises(ValueError):
            registry.acquire(7, "bad", 10_000)


class VerificationRecordTests(unittest.TestCase):
    def test_record_reports_backend_and_elapsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = _project(Path(directory))
            record = verify_candidate(project, candidate.id, files,
                                      verification_id="abc", timeout_seconds=5)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual(record["verification_id"], "abc")
            self.assertEqual(record["timeout_seconds"], 5)
            self.assertEqual(record["backend"], "host")
            self.assertEqual(record["terminated_reason"], "completed")
            self.assertGreaterEqual(record["elapsed_ms"], 0)

    def test_expired_deadline_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = _project(Path(directory))
            record = verify_candidate(project, candidate.id, files,
                                      deadline=time.monotonic() - 1)
            self.assertEqual(record["status"], "inconclusive")
            self.assertEqual(record["terminated_reason"], "timeout")
            self.assertIn("timed out", record["checks"]["reason"])
            self.assertGreaterEqual(record["elapsed_ms"], 0)

    def test_cancelled_event_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = _project(Path(directory))
            event = threading.Event()
            event.set()
            record = verify_candidate(project, candidate.id, files, cancel_event=event)
            self.assertEqual(record["status"], "inconclusive")
            self.assertEqual(record["terminated_reason"], "cancelled")
            self.assertIn("cancelled", record["checks"]["reason"])

    def test_service_uses_the_registry_and_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, files, candidate = _project(Path(directory))
            registry = VerificationRegistry()
            service = CryptoProjectService(files, registry=registry)
            record = service.verify(project, candidate.id, verification_id="svc", timeout_seconds=2)
            self.assertEqual(record["verification_id"], "svc")
            self.assertEqual(record["timeout_seconds"], 2)
            self.assertEqual(registry.active(), [])
            self.assertFalse(service.cancel("svc"))


class VerificationClientTests(unittest.TestCase):
    def test_verify_and_cancel_requests(self) -> None:
        client = ProjectClient("http://unused.test")
        calls = []

        def fake_request(method, path, payload=None, **kwargs):
            calls.append((method, path, kwargs))
            return {"status": "pass"}

        with patch.object(client, "_request", side_effect=fake_request):
            client.verify_crypto_candidate(7, 2, verification_id="vid", timeout_seconds=10)
            client.cancel_crypto_verification(7, "vid")

        self.assertIn("verification_id=vid", calls[0][1])
        self.assertIn("timeout_seconds=10", calls[0][1])
        self.assertEqual(calls[0][2]["timeout"], 15)
        self.assertFalse(calls[0][2]["retry"])
        self.assertEqual(calls[1][1], "/projects/7/verifications/vid/cancel")

    def test_default_deadline_is_sent(self) -> None:
        client = ProjectClient("http://unused.test")
        with patch.object(client, "_request", return_value={}) as request:
            client.verify_crypto_candidate(7, 2)
        self.assertIn(f"timeout_seconds={DEFAULT_VERIFY_SECONDS}", request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs["timeout"], DEFAULT_VERIFY_SECONDS + 5)


class VerificationHttpTests(unittest.TestCase):
    def test_verify_accepts_id_and_cancel_is_an_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, _, candidate = _project(root)
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store)
            with TestClient(app) as http:
                response = http.post(
                    f"/projects/7/candidates/{candidate.id}/verify"
                    "?verification_id=http-vid&timeout_seconds=5"
                )
                self.assertEqual(response.status_code, 200, response.text)
                record = response.json()
                self.assertEqual(record["verification_id"], "http-vid")
                self.assertEqual(record["timeout_seconds"], 5)
                self.assertEqual(record["terminated_reason"], "completed")

                cancel = http.post("/projects/7/verifications/http-vid/cancel")
                self.assertEqual(cancel.status_code, 200)
                self.assertFalse(cancel.json()["cancelled"])

    def test_busy_verification_returns_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, _, candidate = _project(root)
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store)
            registry = app.state.crypto_service.registry
            registry.acquire(7, "held", 5)
            try:
                with TestClient(app) as http:
                    response = http.post(f"/projects/7/candidates/{candidate.id}/verify")
                    self.assertEqual(response.status_code, 409, response.text)
            finally:
                registry.release(7, "held")


class _DummyWorker:
    def execute_intent(self, *args, **kwargs):
        raise AssertionError("worker must not be used for a verification intent")


class _DummyReasoner:
    def plan(self, *args, **kwargs):
        raise AssertionError("reasoner must not be used")


class _DummyAgent:
    def cancel_running(self) -> None:
        pass


class _BlockingClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[tuple[int, str]] = []

    def verify_crypto_candidate(self, project_id: int, candidate_fact_id: int, *,
                                verification_id: str | None = None,
                                timeout_seconds: float | None = None) -> dict:
        self.started.set()
        self.release.wait(timeout=5)
        return {"status": "pass", "verification_id": verification_id}

    def cancel_crypto_verification(self, project_id: int, verification_id: str) -> dict:
        self.cancelled.append((project_id, verification_id))
        self.release.set()
        return {"cancelled": True}


class DispatcherCancellationTests(unittest.TestCase):
    def test_request_stop_cancels_the_inflight_verification(self) -> None:
        project = Project(id=7, name="stop", status=ProjectStatus.RUNNING, task_type="crypto",
                          crypto={"schema_version": 1, "inputs": []})
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent(
            "verify", from_fact_id=0,
            crypto={"method": "verify.candidate", "params": {"candidate_fact_id": 2}},
        )
        client = _BlockingClient()
        dispatcher = Dispatcher(client, worker=_DummyWorker(), reasoner=_DummyReasoner(),
                                agent=_DummyAgent())
        thread = threading.Thread(
            target=dispatcher._execute_intent,
            args=(project, intent, DispatchMode.EXPLORE),
        )
        thread.start()
        try:
            self.assertTrue(client.started.wait(timeout=2))
            dispatcher.request_stop()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(client.cancelled), 1)
            self.assertEqual(client.cancelled[0][0], 7)
        finally:
            client.release.set()
            thread.join(timeout=2)


class VerificationBudgetTests(unittest.TestCase):
    def test_budget_precedence_and_cap(self) -> None:
        project = Project(id=7, name="b", status=ProjectStatus.ACTIVE, task_type="crypto",
                          crypto={"schema_version": 1, "inputs": [],
                                  "default_budget": {"wall_seconds": 90}})
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("v", from_fact_id=0, crypto={"budget": {"wall_seconds": 120}})
        self.assertEqual(Dispatcher._verification_timeout(project, intent), 120)
        intent.crypto.pop("budget")
        self.assertEqual(Dispatcher._verification_timeout(project, intent), 90)
        project.crypto.pop("default_budget")
        self.assertEqual(Dispatcher._verification_timeout(project, intent), DEFAULT_VERIFY_SECONDS)
        intent.crypto["budget"] = {"wall_seconds": 9000}
        self.assertEqual(Dispatcher._verification_timeout(project, intent), 300)


if __name__ == "__main__":
    unittest.main()
