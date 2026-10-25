import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.crypto_budget import CryptoBudget, resolve_crypto_budget
from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.dispatcher.agents import CodexDockerAgent, CryptoExecutionLimit
from echo_agent.dispatcher.agents import CodexReasoner
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, WorkerObservation, WorkerResult
from echo_agent.models import FactKind, IntentOutcome
from echo_agent.server.docker_runtime import DockerProjectRuntime
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore
from tests.test_crypto_s07 import FixedAgent, MemoryClient, prior, project, spec


class CryptoS08Tests(unittest.TestCase):
    def test_budget_bounds_and_crypto_only_container_limits(self):
        budget = resolve_crypto_budget(None, {"wall_seconds": 5, "memory_mb": 512,
                                              "output_bytes": 4096})
        self.assertEqual(budget.to_dict(), {"wall_seconds": 5, "memory_mb": 512,
                                             "output_bytes": 4096})
        for bad in ({"wall_seconds": 0}, {"wall_seconds": 9999}, {"memory_mb": 128},
                    {"output_bytes": 0}, {"cpu_seconds": 10}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_crypto_budget(None, bad)
        runtime = DockerProjectRuntime(image="worker", context=Path("container"),
                                       dockerfile=Path("container/Dockerfile"))
        crypto_command = runtime._docker_run_command("crypto-1", [], crypto=True)
        general_command = runtime._docker_run_command("web-1", [])
        self.assertIn("--memory", crypto_command)
        self.assertIn("--pids-limit", crypto_command)
        self.assertNotIn("--memory", general_command)

    def test_silent_crypto_process_can_finish_before_hard_wall(self):
        agent = CodexDockerAgent(timeout_seconds=10, no_output_timeout_seconds=1)
        agent._run_state.crypto_budget = CryptoBudget(4, 512, 4096)
        script = "import sys,time;sys.stdin.read();time.sleep(2);print('{\"fact_description\":\"done\"}',flush=True)"
        started = time.monotonic()
        output = agent._run_process((sys.executable, "-c", script), "x", DispatchMode.EXPLORE,
                                    env=None, log=None)
        self.assertIn('"done"', output)
        self.assertGreater(time.monotonic() - started, 1)

    def test_silent_crypto_process_hits_hard_wall_and_output_cap(self):
        agent = CodexDockerAgent(timeout_seconds=10, no_output_timeout_seconds=1)
        agent._run_state.crypto_budget = CryptoBudget(1, 512, 4096)
        with self.assertRaises(CryptoExecutionLimit) as caught:
            agent._run_process((sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(10)"),
                               "x", DispatchMode.EXPLORE, env=None, log=None)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertNotIn("no-output timed out", str(caught.exception))
        agent._run_state.crypto_budget = CryptoBudget(5, 512, 2048)
        with self.assertRaises(CryptoExecutionLimit) as caught:
            agent._run_process((sys.executable, "-c",
                                "import sys,time;sys.stdin.read();print('x'*8192,flush=True);time.sleep(10)"),
                               "x", DispatchMode.EXPLORE, env=None, log=None)
        self.assertEqual(caught.exception.reason, "budget_exhausted")

    def test_budget_failure_saves_prior_fact_without_refuting_search(self):
        item = project()
        intent = item.add_intent("bounded search", from_fact_id=2,
                                 crypto=spec(budget={"wall_seconds": 5}))
        intent.mark_running()
        result = WorkerResult(outcome="error", detail="output budget exceeded",
                              prior_results=[WorkerObservation(fact_description="confirmed gcd=1",
                                                               outcome="verified",
                                                               crypto={"fact": {"claim_status": "observed", "source_input_ids": ["input-1"]}})],
                              crypto={"exit_reason": "budget_exhausted", "covered_scope": {}})
        Dispatcher._apply_intent_result(item, IntentExecutionResult(intent, result))
        self.assertEqual(intent.outcome, IntentOutcome.ERROR)
        self.assertEqual(item.crypto["executions"][-1]["exit_reason"], "budget_exhausted")
        self.assertEqual(item.crypto["executions"][-1]["compute_id"], f"crypto-{item.id}-{intent.id}")
        self.assertTrue(any(fact.description == "confirmed gcd=1" and fact.kind == FactKind.EVIDENCE
                            for fact in item.facts.values()))

    def test_crypto_compute_slot_is_one_even_with_multiple_model_slots(self):
        item = project()
        item.add_intent("range one", from_fact_id=2, crypto=spec({"start": 2, "end": 5}))
        item.add_intent("range two", from_fact_id=2, crypto=spec({"start": 6, "end": 10}))

        class Worker:
            active = 0
            peak = 0
            lock = threading.Lock()

            def execute_intent(self, project, intent, mode=None):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                time.sleep(0.1)
                with self.lock:
                    self.active -= 1
                return WorkerResult(outcome="error", crypto={"exit_reason": "tool_error",
                                                              "covered_scope": {}})

        worker = Worker()
        dispatcher = Dispatcher(client=MemoryClient(item), worker=worker,
                                max_explore_workers=4, max_codex_slots=2)
        self.assertEqual(len(list(dispatcher._execute_intents(item, item.active_intents()))), 2)
        self.assertEqual(worker.peak, 1)

    def test_repeated_unchanged_heavy_plan_stops_after_no_progress_limit(self):
        item = project()
        prior(item, spec(budget={"wall_seconds": 5}), status="failed", reason="timeout", covered={})
        output = json.dumps({"completed": False, "next_intents": [
            {"description": "rename the same search", "from": 2,
             "action_key": "renamed-search", "crypto": spec(budget={"wall_seconds": 5})}]})
        client = MemoryClient(item)
        dispatcher = Dispatcher(client=client, reasoner=CodexReasoner(FixedAgent(output)),
                                max_retryable_attempts=3)
        messages = [dispatcher._reason(client.project, release_project=False).message for _ in range(3)]
        self.assertIn("reason made no progress", messages[-1])
        self.assertEqual(len(client.project.intents), 1)

    def test_invalid_checkpoint_file_is_not_offered_for_resume(self):
        item = project()
        intent = item.add_intent("search", from_fact_id=2, crypto=spec())
        intent.mark_running()
        with tempfile.TemporaryDirectory() as directory:
            files = CryptoProjectFiles(Path(directory))
            path = files.project_root(item.id) / "artifacts" / "intents" / str(intent.id) / "state.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"real state")
            reference = {"path": f"artifacts/intents/{intent.id}/state.bin",
                         "sha256": hashlib.sha256(b"wrong state").hexdigest(),
                         "size_bytes": len(b"wrong state"), "producer_intent_id": intent.id}
            client = MemoryClient(item)
            client.verify_crypto_artifact = lambda project_id, ref: files.verify_ref(project_id, ref, "artifact")
            dispatcher = Dispatcher(client=client)
            result = WorkerResult(outcome="error", detail="timeout",
                                  crypto={"exit_reason": "timeout", "covered_scope": {},
                                          "checkpoint_ref": reference})
            dispatcher._commit_intent_result(item, IntentExecutionResult(intent, result))
            record = client.project.crypto["executions"][-1]
            self.assertNotIn("checkpoint_ref", record)
            self.assertIn("checkpoint file verification failed", record["checkpoint_error"])

    def test_resume_checkpoint_is_rechecked_before_worker_runs(self):
        item = project()
        old = item.add_intent("old", from_fact_id=2, crypto=spec())
        checkpoint = {"path": f"artifacts/intents/{old.id}/state.bin", "sha256": "a" * 64,
                      "size_bytes": 5, "producer_intent_id": old.id}
        current = item.add_intent("resume", from_fact_id=2, crypto=spec(resume=checkpoint))

        class NeverRunWorker:
            def execute_intent(self, *args, **kwargs):
                raise AssertionError("worker must not read an invalid checkpoint")

        client = MemoryClient(item)
        client.verify_crypto_artifact = lambda project_id, ref: (_ for _ in ()).throw(
            FileNotFoundError("checkpoint changed"))
        dispatcher = Dispatcher(client=client, worker=NeverRunWorker())
        result = dispatcher._execute_with_retry(item, current, DispatchMode.EXPLORE, "intent")
        self.assertEqual(result.crypto["exit_reason"], "tool_error")
        self.assertIn("checkpoint changed", result.detail)

    def test_api_artifact_verification_checks_real_bytes(self):
        item = project()
        intent = item.add_intent("save checkpoint", from_fact_id=2, crypto=spec())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteProjectStore(root / "state.db")
            store.save_project(item)
            files = CryptoProjectFiles(root / "projects")
            path = files.project_root(item.id) / "artifacts" / "intents" / str(intent.id) / "state.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"verified state")
            ref = files.describe_artifact(item.id, intent.id,
                                          f"artifacts/intents/{intent.id}/state.bin")

            class RuntimeStub:
                def prepare_shared_resources(self):
                    pass

            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings",
                       return_value=RuntimeStub()):
                app = create_app(store)
            with TestClient(app) as http:
                self.assertEqual(http.post(f"/projects/{item.id}/artifacts/verify", json=ref).status_code, 200)
                self.assertEqual(http.post(f"/projects/{item.id}/artifacts/verify",
                                           json={**ref, "sha256": "0" * 64}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
