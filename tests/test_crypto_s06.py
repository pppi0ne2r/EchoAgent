from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from echo_agent.dispatcher.agents import CodexReasoner, CodexWorker
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import parse_reason_result, parse_worker_result
from echo_agent.dispatcher.prompts import PromptBuilder, _evidence_items
from echo_agent.dispatcher.types import AgentRunResult, DispatchMode, IntentExecutionResult, WorkerResult
from echo_agent.models import FactKind, IntentOutcome, Project, ProjectStatus
from echo_agent.server.app import create_app
from echo_agent.storage import SQLiteProjectStore


class MemoryClient:
    def __init__(self, project: Project) -> None:
        self.project = copy.deepcopy(project)
        self.logs = []

    def get_project(self, project_id: int) -> Project:
        return copy.deepcopy(self.project)

    def save_project(self, project: Project) -> Project:
        self.project = copy.deepcopy(project)
        return copy.deepcopy(project)

    def add_project_log(self, project_id: int, message: str, level: str = "info", metadata=None) -> None:
        self.logs.append((message, level, metadata))


class FixedAgent:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, project, prompt, mode, **kwargs) -> AgentRunResult:
        return AgentRunResult(output=self.output)


def crypto_project() -> Project:
    project = Project(
        id=7, name="rsa", status=ProjectStatus.ACTIVE, task_type="crypto", objective="factor n",
        crypto={"schema_version": 1, "inputs": [
            {"id": "input-1", "role": "parameters", "path": "inputs/rsa.txt",
             "sha256": "0" * 64, "size_bytes": 14},
        ]},
    )
    project.add_fact("开始分析", fact_id=0)
    project.add_fact("恢复明文", fact_id=1)
    return project


class CryptoS06Tests(unittest.TestCase):
    def test_crypto_prompts_are_isolated_from_web_prompts(self) -> None:
        project = crypto_project()
        intent = Dispatcher._create_initial_intent(project)
        builder = PromptBuilder()
        for prompt in (builder.build_first_run_prompt(project, intent),
                       builder.build_explore_prompt(project, intent), builder.build_reason_prompt(project)):
            self.assertIn("密码学", prompt)
            self.assertIn("input-1", prompt)
            self.assertNotIn("上传漏洞", prompt)
            self.assertNotIn("目录枚举", prompt)
        self.assertEqual(intent.crypto["method"], "inspect.inputs")
        self.assertEqual(intent.crypto["input_ids"], ["input-1"])
        general = Project(id=8, name="web", status="active", target_url="https://example.test")
        general.add_fact("start", fact_id=0)
        general.add_fact("goal", fact_id=1)
        self.assertIn("文件上传漏洞", builder.build_reason_prompt(general))

    def test_fixed_worker_and_reasoner_outputs_round_trip(self) -> None:
        project = crypto_project()
        first_output = "\n".join([
            json.dumps({"answer": None, "fact_description": "从 input-1 核对得到 n=55", "outcome": "verified",
                "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                           "fact": {"claim_status": "observed", "source_input_ids": ["input-1"], "typed_value": {
                               "type": "integer", "radix": 10, "value": "55"}}}}),
            json.dumps({"answer": None, "fact_description": "参数 e=3 已核对", "outcome": "verified",
                "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                           "fact": {"claim_status": "observed", "source_input_ids": ["input-1"], "typed_value": {
                               "type": "integer", "radix": 10, "value": "3"}}}}),
        ])
        client = MemoryClient(project)
        worker = CodexWorker(FixedAgent(first_output))
        reason_output = json.dumps({"completed": False, "answer": None, "stop_reason": "",
            "next_intents": [{"from": 2, "to": None, "action_key": "check-small-factor",
                "description": "只在 2 到 10 之间检查 n=55 的因子",
                "crypto": {"method": "factor.trial", "input_ids": ["input-1"],
                           "input_fact_ids": [2], "params": {"n": {
                               "type": "integer", "radix": 10, "value": "55"}},
                           "scope": {"start": 2, "end": 10}}}]})
        dispatcher = Dispatcher(client=client, worker=worker, reasoner=CodexReasoner(FixedAgent(reason_output)))
        self.assertEqual(dispatcher.run_once(project.id).mode, DispatchMode.FIRST_RUN)
        first = client.project
        self.assertEqual(first.facts[2].crypto["typed_value"]["value"], "55")
        self.assertEqual(first.facts[3].crypto["typed_value"]["value"], "3")
        self.assertEqual(first.facts[2].crypto["producer_intent_id"], 0)
        self.assertEqual(first.intents[0].outcome, IntentOutcome.VERIFIED)
        self.assertEqual(first.crypto["executions"][0]["exit_reason"], "completed")
        self.assertEqual(dispatcher.run_once(project.id).mode, DispatchMode.REASON)
        planned = client.project.intents[1]
        self.assertEqual(planned.crypto["method"], "factor.trial")
        self.assertEqual(planned.crypto["scope"], {"start": 2, "end": 10})
        self.assertEqual(planned.crypto["params"]["n"]["value"], "55")

        negative = parse_worker_result(json.dumps({"answer": None,
            "fact_description": "2..10 范围内未发现因子", "outcome": "refuted",
            "crypto": {"exit_reason": "range_exhausted", "covered_scope": {"start": 2, "end": 10},
                       "fact": {"claim_status": "observed", "observation_kind": "evidence", "source_input_ids": ["input-1"]}}}))
        planned.mark_running()
        Dispatcher._apply_intent_result(client.project, IntentExecutionResult(planned, negative))
        self.assertEqual(planned.outcome, IntentOutcome.REFUTED)
        self.assertEqual(client.project.facts[4].kind, FactKind.EVIDENCE)
        self.assertIn('"end": 10', client.project.facts[4].description)
        self.assertEqual(client.project.crypto["executions"][-1]["exit_reason"], "range_exhausted")

    def test_gcd_negative_observation_is_evidence_but_hypothesis_refuted(self) -> None:
        project = crypto_project()
        source = project.add_fact("模数 n1,n2 已核对", crypto={"source_input_ids": ["input-1"]})
        intent = project.add_intent("检查共享因子", from_fact_id=source.id, crypto={
            "method": "rsa.gcd", "input_ids": ["input-1"], "input_fact_ids": [source.id],
            "scope": {"input_ids": ["input-1"]},
        })
        intent.mark_running()
        result = parse_worker_result(json.dumps({"answer": None,
            "fact_description": "gcd(n1,n2)=1；指定模数无共享因子", "outcome": "refuted",
            "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                       "fact": {"claim_status": "observed", "observation_kind": "evidence", "source_input_ids": ["input-1"]}}}))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
        fact = project.facts[intent.to_fact_id]
        self.assertEqual(fact.kind, FactKind.EVIDENCE)
        self.assertEqual(intent.outcome, IntentOutcome.REFUTED)
        self.assertEqual(fact.crypto["producer_intent_id"], intent.id)
        self.assertEqual(fact.crypto["source_input_ids"], ["input-1"])

    def test_unknown_and_timeout_keep_prior_facts_without_global_refutation(self) -> None:
        for reason in ("solver_unknown", "timeout", "tool_error"):
            with self.subTest(reason=reason):
                project = crypto_project()
                source = project.add_fact("n=55", crypto={"source_input_ids": ["input-1"]})
                intent = project.add_intent("尝试有界求解", from_fact_id=source.id, crypto={
                    "method": "factor.trial", "input_ids": ["input-1"], "scope": {"start": 2, "end": 1000},
                })
                intent.mark_running()
                output = "\n".join([
                    json.dumps({"answer": None, "fact_description": "已核对 n 为奇数", "outcome": "verified",
                        "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                                   "fact": {"claim_status": "observed", "confirmed": True}}}),
                    json.dumps({"answer": None, "fact_description": None, "outcome": "error",
                        "crypto": {"exit_reason": reason, "covered_scope": {"start": 2, "end": 5}}}),
                ])
                result = parse_worker_result(output)
                Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
                self.assertEqual(intent.outcome, IntentOutcome.ERROR)
                self.assertEqual(project.facts[3].kind, FactKind.EVIDENCE)
                self.assertEqual(project.crypto["executions"][-1]["covered_scope"], {"start": 2, "end": 5})
                self.assertFalse(any(fact.kind == FactKind.REFUTED for fact in project.facts.values()))

    def test_invalid_late_observation_does_not_discard_earlier_fact(self) -> None:
        project = crypto_project()
        source = project.add_fact("输入已核对", crypto={"source_input_ids": ["input-1"]})
        intent = project.add_intent("检查参数", from_fact_id=source.id, crypto={
            "method": "inspect.params", "input_ids": ["input-1"],
            "scope": {"input_ids": ["input-1"]},
        })
        intent.mark_running()
        output = "\n".join([
            json.dumps({"answer": None, "fact_description": "n=55 已核对", "outcome": "verified",
                "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                           "fact": {"claim_status": "observed", "typed_value": {"type": "integer", "radix": 10, "value": "55"}}}}),
            json.dumps({"answer": None, "fact_description": "无效的 e 参数", "outcome": "verified",
                "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                           "fact": {"claim_status": "observed", "typed_value": {"type": "integer", "radix": 7, "value": "3"}}}}),
        ])
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, parse_worker_result(output)))
        self.assertEqual(project.facts[3].crypto["typed_value"]["value"], "55")
        self.assertEqual(intent.outcome, IntentOutcome.ERROR)
        self.assertEqual(project.crypto["executions"][-1]["exit_reason"], "tool_error")
        self.assertEqual(Project.from_dict(project.to_dict()).facts[3].description, "n=55 已核对")

    def test_old_crypto_evidence_keeps_full_structured_reference(self) -> None:
        project = crypto_project()
        for index in range(45):
            project.add_fact("long observation " + "x" * 250, crypto={"typed_value": {
                "type": "integer", "radix": 16, "value": format(index + 1, "x")}})
        items = _evidence_items(project)
        self.assertIn("已截断", items[0]["description"])
        self.assertEqual(items[0]["crypto"]["typed_value"]["value"], "1")
        self.assertEqual(items[0]["fact_ref"], "/projects/7/facts/2")
        self.assertEqual(items[-1]["crypto"]["typed_value"]["value"], "2d")
        project.add_intent("保存产物", from_fact_id=2, intent_id=3)
        artifact_fact = project.add_fact("有界计算产物", crypto={"producer_intent_id": 3, "artifact_refs": [
            {"path": "artifacts/intents/3/result.txt", "sha256": "a" * 64,
             "size_bytes": 12, "producer_intent_id": 3},
        ]})
        self.assertEqual(_evidence_items(project)[-1]["artifact_worker_paths"],
                         ["/home/kali/workspace/intents/3/result.txt"])
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteProjectStore(Path(directory) / "state.db")
            store.save_project(project)
            class RuntimeStub:
                def prepare_shared_resources(self) -> None:
                    pass
            with patch("echo_agent.server.app.DockerProjectRuntime.from_settings", return_value=RuntimeStub()):
                app = create_app(store)
            with TestClient(app) as http:
                full = http.get(items[0]["fact_ref"])
                self.assertEqual(full.status_code, 200)
                self.assertEqual(full.json()["description"], project.facts[2].description)
                self.assertEqual(full.json()["crypto"]["typed_value"]["value"], "1")

    def test_hypothesis_is_not_saved_as_verified_fact(self) -> None:
        project = crypto_project()
        intent = project.add_intent("检查某个猜想", from_fact_id=0, crypto={
            "method": "rsa.inspect", "input_ids": ["input-1"], "scope": {"input_ids": ["input-1"]},
        })
        intent.mark_running()
        result = parse_worker_result(json.dumps({"answer": None,
            "fact_description": "可能存在弱因子", "outcome": "verified",
            "crypto": {"exit_reason": "completed", "covered_scope": {"input_ids": ["input-1"]},
                       "fact": {"claim_status": "hypothesis"}}}))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))
        self.assertEqual(intent.outcome, IntentOutcome.EMPTY)
        self.assertIn("unverified hypothesis", intent.note)
        self.assertEqual(len(project.facts), 3)
        self.assertEqual(project.facts[2].kind, FactKind.ERROR)
        self.assertFalse(project.facts[2].is_plannable)

    def test_crypto_completion_is_not_accepted_before_independent_verification(self) -> None:
        project = crypto_project()
        fact = project.add_fact("candidate plaintext 7", crypto={"source_input_ids": ["input-1"]})
        done = project.add_intent("initial analysis", from_fact_id=0, to_fact_id=fact.id)
        done.mark_running()
        done.mark_completed(to_fact_id=fact.id)
        client = MemoryClient(project)
        reasoner = CodexReasoner(FixedAgent(json.dumps({"completed": True, "answer": "7", "next_intents": []})))
        dispatcher = Dispatcher(client=client, worker=CodexWorker(FixedAgent("")), reasoner=reasoner)
        dispatcher.run_once(project.id)
        self.assertNotEqual(client.project.status, ProjectStatus.COMPLETED)
        self.assertEqual(client.project.intents[1].crypto["method"], "verify.candidate")


if __name__ == "__main__":
    unittest.main()
