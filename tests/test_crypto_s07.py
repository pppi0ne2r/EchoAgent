import copy
import json
import unittest

from echo_agent.crypto_dedup import plan_crypto_intents
from echo_agent.dispatcher.agents import CodexReasoner, CodexWorker
from echo_agent.dispatcher.core import Dispatcher
from echo_agent.dispatcher.parsing import parse_worker_result
from echo_agent.dispatcher.types import AgentRunResult, IntentExecutionResult
from echo_agent.models import FactKind, IntentOutcome, Project, ProjectStatus


def project() -> Project:
    item = Project(id=70, name="crypto-s07", status=ProjectStatus.ACTIVE,
                   task_type="crypto", crypto={"schema_version": 1, "inputs": [
                       {"id": "input-1", "role": "parameters", "path": "inputs/n.txt",
                        "sha256": "a" * 64, "size_bytes": 10}]})
    item.add_fact("input", fact_id=0)
    item.add_fact("goal", fact_id=1)
    item.add_fact("n=55", fact_id=2, kind=FactKind.EVIDENCE,
                  crypto={"source_input_ids": ["input-1"],
                          "typed_value": {"type": "integer", "radix": 10, "value": "55"}})
    return item


def spec(scope=None, *, facts=None, params=None, budget=None, resume=None):
    item = {"method": "rsa.trial_division", "input_ids": ["input-1"],
            "input_fact_ids": [2] if facts is None else facts,
            "params": {"stride": 1} if params is None else params,
            "scope": {"start": 2, "end": 10} if scope is None else scope}
    if budget is not None:
        item["budget"] = budget
    if resume is not None:
        item["resume_from"] = resume
    return item


def prior(item, metadata, *, status="completed", outcome="refuted", reason="range_exhausted", covered=None):
    intent = item.add_intent("old description", from_fact_id=2, crypto=metadata)
    intent.mark_running()
    if status == "completed":
        intent.mark_completed(outcome=IntentOutcome(outcome))
    else:
        intent.mark_failed(IntentOutcome.ERROR, note=reason)
    item.crypto.setdefault("executions", []).append({
        "intent_id": intent.id, "exit_reason": reason,
        "planned_scope": metadata["scope"],
        "covered_scope": metadata["scope"] if covered is None else covered,
    })
    return intent


class MemoryClient:
    def __init__(self, item):
        self.project = copy.deepcopy(item)

    def get_project(self, project_id):
        return copy.deepcopy(self.project)

    def save_project(self, item):
        self.project = copy.deepcopy(item)
        return copy.deepcopy(item)

    def add_project_log(self, *args, **kwargs):
        pass


class FixedAgent:
    def __init__(self, output):
        self.output = output

    def run(self, *args, **kwargs):
        return AgentRunResult(output=self.output)


class CryptoS07Tests(unittest.TestCase):
    def test_same_computation_blocked_across_fact_start_and_rewording(self):
        item = project()
        prior(item, spec())
        item.add_fact("另一个图起点", fact_id=3, kind=FactKind.EVIDENCE)
        self.assertEqual(plan_crypto_intents(item, spec()), [])
        # Typed integer radix and key ordering do not create a new computation.
        self.assertEqual(plan_crypto_intents(item, spec({"end": {"type": "integer", "radix": 16,
                                                                 "value": "a"}, "start": 2})), [])

    def test_changed_params_input_version_and_new_premise_are_allowed(self):
        item = project()
        old = prior(item, spec())
        self.assertEqual(Project.from_dict(item.to_dict()).intents[old.id].crypto["input_versions"]
                         ["input-1"]["sha256"], "a" * 64)
        self.assertEqual(len(plan_crypto_intents(item, spec(params={"stride": 2}))), 1)
        item.crypto["inputs"][0]["sha256"] = "b" * 64
        self.assertEqual(len(plan_crypto_intents(item, spec())), 1)
        item.crypto["inputs"][0]["sha256"] = "a" * 64
        item.add_fact("n is odd", fact_id=3, kind=FactKind.EVIDENCE)
        self.assertEqual(len(plan_crypto_intents(item, spec(facts=[2, 3]))), 1)

    def test_adjacent_and_expanded_ranges_skip_only_conclusive_coverage(self):
        item = project()
        prior(item, spec())
        adjacent = plan_crypto_intents(item, spec({"start": 11, "end": 20}))
        self.assertEqual(adjacent[0]["scope"], {"start": 11, "end": 20})
        expanded = plan_crypto_intents(item, spec({"start": 2, "end": 20}))
        self.assertEqual(expanded[0]["scope"], {"start": 11, "end": 20})
        self.assertEqual(expanded[0]["continuation"]["reason"], "uncovered_range")
        prior(item, spec({"start": 21, "end": 30}))
        gaps = plan_crypto_intents(item, spec({"start": 2, "end": 40}))
        self.assertEqual([entry["scope"] for entry in gaps],
                         [{"start": 11, "end": 20}, {"start": 31, "end": 40}])

    def test_timeout_requires_checkpoint_or_higher_budget_and_cannot_loop(self):
        item = project()
        old = prior(item, spec(budget={"wall_seconds": 5}), status="failed", reason="timeout",
                    covered={"start": 2, "end": 5})
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 5})), [])
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 5,
                                                            "label": "renamed"})), [])
        continued = plan_crypto_intents(item, spec(budget={"wall_seconds": 10}))
        self.assertEqual(continued[0]["continuation"]["reason"], "increased_budget")
        active = item.add_intent("continued", from_fact_id=0, crypto=continued[0])
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 20})), [])
        active.mark_running()
        active.mark_failed(IntentOutcome.ERROR, note="timeout")
        item.crypto["executions"].append({"intent_id": active.id, "exit_reason": "timeout",
                                          "planned_scope": active.crypto["scope"], "covered_scope": {}})
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 10})), [])
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 20}))[0]
                         ["continuation"]["prior_intent_id"], active.id)
        checkpoint = {"path": f"artifacts/intents/{old.id}/state.bin", "sha256": "c" * 64,
                      "size_bytes": 8, "producer_intent_id": old.id}
        item.crypto["executions"][0]["checkpoint_ref"] = checkpoint
        # The latest failed attempt has no checkpoint: an older one cannot bypass it.
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 10},
                                                        resume=checkpoint)), [])

    def test_timeout_reported_coverage_is_not_treated_as_proven_range(self):
        item = project()
        prior(item, spec(budget={"wall_seconds": 5}), status="failed", reason="timeout",
              covered={"start": 2, "end": 5})
        retry = plan_crypto_intents(item, spec(budget={"wall_seconds": 10}))
        self.assertEqual(retry[0]["scope"], {"start": 2, "end": 10})
        expanded = plan_crypto_intents(item, spec({"start": 2, "end": 11},
                                                  budget={"wall_seconds": 5}))
        self.assertEqual(expanded[0]["scope"], {"start": 11, "end": 11})
        self.assertEqual(expanded[0]["continuation"]["reason"], "new_unattempted_range")

    def test_same_range_has_finite_retry_count(self):
        item = project()
        prior(item, spec(budget={"wall_seconds": 5}), status="failed", reason="timeout", covered={})
        prior(item, spec(budget={"wall_seconds": 10}), status="failed", reason="timeout", covered={})
        self.assertEqual(len(plan_crypto_intents(item, spec(budget={"wall_seconds": 20}))), 1)
        prior(item, spec(budget={"wall_seconds": 20}), status="failed", reason="timeout", covered={})
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 30})), [])

    def test_spelling_out_the_implicit_default_budget_is_not_a_retry(self):
        item = project()
        prior(item, spec(), status="failed", reason="timeout", covered={})
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 300})), [])
        self.assertEqual(len(plan_crypto_intents(item, spec(budget={"wall_seconds": 301}))), 1)

    def test_retry_compares_budget_recorded_at_execution_time(self):
        item = project()
        prior(item, spec(), status="failed", reason="timeout", covered={})
        item.crypto["executions"][-1]["budget"] = {"wall_seconds": 300,
                                                     "memory_mb": 1024,
                                                     "output_bytes": 4 * 1024 * 1024}
        item.crypto["default_budget"] = {"wall_seconds": 100}
        self.assertEqual(plan_crypto_intents(item, spec(budget={"wall_seconds": 200})), [])
        self.assertEqual(len(plan_crypto_intents(item, spec(budget={"wall_seconds": 400}))), 1)

    def test_checkpoint_resume_and_nonrange_scope(self):
        item = project()
        metadata = spec({"checks": ["format"]}, budget={"wall_seconds": 5})
        old = prior(item, metadata, status="failed", reason="timeout", covered={})
        checkpoint = {"path": f"artifacts/intents/{old.id}/state.bin", "sha256": "c" * 64,
                      "size_bytes": 8, "producer_intent_id": old.id}
        item.crypto["executions"][0]["checkpoint_ref"] = checkpoint
        self.assertEqual(plan_crypto_intents(item, spec({"checks": ["format"]},
                                                        budget={"wall_seconds": 5})), [])
        resumed = plan_crypto_intents(item, spec({"checks": ["format"]},
                                                budget={"wall_seconds": 5}, resume=checkpoint))
        self.assertEqual(resumed[0]["continuation"]["reason"], "checkpoint")
        continuation = item.add_intent("resume", from_fact_id=0, crypto=resumed[0])
        continuation.mark_running()
        continuation.mark_failed(IntentOutcome.ERROR, note="timeout")
        same_state = {**checkpoint, "path": f"artifacts/intents/{continuation.id}/state.bin",
                      "producer_intent_id": continuation.id}
        item.crypto["executions"].append({"intent_id": continuation.id,
                                          "exit_reason": "timeout", "planned_scope": continuation.crypto["scope"],
                                          "covered_scope": {}, "checkpoint_ref": same_state})
        self.assertEqual(plan_crypto_intents(item, spec({"checks": ["format"]},
                                                        budget={"wall_seconds": 5}, resume=same_state)), [])

    def test_worker_checkpoint_record_is_available_for_resume(self):
        item = project()
        intent = item.add_intent("search", from_fact_id=2, crypto=spec(budget={"wall_seconds": 5}))
        intent.mark_running()
        checkpoint = {"path": f"artifacts/intents/{intent.id}/state.bin", "sha256": "d" * 64,
                      "size_bytes": 20, "producer_intent_id": intent.id}
        result = parse_worker_result(json.dumps({"answer": None, "fact_description": None,
            "outcome": "error", "crypto": {"exit_reason": "timeout", "covered_scope": {},
                                            "checkpoint_ref": checkpoint}}))
        Dispatcher._apply_intent_result(item, IntentExecutionResult(intent, result))
        self.assertEqual(item.crypto["executions"][-1]["checkpoint_ref"], checkpoint)
        self.assertEqual(intent.outcome, IntentOutcome.ERROR)
        next_plan = plan_crypto_intents(item, spec(budget={"wall_seconds": 5}, resume=checkpoint))
        self.assertEqual(next_plan[0]["continuation"]["reason"], "checkpoint")

    def test_reason_flow_preserves_general_dedup_and_crypto_fragments(self):
        item = project()
        prior(item, spec())
        item.add_fact("另一个起点", fact_id=3, kind=FactKind.EVIDENCE)
        output = json.dumps({"completed": False, "next_intents": [
            {"description": "全新文字，相同计算", "from": 3, "action_key": "new-name",
             "crypto": spec()},
            {"description": "扩大搜索", "from": 3, "action_key": "new-name",
             "crypto": spec({"start": 2, "end": 20})},
        ]})
        client = MemoryClient(item)
        dispatcher = Dispatcher(client=client, worker=CodexWorker(FixedAgent("")),
                                reasoner=CodexReasoner(FixedAgent(output)))
        dispatcher._reason(client.project, release_project=False)
        added = [intent for intent in client.project.intents.values() if intent.id != 0]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].crypto["scope"], {"start": 11, "end": 20})
        self.assertEqual(added[0].crypto["input_fact_ids"], [2])


if __name__ == "__main__":
    unittest.main()
