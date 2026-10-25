from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from echo_agent.crypto_contract import artifact_ref, intent_crypto
from echo_agent.dispatcher.agents import CodexDockerAgent
from echo_agent.dispatcher.core import Dispatcher, step_consumes_budget
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import DispatchMode, ReasonResult
from echo_agent.models import Project
from echo_agent.storage import SQLiteProjectStore


def project_for_reason() -> Project:
    project = Project(id=7, name="contract recovery", status="active", task_type="crypto", crypto={
        "schema_version": 1,
        "inputs": [{"id": "input-1", "role": "source", "path": "inputs/task.py",
                    "sha256": "a" * 64, "size_bytes": 100}],
    })
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    source = project.add_fact("input inspected", crypto={"source_input_ids": ["input-1"]})
    initial = project.add_intent("inspect input", from_fact_id=0, to_fact_id=source.id)
    initial.mark_running()
    initial.mark_completed(to_fact_id=source.id)
    return project


def plan(*, valid: bool, action_key: str = "inspect-source") -> dict:
    value = {"type": "bytes", "length": 32}
    value.update({"encoding": "hex", "value": "a" * 64} if valid else {"hex": "a" * 64})
    return {
        "from": 2, "action_key": action_key, "description": "Inspect the provided source",
        "crypto": {"method": "source.inspect", "input_ids": ["input-1"], "input_fact_ids": [2],
                   "params": {"input_sha256": value}, "scope": {"input_ids": ["input-1"]}},
    }


class MemoryClient:
    def __init__(self) -> None:
        self.project = project_for_reason()
        self.logs: list[dict] = []

    def get_project(self, project_id: int) -> Project:
        return copy.deepcopy(self.project)

    def save_project(self, project: Project) -> Project:
        self.project = Project.from_dict(project.to_dict())
        return copy.deepcopy(self.project)

    def add_project_log(self, project_id, message, level="info", metadata=None):
        self.logs.append({"message": message, "level": level, "metadata": metadata or {}})


class SequenceReasoner:
    def __init__(self, *plans: list[dict]) -> None:
        self.plans = iter(plans)
        self.prompts: list[str] = []

    def plan(self, project: Project) -> ReasonResult:
        self.prompts.append(PromptBuilder().build_reason_prompt(project))
        return ReasonResult(next_intents=copy.deepcopy(next(self.plans)))


class CryptoContractRecoveryTests(unittest.TestCase):
    def test_rejected_plan_is_explained_and_repaired_without_losing_direction(self) -> None:
        client = MemoryClient()
        reasoner = SequenceReasoner([plan(valid=False)], [plan(valid=True)])
        dispatcher = Dispatcher(client=client, reasoner=reasoner)
        rejected = dispatcher.run_once(7)
        self.assertEqual(rejected.mode, DispatchMode.REASON)
        self.assertFalse(step_consumes_budget(rejected))
        self.assertEqual(dispatcher._no_progress_reasons, 0)
        self.assertEqual(len(client.project.intents), 1)
        self.assertEqual(len(client.project.facts), 3)
        feedback = client.project.crypto["planning_feedback"]
        self.assertIn("crypto.params.input_sha256", feedback["rejected_plans"][0]["error"])
        self.assertIn('"encoding":"hex"', feedback["rejected_plans"][0]["error"])
        self.assertTrue(any(log["level"] == "warning" and log["metadata"].get("action_key")
                            == "inspect-source" for log in client.logs))

        repaired = dispatcher.run_once(7)
        self.assertIn("crypto.params.input_sha256", reasoner.prompts[1])
        self.assertIn('"planning_feedback"', reasoner.prompts[1])
        self.assertTrue(step_consumes_budget(repaired))
        self.assertEqual(client.project.intents[1].action_key, "inspect-source")
        self.assertEqual(client.project.intents[1].crypto["params"]["input_sha256"]["value"], "a" * 64)
        self.assertNotIn("planning_feedback", client.project.crypto)
        self.assertEqual(dispatcher._crypto_plan_failures, 0)

    def test_invalid_plans_stop_with_a_contract_reason_not_no_progress(self) -> None:
        client = MemoryClient()
        reasoner = SequenceReasoner(*[[plan(valid=False)] for _ in range(3)])
        dispatcher = Dispatcher(client=client, reasoner=reasoner)
        results = [dispatcher.run_once(7) for _ in range(3)]
        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertIn("contract errors", results[-1].message)
        self.assertEqual(dispatcher._no_progress_reasons, 0)
        self.assertEqual(dispatcher._crypto_plan_failures, 3)
        self.assertFalse(any("reason made no progress" in log["message"] for log in client.logs))

    def test_genuine_empty_plans_still_stop_as_no_progress(self) -> None:
        client = MemoryClient()
        dispatcher = Dispatcher(client=client, reasoner=SequenceReasoner([], [], []))
        results = [dispatcher.run_once(7) for _ in range(3)]
        self.assertEqual(results[-1].mode, DispatchMode.SKIPPED)
        self.assertIn("no progress", results[-1].message)
        self.assertEqual(dispatcher._crypto_plan_failures, 0)

    def test_valid_plan_survives_a_mixed_round(self) -> None:
        client = MemoryClient()
        dispatcher = Dispatcher(client=client, reasoner=SequenceReasoner([
            plan(valid=False, action_key="bad-format"), plan(valid=True),
        ]))
        result = dispatcher.run_once(7)
        self.assertEqual(result.mode, DispatchMode.REASON)
        self.assertEqual(len(client.project.active_intents()), 1)
        self.assertEqual(client.project.crypto["planning_feedback"]["rejected_count"], 1)
        self.assertEqual(dispatcher._crypto_plan_failures, 0)

    def test_unknown_input_produces_feedback_instead_of_crashing_dedup(self) -> None:
        client = MemoryClient()
        invalid = plan(valid=True)
        invalid["crypto"]["input_ids"] = ["invented-input"]
        dispatcher = Dispatcher(client=client, reasoner=SequenceReasoner([invalid]))
        dispatcher.run_once(7)
        self.assertIn("crypto.input_ids", client.project.crypto["planning_feedback"]["rejected_plans"][0]["error"])

    def test_feedback_survives_sqlite_without_becoming_a_fact(self) -> None:
        client = MemoryClient()
        Dispatcher(client=client, reasoner=SequenceReasoner([plan(valid=False)])).run_once(7)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "check.db")
            store.save_project(client.project)
            restored = store.get_project(7)
        self.assertEqual(restored.crypto["planning_feedback"], client.project.crypto["planning_feedback"])
        self.assertEqual(len(restored.facts), 3)

    def test_bytes_contract_stays_strict_and_locates_nested_error(self) -> None:
        invalid = plan(valid=False)["crypto"]
        with self.assertRaisesRegex(ValueError, r"crypto.params.input_sha256: typed bytes"):
            intent_crypto(invalid)
        valid = plan(valid=True)["crypto"]
        self.assertEqual(intent_crypto(valid)["method"], "source.inspect")
        valid["params"]["input_sha256"]["length"] = 31
        with self.assertRaisesRegex(ValueError, "length or hex value is invalid"):
            intent_crypto(valid)

    def test_artifact_relative_path_is_accepted_but_escapes_are_rejected_with_help(self) -> None:
        reference = {"path": "artifacts/intents/5/result.bin", "sha256": "a" * 64,
                     "size_bytes": 2, "producer_intent_id": 5}
        self.assertEqual(artifact_ref(reference), reference)
        for path in ("/home/kali/workspace/intents/5/result.bin", "artifacts/intents/5/../secret"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "artifact path must be artifacts/intents"):
                artifact_ref({**reference, "path": path})
        with self.assertRaisesRegex(ValueError, "producer does not match"):
            artifact_ref({**reference, "producer_intent_id": 4})

    def test_all_crypto_modes_have_exact_contract_and_worker_has_current_intent_mapping(self) -> None:
        project = project_for_reason()
        intent = project.add_intent("inspect", intent_id=5, from_fact_id=2)
        builder = PromptBuilder()
        prompts = (builder.build_first_run_prompt(project, intent), builder.build_explore_prompt(project, intent),
                   builder.build_reason_prompt(project))
        for prompt in prompts:
            self.assertIn('{"type":"bytes","encoding":"hex","value":"0007","length":2}', prompt)
            self.assertIn("artifacts/intents/<id>/<file>", prompt)
        for prompt in prompts[:2]:
            self.assertIn("artifacts/intents/5/", prompt)
            self.assertIn("producer_intent_id=5", prompt)
        self.assertIn("不调用 shell", prompts[2])
        self.assertIn("自由命名的稳定动作标识", prompts[2])
        self.assertNotIn("将脚本与必要交互记录写入", prompts[2])

    def test_shell_restrictions_apply_only_to_crypto_reason(self) -> None:
        agent = CodexDockerAgent()
        crypto = project_for_reason()
        crypto.docker = "test-worker"
        general = Project(id=8, name="web", status="active", docker="test-web")
        restricted = agent._docker_command(crypto, DispatchMode.REASON, runtime_key="reason")
        self.assertIn("features.shell_tool=false", restricted)
        self.assertIn("features.unified_exec=false", restricted)
        self.assertIn("features.multi_agent=false", restricted)
        self.assertIn("read-only", restricted)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", restricted)
        self.assertEqual(restricted[-1], "-")
        for project, mode in ((general, DispatchMode.REASON), (crypto, DispatchMode.EXPLORE),
                              (crypto, DispatchMode.FIRST_RUN)):
            self.assertNotIn("features.shell_tool=false", agent._docker_command(project, mode))

    def test_crypto_reason_overrides_a_configured_write_sandbox(self) -> None:
        agent = CodexDockerAgent(codex_command=("codex", "exec", "--sandbox", "workspace-write", "--full-auto", "-"))
        project = project_for_reason()
        project.docker = "test-worker"
        command = agent._docker_command(project, DispatchMode.REASON)
        self.assertEqual(command.count("--sandbox"), 1)
        self.assertNotIn("workspace-write", command)
        self.assertNotIn("--full-auto", command)


if __name__ == "__main__":
    unittest.main()
