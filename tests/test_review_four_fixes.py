from __future__ import annotations

import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from fastapi.testclient import TestClient

from echo_agent.crypto_dedup import plan_crypto_intents, validate_checkpoint_resume
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.client import ProjectClient, ProjectAPIError
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, WorkerResult
from echo_agent.models import IntentOutcome, IntentStatus, Project, ProjectStatus
from echo_agent.server.dispatcher_runner import StoreDispatcherClient
from echo_agent.storage import ProjectConflictError, SQLiteProjectStore
from tests.test_crypto_completion_closure import Runtime
from tests.test_crypto_s07 import MemoryClient, prior, project, spec
from tests.test_review_six_fixes import make_app


class IdentityTests(unittest.TestCase):
    def test_deleted_ids_are_not_reused_even_after_reopening_the_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = SQLiteProjectStore(path)
            old = store.create_project()
            store.delete_project(old.id)
            store = SQLiteProjectStore(path)
            new = store.create_project()
            self.assertGreater(new.id, old.id)
            self.assertNotEqual(new.project_uid, old.project_uid)
            with self.assertRaises(ProjectConflictError):
                store.create_project(project_id=old.id)
            with self.assertRaises(ProjectConflictError):
                store.save_project(Project(id=old.id, name="unversioned stale import"))
            with self.assertRaises(ProjectConflictError):
                store.save_project(old)
            self.assertEqual(len(store.list_projects()), 1)

    def test_same_revision_from_another_incarnation_is_rejected_before_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            old = store.create_project(name="old")
            store.delete_project(old.id)
            new = store.create_project(name="new")
            self.assertEqual(old.revision, new.revision)
            stale = Project.from_dict(old.to_dict())
            stale.id = new.id
            runtime = Runtime()
            with self.assertRaises(ProjectConflictError):
                StoreDispatcherClient(store, runtime).save_project(stale)
            self.assertEqual(runtime.transitions, [])
            with TestClient(make_app(store, runtime)) as http:
                response = http.put(f"/projects/{new.id}", json=stale.to_dict())
                self.assertEqual(response.status_code, 409, response.text)
                for uid in (None, "a" * 32):
                    payload = new.to_dict()
                    payload["project_uid"] = uid
                    self.assertEqual(http.put(f"/projects/{new.id}", json=payload).status_code, 409)
            self.assertEqual(store.get_project(new.id).name, "new")
            self.assertEqual(runtime.transitions, [])

    def test_identity_is_stable_across_saves_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript("CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,docker TEXT); INSERT INTO projects VALUES(18,'legacy','inactive',NULL);")
            finally:
                conn.close()
            store = SQLiteProjectStore(path)
            item = store.get_project(18)
            uid = item.project_uid
            self.assertEqual(len(uid), 32)
            self.assertEqual(Project.from_dict(item.to_dict()).project_uid, uid)
            item.add_fact("preserve graph")
            store.save_project(item)
            store = SQLiteProjectStore(path)
            self.assertEqual(store.get_project(18).project_uid, uid)
            self.assertEqual(len(store.get_project(18).facts), 1)
            self.assertGreater(store.create_project().id, 18)

    def test_failed_creation_rolls_back_the_id_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with patch.object(store, "_insert_initial_facts", side_effect=RuntimeError("write failed")):
                with self.assertRaises(RuntimeError):
                    store.create_project(project_id=12)
            self.assertEqual(store.create_project(project_id=12).id, 12)


class StopTests(unittest.TestCase):
    def test_stop_transport_allows_cleanup_time_without_repeating_the_command(self):
        client = ProjectClient("http://unused.test")
        for command in (lambda: client.update_project_status(1, "stoped"),
                        lambda: client.update_project_status(1, "stopped"),
                        lambda: client.stop_project_run(1)):
            with patch("echo_agent.dispatcher.client.urlopen", side_effect=URLError("timed out")) as request:
                with self.assertRaises(ProjectAPIError): command()
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs["timeout"], 45)

    def test_both_stop_routes_cancel_the_real_background_loop(self):
        for route in ("status", "run"):
            for task_type in ("general", "crypto"):
                with self.subTest(route=route, task_type=task_type), tempfile.TemporaryDirectory() as directory:
                    store = SQLiteProjectStore(Path(directory) / "state.db")
                    item = store.create_project(task_type=task_type)
                    item.activate()
                    store.save_project(item)
                    entered, cancelled = threading.Event(), threading.Event()
                    calls = []

                    class Worker:
                        def execute_intent(self, project, intent, mode=None):
                            calls.append(intent.id)
                            entered.set()
                            if not cancelled.wait(3):
                                raise RuntimeError("test cancellation was not delivered")
                            return WorkerResult(outcome="verified", fact_description="late observation", crypto={
                                "exit_reason": "completed", "fact": {"claim_status": "observed"}})

                    def factory(*args, **kwargs):
                        kwargs["stop_event"] = cancelled
                        return Dispatcher(*args, worker=Worker(), **kwargs)

                    app = make_app(store)
                    runner = app.state.dispatcher_runner
                    with TestClient(app) as http, patch("echo_agent.server.dispatcher_runner.Dispatcher", side_effect=factory):
                        response = http.post(f"/projects/{item.id}/run", json={"interval_seconds": 0.01})
                        self.assertEqual(response.status_code, 202)
                        try:
                            self.assertTrue(entered.wait(3))
                            if route == "status":
                                response = http.patch(f"/projects/{item.id}/status", json={"status": "stoped"})
                            else:
                                response = http.delete(f"/projects/{item.id}/run")
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertTrue(cancelled.is_set())
                            self.assertTrue(runner._runs[item.id].stop_event.is_set())
                            self.assertFalse(runner.status(item.id).running)
                            self.assertEqual(store.get_project(item.id).status, ProjectStatus.STOPED)
                            self.assertEqual(len(calls), 1)
                            # A duplicate stop is harmless and cannot restart the worker.
                            response = http.patch(f"/projects/{item.id}/status", json={"status": "stopped"})
                            self.assertEqual(response.status_code, 200, response.text)
                        finally:
                            cancelled.set()
                            runner._runs[item.id].thread.join(4)

    def test_status_stop_preserves_invalid_transition_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            item = store.create_project()
            with TestClient(make_app(store)) as http:
                self.assertEqual(http.patch(f"/projects/{item.id}/status", json={"status": "stoped"}).status_code, 400)
                self.assertEqual(http.patch("/projects/999/status", json={"status": "stoped"}).status_code, 404)
            self.assertEqual(store.get_project(item.id).status, ProjectStatus.INACTIVE)


def checkpoint_case():
    item = project()
    old = prior(item, spec(), status="failed", reason="timeout", covered={})
    checkpoint = {"path": f"artifacts/intents/{old.id}/state.bin", "sha256": "b" * 64,
                  "size_bytes": 3, "producer_intent_id": old.id}
    item.crypto["executions"][-1]["checkpoint_ref"] = checkpoint
    return item, old, checkpoint


class CheckpointTests(unittest.TestCase):
    def test_same_computation_can_resume_with_a_recorded_checkpoint(self):
        item, old, checkpoint = checkpoint_case()
        plans = plan_crypto_intents(item, spec(resume=checkpoint))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["continuation"]["prior_intent_id"], old.id)
        current = item.add_intent("continue", from_fact_id=2, crypto=plans[0])
        calls = []

        class Worker:
            def execute_intent(self, *args):
                calls.append(True)
                return WorkerResult(outcome="empty", crypto={"exit_reason": "completed"})

        client = MemoryClient(item)
        client.verify_crypto_artifact = lambda *args: None
        result = Dispatcher(client, worker=Worker())._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
        self.assertEqual(calls, [True])
        self.assertEqual(result.crypto["exit_reason"], "completed")

    def test_changed_computations_reject_old_checkpoints_during_planning(self):
        for change in ("premise", "method", "params", "scope", "input", "missing_snapshot", "missing_input_snapshot", "unrecorded", "wrong_hash"):
            with self.subTest(change=change):
                item, old, checkpoint = checkpoint_case()
                metadata = spec(resume=checkpoint)
                if change == "premise": item.facts[2].crypto["typed_value"]["value"] = "77"
                elif change == "method": metadata["method"] = "factor.other"
                elif change == "params": metadata["params"] = {"stride": 3}
                elif change == "scope": metadata["scope"]["end"] = 20
                elif change == "input": item.crypto["inputs"][0]["sha256"] = "c" * 64
                elif change == "missing_snapshot": old.crypto.pop("premise_versions")
                elif change == "missing_input_snapshot": old.crypto.pop("input_versions")
                elif change == "unrecorded": item.crypto["executions"].clear()
                elif change == "wrong_hash": metadata["resume_from"] = {**checkpoint, "sha256": "c" * 64}
                with self.assertRaisesRegex(ValueError, "checkpoint"):
                    plan_crypto_intents(item, metadata)
                # New conditions can still be computed from scratch.
                if change in {"premise", "method", "params", "scope", "input", "missing_snapshot"}:
                    fresh = copy.deepcopy(metadata)
                    fresh.pop("resume_from")
                    self.assertTrue(plan_crypto_intents(item, fresh))

    def test_directly_created_intent_cannot_bypass_checkpoint_compatibility(self):
        item, _, checkpoint = checkpoint_case()
        item.facts[2].crypto["typed_value"]["value"] = "77"
        current = item.add_intent("wrong continuation", from_fact_id=2, crypto=spec(resume=checkpoint))

        class NeverRun:
            def execute_intent(self, *args): raise AssertionError("incompatible checkpoint reached worker")

        client = MemoryClient(item)
        client.verify_crypto_artifact = lambda *args: None
        result = Dispatcher(client, worker=NeverRun())._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
        self.assertEqual(result.crypto["exit_reason"], "tool_error")
        self.assertIn("checkpoint computation changed", result.detail)

    def test_queued_resume_is_invalidated_when_producer_snapshot_is_unknown(self):
        item, old, checkpoint = checkpoint_case()
        current = item.add_intent("continue", from_fact_id=2, crypto=spec(resume=checkpoint))
        old.crypto.pop("premise_versions")
        with self.assertRaises(ValueError): validate_checkpoint_resume(item, current.crypto)

    def test_equivalent_numeric_range_keeps_a_valid_checkpoint_usable(self):
        item, _, checkpoint = checkpoint_case()
        metadata = spec(resume=checkpoint)
        metadata["scope"] = {"start": {"type": "integer", "radix": 16, "value": "2"}, "end": 10}
        self.assertEqual(len(plan_crypto_intents(item, metadata)), 1)

    def test_production_client_rechecks_real_checkpoint_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            item, old, checkpoint = checkpoint_case()
            client = StoreDispatcherClient(store, Runtime())
            files = client.crypto_service.files
            files.ensure_layout(item.id)
            target = files.project_root(item.id) / checkpoint["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"saved state")
            checkpoint = files.describe_artifact(item.id, old.id, checkpoint["path"])
            item.crypto["executions"][-1]["checkpoint_ref"] = checkpoint
            current = item.add_intent("continue", from_fact_id=2, crypto=plan_crypto_intents(item, spec(resume=checkpoint))[0])
            store.save_project(item)
            calls = []

            class Worker:
                def execute_intent(self, *args):
                    calls.append(True)
                    return WorkerResult(crypto={"exit_reason": "completed"})

            dispatcher = Dispatcher(client, worker=Worker())
            result = dispatcher._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
            self.assertEqual(result.crypto["exit_reason"], "completed")
            self.assertEqual(calls, [True])
            target.write_bytes(b"other state")
            result = dispatcher._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
            self.assertEqual(result.crypto["exit_reason"], "tool_error")
            self.assertEqual(calls, [True])


class RetryCancellationTests(unittest.TestCase):
    def test_cancelled_retry_cannot_be_recorded_as_success_or_range_exhaustion(self):
        for reason in ("completed", "range_exhausted"):
            with self.subTest(reason=reason):
                item = project()
                current = item.add_intent("search", from_fact_id=2, crypto=spec())
                current.mark_running()
                calls = []

                class Worker:
                    def execute_intent(self, *args):
                        calls.append(True)
                        if len(calls) == 1: raise RuntimeError("connection reset")
                        dispatcher.request_stop()
                        return WorkerResult(outcome="verified", fact_description="late result", crypto={
                            "exit_reason": reason, "covered_scope": current.crypto["scope"],
                            "fact": {"claim_status": "observed"}})

                dispatcher = Dispatcher(MemoryClient(item), worker=Worker())
                result = dispatcher._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
                Dispatcher._apply_intent_result(item, IntentExecutionResult(current, result))
                self.assertEqual(len(calls), 2)
                self.assertEqual(current.status, IntentStatus.FAILED)
                self.assertEqual(current.outcome, IntentOutcome.ERROR)
                self.assertEqual(item.crypto["executions"][-1]["exit_reason"], "cancelled")

    def test_retry_rechecks_premises_before_calling_worker(self):
        item = project()
        current = item.add_intent("search", from_fact_id=2, crypto=spec())
        calls = []

        class Worker:
            def execute_intent(self, *args):
                calls.append(True)
                item.facts[2].crypto["typed_value"]["value"] = "77"
                raise RuntimeError("connection reset")

        result = Dispatcher(MemoryClient(item), worker=Worker())._execute_with_retry(item, current, DispatchMode.EXPLORE, "review")
        self.assertEqual(calls, [True])
        self.assertEqual(result.crypto["exit_reason"], "tool_error")
        self.assertIn("premises changed", result.detail)

    def test_cancelled_verification_does_not_save_a_passing_record(self):
        item = project()
        current = item.add_intent("check", from_fact_id=2, crypto={**spec(), "method": "verify.candidate"})
        current.mark_running()
        result = WorkerResult(outcome="error", crypto={"exit_reason": "cancelled", "verification": {
            "status": "pass", "method": "crypto-verify-v3", "candidate_fact_id": 2}})
        Dispatcher._apply_intent_result(item, IntentExecutionResult(current, result))
        self.assertEqual(current.status, IntentStatus.FAILED)
        self.assertNotIn("verifications", item.crypto)


if __name__ == "__main__":
    unittest.main()
