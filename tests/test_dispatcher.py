from __future__ import annotations

import copy
import threading
import time
import unittest
from typing import Any

from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import ReasonFormatError
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import DispatchMode, ReasonResult, WorkerObservation, WorkerResult
from echo_agent.models import (
    FactKind,
    IntentOutcome,
    Project,
    ProjectStatus,
    action_keys_equivalent,
    descriptions_equivalent,
)


class MemoryProjectClient:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.logs: list[dict[str, Any]] = []
        self.saved_projects: list[Project] = []

    def get_project(self, project_id: int) -> Project:
        self.assert_project_id(project_id)
        return copy.deepcopy(self.project)

    def save_project(self, project: Project) -> dict[str, Any]:
        self.assert_project_id(project.id)
        self.project = copy.deepcopy(project)
        self.saved_projects.append(copy.deepcopy(project))
        return self.project.to_dict()

    def add_project_log(
        self,
        project_id: int,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.assert_project_id(project_id)
        self.logs.append({"message": message, "level": level, "metadata": metadata or {}})

    def assert_project_id(self, project_id: int) -> None:
        if project_id != self.project.id:
            raise AssertionError(f"unexpected project id: {project_id}")


class StaticWorker:
    def __init__(self, fact_description: str) -> None:
        self.fact_description = fact_description

    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(answer=None, fact_description=self.fact_description)


class FailingWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        raise RuntimeError("agent stalled")


class CountingReasoner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, project: Project) -> ReasonResult:
        self.calls += 1
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 0,
                    "to": None,
                    "action_key": "list-files",
                    "description": "只列出附件文件名",
                }
            ],
        )


class OrderedConcurrentWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        if intent.id == 0:
            time.sleep(0.01)
            return WorkerResult(answer=None, fact_description="first fact")
        time.sleep(0.1)
        return WorkerResult(answer=None, fact_description="second fact")


class IntentFactWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(answer=None, fact_description=f"fact from intent {intent.id}")


class FlagWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(answer="flag{ok}", fact_description=None)


class PlaceholderAnswerWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(answer="真实 flag 字符串", fact_description=None)


class FlagFactWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(answer=None, fact_description="confirmed final answer flag{fact}")


class StaticReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {"from": 2, "to": None, "description": "枚举目录"},
                {"from": 2, "to": None, "description": "测试上传点"},
            ],
        )


class EmptyReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=False, next_intents=[])


class CompletedReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, next_intents=[])


class FlagReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, answer="flag{reason}", next_intents=[])


class FixedAnswerReasoner:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, answer=self.answer, next_intents=[])


class GraphAnswerReasoner:
    def plan(self, project: Project) -> ReasonResult:
        answers = [
            fact.description.removeprefix("agent reported possible final answer: ").strip()
            for fact in project.facts.values()
            if fact.description.startswith("agent reported possible final answer: ")
        ]
        if answers:
            return ReasonResult(completed=True, answer=answers[-1], next_intents=[])
        return ReasonResult(completed=False, next_intents=[])


class LegacyReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=False, next_intent_descriptions=["分析是否存在文件上传漏洞"])


class DispatcherFactInsertionTests(unittest.TestCase):
    def test_first_run_fact_completes_initial_intent_without_auto_bridge(self) -> None:
        project = Project(
            id=1,
            name="test",
            status=ProjectStatus.ACTIVE,
            target_url="http://target",
            objective="get flag",
        )
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("discovered web framework: flask"),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        saved = client.project
        self.assertEqual(saved.facts[2].description, "discovered web framework: flask")
        self.assertEqual(saved.intents[0].from_fact_id, 0)
        self.assertEqual(saved.intents[0].to_fact_id, 2)
        self.assertTrue(saved.intents[0].is_completed)
        self.assertEqual(len(saved.intents), 1)
        self.assertEqual(saved.active_intents(), [])

    def test_first_run_failure_becomes_fact_for_reason(self) -> None:
        project = Project(
            id=1,
            name="test",
            status=ProjectStatus.ACTIVE,
            target_url="http://target",
            objective="get flag",
        )
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=FailingWorker(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        saved = client.project
        self.assertEqual(result.mode, DispatchMode.FIRST_RUN)
        self.assertTrue(result.halt)
        self.assertEqual(saved.facts[2].kind, FactKind.ERROR)
        self.assertIn("first-run execution failed", saved.facts[2].description)
        self.assertEqual(saved.intents[0].status.value, "failed")
        self.assertEqual(saved.intents[0].outcome.value, "error")
        self.assertEqual(saved.intents[0].from_fact_id, 0)
        self.assertEqual(saved.intents[0].to_fact_id, 2)
        self.assertIn("first-run execution failed", saved.intents[0].note)
        self.assertEqual(saved.status, ProjectStatus.STOPED)

    def test_failed_first_run_does_not_plan_until_the_next_run(self) -> None:
        project = Project(
            id=1,
            name="test",
            status=ProjectStatus.ACTIVE,
            target_url="http://target",
            objective="get flag",
        )
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        reasoner = CountingReasoner()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=FailingWorker(),  # type: ignore[arg-type]
            reasoner=reasoner,  # type: ignore[arg-type]
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=5)

        self.assertEqual([result.mode for result in results], [DispatchMode.FIRST_RUN])
        self.assertTrue(results[0].halt)
        self.assertEqual(reasoner.calls, 0)
        self.assertEqual(client.project.facts[2].kind, FactKind.ERROR)
        self.assertEqual(client.project.status, ProjectStatus.STOPED)

        follow_up = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(follow_up.mode, DispatchMode.REASON)
        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(client.project.intents[1].from_fact_id, 0)

    def test_no_output_timeout_failure_fact_is_actionable(self) -> None:
        fact = Dispatcher._execution_failed_fact(
            "intent 3",
            RuntimeError("codex agent no-output timed out in explore mode after 900s without stdout/stderr"),
        )

        self.assertIn("Codex 长时间无 stdout/stderr 输出", fact)
        self.assertIn("重新规划更小的验证步骤", fact)

    def test_reason_can_branch_multiple_intents_from_same_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        seed_intent = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed_intent.mark_running()
        seed_intent.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=StaticReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        saved = client.project
        branch_intents = [intent for intent in saved.intents.values() if intent.from_fact_id == 2]
        self.assertEqual(len(branch_intents), 2)
        self.assertEqual([intent.description for intent in branch_intents], ["枚举目录", "测试上传点"])
        self.assertTrue(all(intent.to_fact_id is None for intent in branch_intents))
        planned = next(log for log in client.logs if str(log["message"]).startswith("reason planned"))
        self.assertEqual(planned["metadata"]["intent_ids"], [intent.id for intent in branch_intents])

    def test_reason_creates_next_intents_after_first_run_fact(self) -> None:
        project = Project(
            id=1,
            name="test",
            status=ProjectStatus.ACTIVE,
            target_url="http://target",
            objective="get flag",
        )
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("web service exposes upload form"),  # type: ignore[arg-type]
            reasoner=StaticReasoner(),  # type: ignore[arg-type]
        )

        first_result = dispatcher.run_once(project.id)
        second_result = dispatcher.run_once(project.id)

        self.assertEqual(first_result.mode, DispatchMode.FIRST_RUN)
        self.assertEqual(second_result.mode, DispatchMode.REASON)
        saved = client.project
        self.assertEqual(saved.intents[0].from_fact_id, 0)
        self.assertEqual(saved.intents[0].to_fact_id, 2)
        branch_intents = [intent for intent in saved.intents.values() if intent.from_fact_id == 2]
        self.assertEqual(len(branch_intents), 2)
        self.assertEqual([intent.description for intent in branch_intents], ["枚举目录", "测试上传点"])
        self.assertTrue(all(intent.to_fact_id is None for intent in branch_intents))

    def test_explore_saves_each_completed_intent_before_all_finish(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        project.add_intent("fast intent", from_fact_id=2, to_fact_id=1)
        project.add_intent("slow intent", from_fact_id=2, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=OrderedConcurrentWorker(),  # type: ignore[arg-type]
            max_explore_workers=2,
        )

        dispatcher.run_once(project.id)

        progressive_snapshots = [
            snapshot
            for snapshot in client.saved_projects
            if (
                len(snapshot.facts) == 4
                and snapshot.intents[0].is_completed
                and snapshot.intents[0].to_fact_id == 3
                and snapshot.intents[1].status.value == "running"
                and snapshot.intents[1].to_fact_id == 1
            )
        ]
        self.assertTrue(progressive_snapshots)
        saved = client.project
        self.assertTrue(saved.intents[0].is_completed)
        self.assertTrue(saved.intents[1].is_completed)
        self.assertEqual(saved.intents[0].to_fact_id, 3)
        self.assertEqual(saved.intents[1].to_fact_id, 4)
        self.assertEqual(saved.facts[3].description, "first fact")
        self.assertEqual(saved.facts[4].description, "second fact")

    def test_explore_answer_is_inserted_as_fact_for_reason_judgement(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        project.add_intent("try exploit", from_fact_id=2, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=FlagWorker(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.EXPLORE)
        self.assertEqual(client.project.status, ProjectStatus.STOPED)
        self.assertEqual(client.project.intents[0].to_fact_id, 3)
        self.assertTrue(client.project.intents[0].is_completed)
        self.assertEqual(client.project.facts[1].description, "goal")
        self.assertEqual(client.project.facts[3].description, "agent reported possible final answer: flag{ok}")

    def test_first_run_answer_is_inserted_as_fact_for_reason_judgement(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=FlagWorker(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.FIRST_RUN)
        self.assertEqual(client.project.facts[1].description, "goal")
        self.assertEqual(client.project.facts[2].description, "agent reported possible final answer: flag{ok}")

    def test_explore_fact_with_flag_form_does_not_complete_without_reason(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        project.add_intent("try exploit", from_fact_id=2, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=FlagFactWorker(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.EXPLORE)
        self.assertEqual(client.project.status, ProjectStatus.STOPED)
        self.assertEqual(client.project.facts[1].description, "goal")
        self.assertEqual(client.project.facts[3].description, "confirmed final answer flag{fact}")

    def test_explore_placeholder_answer_does_not_complete_or_update_end_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        project.add_intent("try exploit", from_fact_id=2, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=PlaceholderAnswerWorker(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.EXPLORE)
        self.assertEqual(client.project.status, ProjectStatus.STOPED)
        self.assertEqual(client.project.facts[1].description, "goal")
        self.assertTrue(client.project.intents[0].is_completed)

    def test_reason_completion_requires_answer_in_evidence(self) -> None:
        project = self._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FlagReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.REASON)
        self.assertEqual(client.project.facts[1].description, "goal")
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)
        self.assertFalse(
            any(intent.description == "确认该事实包含最终答案" for intent in client.project.intents.values())
        )

    def test_existing_graph_answer_only_completes_after_reason_judgement(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("HTTP response contained flag{graph}", fact_id=2)
        seed_intent = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed_intent.mark_running()
        seed_intent.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.COMPLETED)
        self.assertEqual(client.project.facts[1].description, "result\nanswer: flag{graph}")
        self.assertEqual(len(client.project.intents), 1)
        producer = client.project.intents[seed_intent.id]
        self.assertEqual(producer.to_fact_ids, (2, 1))
        self.assertEqual(producer.to_fact_id, 2)
        self.assertFalse(
            any(intent.description == "确认该事实包含最终答案" for intent in client.project.intents.values())
        )

    def test_initial_probe_intent_is_not_reused_for_final_result(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("HTTP response contained flag{graph}", fact_id=2)
        project.add_fact("agent reported possible final answer: flag{graph}", fact_id=3)
        seed = project.add_intent(Dispatcher._INITIAL_PROBE_DESCRIPTION, from_fact_id=0, to_fact_id=1)
        seed.mark_running()
        seed.mark_completed(to_fact_ids=(2, 3), to_fact_id=3)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        saved = client.project
        self.assertEqual(result.mode, DispatchMode.COMPLETED)
        self.assertEqual(saved.facts[1].description, "result\nanswer: flag{graph}")
        probe = saved.intents[seed.id]
        self.assertEqual(probe.to_fact_ids, (2, 3))
        self.assertNotIn(1, probe.to_fact_ids)
        goal_edges = [
            intent
            for intent in saved.intents.values()
            if intent.id != seed.id and intent.from_fact_id == 2 and 1 in intent.to_fact_ids
        ]
        self.assertEqual(len(goal_edges), 1)
        self.assertEqual(goal_edges[0].description, "得到最终结果")
        self.assertTrue(goal_edges[0].is_completed)

    def test_reason_answer_does_not_create_goal_edge_without_source_fact(self) -> None:
        project = self._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FlagReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        self.assertFalse(
            any(intent.to_fact_id == 1 and intent.description == "确认该事实包含最终答案" for intent in client.project.intents.values())
        )

    def test_run_loop_continues_to_reason_after_parallel_explore_facts_without_flag(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        for index in range(5):
            project.add_intent(f"intent {index}", from_fact_id=2)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=IntentFactWorker(),  # type: ignore[arg-type]
            reasoner=StaticReasoner(),  # type: ignore[arg-type]
            max_explore_workers=5,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=2)

        self.assertEqual(
            [result.mode for result in results],
            [DispatchMode.EXPLORE, DispatchMode.REASON, DispatchMode.EXPLORE],
        )
        saved = client.project
        self.assertEqual(len([intent for intent in saved.intents.values() if intent.is_completed]), 7)
        branch_intents = [
            intent
            for intent in saved.intents.values()
            if intent.description in {"枚举目录", "测试上传点"}
        ]
        self.assertEqual([intent.description for intent in branch_intents], ["枚举目录", "测试上传点"])
        self.assertTrue(all(intent.is_completed for intent in branch_intents))
        self.assertTrue(all(intent.from_fact_id == 2 for intent in branch_intents))

    def test_reason_empty_plan_does_not_create_fallback_intent(self) -> None:
        project = self._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(client.project.active_intents(), [])
        self.assertEqual(result.message, "planned 0 intents")

    def test_reason_completed_without_answer_still_completes_by_codex_judgement(self) -> None:
        project = self._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=CompletedReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.REASON)
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)
        self.assertEqual(client.project.facts[1].description, "goal")

    def test_legacy_reason_descriptions_use_leaf_fact_as_fallback(self) -> None:
        project = self._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=LegacyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        active_intents = client.project.active_intents()
        self.assertEqual(len(active_intents), 1)
        self.assertEqual(active_intents[0].from_fact_id, 2)
        self.assertEqual(active_intents[0].description, "分析是否存在文件上传漏洞")

    @staticmethod
    def _project_ready_for_reason() -> Project:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("web service exposes upload form", fact_id=2)
        seed_intent = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed_intent.mark_running()
        seed_intent.mark_completed()
        return project


class SlotGateWorker:
    def __init__(self) -> None:
        self.snapshot: dict[int, str] = {}

    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        if not self.snapshot:
            time.sleep(0.05)
            self.snapshot = {item.id: item.status.value for item in project.intents.values()}
        return WorkerResult(fact_description=f"fact from intent {intent.id}")


class RetryableFailureWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        raise RuntimeError("sqlite database is locked")


class FlakyRetryWorker:
    def __init__(self) -> None:
        self.calls = 0

    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 too many requests")
        return WorkerResult(fact_description="recovered after retry")


class CodexSlotTests(unittest.TestCase):
    def test_waiting_intent_stays_active_until_codex_slot_is_free(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("first", from_fact_id=0, to_fact_id=1)
        project.add_intent("second", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        worker = SlotGateWorker()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            max_explore_workers=2,
            max_codex_slots=1,
        )

        dispatcher.run_once(project.id)

        self.assertEqual(worker.snapshot[0], "running")
        self.assertEqual(worker.snapshot[1], "active")
        self.assertTrue(client.project.intents[0].is_completed)
        self.assertTrue(client.project.intents[1].is_completed)

    def test_retryable_failure_returns_intent_to_active_without_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("probe", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=RetryableFailureWorker(),  # type: ignore[arg-type]
            max_codex_slots=1,
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.EXPLORE)
        self.assertEqual(client.project.intents[0].status.value, "active")
        self.assertEqual(set(client.project.facts), {0, 1})
        self.assertTrue(any("retry failed" in log["message"] for log in client.logs))

    def test_retryable_failure_retries_once_then_completes(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("probe", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        worker = FlakyRetryWorker()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        self.assertEqual(worker.calls, 2)
        self.assertTrue(client.project.intents[0].is_completed)
        self.assertEqual(client.project.facts[2].description, "recovered after retry")

    def test_stop_before_slot_does_not_start_intent(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("probe", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        stop_event = threading.Event()
        stop_event.set()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("should not run"),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        dispatcher.run_once(project.id)

        self.assertEqual(client.project.intents[0].status.value, "active")
        self.assertNotIn(2, client.project.facts)


class RaisingReasoner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, project: Project) -> ReasonResult:
        self.calls += 1
        raise RuntimeError("HTTP 503 service unavailable")


class FormatErrorReasoner:
    def __init__(self, succeed_on: int | None = None) -> None:
        self.calls = 0
        self.succeed_on = succeed_on

    def plan(self, project: Project) -> ReasonResult:
        self.calls += 1
        if self.succeed_on is not None and self.calls >= self.succeed_on:
            return ReasonResult(completed=False, next_intents=[])
        raise ReasonFormatError("reason output does not contain a JSON object")


class ExplodingReasoner:
    def plan(self, project: Project) -> ReasonResult:
        raise AssertionError("reason should not run")


class OuterFailureTests(unittest.TestCase):
    def test_stranded_running_intent_is_recovered_before_reason(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        stranded = project.add_intent("probe", from_fact_id=0, to_fact_id=1)
        stranded.mark_running()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("recovered fact"),  # type: ignore[arg-type]
            reasoner=ExplodingReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(result.mode, DispatchMode.EXPLORE)
        self.assertTrue(client.project.intents[0].is_completed)
        self.assertEqual(client.project.facts[2].description, "recovered fact")
        self.assertTrue(any("stranded" in log["message"] for log in client.logs))

    def test_retry_budget_turns_persistent_failure_into_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("probe", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=RetryableFailureWorker(),  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        first = dispatcher.run_once(project.id, release_project=False)
        second = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(first.mode, DispatchMode.EXPLORE)
        self.assertEqual(second.mode, DispatchMode.EXPLORE)
        self.assertEqual(client.project.intents[0].status.value, "failed")
        self.assertEqual(client.project.intents[0].outcome.value, "error")
        self.assertNotIn(2, client.project.facts)
        self.assertIn("database is locked", client.project.intents[0].note)
        self.assertIn("execution failed", client.project.intents[0].note)

    def test_transient_gateway_error_is_retryable(self) -> None:
        self.assertTrue(
            Dispatcher._is_retryable_failure(RuntimeError("upstream returned HTTP 503 service unavailable"))
        )
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("model is under high demand")))
        self.assertTrue(
            Dispatcher._is_retryable_failure(RuntimeError("stream disconnected before completion"))
        )
        self.assertFalse(
            Dispatcher._is_retryable_failure(
                RuntimeError("codex agent no-output timed out in explore mode after 900s without stdout/stderr")
            )
        )
        self.assertFalse(Dispatcher._is_retryable_failure(RuntimeError("codex agent exited with code 1")))

    def test_reason_exception_does_not_escape_or_complete_project(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        reasoner = RaisingReasoner()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=reasoner,  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(result.mode, DispatchMode.REASON)
        self.assertEqual(result.message, "reason failed; will retry")
        self.assertEqual(client.project.status, ProjectStatus.RUNNING)
        self.assertEqual(len(client.project.intents), 1)
        self.assertEqual(reasoner.calls, 1)

    def test_repeated_empty_reason_does_not_grow_fallback_intents(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("fallback explored"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=6)

        fallbacks = [
            intent
            for intent in client.project.intents.values()
            if "分析下一步可验证的漏洞方向" in intent.description
        ]
        self.assertEqual(len(fallbacks), 0)
        self.assertEqual(len(client.project.intents), 1)
        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertEqual(results[-1].message, "reason made no progress")
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)


class RefutedWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(fact_description="上传点不存在可利用漏洞", outcome="refuted")


class RefutedFromReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 3,
                    "to": None,
                    "action_key": "continue-refuted",
                    "description": "从证伪结果继续枚举",
                }
            ],
        )


class RepeatedActionReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "Check Upload",
                    "description": "换一种说法检查上传",
                }
            ],
        )


class RepeatedDescriptionReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "other-check",
                    "description": "检查上传点",
                }
            ],
        )


class StoppingReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=False, next_intents=[], stop_reason="上传和备份面已经验证关闭")


class ParaphraseReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "recheck-upload",
                    "description": "重新检查上传点",
                }
            ],
        )


class SmallerRetryReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "check-upload",
                    "description": "只请求 /upload 一次并在十秒内停止",
                }
            ],
        )


class SameTextAfterErrorReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "probe-upload-once",
                    "description": "分析是否存在文件上传漏洞",
                }
            ],
        )


class GraphContractTests(unittest.TestCase):
    def test_refuted_path_does_not_become_next_from(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("发现上传点", fact_id=2)
        project.add_intent("分析上传", from_fact_id=2, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=RefutedWorker(),  # type: ignore[arg-type]
            reasoner=RefutedFromReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)
        result = dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        self.assertEqual(saved.facts[3].kind, FactKind.REFUTED)
        self.assertEqual(saved.facts[3].description, "上传点不存在可利用漏洞")
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.REFUTED)
        self.assertTrue(saved.intents[0].is_completed)
        active = saved.active_intents()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].from_fact_id, 2)
        self.assertNotEqual(active[0].from_fact_id, 3)
        self.assertEqual(result.message, "planned 1 intents")

    def test_same_action_key_with_different_description_is_not_planned_again(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        done = project.add_intent(
            "分析是否存在文件上传漏洞",
            from_fact_id=2,
            to_fact_id=1,
            action_key="check_upload",
        )
        done.mark_running()
        done.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=RepeatedActionReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        matches = [intent for intent in client.project.intents.values() if intent.action_key == "check-upload"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(client.project.active_intents(), [])
        self.assertEqual(result.message, "planned 0 intents")

    def test_normalized_description_is_not_planned_again(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        done = project.add_intent("检查上传点。", from_fact_id=2, to_fact_id=1, action_key="old-check")
        done.mark_running()
        done.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=RepeatedDescriptionReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        matches = [
            intent
            for intent in client.project.intents.values()
            if "上传点" in intent.description
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(client.project.active_intents(), [])

    def test_reason_prompt_lists_completed_actions_as_already_done(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        done = project.add_intent(
            "已确认备份目录不存在",
            from_fact_id=2,
            to_fact_id=1,
            action_key="backup-missing",
        )
        done.mark_running()
        done.mark_completed()

        prompt = PromptBuilder().build_reason_prompt(project)

        self.assertIn('"already_done"', prompt)
        self.assertEqual(prompt.count("已确认备份目录不存在"), 1)
        self.assertEqual(prompt.count("backup-missing"), 1)

    def test_reason_prompt_includes_refuted_finding(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("发现上传点", fact_id=2)
        intent = project.add_intent("检查上传点", from_fact_id=2, to_fact_id=1, action_key="check-upload")
        intent.mark_running()
        finding = project.add_fact("上传点不存在可利用漏洞", kind=FactKind.REFUTED)
        intent.mark_completed(to_fact_id=finding.id, outcome=IntentOutcome.REFUTED)

        prompt = PromptBuilder().build_reason_prompt(project)

        self.assertIn('"findings"', prompt)
        self.assertGreaterEqual(prompt.count("上传点不存在可利用漏洞"), 2)
        self.assertIn('"result_kind": "refuted"', prompt)

    def test_refuted_paraphrase_does_not_reset_progress(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        done = project.add_intent("检查上传点", from_fact_id=2, to_fact_id=1, action_key="check-upload")
        done.mark_running()
        done.mark_completed(outcome=IntentOutcome.REFUTED)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=ParaphraseReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=6)

        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertEqual(results[-1].message, "reason made no progress")
        self.assertEqual(len(client.project.intents), 2)
        self.assertEqual(client.project.active_intents(), [])

    def test_timeout_allows_smaller_retry_with_same_key(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        failed = project.add_intent(
            "分析是否存在文件上传漏洞",
            from_fact_id=2,
            to_fact_id=1,
            action_key="check-upload",
        )
        failed.mark_running()
        failed.mark_failed(IntentOutcome.ERROR, note="codex agent no-output timed out")
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=SmallerRetryReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        active = client.project.active_intents()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].action_key, "check-upload")
        self.assertIn("/upload", active[0].description)
        self.assertEqual(result.message, "planned 1 intents")

    def test_timeout_does_not_allow_the_same_description(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        failed = project.add_intent(
            "分析是否存在文件上传漏洞",
            from_fact_id=2,
            to_fact_id=1,
            action_key="check-upload",
        )
        failed.mark_running()
        failed.mark_failed(IntentOutcome.ERROR, note="codex agent no-output timed out")
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=SameTextAfterErrorReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(client.project.active_intents(), [])
        self.assertEqual(result.message, "planned 0 intents")

    def test_action_identity_helpers(self) -> None:
        self.assertTrue(action_keys_equivalent("check-upload", "recheck-upload"))
        self.assertTrue(action_keys_equivalent("Check Upload", "retry-check-upload"))
        self.assertFalse(action_keys_equivalent("check-upload", "enum-backup"))
        self.assertFalse(action_keys_equivalent("", "check-upload"))
        self.assertTrue(descriptions_equivalent("检查上传点", "重新检查上传点"))
        self.assertFalse(descriptions_equivalent("检查上传点", "枚举响应头里的内部地址"))

    def test_repeated_empty_reason_stops_with_stop_reason(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=StoppingReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=6)

        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertEqual(results[-1].message, "reason made no progress: 上传和备份面已经验证关闭")
        self.assertEqual(client.project.active_intents(), [])

    def test_completed_description_is_not_planned_again(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        done = project.add_intent("分析是否存在文件上传漏洞", from_fact_id=2, to_fact_id=1)
        done.mark_running()
        done.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=LegacyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        matches = [
            intent
            for intent in client.project.intents.values()
            if intent.description == "分析是否存在文件上传漏洞"
        ]
        self.assertEqual(len(matches), 1)




    def test_prior_facts_are_kept_when_the_last_result_is_refuted(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("分析上传", from_fact_id=0, to_fact_id=1)
        intent.mark_running()
        result = WorkerResult(
            fact_description="未发现上传点",
            outcome="refuted",
            prior_results=[
                WorkerObservation(fact_description="font cache rce", outcome="verified"),
            ],
        )
        Dispatcher._apply_intent_result(project, type("Item", (), {"intent": intent, "result": result})())
        self.assertEqual(project.facts[2].description, "font cache rce")
        self.assertEqual(project.facts[2].kind, FactKind.EVIDENCE)
        self.assertEqual(project.facts[3].description, "未发现上传点")
        self.assertEqual(project.facts[3].kind, FactKind.REFUTED)
        self.assertEqual(intent.outcome, IntentOutcome.REFUTED)
        self.assertEqual(intent.to_fact_id, 3)
        self.assertEqual(intent.to_fact_ids, (2, 3))
        self.assertEqual(len(project.intents), 1)
        self.assertNotIn(1, intent.to_fact_ids)

    def test_answer_only_json_does_not_duplicate_fact_that_already_contains_flag(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始探测", from_fact_id=0, to_fact_id=1)
        intent.mark_running()
        result = WorkerResult(
            answer="flag{ok}",
            outcome="verified",
            prior_results=[
                WorkerObservation(
                    fact_description="利用路径得到 flag{ok}",
                    outcome="verified",
                )
            ],
        )

        Dispatcher._apply_intent_result(project, type("Item", (), {"intent": intent, "result": result})())

        self.assertEqual([fact.description for fact in project.facts.values() if fact.id >= 2], ["利用路径得到 flag{ok}"])
        self.assertNotIn("agent reported possible final answer", project.facts[2].description)
        self.assertEqual(intent.outcome, IntentOutcome.VERIFIED)
        self.assertEqual(intent.to_fact_ids, (2,))

    def test_answer_only_prior_is_dropped_when_final_fact_contains_flag(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("初始探测", from_fact_id=0, to_fact_id=1)
        intent.mark_running()
        result = WorkerResult(
            fact_description="利用路径得到 flag{ok}",
            outcome="verified",
            answer="flag{ok}",
            prior_results=[
                WorkerObservation(answer="flag{ok}", outcome="verified"),
            ],
        )

        Dispatcher._apply_intent_result(project, type("Item", (), {"intent": intent, "result": result})())

        descriptions = [fact.description for fact in project.facts.values() if fact.id >= 2]
        self.assertEqual(descriptions, ["利用路径得到 flag{ok}"])
        self.assertEqual(intent.to_fact_ids, (2,))

    def test_multiple_targets_round_trip_through_storage(self) -> None:
        import tempfile
        from pathlib import Path

        from echo_agent.storage import SQLiteProjectStore

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            project = store.create_project(name="round", objective="get flag", target_url="http://target")
            intent = project.add_intent("probe", from_fact_id=0, to_fact_id=1)
            first = project.add_fact("first finding")
            second = project.add_fact("second finding")
            intent.mark_running()
            intent.mark_completed(to_fact_id=second.id, outcome=IntentOutcome.VERIFIED)
            intent.set_targets([first.id, second.id])
            intent.add_target(1)
            store.save_project(project)

            loaded = store.get_project(project.id)
            saved = loaded.intents[intent.id]
            self.assertEqual(saved.to_fact_ids, (first.id, second.id, 1))
            self.assertEqual(saved.to_fact_id, second.id)
            restored = Project.from_dict(loaded.to_dict())
            self.assertEqual(restored.intents[intent.id].to_fact_ids, (first.id, second.id, 1))
            self.assertEqual(restored.intents[intent.id].to_fact_id, second.id)

    def test_timeout_tail_is_kept_on_the_failure_note(self) -> None:
        note = Dispatcher._execution_failed_fact(
            "intent 3",
            RuntimeError("codex agent no-output timed out in explore mode after 180s without stdout/stderr\noutput_tail:\npartial rce output before hang"),
        )
        self.assertIn("Codex 长时间无 stdout/stderr 输出", note)
        self.assertIn("partial rce output before hang", note)

    def test_template_worker_does_not_create_refuted_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=TemplateRefutedWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        self.assertFalse(any(fact.kind == FactKind.REFUTED for fact in saved.facts.values()))
        self.assertFalse(any("该方向已验证不存在可利用点" in fact.description for fact in saved.facts.values()))
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.EMPTY)

    def test_timeout_tail_becomes_partial_evidence_fact(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=TimeoutTailWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        evidence = [fact for fact in saved.facts.values() if fact.kind == FactKind.EVIDENCE]
        errors = [fact for fact in saved.facts.values() if fact.kind == FactKind.ERROR]
        self.assertEqual(evidence, [])
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0].is_plannable)
        self.assertTrue(errors[0].description.startswith("未完成执行的输出摘录："))
        self.assertIn("confirmed hidden SSRF endpoint", errors[0].description)
        self.assertNotIn("该方向已验证不存在可利用点", errors[0].description)
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.ERROR)
        self.assertFalse(any(fact.kind == FactKind.REFUTED for fact in saved.facts.values()))
        prompt = PromptBuilder().build_reason_prompt(saved)
        self.assertIn("confirmed hidden SSRF endpoint", prompt)
        self.assertRegex(prompt, r'"allowed_from": \[\s*0\s*\]')
        self.assertNotRegex(prompt, r'"allowed_from": \[[^\]]*\b2\b')

    def test_template_only_timeout_tail_stays_an_error(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=TemplateOnlyTimeoutWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        self.assertFalse(any(fact.kind == FactKind.EVIDENCE for fact in saved.facts.values()))
        self.assertFalse(any(fact.kind == FactKind.REFUTED for fact in saved.facts.values()))
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.ERROR)
        self.assertNotIn("该方向已验证不存在可利用点", saved.intents[0].note)

    def test_short_timeout_tail_does_not_become_evidence(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        intent = project.add_intent("分析上传", from_fact_id=0, to_fact_id=1)
        intent.mark_running()
        result = WorkerResult(outcome="error", detail="failed\noutput_tail:\nshort")

        Dispatcher._apply_intent_result(project, type("Item", (), {"intent": intent, "result": result})())

        self.assertFalse(any(fact.kind == FactKind.EVIDENCE for fact in project.facts.values()))
        self.assertFalse(any(fact.kind == FactKind.ERROR for fact in project.facts.values()))
        self.assertEqual(intent.outcome, IntentOutcome.ERROR)

    def test_closed_from_without_producer_uses_start(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("closed", fact_id=3, kind=FactKind.REFUTED)

        self.assertEqual(Dispatcher._planning_from_fact_id(project, 3), 0)
        self.assertIsNone(Dispatcher._planning_from_fact_id(project, 99))

    def test_reason_prompt_uses_start_when_only_refuted_facts_exist(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("该方向已验证不存在可利用点", kind=FactKind.REFUTED)

        prompt = PromptBuilder().build_reason_prompt(project)

        self.assertRegex(prompt, r'"allowed_from": \[\s*0\s*\]')
        self.assertNotRegex(prompt, r'"allowed_from": \[[^\]]*\b2\b')
        self.assertIn("未完成执行的输出摘录", prompt)

    def test_stop_during_execution_persists_stopped_status(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("分析上传", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        stop_event = threading.Event()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StopDuringExecutionWorker(stop_event),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(client.project.status, ProjectStatus.STOPED)
        self.assertEqual(client.project.facts[2].description, "发现隐藏接口 /internal/fetch")


class TemplateRefutedWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(fact_description="该方向已验证不存在可利用点", outcome="refuted")


class TimeoutJsonWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        raise RuntimeError(
            "codex agent timed out in first_run mode after 1800s\noutput_tail:\n"
            '{"answer": null, "fact_description": "hidden SSRF endpoint /internal/fetch", "outcome": "verified"}\n'
            '{"answer": null, "fact_description": "internal MySQL 5.7.29:3306", "outcome": "verified"}\n'
            '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}\n'
        )


class PartialTimeoutWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(
            timed_out=True,
            fact_description="internal MySQL 5.7.29:3306",
            prior_results=[
                WorkerObservation(
                    fact_description="hidden SSRF endpoint /internal/fetch",
                    outcome="verified",
                )
            ],
        )


class SalvagedEmptyReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=False, next_intents=[], salvaged=True)


class TimeoutReasoner:
    def plan(self, project: Project) -> ReasonResult:
        raise RuntimeError("codex agent timed out in reason mode after 1800s")


class RepairableReasoner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, project: Project) -> ReasonResult:
        self.calls += 1
        raise ReasonFormatError(
            "reason output does not contain a JSON object",
            output='思考一下\n```json\n{"completed": false, "next_intents": [],}\n```',
        )


class TimeoutTailWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        prose = "confirmed hidden SSRF endpoint at /internal/fetch and MySQL 5.7.29:3306"
        template = '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}'
        raise RuntimeError(
            "codex agent timed out in first_run mode after 1800s\noutput_tail:\n" + template + "\n" + prose
        )


class TemplateOnlyTimeoutWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        raise RuntimeError(
            'codex agent timed out in first_run mode after 1800s\noutput_tail:\n'
            '{"answer": null, "fact_description": "该方向已验证不存在可利用点", "outcome": "refuted"}'
        )


class StopDuringExecutionWorker:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event

    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        self.stop_event.set()
        return WorkerResult(fact_description="发现隐藏接口 /internal/fetch")


class TemplateOnlyReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "check-upload",
                    "description": "分析是否存在文件上传漏洞",
                },
                {
                    "from": 2,
                    "to": None,
                    "action_key": "enum-backup",
                    "description": "枚举 Web 目录和敏感备份文件",
                },
            ],
        )


class MixedTemplateReasoner:
    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 2,
                    "to": None,
                    "action_key": "check-upload",
                    "description": "分析是否存在文件上传漏洞",
                },
                {
                    "from": 2,
                    "to": None,
                    "action_key": "probe-header",
                    "description": "只读取 /admin 的响应头",
                },
            ],
        )


class AlwaysPlanReasoner:
    _descriptions = (
        "读取首页 HTTP 状态码",
        "枚举站点根目录里的备份文件",
        "检查登录表单有哪些字段",
    )

    def plan(self, project: Project) -> ReasonResult:
        index = len(project.intents) - 1
        description = self._descriptions[min(index, len(self._descriptions) - 1)]
        return ReasonResult(
            completed=False,
            next_intents=[
                {
                    "from": 0,
                    "to": None,
                    "action_key": f"step-{index}",
                    "description": description,
                }
            ],
        )


class TargetDownWorker:
    def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
        return WorkerResult(outcome="error", detail="target not found")


class OuterLoopFixTests(unittest.TestCase):
    def test_template_reason_intents_are_not_planned(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=TemplateOnlyReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(result.message, "planned 0 intents")
        self.assertEqual(client.project.active_intents(), [])

    def test_real_intent_survives_when_template_intent_is_mixed_in(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=MixedTemplateReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        active = client.project.active_intents()
        self.assertEqual(result.message, "planned 1 intents")
        self.assertEqual([intent.description for intent in active], ["只读取 /admin 的响应头"])

    def test_empty_reason_does_not_consume_step_budget(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=3,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=1)

        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertEqual(results[-1].message, "reason made no progress")
        self.assertGreaterEqual(len(results), 3)
        self.assertNotEqual(results[0].message, "project step budget exhausted")

    def test_reason_failure_does_not_consume_step_budget(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=RaisingReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=1)

        self.assertEqual([result.message for result in results], ["reason failed; will retry", "reason failed repeatedly"])
        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)

    def test_run_loop_releases_project_when_step_raises(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        def boom(project: Project, release_project: bool = False) -> None:
            raise RuntimeError("step failed")

        released: dict[str, int] = {}

        def release(project_id: int) -> None:
            released["id"] = project_id
            raise RuntimeError("release failed")

        dispatcher._run_project_step = boom  # type: ignore[method-assign]
        dispatcher._release_project_if_running = release  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "step failed"):
            dispatcher.run_loop(project.id, interval_seconds=0, max_steps=1)
        self.assertEqual(released["id"], project.id)

    def test_reason_step_logs_reason_mode(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        self.assertTrue(any(log["message"] == "project entered reason mode" for log in client.logs))

    def test_explore_step_logs_explore_mode(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("探测首页", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("首页返回 200"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        self.assertTrue(any(log["message"] == "project entered explore mode" for log in client.logs))

    def test_explore_log_records_started_intents_and_new_fact_ids(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("探测首页", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)

        class MultiFactWorker:
            def execute_intent(self, project: Project, intent, mode: DispatchMode) -> WorkerResult:
                return WorkerResult(
                    fact_description="首页存在上传点",
                    prior_results=[WorkerObservation(fact_description="响应头包含 Server")],
                )

        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=MultiFactWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        started = next(log for log in client.logs if log["message"].startswith("explore started"))
        finished = next(log for log in client.logs if log["message"] == "explore intent 0 finished")
        self.assertEqual(started["metadata"]["intent_ids"], [0])
        self.assertEqual(finished["metadata"]["fact_ids"], [2, 3])

    def test_first_run_log_records_created_fact_ids(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("首页返回 200"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        started = next(log for log in client.logs if log["message"] == "first-run initial intent started")
        finished = next(log for log in client.logs if log["message"] == "first-run finished")
        self.assertEqual(started["metadata"]["intent_id"], 0)
        self.assertEqual(finished["metadata"]["fact_ids"], [2])

    def test_completion_log_includes_answer(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("HTTP response contained flag{graph}", fact_id=2)
        seed_intent = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed_intent.mark_running()
        seed_intent.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id)

        completed = next(log for log in client.logs if log["message"] == "reason marked project completed")
        self.assertEqual(completed["metadata"]["answer"], "flag{graph}")

    def test_target_not_found_three_times_skips(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("确认目标在线", from_fact_id=0, to_fact_id=1, action_key="probe-0")
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=TargetDownWorker(),  # type: ignore[arg-type]
            reasoner=AlwaysPlanReasoner(),  # type: ignore[arg-type]
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=10)

        self.assertEqual(
            [result.mode for result in results],
            [
                DispatchMode.EXPLORE,
                DispatchMode.REASON,
                DispatchMode.EXPLORE,
                DispatchMode.REASON,
                DispatchMode.SKIPPED,
            ],
        )
        self.assertEqual(results[-1].message, "target appears offline: target not found repeated 3 times")

    def test_other_result_resets_target_offline_streak(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )
        dispatcher._target_offline_streak = 2
        dispatcher._observe_target(WorkerResult(fact_description="found /admin"))
        self.assertEqual(dispatcher._target_offline_streak, 0)
        dispatcher._observe_target(WorkerResult(answer="Target Not Found"))
        dispatcher._observe_target(WorkerResult(retryable=True))
        self.assertEqual(dispatcher._target_offline_streak, 1)

    def test_explore_prompt_hides_closed_fact_ids(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("发现上传点", fact_id=2)
        closed = project.add_intent("检查上传点", from_fact_id=2, to_fact_id=1, action_key="check-upload")
        finding = project.add_fact("上传点不存在可利用漏洞", kind=FactKind.REFUTED)
        closed.mark_running()
        closed.mark_completed(to_fact_id=finding.id, outcome=IntentOutcome.REFUTED)
        active = project.add_intent("探测响应头", from_fact_id=2, to_fact_id=1, action_key="probe-header")

        prompt = PromptBuilder().build_explore_prompt(project, active)

        self.assertIn('"closed_actions"', prompt)
        self.assertNotIn('"findings"', prompt)
        self.assertNotIn('"already_done"', prompt)
        self.assertNotIn('"do_not_repeat"', prompt)
        self.assertNotIn('"id": 3', prompt)
        self.assertIn("发现上传点", prompt)
        self.assertIn("就输出一条 JSON", prompt)
        self.assertIn("outcome=refuted", prompt)
        self.assertIn("检查上传点", prompt)


class FormatAndExcerptTests(unittest.TestCase):
    def test_reason_format_error_retries_without_counting_as_infrastructure_failure(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        reasoner = FormatErrorReasoner()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=reasoner,  # type: ignore[arg-type]
            max_retryable_attempts=2,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=1)

        self.assertEqual(reasoner.calls, 4)
        self.assertEqual(results[0].message, "reason output was not JSON; will retry")
        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertIn("never a valid JSON object", results[-1].message)
        self.assertNotIn("reason failed repeatedly", [result.message for result in results])
        self.assertEqual(
            [fact.description for fact in client.project.facts.values()],
            ["start", "goal", "web service exposes upload form"],
        )

    def test_reason_format_error_accepts_the_immediate_retry(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        reasoner = FormatErrorReasoner(succeed_on=2)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=reasoner,  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(reasoner.calls, 2)
        self.assertEqual(result.message, "planned 0 intents")
        self.assertEqual(dispatcher._reason_format_failures, 0)

    def test_partial_output_excerpt_cannot_satisfy_completion(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        project.add_fact(
            "未完成执行的输出摘录：\nflag{reason}",
            kind=FactKind.EVIDENCE,
        )
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FlagReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertNotEqual(result.mode, DispatchMode.COMPLETED)
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)

    def test_timeout_json_is_kept_as_evidence_and_intent_stays_incomplete(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=TimeoutJsonWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        evidence = [fact.description for fact in saved.facts.values() if fact.kind == FactKind.EVIDENCE]
        self.assertEqual(
            evidence,
            ["hidden SSRF endpoint /internal/fetch", "internal MySQL 5.7.29:3306"],
        )
        self.assertFalse(any(fact.kind == FactKind.REFUTED for fact in saved.facts.values()))
        self.assertFalse(saved.intents[0].is_completed)
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.ERROR)
        self.assertTrue(all(fact.is_plannable for fact in saved.facts.values() if fact.kind == FactKind.EVIDENCE))

    def test_timed_out_worker_result_keeps_partial_facts(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_intent("继续探测", from_fact_id=0, to_fact_id=1)
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=PartialTimeoutWorker(),  # type: ignore[arg-type]
            reasoner=EmptyReasoner(),  # type: ignore[arg-type]
        )

        dispatcher.run_once(project.id, release_project=False)

        saved = client.project
        evidence = [fact.description for fact in saved.facts.values() if fact.kind == FactKind.EVIDENCE]
        self.assertEqual(
            evidence,
            ["hidden SSRF endpoint /internal/fetch", "internal MySQL 5.7.29:3306"],
        )
        self.assertFalse(saved.intents[0].is_completed)
        self.assertEqual(saved.intents[0].outcome, IntentOutcome.ERROR)

    def test_wrapper_only_answer_does_not_complete(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("agent reported possible final answer: flag{graph}", fact_id=2)
        seed = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed.mark_running()
        seed.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertNotEqual(result.mode, DispatchMode.COMPLETED)
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)

    def test_wrapper_with_external_source_can_complete(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact(
            "agent reported possible final answer: flag{graph}\nsource: HTTP/1.1 200 body flag{graph}",
            fact_id=2,
        )
        seed = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed.mark_running()
        seed.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(result.mode, DispatchMode.COMPLETED)
        self.assertEqual(client.project.status, ProjectStatus.COMPLETED)

    def test_refuted_fact_cannot_complete_project(self) -> None:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact("decoy flag{graph}", fact_id=2, kind=FactKind.REFUTED)
        seed = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=1)
        seed.mark_running()
        seed.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=FixedAnswerReasoner("flag{graph}"),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertNotEqual(result.mode, DispatchMode.COMPLETED)
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)

    def test_salvaged_empty_plan_does_not_count_as_no_progress(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=SalvagedEmptyReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(result.message, "reason salvaged after timeout; will retry")
        self.assertEqual(dispatcher._no_progress_reasons, 0)
        self.assertEqual(dispatcher._reason_salvage_failures, 1)

    def test_repeated_reason_timeout_stops_without_no_progress(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=TimeoutReasoner(),  # type: ignore[arg-type]
            max_retryable_attempts=1,
        )

        results = dispatcher.run_loop(project.id, interval_seconds=0, max_steps=1)

        self.assertEqual(results[-1].message, "reason timed out repeatedly without producing a plan")
        self.assertEqual(dispatcher._no_progress_reasons, 0)
        self.assertEqual(dispatcher._reason_failures, 0)

    def test_reason_prose_is_repaired_before_another_full_plan(self) -> None:
        project = DispatcherFactInsertionTests._project_ready_for_reason()
        client = MemoryProjectClient(project)
        reasoner = RepairableReasoner()
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=reasoner,  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id, release_project=False)

        self.assertEqual(reasoner.calls, 1)
        self.assertEqual(result.message, "planned 0 intents")
        self.assertEqual(
            [fact.description for fact in client.project.facts.values()],
            ["start", "goal", "web service exposes upload form"],
        )

    def test_json_parse_failure_and_upstream_overload_are_retryable(self) -> None:
        self.assertTrue(
            Dispatcher._is_retryable_failure(RuntimeError("explore output does not contain a JSON object"))
        )
        self.assertTrue(
            Dispatcher._is_retryable_failure(
                RuntimeError("codex agent transient upstream failure in explore mode with exit code 1")
            )
        )
        self.assertTrue(Dispatcher._is_retryable_failure(RuntimeError("provider overloaded, reconnecting")))


class BadCompletionReasoner:
    """声称完成，但答案不在 evidence 中，且不规划下一步。"""

    def plan(self, project: Project) -> ReasonResult:
        return ReasonResult(completed=True, answer="DASCTF{not-in-evidence}", next_intents=[])


class AnswerSourceBackfillTests(unittest.TestCase):
    @staticmethod
    def _project_with_evidence(description: str) -> Project:
        project = Project(id=1, name="test", status=ProjectStatus.ACTIVE)
        project.add_fact("start", fact_id=0)
        project.add_fact("goal", fact_id=1)
        project.add_fact(description, fact_id=2)
        return project

    def test_backfill_uses_existing_evidence_fact(self) -> None:
        project = self._project_with_evidence(
            'UNION 回显 array(1) { [0]=> string(40) "DASCTF{abc}" }'
        )
        result = WorkerResult(answer="DASCTF{abc}")

        Dispatcher._backfill_answer_source(project, result)

        self.assertIsNotNone(result.answer_source)
        self.assertIn("DASCTF{abc}", result.answer_source or "")

    def test_backfill_ignores_wrapper_fact(self) -> None:
        project = self._project_with_evidence("agent reported possible final answer: DASCTF{abc}")
        result = WorkerResult(answer="DASCTF{abc}")

        Dispatcher._backfill_answer_source(project, result)

        self.assertIsNone(result.answer_source)

    def test_backfill_keeps_existing_source(self) -> None:
        project = self._project_with_evidence('var_dump "DASCTF{abc}"')
        result = WorkerResult(answer="DASCTF{abc}", answer_source="already-set")

        Dispatcher._backfill_answer_source(project, result)

        self.assertEqual(result.answer_source, "already-set")

    def test_rejected_completion_plans_verification_intent(self) -> None:
        project = self._project_with_evidence("some unrelated evidence")
        seed = project.add_intent("initial discovery", from_fact_id=0, to_fact_id=2)
        seed.mark_running()
        seed.mark_completed()
        client = MemoryProjectClient(project)
        dispatcher = Dispatcher(
            client=client,  # type: ignore[arg-type]
            worker=StaticWorker("unused"),  # type: ignore[arg-type]
            reasoner=BadCompletionReasoner(),  # type: ignore[arg-type]
        )

        result = dispatcher.run_once(project.id)

        self.assertEqual(result.mode, DispatchMode.REASON)
        keys = [intent.action_key for intent in client.project.intents.values()]
        self.assertTrue(
            any(key.startswith("verify-candidate-answer") for key in keys),
            f"expected a verify-candidate-answer intent, got {keys}",
        )

    def test_rejected_answer_is_recorded_as_finding(self) -> None:
        project = self._project_with_evidence("some unrelated evidence")

        Dispatcher._record_rejected_answer(project, "DASCTF{abc}")

        matches = [
            fact
            for fact in project.facts.values()
            if fact.kind == FactKind.REFUTED and "DASCTF{abc}" in fact.description
        ]
        self.assertEqual(len(matches), 1)

        # 幂等：同一个答案重复被拒不会刷出多条事实
        Dispatcher._record_rejected_answer(project, "DASCTF{abc}")
        matches = [
            fact
            for fact in project.facts.values()
            if fact.kind == FactKind.REFUTED and "DASCTF{abc}" in fact.description
        ]
        self.assertEqual(len(matches), 1)

    def test_rejected_answer_ignores_empty_value(self) -> None:
        project = self._project_with_evidence("some unrelated evidence")
        before = len(project.facts)

        Dispatcher._record_rejected_answer(project, "   ")

        self.assertEqual(len(project.facts), before)


if __name__ == "__main__":
    unittest.main()
