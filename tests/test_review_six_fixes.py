from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_contract import fact_crypto
from echo_agent.crypto_dedup import plan_crypto_intents
from echo_agent.crypto_verify import VERSION, verify_candidate
from echo_agent.dispatcher.client import ProjectAPIError, ProjectClient
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import IntentExecutionResult, WorkerResult
from echo_agent.models import FactKind, IntentStatus, Project, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.server.dispatcher_runner import StoreDispatcherClient
from echo_agent.storage import ProjectConflictError, SQLiteProjectStore
from tests.test_crypto_completion_closure import Runtime, add_candidate
from tests.test_crypto_s09 import project_in


def make_app(store, runtime=None):
    runtime = runtime or Runtime()
    with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
        return create_app(store)


class SourceAndInputBoundaryTests(unittest.TestCase):
    def test_dynamic_sources_never_pass_independent_verification(self):
        suffixes = ["globals()['c']=14", "exec('c=14')", "from other import c", "del c",
                    "if True:\n c=14", "def f():\n pass", "print(c)", "raise RuntimeError('no execution')"]
        for suffix in suffixes:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                project, files, _ = project_in(Path(directory), ("n=55\ne=3\nc=13\n" + suffix + "\n").encode())
                record = verify_candidate(project, add_candidate(project).id, files)
                self.assertEqual(record["status"], "inconclusive", record)
                self.assertEqual(record["method"], VERSION)
                self.assertIn("parameter-only", record["checks"]["reason"])

    def test_literal_parameter_files_still_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = b'"""RSA instance parameters"""\n# no code execution\nn: int = 0x37\ne=3\nc=13\n'
            project, files, _ = project_in(Path(directory), raw)
            self.assertEqual(verify_candidate(project, add_candidate(project).id, files)["status"], "pass")

    def test_completed_project_rejects_upload_before_writing_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, files, path = project_in(root)
            project.crypto["verifications"] = [verify_candidate(project, add_candidate(project).id, files)]
            project.status = ProjectStatus.COMPLETED
            project.facts[1].description = "result\nanswer: 7"
            store = SQLiteProjectStore(root / "state.db")
            store.save_project(project)
            before = sorted(p.name for p in path.parent.iterdir())
            with TestClient(make_app(store)) as http:
                response = http.post("/projects/7/inputs?filename=extra.txt", content=b"n=77\n",
                                     headers={"Content-Type": "application/octet-stream"})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(len(store.get_project(7).crypto["inputs"]), 1)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), before)

    def test_registration_rechecks_status_in_its_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, _, _ = project_in(root)
            store = SQLiteProjectStore(root / "state.db")
            store.save_project(project)
            reference = {**project.crypto["inputs"][0], "id": "new-file"}
            for status in (ProjectStatus.RUNNING, ProjectStatus.COMPLETED):
                current = store.get_project(7)
                current.status = status
                store.save_project(current)
                with self.subTest(status=status), self.assertRaises(ValueError):
                    store.add_crypto_input(7, reference)
            self.assertEqual(len(store.get_project(7).crypto["inputs"]), 1)


class CreationAndRevisionTests(unittest.TestCase):
    def test_invalid_names_do_not_commit_a_bad_row(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with TestClient(make_app(store), raise_server_exceptions=False) as http:
                for name in ("", " ", "\t\n"):
                    for extra in ({}, {"id": 99}):
                        with self.subTest(name=name, extra=extra):
                            self.assertEqual(http.post("/projects", json={"name": name, **extra}).status_code, 400)
                response = http.get("/projects")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])
                self.assertEqual(http.post("/projects", json={}).status_code, 201)

    def test_creation_rolls_back_if_initial_graph_insertion_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with patch.object(store, "_insert_initial_facts", side_effect=RuntimeError("write failed")):
                with self.assertRaises(RuntimeError):
                    store.create_project(name="valid-name")
            self.assertEqual(store.list_projects(), [])

    def test_stale_snapshot_is_rejected_before_lifecycle_and_data_is_preserved(self):
        for task_type in ("general", "crypto"):
            with self.subTest(task_type=task_type), tempfile.TemporaryDirectory() as directory:
                store = SQLiteProjectStore(Path(directory) / "state.db")
                project = store.create_project(task_type=task_type)
                runtime = Runtime()
                client = StoreDispatcherClient(store, runtime)
                first, stale = client.get_project(project.id), client.get_project(project.id)
                first.add_fact("writer-A")
                client.save_project(first)
                transitions = len(runtime.transitions)
                stale.add_fact("writer-B")
                with self.assertRaises(ProjectConflictError):
                    client.save_project(stale)
                self.assertEqual(len(runtime.transitions), transitions)
                descriptions = [fact.description for fact in store.get_project(project.id).facts.values()]
                self.assertIn("writer-A", descriptions)
                self.assertNotIn("writer-B", descriptions)
                fresh = client.get_project(project.id)
                fresh.add_fact("writer-B")
                client.save_project(fresh)
                self.assertEqual(len(store.get_project(project.id).facts), 4)

    def test_parallel_http_writes_get_one_success_and_one_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project()
            project.activate()
            store.save_project(project)
            project.start()
            payload = project.to_dict()
            runtime = Runtime()
            statuses = []
            barrier = threading.Barrier(2)
            with TestClient(make_app(store, runtime)) as http:
                def save(owner):
                    barrier.wait(timeout=3)
                    result = http.put(f"/projects/{project.id}", json=payload, headers={"X-Echo-Agent-Dispatcher": owner})
                    statuses.append(result.status_code)
                threads = [threading.Thread(target=save, args=(str(i) * 32,)) for i in (1, 2)]
                for thread in threads: thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(statuses), [200, 409])
            self.assertEqual(len(runtime.transitions), 1)

    def test_missing_revision_cannot_replace_an_existing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project()
            payload = project.to_dict()
            payload.pop("revision")
            payload["facts"] = []
            with TestClient(make_app(store)) as http:
                self.assertEqual(http.put(f"/projects/{project.id}", json=payload).status_code, 409)
            self.assertEqual(len(store.get_project(project.id).facts), 2)

    def test_deleted_snapshot_cannot_resurrect_a_project(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project()
            store.delete_project(project.id)
            with self.assertRaises(ProjectConflictError): store.save_project(project)
            self.assertEqual(store.list_projects(), [])

    def test_http_client_updates_the_callers_revision_after_every_save(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            with TestClient(make_app(store)) as http:
                client = ProjectClient("http://unused.test")
                def request(method, path, payload=None, **kwargs):
                    result = http.request(method, path, json=payload, headers={"X-Echo-Agent-Dispatcher": client.dispatcher_id})
                    if result.status_code >= 400: raise ProjectAPIError(result.text)
                    return result.json()
                client._request = request
                project = client.create_project()
                project.add_fact("first")
                client.save_project(project)
                self.assertEqual(project.revision, 1)
                project.add_fact("second")
                client.save_project(project)
                self.assertEqual(project.revision, 2)

    def test_legacy_database_migrates_revision_without_changing_existing_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as conn:
                conn.executescript("CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,docker TEXT,target_url TEXT,hint TEXT NOT NULL DEFAULT '',objective TEXT NOT NULL DEFAULT ''); INSERT INTO projects(id,name,status) VALUES(1,'legacy','inactive');")
            conn.close()
            store = SQLiteProjectStore(path)
            project = store.get_project(1)
            self.assertEqual(project.revision, 0)
            self.assertIsNone(project.run_owner)
            store.save_project(project)
            self.assertEqual(project.revision, 1)


class DispatcherOwnershipTests(unittest.TestCase):
    def test_another_dispatcher_cannot_recover_or_execute_an_active_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project()
            project.activate()
            store.save_project(project)
            entered, release = threading.Event(), threading.Event()
            calls, errors = [], []
            class Worker:
                def execute_intent(self, project, intent, mode=None):
                    calls.append(intent.id)
                    entered.set()
                    if not release.wait(5): raise RuntimeError("test did not release worker")
                    return WorkerResult(outcome="verified", fact_description="real result")
            first = Dispatcher(StoreDispatcherClient(store, Runtime()), worker=Worker())
            second = Dispatcher(StoreDispatcherClient(store, Runtime()), worker=Worker())
            def run():
                try: first.run_once(project.id, release_project=False)
                except Exception as exc: errors.append(exc)
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(ProjectConflictError): second.run_once(project.id)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            first._release_project_if_running(project.id)
            self.assertEqual(store.get_project(project.id).status, ProjectStatus.STOPED)

    def test_foreign_writes_are_blocked_until_explicit_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project()
            project.activate()
            store.save_project(project)
            owner = StoreDispatcherClient(store, Runtime())
            project.start()
            owner.save_project(project)
            with TestClient(make_app(store)) as http:
                self.assertNotIn("run_owner", http.get(f"/projects/{project.id}").json())
                self.assertEqual(http.post(f"/projects/{project.id}/facts", json={"description": "foreign"}).status_code, 409)
                self.assertEqual(http.post(f"/projects/{project.id}/intents", json={"description": "foreign"}).status_code, 409)
                self.assertEqual(http.put(f"/projects/{project.id}", json=project.to_dict()).status_code, 409)
                self.assertEqual(http.patch(f"/projects/{project.id}/status", json={"status": "stoped"}).status_code, 200)
                self.assertEqual(http.post(f"/projects/{project.id}/facts", json={"description": "allowed"}).status_code, 201)
            self.assertIsNone(store.get_project(project.id).run_owner)


class ObservationAndPremiseTests(unittest.TestCase):
    def test_from_fact_is_snapshotted_when_explicit_dependencies_are_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            source = project.add_fact("parameter", crypto={"typed_value": {"type": "integer", "radix": 10, "value": "55"}})
            intent = project.add_intent("compute", from_fact_id=source.id, crypto={"method": "factor.trial"})
            self.assertEqual(intent.crypto["input_fact_ids"], [source.id])
            self.assertIn(str(source.id), intent.crypto["premise_versions"])
            from echo_agent.crypto_identity import premises_current
            self.assertTrue(premises_current(project, intent.crypto))
            source.crypto["typed_value"]["value"] = "77"
            self.assertFalse(premises_current(project, intent.crypto))

    def test_malformed_enum_values_become_value_errors(self):
        for key in ("claim_status", "observation_kind"):
            for value in ([], {}, None, 1):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError): fact_crypto({key: value})

    def test_malformed_observation_is_saved_as_an_error_and_intent_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            intent = project.add_intent("inspect", from_fact_id=0, crypto={"method": "source.inspect"})
            intent.mark_running()
            result = WorkerResult(outcome="verified", fact_description="observation", crypto={
                "exit_reason": "completed", "fact": {"claim_status": "observed", "observation_kind": []}})
            Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
            self.assertNotEqual(intent.status, IntentStatus.RUNNING)
            self.assertIn("observation_kind", intent.note)
            self.assertEqual(project.crypto["executions"][-1]["exit_reason"], "tool_error")

    def test_malformed_api_fact_returns_400_and_preserves_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(task_type="crypto")
            with TestClient(make_app(store), raise_server_exceptions=False) as http:
                response = http.post(f"/projects/{project.id}/facts", json={"description": "bad", "crypto": {"observation_kind": []}})
                self.assertEqual(response.status_code, 400)
            self.assertEqual(len(store.get_project(project.id).facts), 2)

    def test_fact_correction_allows_recomputation_but_unchanged_fact_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            source = project.add_fact("derived parameter", crypto={"typed_value": {"type": "integer", "radix": 10, "value": "55"}})
            spec = {"method": "factor.trial", "params": {}, "input_ids": ["input-1"],
                    "input_fact_ids": [source.id], "scope": {"limit": 100}}
            old = project.add_intent("compute", from_fact_id=source.id, crypto=spec)
            old.mark_running(); old.mark_completed()
            stored_snapshot = copy.deepcopy(old.crypto["premise_versions"])
            self.assertEqual(plan_crypto_intents(project, spec), [])
            source.crypto["typed_value"]["value"] = "77"
            self.assertEqual(len(plan_crypto_intents(project, spec)), 1)
            self.assertEqual(old.crypto["premise_versions"], stored_snapshot)
            self.assertEqual(Project.from_dict(project.to_dict()).intents[old.id].crypto["premise_versions"], stored_snapshot)

    def test_transitive_changes_and_artifact_reference_changes_invalidate_premises(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            parent = project.add_fact("parent", crypto={"typed_value": {"type": "integer", "radix": 10, "value": "55"}})
            source = project.add_fact("derived", crypto={"depends_on_fact_ids": [parent.id], "artifact_refs": [
                {"path": "artifacts/intents/0/result.txt", "size_bytes": 1, "sha256": "a" * 64}]})
            spec = {"method": "factor.trial", "params": {}, "input_fact_ids": [source.id], "scope": {"limit": 100}}
            old = project.add_intent("compute", from_fact_id=source.id, crypto=spec)
            old.mark_running(); old.mark_completed()
            parent.crypto["typed_value"]["value"] = "77"
            self.assertEqual(len(plan_crypto_intents(project, spec)), 1)
            parent.crypto["typed_value"]["value"] = "55"
            source.crypto["artifact_refs"][0]["sha256"] = "b" * 64
            self.assertEqual(len(plan_crypto_intents(project, spec)), 1)

    def test_stale_queued_intent_is_not_executed_with_corrected_premises(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            source = project.add_fact("parameter", crypto={"typed_value": {"type": "integer", "radix": 10, "value": "55"}})
            intent = project.add_intent("compute", from_fact_id=source.id, crypto={"method": "factor.trial", "input_fact_ids": [source.id]})
            source.crypto["typed_value"]["value"] = "77"
            class Worker:
                def execute_intent(self, *args): raise AssertionError("stale intent must not run")
            class Client:
                def add_project_log(self, *args, **kwargs): pass
            result = Dispatcher(Client(), worker=Worker())._execute_with_retry(project, intent, None, "test")
            self.assertEqual(result.crypto["exit_reason"], "tool_error")
            self.assertIn("premises changed", result.detail)

    def test_legacy_records_without_a_snapshot_do_not_claim_new_premises_were_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            source = project.add_fact("parameter")
            spec = {"method": "factor.trial", "params": {}, "input_fact_ids": [source.id], "scope": {"limit": 100}}
            old = project.add_intent("compute", from_fact_id=source.id, crypto=spec)
            old.mark_running(); old.mark_completed()
            old.crypto.pop("premise_versions")
            self.assertEqual(len(plan_crypto_intents(project, spec)), 1)


if __name__ == "__main__": unittest.main()
