from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from echo_agent.crypto_contract import fact_crypto, intent_crypto
from echo_agent.dispatcher.agents import CodexWorker
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import parse_worker_result, substantive_worker_output
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import AgentRunResult, DispatchMode, IntentExecutionResult
from echo_agent.models import FactKind, IntentOutcome, IntentStatus

from tests.test_review_seven_fixes import crypto_intent, crypto_project

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def observation(description: str, claim_status: str = "observed", outcome: str = "verified") -> dict:
    return {
        "answer": None,
        "fact_description": description,
        "outcome": outcome,
        "crypto": {
            "exit_reason": "completed",
            "covered_scope": {"input_ids": ["input-1"]},
            "fact": {"claim_status": claim_status, "source_input_ids": ["input-1"]},
        },
    }


def output(*objects: dict) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in objects)


class BatchIsolationTests(unittest.TestCase):
    def test_one_malformed_object_does_not_discard_the_batch(self) -> None:
        good = observation("q=1342261 已核对")
        bad = {"answer": None, "fact_description": "无法判定的枚举", "outcome": "banana"}

        result = parse_worker_result(output(good, bad))

        self.assertEqual(result.fact_description, "q=1342261 已核对")
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("object 1", result.rejected[0])
        self.assertIn("outcome must be verified", result.rejected[0])

    def test_all_objects_rejected_raises_with_object_details(self) -> None:
        bad = {"answer": None, "fact_description": "无法判定的枚举", "outcome": "banana"}

        with self.assertRaisesRegex(ValueError, "were all rejected"):
            parse_worker_result(output(bad))

    def test_substantive_output_skips_malformed_objects(self) -> None:
        good = observation("保留的观察")
        bad = {"answer": None, "fact_description": "坏对象", "outcome": "banana"}

        text = substantive_worker_output(output(bad, good))

        self.assertIsNotNone(text)
        self.assertIn("保留的观察", text or "")
        self.assertNotIn("坏对象", text or "")

    def test_worker_logs_rejected_objects(self) -> None:
        logs: list[tuple[str, str, dict]] = []

        class Agent:
            def run(self, project, prompt, mode, **kwargs):
                return AgentRunResult(output=output(observation("有效观察"),
                                                    {"answer": None, "fact_description": "坏", "outcome": "banana"}))

        project = crypto_project()
        intent = crypto_intent(project)
        result = CodexWorker(Agent()).execute_intent(
            project, intent, DispatchMode.EXPLORE,
            log=lambda message, level, metadata: logs.append((message, level, metadata)),
        )

        self.assertEqual(result.fact_description, "有效观察")
        self.assertTrue(any(level == "warning" and "rejected" in message for message, level, _ in logs))


class HypothesisBatchTests(unittest.TestCase):
    def test_hypothesis_last_does_not_become_the_result(self) -> None:
        good = observation("q=1342261 已核对")
        hypothesis = observation("假设 q 的阶为 1030，尚未验证", claim_status="hypothesis", outcome="inconclusive")

        result = parse_worker_result(output(good, hypothesis))

        self.assertEqual(result.fact_description, "q=1342261 已核对")
        self.assertEqual(result.rejected, [])
        self.assertEqual(len(result.prior_results), 1)
        self.assertEqual(result.prior_results[0].fact_description, "假设 q 的阶为 1030，尚未验证")
        self.assertEqual(result.prior_results[0].outcome, "error")

    def test_thirteen_valid_observations_survive_a_trailing_hypothesis(self) -> None:
        observations = [observation(f"观察 {index}") for index in range(13)]
        hypothesis = observation("未验证的假设", claim_status="hypothesis", outcome="inconclusive")

        result = parse_worker_result(output(*observations, hypothesis))

        self.assertEqual(result.fact_description, "观察 12")
        self.assertEqual(len(result.prior_results), 13)
        self.assertEqual(result.rejected, [])

    def test_hypothesis_only_output_is_still_recorded_as_pending(self) -> None:
        project = crypto_project()
        intent = crypto_intent(project)
        intent.mark_running()
        hypothesis = observation("只有假设", claim_status="hypothesis", outcome="inconclusive")

        result = parse_worker_result(output(hypothesis))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

        self.assertTrue(any("只有假设" in fact.description and fact.kind == FactKind.ERROR
                            for fact in project.facts.values()))
        self.assertFalse(any("只有假设" in fact.description and fact.kind == FactKind.EVIDENCE
                             for fact in project.facts.values()))
        self.assertEqual(intent.status, IntentStatus.FAILED)

    def test_batch_with_hypothesis_completes_on_the_real_observation(self) -> None:
        project = crypto_project()
        intent = crypto_intent(project)
        intent.mark_running()
        good = observation("q=1342261 已核对")
        hypothesis = observation("未验证的假设", claim_status="hypothesis", outcome="inconclusive")

        result = parse_worker_result(output(good, hypothesis))
        Dispatcher._apply_intent_result(project, IntentExecutionResult(intent, result))

        self.assertTrue(any(fact.description == "q=1342261 已核对" and fact.kind == FactKind.EVIDENCE
                            for fact in project.facts.values()))
        self.assertTrue(any("未验证的假设" in fact.description and fact.kind == FactKind.ERROR
                            for fact in project.facts.values()))
        self.assertEqual(intent.status, IntentStatus.COMPLETED)
        self.assertEqual(intent.outcome, IntentOutcome.VERIFIED)


class OutcomeContractTests(unittest.TestCase):
    def test_crypto_prompts_define_the_allowed_outcome_values(self) -> None:
        project = crypto_project()
        intent = Dispatcher._create_initial_intent(project)
        builder = PromptBuilder()
        for prompt in (
            builder.build_first_run_prompt(project, intent),
            builder.build_explore_prompt(project, intent),
            builder.build_reason_prompt(project),
        ):
            self.assertIn("outcome 只取 verified、refuted、error", prompt)
            self.assertIn("inconclusive", prompt)

        guidance = (PROJECT_ROOT / "container" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("outcome` 只取 verified/refuted/error", guidance)


class PromptExampleContractTests(unittest.TestCase):
    @staticmethod
    def _examples(prompt: str) -> list[dict]:
        examples = []
        for line in prompt.splitlines():
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                examples.append(data)
        return examples

    def test_worker_prompt_examples_satisfy_the_crypto_contract(self) -> None:
        project = crypto_project()
        intent = Dispatcher._create_initial_intent(project)
        builder = PromptBuilder()
        checked = 0
        for prompt in (
            builder.build_first_run_prompt(project, intent),
            builder.build_explore_prompt(project, intent),
        ):
            for example in self._examples(prompt):
                if "template" not in example:
                    continue
                fact_crypto(example["crypto"]["fact"])
                # The echoed example must parse without a contract error even without the flag.
                parse_worker_result(json.dumps({**example, "template": False}, ensure_ascii=False))
                checked += 1
        self.assertEqual(checked, 2)

    def test_reason_prompt_example_satisfies_intent_contract(self) -> None:
        prompt = PromptBuilder().build_reason_prompt(crypto_project())
        checked = 0
        for example in self._examples(prompt):
            if "next_intents" not in example:
                continue
            intent_crypto(example["next_intents"][0]["crypto"])
            checked += 1
        self.assertEqual(checked, 1)

    def test_documented_typed_value_examples_are_accepted(self) -> None:
        prompt = PromptBuilder().build_reason_prompt(crypto_project())
        for type_name in ("integer", "bytes"):
            match = re.search(r'\{"type":"' + type_name + r'"[^}]*\}', prompt)
            self.assertIsNotNone(match, f"missing {type_name} example in prompt")
            fact_crypto({"typed_value": json.loads(match.group(0))})


if __name__ == "__main__":
    unittest.main()
