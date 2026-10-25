"""BUG-033: budgets placed on the intent are migrated into crypto.budget."""

from __future__ import annotations

import json
import unittest

from echo_agent.crypto_budget import resolve_crypto_budget
from echo_agent.crypto_dedup import plan_crypto_intents
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import parse_reason_result
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.models import Project, ProjectStatus


def _project() -> Project:
    project = Project(
        id=7, name="budget", status=ProjectStatus.ACTIVE, task_type="crypto",
        crypto={"schema_version": 1, "inputs": [
            {"id": "input-1", "role": "input", "path": "inputs/x",
             "sha256": "0" * 64, "size_bytes": 1},
        ]},
    )
    project.add_fact("start", fact_id=0)
    project.add_fact("goal", fact_id=1)
    return project


def _item(*, with_budget: bool = True) -> dict:
    item = {
        "description": "recover state",
        "from": 0,
        "to": 1,
        "action_key": "recover-state",
        "crypto": {"method": "mt.test", "input_ids": ["input-1"],
                   "input_fact_ids": [0], "params": {},
                   "scope": {"input_ids": ["input-1"]}},
    }
    if with_budget:
        item["budget"] = {"wall_seconds": 1200, "memory_mb": 2048, "output_bytes": 4194304}
    return item


class ReasonBudgetParsingTests(unittest.TestCase):
    def test_reason_intent_keeps_top_level_budget(self) -> None:
        result = parse_reason_result(json.dumps({
            "completed": False,
            "answer": None,
            "next_intents": [{
                "from": 0, "to": None, "action_key": "recover-state",
                "description": "recover state",
                "budget": {"wall_seconds": 1200, "memory_mb": 2048, "output_bytes": 4194304},
                "crypto": {"method": "mt.test", "input_ids": ["input-1"],
                           "scope": {"input_ids": ["input-1"]}},
            }],
        }))
        self.assertEqual(result.next_intents[0]["budget"]["wall_seconds"], 1200)


class CryptoPlanBudgetTests(unittest.TestCase):
    def test_top_level_budget_is_migrated(self) -> None:
        project = _project()
        metadata = Dispatcher._crypto_plan_metadata(project, _item(), 0)
        self.assertEqual(metadata["budget"]["wall_seconds"], 1200)
        self.assertEqual(resolve_crypto_budget(None, metadata["budget"]).wall_seconds, 1200)

        planned = plan_crypto_intents(project, metadata)
        self.assertTrue(planned)
        self.assertEqual(planned[0]["budget"]["wall_seconds"], 1200)

    def test_version_fields_are_not_migrated_from_the_intent(self) -> None:
        project = _project()
        item = _item()
        item["premise_versions"] = {"0": "forged"}
        item["input_versions"] = {"input-1": {"sha256": "f" * 64, "size_bytes": 1}}
        metadata = Dispatcher._crypto_plan_metadata(project, item, 0)
        self.assertNotEqual(metadata.get("premise_versions"), {"0": "forged"})
        self.assertNotIn("input_versions", metadata)

    def test_nested_budget_wins_over_a_misplaced_duplicate(self) -> None:
        project = _project()
        item = _item()
        item["crypto"]["budget"] = {"wall_seconds": 600, "memory_mb": 1024,
                                    "output_bytes": 4194304}
        metadata = Dispatcher._crypto_plan_metadata(project, item, 0)
        self.assertEqual(metadata["budget"]["wall_seconds"], 600)


class BudgetPromptTests(unittest.TestCase):
    def test_reason_prompt_names_the_crypto_budget_location(self) -> None:
        project = _project()
        prompt = PromptBuilder().build_reason_prompt(project)
        self.assertIn("预算字段位置", prompt)
        self.assertIn("crypto.budget", prompt)
        self.assertIn("不要写在 intent 顶层", prompt)

    def test_reason_context_warns_after_timeouts(self) -> None:
        project = _project()
        project.crypto["executions"] = [{
            "intent_id": 1, "exit_reason": "timeout",
            "budget": {"wall_seconds": 300, "memory_mb": 1024, "output_bytes": 4194304},
        }]
        prompt = PromptBuilder().build_reason_prompt(project)
        self.assertIn("budget_note", prompt)
        self.assertIn("timeout 结束", prompt)
        self.assertIn('"wall_seconds": 300', prompt)


if __name__ == "__main__":
    unittest.main()
