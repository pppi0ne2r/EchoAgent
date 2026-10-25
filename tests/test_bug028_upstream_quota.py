"""BUG-028 / BUG-019: upstream quota and credential failures must stop, not be salvaged."""

from __future__ import annotations

import unittest

from echo_agent.dispatcher.agents import CodexDockerAgent, UpstreamFatalError, fatal_upstream_marker
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.types import DispatchMode, ReasonResult
from echo_agent.models import Project, ProjectStatus
from tests.test_dispatcher import MemoryProjectClient

QUOTA_403 = ('unexpected status 403 Forbidden: {"code":"INSUFFICIENT_BALANCE",'
             '"message":"Insufficient account balance"}')


class FatalMarkerTests(unittest.TestCase):
    def test_quota_and_credential_markers(self) -> None:
        for text in (
            QUOTA_403,
            "unexpected status 402 Payment Required: 余额不足",
            "401 Unauthorized: invalid api key",
            "insufficient balance for this request",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(fatal_upstream_marker(text))

    def test_ordinary_failures_are_not_fatal(self) -> None:
        for text in (
            "unexpected status 403 Forbidden: no permission for this path",
            "unexpected status 401 Unauthorized",
            "unexpected status 402 from the target service",
            "rate limit exceeded; retry later",
            "service unavailable",
            "codex agent failed in explore mode with exit code 1",
        ):
            with self.subTest(text=text):
                self.assertIsNone(fatal_upstream_marker(text))


class UpstreamFatalProcessTests(unittest.TestCase):
    def test_quota_error_is_not_salvaged_into_a_template_plan(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        # stdout carries a valid-looking template JSON, as in the prompt echo from BUG-019.
        command = (
            "sh", "-c",
            "cat >/dev/null; "
            "echo '{\"completed\": false, \"answer\": null, \"next_intents\": []}'; "
            "echo 'unexpected status 403 Forbidden: {\"code\":\"INSUFFICIENT_BALANCE\"}' >&2; "
            "exit 1",
        )
        with self.assertRaises(UpstreamFatalError) as raised:
            agent._run_process(command, "prompt", DispatchMode.REASON, None, None)
        self.assertIn("insufficient_balance", str(raised.exception))
        self.assertIn("balance", str(raised.exception).lower())

    def test_transient_failure_still_reports_runtime_error(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        command = ("sh", "-c", "cat >/dev/null; echo 'rate limit exceeded' >&2; exit 1")
        with self.assertRaises(RuntimeError) as raised:
            agent._run_process(command, "prompt", DispatchMode.EXPLORE, None, None)
        self.assertNotIsInstance(raised.exception, UpstreamFatalError)

    def test_model_output_mentioning_balance_is_not_treated_as_an_upstream_error(self) -> None:
        agent = CodexDockerAgent(timeout_seconds=5)
        command = (
            "sh", "-c",
            "cat >/dev/null; echo 'observed: insufficient balance is a payment error'; "
            "echo 'sh: command failed' >&2; exit 1",
        )
        with self.assertRaises(RuntimeError) as raised:
            agent._run_process(command, "prompt", DispatchMode.EXPLORE, None, None)
        self.assertNotIsInstance(raised.exception, UpstreamFatalError)


class FatalWorker:
    def execute_intent(self, project, intent, mode, log=None):
        raise UpstreamFatalError(
            "codex upstream quota/auth failure in explore mode: matched 'insufficient_balance'; "
            "check the provider balance or API key."
        )


class BoomWorker:
    def execute_intent(self, project, intent, mode, log=None):
        raise RuntimeError("agent stalled")


class FatalReasoner:
    def plan(self, project, log=None):
        raise UpstreamFatalError(
            "codex upstream quota/auth failure in reason mode: matched 'payment required'"
        )


class OkReasoner:
    def plan(self, project, log=None):
        return ReasonResult(completed=False, next_intents=[])


def _project(*, with_active_intent: bool) -> Project:
    project = Project(id=7, name="quota", status=ProjectStatus.ACTIVE, target_url="https://target.example")
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    intent = project.add_intent("check", from_fact_id=0)
    if not with_active_intent:
        intent.mark_running()
        intent.mark_completed(to_fact_id=1)
    return project


class DispatcherFatalUpstreamTests(unittest.TestCase):
    def test_explore_stops_with_quota_diagnosis(self) -> None:
        client = MemoryProjectClient(_project(with_active_intent=True))
        dispatcher = Dispatcher(client, worker=FatalWorker(), reasoner=OkReasoner())

        result = dispatcher.run_once(7)

        self.assertEqual(result.mode, DispatchMode.SKIPPED)
        self.assertIn("quota/auth", result.message)
        self.assertIsNotNone(dispatcher._fatal_upstream_error)
        self.assertTrue(any(
            item["level"] == "error" and "quota/auth" in item["message"] for item in client.logs
        ))
        self.assertNotEqual(client.project.status, ProjectStatus.RUNNING)

    def test_reason_stops_without_counting_a_format_failure(self) -> None:
        client = MemoryProjectClient(_project(with_active_intent=False))
        dispatcher = Dispatcher(client, worker=BoomWorker(), reasoner=FatalReasoner())

        result = dispatcher.run_once(7)

        self.assertEqual(result.mode, DispatchMode.SKIPPED)
        self.assertIn("quota/auth", result.message)
        self.assertEqual(dispatcher._reason_failures, 0)
        self.assertEqual(dispatcher._reason_format_failures, 0)
        self.assertIsNotNone(dispatcher._fatal_upstream_error)

    def test_ordinary_failure_does_not_trigger_the_fatal_stop(self) -> None:
        client = MemoryProjectClient(_project(with_active_intent=True))
        dispatcher = Dispatcher(client, worker=BoomWorker(), reasoner=OkReasoner())

        result = dispatcher.run_once(7)

        self.assertNotEqual(result.mode, DispatchMode.SKIPPED)
        self.assertIsNone(dispatcher._fatal_upstream_error)

    def test_next_step_is_skipped_after_a_fatal_error(self) -> None:
        client = MemoryProjectClient(_project(with_active_intent=True))
        dispatcher = Dispatcher(client, worker=FatalWorker(), reasoner=OkReasoner())
        dispatcher.run_once(7)

        result = dispatcher.run_once(7)

        self.assertEqual(result.mode, DispatchMode.SKIPPED)
        self.assertIn("quota/auth", result.message)

    def test_retry_classification(self) -> None:
        self.assertFalse(Dispatcher._is_retryable_failure(UpstreamFatalError(QUOTA_403)))
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("rate limit exceeded")))


if __name__ == "__main__":
    unittest.main()
