from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from echo_agent.crypto_verify import VERSION as CRYPTO_VERIFY_VERSION
from echo_agent.dispatcher.agents import CodexDockerAgent, CryptoExecutionLimit
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import parse_worker_result, strip_prompt_template_text, substantive_worker_output
from echo_agent.dispatcher.prompts import PromptBuilder, _evidence_items
from echo_agent.dispatcher.types import DispatchMode, IntentExecutionResult, WorkerResult
from echo_agent.models import FactKind, IntentOutcome, IntentStatus, Project, ProjectStatus
from echo_agent.server.dispatcher_runner import DispatcherRunner, DispatcherRunState, _RunHandle
from echo_agent.server.docker_runtime import DockerRuntimeError
from echo_agent.storage import SQLiteProjectStore

from tests.test_dispatcher_runner import ForceStopRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def crypto_project() -> Project:
    project = Project(
        id=7, name="rsa", status=ProjectStatus.ACTIVE, task_type="crypto", objective="factor n", docker="echo-agent-task-7",
        crypto={"schema_version": 1, "inputs": [
            {"id": "input-1", "role": "parameters", "path": "inputs/rsa.txt",
             "sha256": "0" * 64, "size_bytes": 14},
        ]},
    )
    project.add_fact("开始分析", fact_id=0)
    project.add_fact("恢复明文", fact_id=1)
    return project


def crypto_intent(project: Project, method: str = "rsa.inspect"):
    return project.add_intent(
        "有界检查", from_fact_id=0,
        crypto={"method": method, "input_ids": ["input-1"], "scope": {"input_ids": ["input-1"]}},
    )


class ConfirmedContractTests(unittest.TestCase):
    def test_crypto_prompts_and_worker_guidance_require_confirmed(self) -> None:
        project = crypto_project()
        intent = Dispatcher._create_initial_intent(project)
        builder = PromptBuilder()
        prompts = (
            builder.build_first_run_prompt(project, intent),
            builder.build_explore_prompt(project, intent),
            builder.build_reason_prompt(project),
        )
        for prompt in prompts:
            self.assertIn("confirmed=true", prompt)
        guidance = (PROJECT_ROOT / "container" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("confirmed=true", guidance)
        self.assertIn('"confirmed":true', prompts[0])

    def test_timeout_keeps_confirmed_observation(self) -> None:
        project = crypto_project()
        intent = crypto_intent(project)
        intent.mark_running()
        result = parse_worker_result(json.dumps({
            "answer": None, "fact_description": "实际观察到 n=55", "outcome": "verified",
            "crypto": {"exit_reason": "timeout", "covered_scope": {"input_ids": ["input-1"]},
                       "fact": {"claim_status": "observed", "confirmed": True, "source_input_ids": ["input-1"]}},
        }))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

        self.assertTrue(any(fact.description == "实际观察到 n=55" and fact.kind == FactKind.EVIDENCE
                            for fact in project.facts.values()))
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertEqual(intent.outcome, IntentOutcome.ERROR)

    def test_timeout_without_confirmed_stays_pending(self) -> None:
        project = crypto_project()
        intent = crypto_intent(project)
        intent.mark_running()
        result = parse_worker_result(json.dumps({
            "answer": None, "fact_description": "未确认的观察", "outcome": "verified",
            "crypto": {"exit_reason": "timeout", "covered_scope": {"input_ids": ["input-1"]},
                       "fact": {"claim_status": "observed", "source_input_ids": ["input-1"]}},
        }))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

        self.assertFalse(any(fact.description == "未确认的观察" and fact.kind == FactKind.EVIDENCE
                             for fact in project.facts.values()))
        self.assertTrue(any("未确认的观察" in fact.description and fact.kind == FactKind.ERROR
                            for fact in project.facts.values()))


class VerificationRecordTests(unittest.TestCase):
    @staticmethod
    def _project_and_intent() -> tuple[Project, object]:
        project = crypto_project()
        project.add_fact(
            "candidate answer: 7", kind=FactKind.CANDIDATE,
            crypto={"claim_status": "candidate", "source_input_ids": ["input-1"],
                    "candidate": {"answer": "7", "scheme": "rsa"}},
        )
        intent = project.add_intent(
            "独立校验", from_fact_id=0, crypto={
                "method": "verify.candidate", "input_ids": ["input-1"],
                "params": {"candidate_fact_id": 2}, "scope": {"input_ids": ["input-1"]},
            })
        intent.mark_running()
        return project, intent

    @staticmethod
    def _result(status: str, outcome: str, exit_reason: str = "completed") -> WorkerResult:
        return WorkerResult(outcome=outcome, crypto={
            "exit_reason": exit_reason, "covered_scope": {"input_ids": ["input-1"]},
            "verification": {"candidate_fact_id": 2, "method": CRYPTO_VERIFY_VERSION,
                             "status": status, "answer": "7", "checks": {"reencrypt": False}},
        })

    def test_failed_verification_is_recorded(self) -> None:
        project, intent = self._project_and_intent()
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, self._result("fail", "error")))

        self.assertEqual(project.crypto["verifications"][-1]["status"], "fail")
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertEqual(intent.outcome, IntentOutcome.EMPTY)
        self.assertIn("independent verification fail", intent.note)

    def test_inconclusive_verification_is_recorded(self) -> None:
        project, intent = self._project_and_intent()
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, self._result("inconclusive", "error")))

        self.assertEqual(project.crypto["verifications"][-1]["status"], "inconclusive")
        self.assertIn("independent verification inconclusive", intent.note)

    def test_passing_verification_still_completes(self) -> None:
        project, intent = self._project_and_intent()
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, self._result("pass", "verified")))

        self.assertEqual(project.crypto["verifications"][-1]["status"], "pass")
        self.assertEqual(intent.status, IntentStatus.COMPLETED)
        self.assertEqual(intent.outcome, IntentOutcome.VERIFIED)

    def test_cancelled_verification_never_persists(self) -> None:
        project, intent = self._project_and_intent()
        Dispatcher._apply_intent_result(
            project, IntentExecutionResult(intent, self._result("pass", "verified", exit_reason="cancelled"))
        )

        self.assertNotIn("verifications", project.crypto)
        self.assertEqual(intent.status, IntentStatus.FAILED)


class _RunAgent(CodexDockerAgent):
    def __init__(self, *, process_error: Exception | None = None, reset_error: Exception | None = None) -> None:
        super().__init__(timeout_seconds=5)
        self.process_error = process_error
        self.reset_error = reset_error
        self.reset_calls = 0
        self.logs: list[tuple[str, str, dict]] = []

    def _set_crypto_memory_limit(self, container, budget) -> None:
        pass

    def _run_preflight(self, project, log=None) -> None:
        pass

    def _prepare_runtime(self, project, runtime_key, log=None) -> None:
        pass

    def _docker_command(self, project, mode, runtime_key=None):
        return ["true"]

    def _run_process(self, command, prompt, mode, env=None, log=None) -> str:
        if self.process_error is not None:
            raise self.process_error
        return "ok"

    def _reset_crypto_container(self, container: str) -> None:
        self.reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error


class AgentCleanupTests(unittest.TestCase):
    def _run(self, agent: _RunAgent):
        project = crypto_project()
        intent = crypto_intent(project)
        return agent.run(
            project, "prompt", DispatchMode.EXPLORE,
            log=lambda message, level, metadata: agent.logs.append((message, level, metadata)),
            intent_id=intent.id,
        )

    def test_reset_failure_does_not_replace_successful_result(self) -> None:
        agent = _RunAgent(reset_error=RuntimeError("docker restart failed"))
        result = self._run(agent)

        self.assertEqual(result.output, "ok")
        self.assertEqual(agent.reset_calls, 1)
        self.assertTrue(any(level == "warning" and "reset failed" in message
                            for message, level, _ in agent.logs))
        self.assertIsNone(agent._run_state.crypto_budget)

    def test_reset_failure_does_not_replace_execution_limit(self) -> None:
        agent = _RunAgent(
            process_error=CryptoExecutionLimit("timeout", "wall clock exceeded"),
            reset_error=RuntimeError("docker restart failed"),
        )
        with self.assertRaises(CryptoExecutionLimit) as caught:
            self._run(agent)

        self.assertEqual(caught.exception.reason, "timeout")
        self.assertIsNone(agent._run_state.crypto_budget)

    def test_failed_reset_still_invalidates_preflight(self) -> None:
        agent = CodexDockerAgent()
        agent._preflight_ready.add("echo-agent-task-7")
        completed = subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="daemon is busy")

        with patch("echo_agent.dispatcher.agents.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError):
                agent._reset_crypto_container("echo-agent-task-7")

        self.assertNotIn("echo-agent-task-7", agent._preflight_ready)


class CancelRunningTests(unittest.TestCase):
    def test_cancel_running_tolerates_failed_container_stop(self) -> None:
        agent = CodexDockerAgent()
        agent._crypto_containers.update({"container-a", "container-b"})
        agent._preflight_ready.update({"container-a", "container-b"})
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if command[-1] == "container-a":
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such container")

        with patch("echo_agent.dispatcher.agents.subprocess.run", fake_run):
            self.assertTrue(agent.cancel_running())

        self.assertEqual(len(calls), 2)
        self.assertTrue({"container-a", "container-b"}.issubset(agent._cancelled_containers))
        self.assertFalse({"container-a", "container-b"} & agent._preflight_ready)


class _RaisingDispatcher:
    def request_stop(self) -> None:
        raise RuntimeError("docker stop failed")


class _FailingForceRuntime(ForceStopRuntime):
    def force_stop_project_container(self, project, log=None) -> None:
        raise DockerRuntimeError("docker daemon unavailable")


class DispatcherStopReleaseTests(unittest.TestCase):
    def _running_project(self, store: SQLiteProjectStore) -> None:
        project = store.create_project(project_id=7, target_url="http://target", objective="get flag")
        project.activate()
        project.start()
        store.save_project(project)

    def test_stop_releases_project_even_when_cancel_raises(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            self._running_project(store)
            runner = DispatcherRunner(store, runtime=ForceStopRuntime())
            runner._runs[7] = _RunHandle(
                thread=threading.Thread(target=lambda: None),
                stop_event=threading.Event(),
                state=DispatcherRunState(project_id=7, running=True),
                dispatcher=_RaisingDispatcher(),
            )

            with self.assertRaises(RuntimeError):
                runner.stop(7)

            self.assertEqual(store.get_project(7).status, ProjectStatus.STOPED)

    def test_force_stop_failure_still_releases_project(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            self._running_project(store)
            runner = DispatcherRunner(store, runtime=_FailingForceRuntime())
            thread = threading.Thread(target=lambda: time.sleep(0.5))
            thread.start()
            runner._runs[7] = _RunHandle(
                thread=thread,
                stop_event=threading.Event(),
                state=DispatcherRunState(project_id=7, running=True),
            )

            state = runner.stop(7, join_timeout=0.01)

            self.assertEqual(store.get_project(7).status, ProjectStatus.STOPED)
            self.assertEqual(state.project_id, 7)


class CryptoEvidenceBudgetTests(unittest.TestCase):
    def test_raw_crypto_evidence_is_bounded(self) -> None:
        project = crypto_project()
        long_text = "观察" + "x" * 1000
        for _ in range(45):
            project.add_fact(long_text)

        items = _evidence_items(project)

        self.assertIn("已截断", items[0]["description"])
        self.assertEqual(items[0]["description"], long_text[:600] + "…（较早事实已截断）")
        self.assertEqual(items[-1]["description"], long_text)
        self.assertEqual(items[0]["id"], 2)

    def test_structured_crypto_evidence_keeps_short_summary(self) -> None:
        project = crypto_project()
        for index in range(45):
            project.add_fact("long observation " + "x" * 250, crypto={"typed_value": {
                "type": "integer", "radix": 16, "value": format(index + 1, "x")}})

        items = _evidence_items(project)

        self.assertIn("已截断", items[0]["description"])
        self.assertEqual(items[0]["crypto"]["typed_value"]["value"], "1")


class CryptoExamplePlaceholderTests(unittest.TestCase):
    EXAMPLE = {
        "answer": None, "fact_description": "示例事实；不要照抄", "outcome": "verified",
        "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                   "fact": {"claim_status": "observed", "confirmed": True, "source_input_ids": ["input-1"],
                            "typed_value": {"type": "integer", "radix": 10, "value": "55"}}},
    }

    def test_echoed_example_without_template_is_not_a_fact(self) -> None:
        output = json.dumps(self.EXAMPLE, ensure_ascii=False)
        result = parse_worker_result(output)

        self.assertIsNone(result.fact_description)
        self.assertIsNone(result.answer)
        self.assertIsNone(substantive_worker_output(output))
        self.assertNotIn("示例事实", strip_prompt_template_text(output))

    def test_echoed_example_is_not_persisted(self) -> None:
        project = crypto_project()
        intent = crypto_intent(project)
        intent.mark_running()
        result = parse_worker_result(json.dumps(self.EXAMPLE, ensure_ascii=False))

        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

        self.assertFalse(any("示例事实" in fact.description for fact in project.facts.values()))
        self.assertEqual(intent.status, IntentStatus.FAILED)

    def test_real_crypto_observation_is_still_parsed(self) -> None:
        data = {**self.EXAMPLE, "fact_description": "实际观察到 n=55"}
        result = parse_worker_result(json.dumps(data, ensure_ascii=False))

        self.assertEqual(result.fact_description, "实际观察到 n=55")


class RetryClassificationTests(unittest.TestCase):
    def test_output_tail_does_not_make_a_timeout_retryable(self) -> None:
        tail = "\noutput_tail:\nreconnecting to upstream 429 too many requests"
        self.assertFalse(Dispatcher._is_retryable_failure(
            CryptoExecutionLimit("timeout", "codex agent timed out in explore mode after 1800s" + tail)
        ))
        self.assertFalse(Dispatcher._is_retryable_failure(
            RuntimeError("codex agent timed out\noutput_tail:\nconnection refused")
        ))

    def test_prefix_transient_errors_are_still_retryable(self) -> None:
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("provider overloaded, reconnecting")))
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("HTTP 429 too many requests")))
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("upstream returned HTTP 503 service unavailable")))


if __name__ == "__main__":
    unittest.main()
