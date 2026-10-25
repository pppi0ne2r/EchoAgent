from __future__ import annotations

import copy
import json
import tempfile
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from fastapi.testclient import TestClient

from echo_agent.crypto_contract import crypto_goal
from echo_agent.crypto_dedup import plan_crypto_intents
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_service import CryptoProjectService
from echo_agent.crypto_verification import DEFAULT_VERIFY_SECONDS
from echo_agent.crypto_verify import VERSION, answer_is_verified, verify_candidate
from echo_agent.dispatcher.client import ProjectAPIError, ProjectClient
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.prompts import PromptBuilder, _evidence_items
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, ReasonResult, WorkerObservation, WorkerResult
from echo_agent.models import FactKind, IntentOutcome, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.server.dispatcher_runner import StoreDispatcherClient
from echo_agent.storage import SQLiteProjectStore
from tests.test_crypto_s09 import UnusedWorker, project_in


class Runtime:
    def __init__(self):
        self.transitions = []

    def prepare_shared_resources(self):
        pass

    def apply_status_transition(self, project, old_status, log=None):
        self.transitions.append((old_status, project.status))


def add_candidate(project, **candidate):
    return project.add_fact("unverified answer", kind=FactKind.CANDIDATE, crypto={
        "claim_status": "candidate", "source_input_ids": [ref["id"] for ref in project.crypto["inputs"]],
        "candidate": {"scheme": "rsa", "answer": "7", "m": "7", **candidate},
    })


class Complete:
    def plan(self, project):
        return ReasonResult(completed=True, answer="7", next_intents=[])


class CryptoCompletionClosureTests(unittest.TestCase):
    def test_real_http_transport_completes_with_production_client(self):
        import uvicorn
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, _, _ = project_in(root)
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store, auth_token="local-test-only")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started)
                client = ProjectClient(f"http://127.0.0.1:{listener.getsockname()[1]}", auth_token="local-test-only")
                dispatcher = Dispatcher(client=client, worker=UnusedWorker(), reasoner=Complete())
                self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.REASON)
                self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.EXPLORE)
                self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.COMPLETED)
                self.assertEqual(client.get_project(7).status, ProjectStatus.COMPLETED)
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                listener.close()
                self.assertFalse(thread.is_alive())

    def test_http_verification_has_a_longer_deadline_without_automatic_retries(self):
        client = ProjectClient("http://unused.test")
        with patch("echo_agent.dispatcher.client.urlopen", side_effect=URLError("timed out")) as request:
            with self.assertRaises(ProjectAPIError):
                client.verify_crypto_candidate(7, 2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], DEFAULT_VERIFY_SECONDS + 5)

    def test_real_background_and_http_clients_complete_using_the_same_gate(self):
        for entry in ("background", "http"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SQLiteProjectStore(root / "state.db")
                project, _, _ = project_in(root)
                store.save_project(project)
                runtime = Runtime()
                with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
                    app = create_app(store)
                with TestClient(app) as http:
                    if entry == "background":
                        client = StoreDispatcherClient(store, runtime)
                    else:
                        client = ProjectClient("http://unused.test")
                        # Only replace transport. Serialization, routes, service,
                        # persistence and candidate verification remain production code.
                        def request(method, path, payload=None, **kwargs):
                            response = http.request(method, path, json=payload)
                            if response.status_code >= 400:
                                raise ProjectAPIError(response.text)
                            return response.json()
                        client._request = request
                    dispatcher = Dispatcher(client=client, worker=UnusedWorker(), reasoner=Complete())
                    self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.REASON)
                    self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.EXPLORE)
                    record = store.get_project(7).crypto["verifications"][-1]
                    self.assertEqual(record["method"], VERSION)
                    self.assertEqual(record["math_status"], "pass")
                    self.assertTrue(record["checks"]["goal_satisfied"])
                    self.assertEqual(dispatcher.run_once(7).mode, DispatchMode.COMPLETED)
                    self.assertEqual(store.get_project(7).status, ProjectStatus.COMPLETED)

    def test_completion_cannot_bypass_verification_through_put_patch_or_background(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            project, files, _ = project_in(root)
            candidate = add_candidate(project, answer_kind="factors", answer="5,11", p="5", q="11")
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["math_status"], "pass")
            self.assertEqual(record["status"], "fail")
            record["status"] = "pass"  # A fabricated status cannot replace fresh checks.
            project.crypto["verifications"] = [record]
            project.status = ProjectStatus.RUNNING
            project.facts[1].description = "result\nanswer: 5,11"
            store.save_project(project)
            runtime = Runtime()
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
                app = create_app(store)
            with TestClient(app) as http:
                self.assertEqual(http.patch("/projects/7/status", json={"status": "completed"}).status_code, 400)
                completed = copy.deepcopy(project)
                completed.status = ProjectStatus.COMPLETED
                self.assertEqual(http.put("/projects/7", json=completed.to_dict()).status_code, 400)
                with self.assertRaisesRegex(ValueError, "current independent verification"):
                    StoreDispatcherClient(store, runtime).save_project(completed)
                self.assertEqual(store.get_project(7).status, ProjectStatus.RUNNING)
                self.assertEqual(runtime.transitions, [])

    def test_public_status_completion_accepts_a_current_goal_bound_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project, files, _ = project_in(Path(directory))
            candidate = add_candidate(project)
            project.crypto["verifications"] = [verify_candidate(project, candidate.id, files)]
            project.facts[1].description = "result\nanswer: 7"
            project.status = ProjectStatus.RUNNING
            store.save_project(project)
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=Runtime()):
                app = create_app(store)
            with TestClient(app) as http:
                self.assertEqual(http.patch("/projects/7/status", json={"status": "completed"}).status_code, 200)

    def test_goal_or_objective_changes_invalidate_pass_and_allow_fresh_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            project, files, _ = project_in(Path(directory))
            candidate = add_candidate(project)
            record = verify_candidate(project, candidate.id, files)
            project.crypto["verifications"] = [record]
            verifier = lambda fact_id: verify_candidate(project, fact_id, files)
            self.assertTrue(answer_is_verified(project, "7", verifier))
            item = {"crypto": {"method": "verify.candidate", "params": {"candidate_fact_id": candidate.id},
                    "scope": {"input_ids": ["input-1"]}, "input_ids": ["input-1"], "input_fact_ids": [candidate.id]}}
            old_meta = Dispatcher._crypto_plan_metadata(project, item, 0)
            old = project.add_intent("verify", from_fact_id=0, crypto=old_meta)
            old.mark_running()
            old.mark_completed()
            self.assertEqual(plan_crypto_intents(project, old_meta), [])
            project.objective = "new task wording must be re-evaluated"
            self.assertFalse(answer_is_verified(project, "7", verifier))
            new_meta = Dispatcher._crypto_plan_metadata(project, item, 0)
            self.assertEqual(len(plan_crypto_intents(project, new_meta)), 1)
            project.crypto["goal"] = {"answer_kind": "factors"}
            self.assertFalse(answer_is_verified(project, "7", verifier))
            self.assertEqual(verifier(candidate.id)["status"], "fail")

    def test_candidate_change_is_not_hidden_by_verification_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            candidate = add_candidate(project)
            item = {"crypto": {"method": "verify.candidate", "params": {"candidate_fact_id": candidate.id},
                    "scope": {"input_ids": ["input-1"]}, "input_ids": ["input-1"], "input_fact_ids": [candidate.id]}}
            metadata = Dispatcher._crypto_plan_metadata(project, item, 0)
            old = project.add_intent("verify", from_fact_id=0, crypto=metadata)
            old.mark_running()
            old.mark_completed()
            candidate.crypto["candidate"]["p"] = "5"
            candidate.crypto["candidate"]["q"] = "11"
            new_meta = Dispatcher._crypto_plan_metadata(project, item, 0)
            self.assertEqual(len(plan_crypto_intents(project, new_meta)), 1)

    def test_input_tampering_and_manifest_replacement_rejected_by_both_entry_points(self):
        for mode in ("bytes", "manifest"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SQLiteProjectStore(root / "state.db")
                project, _, path = project_in(root)
                store.save_project(project)
                if mode == "bytes":
                    path.write_bytes(b"n=55\ne=3\nc=14\n")
                else:
                    project.crypto["inputs"] = []
                runtime = Runtime()
                with self.assertRaises(ValueError):
                    StoreDispatcherClient(store, runtime).save_project(project)
                with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=runtime):
                    app = create_app(store)
                with TestClient(app) as http:
                    self.assertEqual(http.put("/projects/7", json=project.to_dict()).status_code, 400)
                self.assertEqual(runtime.transitions, [])

    def test_rsa_source_and_separate_ciphertext_are_extracted_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            project, files, source = project_in(Path(directory), b'n: int = 55\ne = 0x3\n')
            ciphertext = source.parent / "ciphertext.txt"
            ciphertext.write_bytes(b"c = 13\n")
            project.crypto["inputs"] = [files.describe_input(7, "input-1", "source", "inputs/rsa.txt"),
                                       files.describe_input(7, "ciphertext", "ciphertext", "inputs/ciphertext.txt")]
            candidate = add_candidate(project)
            record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "pass", record)
            self.assertEqual({entry["input_id"] for entry in record["checks"]["extraction"]}, {"input-1", "ciphertext"})
            self.assertEqual(len(record["input_refs"]), 2)

    def test_unsupported_or_incomplete_inputs_are_not_mathematical_failures(self):
        cases = [b"q=1342261\nn=1031\n", b"n=5*11\ne=3\nc=13\n",
                 b"n=55\nn=77\ne=3\nc=13\n", b"\xff\x00", b"n=True\ne=3\nc=13\n",
                 b'{"n":55,"n":77,"e":3,"c":13}']
        for raw in cases:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                project, files, _ = project_in(Path(directory), raw)
                candidate = add_candidate(project)
                record = verify_candidate(project, candidate.id, files)
                self.assertEqual(record["status"], "inconclusive", record)
                self.assertTrue(record["checks"]["reason"])

    def test_conflicting_multi_input_parameters_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project, files, path = project_in(Path(directory))
            (path.parent / "other.txt").write_bytes(b"n=77\n")
            project.crypto["inputs"].append(files.describe_input(7, "other", "parameters", "inputs/other.txt"))
            record = verify_candidate(project, add_candidate(project).id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("conflicting", record["checks"]["reason"])

    def test_rsa_json_hex_parameters_and_goal_byte_conversions(self):
        cases = [({"answer_encoding": "hex", "byte_length": 2}, "0041"),
                 ({"answer_encoding": "utf8", "byte_length": 2}, "\0A"),
                 ({"answer_encoding": "utf8", "byte_length": 2, "byte_order": "little"}, "A\0")]
        for goal, answer in cases:
            with self.subTest(goal=goal), tempfile.TemporaryDirectory() as directory:
                raw = json.dumps({"algorithm": "rsa", "n": "0xca1", "e": "17", "c": "2790"}).encode()
                project, files, _ = project_in(Path(directory), raw)
                project.crypto["goal"] = goal
                candidate = add_candidate(project, answer=answer, m="65", p="61", q="53")
                record = verify_candidate(project, candidate.id, files)
                self.assertEqual(record["status"], "pass", record)
                candidate.crypto["candidate"]["answer"] = "41"
                self.assertEqual(verify_candidate(project, candidate.id, files)["status"], "fail")

    def test_any_root_goal_accepts_a_root_without_claiming_uniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            project, files, _ = project_in(Path(directory), b"n=15\ne=2\nc=4\n")
            candidate = add_candidate(project, answer="2", m="2")
            unique = verify_candidate(project, candidate.id, files)
            self.assertEqual(unique["math_status"], "pass")
            self.assertEqual(unique["status"], "inconclusive")
            project.crypto["goal"] = {"solution_policy": "any"}
            any_root = verify_candidate(project, candidate.id, files)
            self.assertEqual(any_root["status"], "pass")
            self.assertFalse(any_root["checks"]["unique_root"])

    def test_factor_and_private_exponent_goals_only_require_relevant_parameters(self):
        cases = [(b"n=55\n", {"answer_kind": "factors"}, {"answer_kind": "factors", "answer": "5,11", "p": "5", "q": "11"}),
                 (b"n=55\ne=3\n", {"answer_kind": "private_exponent"}, {"answer_kind": "private_exponent", "answer": "27", "d": "27", "p": "5", "q": "11"})]
        for raw, goal, answer in cases:
            with self.subTest(goal=goal), tempfile.TemporaryDirectory() as directory:
                project, files, _ = project_in(Path(directory), raw)
                project.crypto["goal"] = goal
                candidate = add_candidate(project, **answer)
                record = verify_candidate(project, candidate.id, files)
                self.assertEqual(record["status"], "pass", record)

    def test_cumulative_input_limit_is_checked_before_reading_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            project, files, path = project_in(Path(directory))
            path.write_bytes(b" " * (1024 * 1024 + 1))
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "parameters", "inputs/rsa.txt")
            candidate = add_candidate(project)
            with patch.object(Path, "open", side_effect=AssertionError("must reject before opening")):
                record = verify_candidate(project, candidate.id, files)
            self.assertEqual(record["status"], "inconclusive")
            self.assertIn("1 MiB", record["checks"]["reason"])

    def test_general_completion_retains_existing_service_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="web", target_url="https://example.test")
            project.status = ProjectStatus.RUNNING
            store.save_project(project)
            project.complete()
            StoreDispatcherClient(store, Runtime()).save_project(project)
            self.assertEqual(store.get_project(project.id).status, ProjectStatus.COMPLETED)

    def test_invalid_goal_fields_are_rejected_without_guessing(self):
        for goal in ({"solution_policy": "all"}, {"solution_policy": "uniqe"}, {"byte_length": 2},
                     {"byte_length": True, "answer_encoding": "hex"}, {"answer_kind": "key"},
                     {"answer_encoding": "hex", "byte_order": "middle"}):
            with self.subTest(goal=goal), self.assertRaises(ValueError):
                crypto_goal(goal)


class CryptoOutputTrustTests(unittest.TestCase):
    def apply(self, project, result):
        intent = project.add_intent("compute", from_fact_id=0, crypto={"method": "rsa.compute", "input_ids": ["input-1"]})
        intent.mark_running()
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
        return intent

    def test_answer_without_crypto_metadata_is_saved_as_candidate_not_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            intent = self.apply(project, WorkerResult(answer="7", fact_description="looks right", outcome="verified"))
            self.assertEqual(project.facts[2].kind, FactKind.CANDIDATE)
            self.assertEqual(intent.outcome, IntentOutcome.ERROR)
            self.assertEqual(project.crypto["executions"][-1]["exit_reason"], "tool_error")
            self.assertEqual(_evidence_items(project), [])

    def test_missing_claim_status_is_preserved_as_pending_not_evidence(self):
        for crypto in ({}, {"exit_reason": "completed", "fact": {"typed_value": {"type": "integer", "radix": 10, "value": "7"}}}):
            with self.subTest(crypto=crypto), tempfile.TemporaryDirectory() as directory:
                project, _, _ = project_in(Path(directory))
                self.apply(project, WorkerResult(fact_description="m may be 7", outcome="verified", crypto=crypto))
                self.assertEqual(project.facts[2].kind, FactKind.ERROR)
                self.assertFalse(project.facts[2].is_plannable)
                self.assertIn("m may be 7", project.facts[2].description)

    def test_candidate_payload_without_claim_cannot_become_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            intent = self.apply(project, WorkerResult(outcome="verified", crypto={
                "exit_reason": "completed", "candidate": {"answer": "7", "m": "7", "scheme": "rsa"},
                "fact": {"claim_status": "observed"}}))
            self.assertEqual(project.facts[2].kind, FactKind.CANDIDATE)
            self.assertIn("candidate saved as fact", intent.note)

    def test_candidate_save_errors_are_visible_and_never_reported_as_success(self):
        for candidate in ({}, {"answer": ""}, {"answer": "8"}):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                project, _, _ = project_in(Path(directory))
                intent = self.apply(project, WorkerResult(answer="7", fact_description="possible result", outcome="verified", crypto={
                    "exit_reason": "completed", "candidate": candidate, "fact": {"claim_status": "candidate"}}))
                self.assertEqual(intent.outcome, IntentOutcome.ERROR)
                self.assertIn("candidate not saved", intent.note)
                self.assertNotIn("candidate saved;", intent.note)
                self.assertTrue(project.crypto["executions"][-1]["candidate_error"])
                self.assertFalse(any(f.kind == FactKind.CANDIDATE for f in project.facts.values()))
                self.assertEqual(project.facts[2].kind, FactKind.ERROR)

    def test_invalid_artifact_candidate_keeps_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            result = WorkerResult(answer="7", outcome="verified", crypto={"exit_reason": "completed", "fact": {
                "artifact_refs": [{"path": "/tmp/solver.py", "size_bytes": 1, "sha256": "0" * 64}]}})
            intent = self.apply(project, result)
            self.assertEqual(intent.outcome, IntentOutcome.ERROR)
            self.assertIn("artifacts/intents", intent.note)

    def test_prior_answers_and_unlabelled_observations_do_not_gain_evidence_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _, _ = project_in(Path(directory))
            result = WorkerResult(outcome="error", crypto={"exit_reason": "timeout"}, prior_results=[
                WorkerObservation(answer="7", outcome="verified"),
                WorkerObservation(fact_description="unconfirmed", outcome="verified"),
                WorkerObservation(fact_description="checked n=55", outcome="verified", crypto={"fact": {"claim_status": "observed"}}),
            ])
            self.apply(project, result)
            self.assertEqual([project.facts[i].kind for i in (2, 3, 4)], [FactKind.CANDIDATE, FactKind.ERROR, FactKind.EVIDENCE])


if __name__ == "__main__":
    unittest.main()
