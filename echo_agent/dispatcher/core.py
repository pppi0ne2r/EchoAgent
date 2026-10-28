from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Mapping
from uuid import uuid4

from echo_agent.crypto_contract import intent_crypto, project_crypto
from echo_agent.crypto_budget import resolve_crypto_budget
from echo_agent.crypto_dedup import plan_crypto_intents, validate_checkpoint_resume
from echo_agent.crypto_identity import premise_versions, premises_current
from echo_agent.crypto_verification import DEFAULT_VERIFY_SECONDS, MAX_VERIFY_SECONDS
from echo_agent.crypto_verify import (VERSION as CRYPTO_VERIFY_VERSION, completion_source,
                                      verification_context)
from echo_agent.evidence import ANSWER_WRAPPER_PREFIX, PARTIAL_OUTPUT_PREFIX, answer_source_line, fact_supports_answer
from echo_agent.dispatcher.agents import (
    CryptoExecutionLimit,
    CodexDockerAgent,
    CodexReasoner,
    CodexWorker,
    UpstreamFatalError,
    fatal_upstream_marker,
)
from echo_agent.dispatcher.client import ProjectClient
from echo_agent.dispatcher.objectives import required_finding_count
from echo_agent.dispatcher.parsing import (
    ReasonFormatError,
    _optional_fact_description,
    is_template_reason_intent,
    parse_reason_result,
    parse_worker_result,
    repair_reason_output,
    strip_prompt_template_text,
    strip_result_json_text,
)
from echo_agent.dispatcher.prompts import PromptBuilder
from echo_agent.dispatcher.types import (
    AgentAdapter,
    DispatchMode,
    DispatchResult,
    IntentExecutionResult,
    ReasonResult,
    Reasoner,
    WorkerAdapter,
    WorkerObservation,
    WorkerResult,
)
from echo_agent.dispatcher.utils import callable_accepts_mode
from echo_agent.models import (
    FactKind,
    Intent,
    IntentOutcome,
    IntentStatus,
    Project,
    ProjectStatus,
    action_keys_equivalent,
    descriptions_equivalent,
)


def step_consumes_budget(result: DispatchResult) -> bool:
    message = result.message or ""
    if message.startswith("planned 0"):
        return False
    if message in {
        "reason failed; will retry",
        "reason output was not JSON; will retry",
        "reason skipped",
        "reason timed out; will retry",
        "reason salvaged after timeout; will retry",
        "crypto plan contract rejected; will retry",
    }:
        return False
    return True


def step_requests_trailing_explore(result: DispatchResult) -> bool:
    message = result.message or ""
    return message.startswith("planned ") and not message.startswith("planned 0")


class Dispatcher:
    _OUTPUT_TAIL_MARKER = "\noutput_tail:\n"
    _PARTIAL_OUTPUT_PREFIX = PARTIAL_OUTPUT_PREFIX
    _PARTIAL_OUTPUT_MIN_CHARS = 40
    _PARTIAL_OUTPUT_LIMIT = 4000
    # reason sometimes puts these crypto fields on the intent instead of under crypto.
    _MISPLACED_CRYPTO_FIELDS = ("method", "input_ids", "input_fact_ids", "params", "scope",
                                "budget", "resume_from")

    def __init__(
        self,
        client: ProjectClient,
        worker: WorkerAdapter | None = None,
        reasoner: Reasoner | None = None,
        agent: AgentAdapter | None = None,
        prompt_builder: PromptBuilder | None = None,
        max_explore_workers: int = 4,
        max_codex_slots: int = 2,
        max_retryable_attempts: int = 3,
        stop_event: threading.Event | None = None,
        codex_model: str | None = None,
        codex_mode_models: Mapping[DispatchMode | str, str] | None = None,
        codex_workspace: str = "/home/kali/workspace",
        codex_timeout_seconds: int = 1800,
        codex_no_output_timeout_seconds: int = 900,
        codex_mode_no_output_timeout_seconds: Mapping[DispatchMode | str, int] | None = None,
        codex_mode_timeout_seconds: Mapping[DispatchMode | str, int] | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        codex_command: tuple[str, ...] | None = None,
        codex_provider_name: str = "echo_agent_proxy",
        codex_provider_supports_websockets: bool = False,
        codex_request_max_retries: int = 2,
        codex_stream_max_retries: int = 2,
        codex_stream_idle_timeout_ms: int = 60_000,
    ) -> None:
        self.client = client
        if max_explore_workers < 1:
            raise ValueError("max_explore_workers must be at least 1")
        if max_codex_slots < 1:
            raise ValueError("max_codex_slots must be at least 1")
        if max_retryable_attempts < 1:
            raise ValueError("max_retryable_attempts must be at least 1")
        self.max_explore_workers = max_explore_workers
        self.max_codex_slots = max_codex_slots
        self.max_retryable_attempts = max_retryable_attempts
        self._model_slots = threading.BoundedSemaphore(max_codex_slots)
        self._crypto_compute_slots = threading.BoundedSemaphore(1)
        self._stop_event = stop_event or threading.Event()
        self._state_lock = threading.Lock()
        self._verification_lock = threading.Lock()
        self._active_verifications: dict[int, str] = {}
        self._retry_counts: dict[int, int] = {}
        self._reason_failures = 0
        self._reason_format_failures = 0
        self._reason_salvage_failures = 0
        self._crypto_plan_failures = 0
        # Set once when the upstream reports a quota/credential failure.  No retry, salvage or
        # further step can succeed, so the dispatcher stops with an explicit diagnosis.
        self._fatal_upstream_error: str | None = None
        self.max_reason_salvage_failures = max_retryable_attempts * 2
        self._no_progress_reasons = 0
        self._target_offline_streak = 0
        if agent is None:
            agent = CodexDockerAgent(
                codex_command=codex_command,
                model=codex_model,
                mode_models=codex_mode_models,
                workspace=codex_workspace,
                timeout_seconds=codex_timeout_seconds,
                no_output_timeout_seconds=codex_no_output_timeout_seconds,
                mode_no_output_timeout_seconds=codex_mode_no_output_timeout_seconds,
                mode_timeout_seconds=codex_mode_timeout_seconds,
                openai_api_key=openai_api_key,
                openai_base_url=openai_base_url,
                provider_name=codex_provider_name,
                provider_supports_websockets=codex_provider_supports_websockets,
                request_max_retries=codex_request_max_retries,
                stream_max_retries=codex_stream_max_retries,
                stream_idle_timeout_ms=codex_stream_idle_timeout_ms,
            )
        self.agent = agent
        record_usage = getattr(self.client, "add_token_usage", None)
        if callable(record_usage):
            try:
                agent.on_token_usage = record_usage
            except (AttributeError, TypeError):
                pass
        self.worker = worker or CodexWorker(agent, prompt_builder)
        self.reasoner = reasoner or CodexReasoner(agent, prompt_builder)
        self._worker_accepts_mode = callable_accepts_mode(self.worker.execute_intent)
        self._worker_accepts_log = callable_accepts_mode(self.worker.execute_intent, parameter="log")
        self._reasoner_accepts_log = callable_accepts_mode(self.reasoner.plan, parameter="log")

    def request_stop(self) -> None:
        self._stop_event.set()
        cancel = getattr(self.agent, "cancel_running", None)
        if callable(cancel):
            cancel()
        self._cancel_active_verifications()

    def _cancel_active_verifications(self) -> None:
        cancel = getattr(self.client, "cancel_crypto_verification", None)
        if not callable(cancel):
            return
        with self._verification_lock:
            active = list(self._active_verifications.items())
        for project_id, verification_id in active:
            try:
                cancel(project_id, verification_id)
            except Exception as exc:  # cancellation is best-effort; the deadline still applies
                self._log(
                    project_id,
                    f"failed to cancel verification {verification_id}: {exc}",
                    "warning",
                )

    @staticmethod
    def _verification_timeout(project: Project, intent: Intent) -> float:
        budget = intent.crypto.get("budget")
        default_budget = project.crypto.get("default_budget")
        requested = None
        if isinstance(budget, dict):
            requested = budget.get("wall_seconds")
        if type(requested) is not int and isinstance(default_budget, dict):
            requested = default_budget.get("wall_seconds")
        if type(requested) is not int:
            requested = DEFAULT_VERIFY_SECONDS
        return float(max(1, min(requested, MAX_VERIFY_SECONDS)))

    def run_once(self, project_id: int, release_project: bool = True) -> DispatchResult:
        project = self.client.get_project(project_id)
        return self._run_project_step(project, release_project=release_project)

    def run_loop(
        self,
        project_id: int,
        interval_seconds: float = 3.0,
        max_steps: int | None = None,
    ) -> list[DispatchResult]:
        if max_steps == 0:
            return []
        results: list[DispatchResult] = []
        try:
            steps = 0
            while max_steps is None or steps < max_steps:
                if self._stop_event.is_set():
                    break
                project = self.client.get_project(project_id)
                result = self._run_project_step(project, release_project=False)
                results.append(result)
                if result.halt or result.mode in {DispatchMode.COMPLETED, DispatchMode.SKIPPED} or self._stop_event.is_set():
                    break
                if step_consumes_budget(result):
                    steps += 1
                    if max_steps is not None and steps >= max_steps:
                        self._append_trailing_explore(project_id, result, results)
                        break
                time.sleep(interval_seconds)
            return results
        finally:
            try:
                self._release_project_if_running(project_id)
            except Exception:
                pass

    def _run_project_step(self, project: Project, release_project: bool) -> DispatchResult:
        if project.is_completed:
            return DispatchResult(
                project.id,
                DispatchMode.COMPLETED,
                project.status,
                "project already completed",
            )

        fatal = self._fatal_upstream_result(project)
        if fatal is not None:
            return fatal

        if not project.can_execute and project.status != ProjectStatus.RUNNING:
            return DispatchResult(
                project.id,
                DispatchMode.SKIPPED,
                project.status,
                f"project is not executable in status {project.status.value}",
            )

        recovered = self._recover_stranded_intents(project)
        mode = self._enter_running(project)
        self.client.save_project(project)
        self._log(project.id, f"project entered {mode.value} mode", "info", {"status": project.status.value})
        if recovered:
            self._log(
                project.id,
                f"recovered {recovered} stranded running intents",
                "warning",
                {"recovered": recovered},
            )

        if project.is_first_run:
            intent = self._create_initial_intent(project)
            return self._first_run(project, intent, release_project=release_project)

        intents = project.active_intents()
        if intents:
            return self._explore(project, intents, mode, release_project=release_project)
        return self._reason(project, release_project=release_project)

    def _first_run(self, project: Project, intent: Intent, release_project: bool) -> DispatchResult:
        self._log(project.id, "first-run initial intent started", "info", {"intent_id": intent.id})
        result = self._run_intent_with_slot(project, intent, DispatchMode.FIRST_RUN, scope="first-run")
        item = IntentExecutionResult(intent=intent, result=result)
        before_facts = set(project.facts)
        try:
            self._commit_intent_result(project, item, release_project=release_project)
        except Exception as exc:
            self._log(
                project.id,
                f"first-run result was not saved: {type(exc).__name__}: {exc}",
                "error",
                {"intent_id": intent.id},
            )
        result = item.result
        self._ensure_first_run_failure_fact(project, intent, result, before_facts, release_project)
        self._log(
            project.id,
            "first-run finished",
            "info" if not result.retryable else "warning",
            {
                "intent_id": intent.id,
                "retryable": result.retryable,
                "fact_ids": self._new_fact_ids(project, before_facts),
            },
        )
        self._observe_target(item.result)
        fatal = self._fatal_upstream_result(project)
        if fatal is not None:
            return fatal
        offline = self._target_offline_result(project)
        if offline is not None:
            return offline
        if self._failed_first_run_waits_for_reason(project, intent, result):
            return DispatchResult(
                project.id,
                DispatchMode.FIRST_RUN,
                project.status,
                "initial intent failed; fact saved, reason waits for the next run",
                halt=True,
            )
        return DispatchResult(project.id, DispatchMode.FIRST_RUN, project.status, "initial intent executed")

    def _explore(
        self,
        project: Project,
        intents: list[Intent],
        mode: DispatchMode,
        release_project: bool,
    ) -> DispatchResult:
        intent_ids = [intent.id for intent in intents]
        started_message = f"explore started with {len(intents)} active intents"
        if intent_ids:
            started_message = f"{started_message}: {'、'.join(f'#{intent_id}' for intent_id in intent_ids)}"
        self._log(
            project.id,
            started_message,
            "info",
            {"intent_ids": intent_ids, "codex_slots": self.max_codex_slots},
        )

        finished_count = 0
        released_count = 0
        finished_items: list[IntentExecutionResult] = []
        for item in self._execute_intents(project, intents):
            before_facts = set(project.facts)
            try:
                self._commit_intent_result(project, item)
            except Exception as exc:
                self._log(
                    project.id,
                    f"explore intent {item.intent.id} result was not saved: {type(exc).__name__}: {exc}",
                    "error",
                    {"intent_id": item.intent.id},
                )
            if item.result.retryable:
                released_count += 1
                level = "warning"
                message = f"explore intent {item.intent.id} released for retry"
            else:
                finished_count += 1
                level = "info"
                message = f"explore intent {item.intent.id} finished"
            self._log(
                project.id,
                message,
                level,
                {
                    "intent_id": item.intent.id,
                    "finished_count": finished_count,
                    "released_count": released_count,
                    "total_count": len(intents),
                    "has_fact": bool(item.result.fact_description),
                    "has_answer": bool(item.result.answer),
                    "retryable": item.result.retryable,
                    "fact_ids": self._new_fact_ids(project, before_facts),
                },
            )
            finished_items.append(item)
        for item in sorted(finished_items, key=lambda finished: finished.intent.id):
            self._observe_target(item.result)

        with self._state_lock:
            self._persist_step(project, release_project=release_project)
        self._log(
            project.id,
            f"explore finished {finished_count} intents, released {released_count}",
            "info",
        )
        fatal = self._fatal_upstream_result(project)
        if fatal is not None:
            return fatal
        offline = self._target_offline_result(project)
        if offline is not None:
            return offline
        return DispatchResult(
            project.id,
            mode,
            project.status,
            f"executed {finished_count} intents, released {released_count}",
        )

    def _reason(self, project: Project, release_project: bool) -> DispatchResult:
        self._log(project.id, "reason started", "info")
        if not self._acquire_model_slot():
            self._log(project.id, "reason skipped because stop was requested", "warning")
            self._persist_step(project, release_project=release_project)
            return DispatchResult(project.id, DispatchMode.REASON, project.status, "reason skipped")
        try:
            try:
                result = self._call_reasoner(project)
            except ReasonFormatError as exc:
                repaired = self._reason_result_from_repair(exc)
                if repaired is not None:
                    self._log(project.id, "reason prose repaired into JSON", "warning")
                    result = repaired
                else:
                    self._log(
                        project.id,
                        f"reason output was not a JSON object; retrying once: {exc}",
                        "warning",
                        {"error": str(exc)},
                    )
                    try:
                        result = self._call_reasoner(project)
                    except ReasonFormatError as retry_exc:
                        repaired = self._reason_result_from_repair(retry_exc)
                        if repaired is None:
                            return self._reason_format_failure(
                                project,
                                retry_exc,
                                release_project=release_project,
                            )
                        self._log(project.id, "reason prose repaired into JSON", "warning")
                        result = repaired
            except Exception as exc:
                if self._is_fatal_upstream_failure(exc):
                    return self._reason_fatal_upstream(project, exc, release_project=release_project)
                if self._is_timeout_failure(exc):
                    return self._reason_timeout_failure(project, exc, release_project=release_project)
                return self._reason_failure_result(project, exc, release_project=release_project)
        finally:
            self._model_slots.release()
        self._reason_failures = 0
        self._reason_format_failures = 0
        completion_rejected = False
        finding_completion_rejected = False
        if result.completed:
            if any(intent.status == IntentStatus.RUNNING for intent in project.intents.values()):
                recovered = self._recover_stranded_intents(project)
                self._persist_step(project, release_project=release_project)
                self._log(
                    project.id,
                    "reason completion deferred because intents were still running",
                    "warning",
                    {"recovered": recovered},
                )
                return DispatchResult(
                    project.id,
                    DispatchMode.REASON,
                    project.status,
                    "reason completion deferred",
                )
            source = None
            if project.task_type == "crypto" and result.answer:
                source = self._crypto_completion_source(project, result.answer)
            if source:
                self._set_end_fact_result(project, result.answer)
                self._connect_crypto_answer_to_goal(project, result.answer)
                self._complete_project(project)
                self._no_progress_reasons = 0
                self._persist_step(project, release_project=release_project)
                messages = {
                    "verification": "independently verified crypto answer completed project",
                    "evidence": "crypto answer completed by evidence provenance (no independent verification)",
                    "acceptance": "crypto answer completed by platform acceptance",
                }
                self._log(project.id, messages[source], "success")
                return DispatchResult(project.id, DispatchMode.COMPLETED, project.status,
                                      f"crypto answer completed by {source}")
            if project.task_type != "crypto" and required_finding_count(project.objective) > 1:
                required = required_finding_count(project.objective)
                accepted, rejection = self._accepted_finding_facts(
                    project,
                    result.accepted_fact_ids,
                    required,
                )
                if accepted is not None:
                    self._set_finding_result(project, result.answer, accepted)
                    for fact in accepted:
                        self._connect_fact_to_goal(project, fact.id)
                    self._complete_project(project)
                    self._no_progress_reasons = 0
                    self._persist_step(project, release_project=release_project)
                    self._log(
                        project.id,
                        "reason marked project completed from accepted findings",
                        "success",
                        {
                            "has_answer": bool(result.answer),
                            "answer": result.answer,
                            "accepted_fact_ids": [fact.id for fact in accepted],
                        },
                    )
                    return DispatchResult(
                        project.id,
                        DispatchMode.COMPLETED,
                        project.status,
                        "reason marked project completed",
                    )
                self._log(
                    project.id,
                    "reason completion rejected because accepted findings are incomplete",
                    "warning",
                    {
                        "required": required,
                        "accepted_fact_ids": list(result.accepted_fact_ids),
                        "reason": rejection,
                    },
                )
                self._record_rejected_findings(project, required, result.accepted_fact_ids, rejection)
                completion_rejected = True
                finding_completion_rejected = True
            elif project.task_type != "crypto" and result.answer and self._answer_provenance(project, result.answer) == "verified":
                self._set_end_fact_result(project, result.answer)
                self._connect_answer_fact_to_goal(project, result.answer)
                self._complete_project(project)
                self._no_progress_reasons = 0
                self._persist_step(project, release_project=release_project)
                self._log(
                    project.id,
                    "reason marked project completed",
                    "success",
                    {"has_answer": True, "answer": result.answer},
                )
                return DispatchResult(project.id, DispatchMode.COMPLETED, project.status, "reason marked project completed")
            else:
                self._log(
                    project.id,
                    "reason completion rejected: no current passing crypto verification" if project.task_type == "crypto" else "reason completion rejected because the answer is not in evidence",
                    "warning",
                    {"has_answer": bool(result.answer)},
                )
                completion_rejected = True
                if project.task_type != "crypto":
                    self._record_rejected_answer(project, result.answer)

        planned_intents = self._planned_intents_from_reason_result(project, result)
        if project.task_type == "crypto":
            for item in planned_intents:
                metadata = item.get("crypto")
                if not isinstance(metadata, dict) or metadata.get("method") != "verify.candidate":
                    continue
                params = metadata.get("params")
                if isinstance(params, dict) and type(params.get("candidate_fact_id")) is int:
                    continue
                candidate = self._ensure_crypto_candidate(project, result.answer) if result.answer else next(
                    (fact for fact in sorted(project.facts.values(), key=lambda fact: fact.id, reverse=True)
                     if fact.kind == FactKind.CANDIDATE), None,
                )
                if candidate is not None:
                    metadata["params"] = {**(params if isinstance(params, dict) else {}),
                                          "candidate_fact_id": candidate.id}
                    metadata["input_ids"] = candidate.crypto.get("source_input_ids", [])
                    metadata["input_fact_ids"] = [candidate.id]
                    metadata.setdefault("scope", {"input_ids": metadata["input_ids"]})
        if completion_rejected and not finding_completion_rejected and not planned_intents and result.answer:
            # 模型声称完成但缺少证据、又没规划下一步：自动生成一条针对性验证，
            # 避免运行停在原地（BUG-016）。action_key 带答案哈希，避免不同候选
            # 被同一条 intent 去重掉（BUG-018）。
            digest = hashlib.sha1(str(result.answer).encode("utf-8")).hexdigest()[:8]
            if project.task_type == "crypto":
                candidate = self._ensure_crypto_candidate(project, result.answer)
                if candidate is not None:
                    input_ids = candidate.crypto.get("source_input_ids", [])
                    planned_intents = [{
                        "description": "从原始输入独立验算候选并保存校验记录",
                        "from": None, "to": None,
                        "action_key": f"verify-candidate-answer-{digest}",
                        "crypto": {"method": "verify.candidate", "input_ids": input_ids,
                                   "input_fact_ids": [candidate.id],
                                   "params": {"candidate_fact_id": candidate.id},
                                   "scope": {"input_ids": input_ids}},
                    }]
            else:
                planned_intents = [
                    {
                        "description": (
                            f"重新取证并确认候选答案 {result.answer}：从目标响应中重新获取该字符串，"
                            "确认它确实来自靶机而非模型推测；若确认无法找到，请换一个候选或继续取证。"
                        ),
                        "from": None,
                        "to": None,
                        "action_key": f"verify-candidate-answer-{digest}",
                    }
                ]
        planned_count = 0
        planned_intent_ids: list[int] = []
        planned_intent_summaries: list[dict[str, object]] = []
        skipped_repeat = 0
        skipped_from = 0
        skipped_crypto = 0
        crypto_rejections: list[dict[str, str]] = []
        if project.task_type == "crypto":
            # Diagnostics are not mathematical facts. Keep only the latest round.
            project.crypto.pop("planning_feedback", None)

        def reject_crypto_plan(item: dict[str, object], error: ValueError) -> None:
            nonlocal skipped_crypto
            skipped_crypto += 1
            rejection = {
                "action_key": str(item.get("action_key") or "")[:128],
                "error": str(error)[:600],
            }
            if len(crypto_rejections) < 5:
                crypto_rejections.append(rejection)
            self._log(project.id, "crypto plan rejected by contract", "warning", rejection)

        for item in planned_intents:
            description = str(item.get("description", "")).strip()
            if not description or is_template_reason_intent(item):
                continue
            raw_from = item.get("from")
            if raw_from is None:
                from_fact_id = self._select_reason_fallback_fact_id(project)
            else:
                from_fact_id = self._planning_from_fact_id(project, raw_from)
                if from_fact_id is None:
                    skipped_from += 1
                    continue
            to_fact_id = self._valid_fact_id(project, item.get("to"))
            if to_fact_id is not None and not self._is_goal_or_evidence(project, to_fact_id):
                to_fact_id = None
            # 规划阶段不能把尚未执行的 intent 连到 goal。空目标或指向终点都先留空，
            # 执行得到事实后再连接；只有最终答案才连到 F1。
            if to_fact_id is not None and project.facts[to_fact_id].kind == FactKind.GOAL:
                to_fact_id = None
            if from_fact_id is None:
                skipped_from += 1
                continue
            action_key = str(item.get("action_key") or "")
            crypto_metadata: dict[str, Any] = {}
            if project.task_type == "crypto":
                try:
                    crypto_metadata = self._crypto_plan_metadata(project, item, from_fact_id)
                    crypto_source = item.get("crypto") if isinstance(item.get("crypto"), dict) else {}
                    misplaced = [key for key in Dispatcher._MISPLACED_CRYPTO_FIELDS
                                 if key in item and key not in crypto_source]
                    if misplaced:
                        self._log(
                            project.id,
                            "crypto plan fields were placed outside crypto; moved into crypto: "
                            + ", ".join(misplaced),
                            "warning",
                            {"moved_fields": misplaced,
                             "action_key": str(item.get("action_key") or "")[:128]},
                        )
                except ValueError as exc:
                    reject_crypto_plan(item, exc)
                    continue
            if project.task_type == "crypto":
                try:
                    candidate_metadata = plan_crypto_intents(project, crypto_metadata)
                except ValueError as exc:
                    reject_crypto_plan(item, exc)
                    continue
                if not candidate_metadata:
                    skipped_repeat += 1
                    continue
            else:
                if self._is_duplicate_intent(project, description, from_fact_id, action_key):
                    skipped_repeat += 1
                    continue
                candidate_metadata = [crypto_metadata]
            for candidate in candidate_metadata:
                try:
                    intent = project.add_intent(
                        description=description,
                        from_fact_id=from_fact_id,
                        to_fact_id=to_fact_id,
                        action_key=action_key,
                        crypto=candidate,
                    )
                except ValueError as exc:
                    if project.task_type != "crypto":
                        raise
                    reject_crypto_plan(item, exc)
                    continue
                planned_intent_ids.append(intent.id)
                planned_intent_summaries.append({"id": intent.id, "description": description[:160]})
                planned_count += 1
        if crypto_rejections:
            project.crypto["planning_feedback"] = {
                "kind": "plan_contract_error",
                "rejected_plans": crypto_rejections,
                "rejected_count": skipped_crypto,
                "instruction": "Correct these field errors; diagnostics are not evidence or refutation.",
            }
        if project.task_type == "crypto" and planned_count == 0 and skipped_crypto:
            self._crypto_plan_failures += 1
            self._persist_step(project, release_project=release_project)
            exhausted = self._crypto_plan_failures >= self.max_retryable_attempts
            self._log(
                project.id,
                "crypto planning contract errors persisted; dispatcher will stop" if exhausted
                else "crypto planning contract errors; correction required",
                "error" if exhausted else "warning",
                {"attempt": self._crypto_plan_failures, "limit": self.max_retryable_attempts,
                 "skipped_crypto_contract": skipped_crypto},
            )
            return DispatchResult(
                project.id, DispatchMode.SKIPPED if exhausted else DispatchMode.REASON, project.status,
                "crypto planning contract errors persisted" if exhausted
                else "crypto plan contract rejected; will retry",
            )
        if project.task_type == "crypto":
            self._crypto_plan_failures = 0
        if planned_count == 0 and result.salvaged:
            self._reason_salvage_failures += 1
            self._persist_step(project, release_project=release_project)
            self._log(
                project.id,
                f"reason produced no plan after timeout salvage "
                f"({self._reason_salvage_failures}/{self.max_reason_salvage_failures}); will retry",
                "warning",
            )
            if self._reason_salvage_failures >= self.max_reason_salvage_failures:
                self._log(project.id, "reason kept timing out; dispatcher will stop", "error")
                return DispatchResult(
                    project.id,
                    DispatchMode.SKIPPED,
                    project.status,
                    "reason timed out repeatedly without producing a plan",
                )
            return DispatchResult(
                project.id,
                DispatchMode.REASON,
                project.status,
                "reason salvaged after timeout; will retry",
            )
        if planned_count == 0:
            self._no_progress_reasons += 1
        else:
            self._no_progress_reasons = 0
            self._reason_salvage_failures = 0
        self._persist_step(project, release_project=release_project)
        planned_message = f"reason planned {planned_count} new intents"
        if planned_intent_ids:
            planned_message = (
                f"{planned_message}: {'、'.join(f'#{intent_id}' for intent_id in planned_intent_ids)}"
            )
        self._log(
            project.id,
            planned_message,
            "info",
            {
                "intent_ids": planned_intent_ids,
                "intent_summaries": planned_intent_summaries,
                "skipped_repeat": skipped_repeat,
                "skipped_unplannable_from": skipped_from,
            },
        )
        if planned_count == 0 and self._no_progress_reasons >= self.max_retryable_attempts:
            message = "reason made no progress"
            stop_reason = str(result.stop_reason or "").strip()
            if stop_reason:
                message = f"{message}: {stop_reason}"
            self._log(project.id, "reason made no progress; dispatcher will stop", "error")
            return DispatchResult(
                project.id,
                DispatchMode.SKIPPED,
                project.status,
                message,
            )
        return DispatchResult(
            project.id,
            DispatchMode.REASON,
            project.status,
            f"planned {planned_count} intents",
        )

    @staticmethod
    def _record_rejected_answer(project: Project, answer: str | None) -> None:
        """把「候选答案不被证据支持」写进 findings，供下一轮 reason 参考（BUG-018）。

        否则下一轮 reason 看到的上下文与上一轮几乎一致，会反复提交同一个答案。
        """
        text = str(answer or "").strip()
        if not text:
            return
        marker = (
            f"候选答案 {text} 未在任何证据中逐字出现，已拒绝作为最终答案；"
            "请给出新的候选答案或继续取证。"
        )
        if any(fact.description == marker for fact in project.facts.values()):
            return
        project.add_fact(marker, kind=FactKind.REFUTED)

    @classmethod
    def _planned_intents_from_reason_result(
        cls,
        project: Project,
        result: ReasonResult,
    ) -> list[dict[str, object]]:
        planned_intents = result.next_intents
        if not planned_intents:
            planned_intents = [
                {
                    "description": description,
                    "from": None,
                    "to": None,
                    "action_key": "",
                }
                for description in (result.next_intent_descriptions or [])
            ]
        if not planned_intents:
            return []
        normalized: list[dict[str, object]] = []
        for item in planned_intents:
            normalized.append(
                {
                    "description": item.get("description", ""),
                    "from": item.get("from"),
                    "to": item.get("to"),
                    "action_key": str(item.get("action_key") or ""),
                    **({"crypto": item["crypto"]} if "crypto" in item else {}),
                }
            )
        return normalized

    @staticmethod
    def _crypto_plan_metadata(project: Project, item: dict[str, object], from_fact_id: int) -> dict[str, Any]:
        raw = item.get("crypto")
        if not isinstance(raw, dict):
            raise ValueError("crypto intent needs structured metadata")
        metadata = intent_crypto(raw)
        # Migrate misplaced top-level crypto fields (notably budget) instead of silently
        # dropping them.  Framework-generated/version fields are intentionally excluded so a
        # model cannot forge premise snapshots or retry bookkeeping through this path.
        misplaced = {key: item[key] for key in Dispatcher._MISPLACED_CRYPTO_FIELDS
                     if key in item and key not in metadata}
        if misplaced:
            metadata.update(misplaced)
        if not metadata.get("method") or not isinstance(metadata.get("scope"), dict) or not metadata["scope"]:
            raise ValueError("crypto intent needs method and bounded scope")
        # These fields are generated by the dispatcher; never trust model-supplied versions or retry notes.
        metadata.pop("input_versions", None)
        metadata.pop("continuation", None)
        metadata.pop("premise_versions", None)
        metadata.pop("verification_context", None)
        metadata["params"] = metadata.get("params") or {}
        if metadata.get("method") == "verify.candidate":
            candidate_id = metadata["params"].get("candidate_fact_id")
            if type(candidate_id) is not int or candidate_id not in project.facts or project.facts[candidate_id].kind != FactKind.CANDIDATE:
                raise ValueError("verify.candidate needs an existing candidate_fact_id")
            metadata["verification_context"] = verification_context(project, metadata.get("params", {}).get("candidate_fact_id"))
        resolve_crypto_budget(project.crypto.get("default_budget"), metadata.get("budget"))
        metadata.setdefault("input_fact_ids", [from_fact_id])
        source = project.facts.get(from_fact_id)
        if "input_ids" not in metadata:
            metadata["input_ids"] = list(source.crypto.get("source_input_ids", [])) if source is not None else []
        known_inputs = {ref["id"] for ref in project.crypto.get("inputs", [])}
        if not set(metadata.get("input_ids", [])).issubset(known_inputs):
            raise ValueError("crypto.input_ids must reference existing project inputs")
        if not set(metadata.get("input_fact_ids", [])).issubset(project.facts):
            raise ValueError("crypto.input_fact_ids must reference existing project facts")
        metadata["premise_versions"] = premise_versions(project, metadata.get("input_fact_ids", []))
        return intent_crypto(metadata)

    @staticmethod
    def _select_reason_fallback_fact_id(project: Project) -> int | None:
        closed = [
            intent
            for intent in project.intents.values()
            if intent.outcome in {IntentOutcome.ERROR, IntentOutcome.EMPTY, IntentOutcome.REFUTED}
        ]
        if closed:
            latest = max(closed, key=lambda item: item.id)
            source = project.facts.get(latest.from_fact_id) if latest.from_fact_id is not None else None
            if source is not None and source.is_plannable:
                return source.id
        candidates = [
            fact_id
            for fact_id, fact in project.facts.items()
            if fact.is_plannable and fact_id != 1
        ]
        if not candidates:
            return None
        from_fact_ids = {
            intent.from_fact_id for intent in project.intents.values() if intent.from_fact_id is not None
        }
        unused = sorted(fact_id for fact_id in candidates if fact_id not in from_fact_ids)
        if unused:
            return unused[-1]
        return max(candidates)

    @staticmethod
    def _plannable_fact_id(project: Project, value: object) -> int | None:
        fact_id = Dispatcher._valid_fact_id(project, value)
        if fact_id is None:
            return None
        fact = project.facts.get(fact_id)
        if fact is None or not fact.is_plannable:
            return None
        return fact_id

    @staticmethod
    def _planning_from_fact_id(project: Project, value: object) -> int | None:
        fact_id = Dispatcher._plannable_fact_id(project, value)
        if fact_id is not None:
            return fact_id
        return Dispatcher._redirect_closed_from_fact_id(project, value)

    @staticmethod
    def _redirect_closed_from_fact_id(project: Project, value: object) -> int | None:
        fact_id = Dispatcher._valid_fact_id(project, value)
        if fact_id is None:
            return None
        fact = project.facts.get(fact_id)
        if fact is None or fact.kind not in {FactKind.REFUTED, FactKind.ERROR}:
            return None
        producers = [
            intent
            for intent in project.intents.values()
            if fact_id in intent.to_fact_ids and intent.from_fact_id is not None
        ]
        if producers:
            latest = max(producers, key=lambda item: item.id)
            source = project.facts.get(latest.from_fact_id)
            if source is not None and source.is_plannable:
                return source.id
        start = project.facts.get(0)
        if start is not None and start.is_plannable:
            return start.id
        return None

    @classmethod
    def _record_partial_output(cls, project: Project, detail: str | None, intent: Intent | None = None) -> None:
        if not detail or cls._OUTPUT_TAIL_MARKER not in detail:
            return
        tail = strip_prompt_template_text(detail.split(cls._OUTPUT_TAIL_MARKER, 1)[1]).strip()
        tail = strip_result_json_text(tail).strip()
        if len(tail) < cls._PARTIAL_OUTPUT_MIN_CHARS:
            return
        if len(tail) > cls._PARTIAL_OUTPUT_LIMIT:
            tail = tail[-cls._PARTIAL_OUTPUT_LIMIT:]
        project.add_fact(
            f"{cls._PARTIAL_OUTPUT_PREFIX}\n{tail}",
            kind=FactKind.ERROR,
            crypto=cls._crypto_fact_metadata(project, intent),
        )

    @staticmethod
    def _is_goal_or_evidence(project: Project, fact_id: int) -> bool:
        fact = project.facts.get(fact_id)
        if fact is None:
            return False
        return fact.kind in {FactKind.GOAL, FactKind.EVIDENCE}

    _ANSWER_WRAPPER_PREFIX = ANSWER_WRAPPER_PREFIX
    _ANSWER_SOURCE_CONTEXT_CHARS = 240

    @staticmethod
    def _answer_source_line(description: str) -> str:
        return answer_source_line(description)

    @classmethod
    def _backfill_answer_source(cls, project: Project, result: WorkerResult) -> None:
        """worker 自身输出里找不到答案时，从项目已有事实回填来源（BUG-016）。

        并行 explore 中，一个 intent 常转述另一个 intent 刚发现的答案；它自己的
        原始输出里没有该字符串，导致 `source:` 缺失、完成判定被永久阻塞。
        """
        if result.answer_source or not result.answer:
            return
        text = result.answer.strip()
        if not text:
            return
        for fact in sorted(project.facts.values(), key=lambda item: item.id):
            description = fact.description
            if text not in description:
                continue
            if description.startswith(cls._PARTIAL_OUTPUT_PREFIX):
                continue
            if description.startswith(cls._ANSWER_WRAPPER_PREFIX):
                continue
            index = description.find(text)
            start = max(0, index - cls._ANSWER_SOURCE_CONTEXT_CHARS)
            end = min(len(description), index + len(text) + cls._ANSWER_SOURCE_CONTEXT_CHARS)
            snippet = " ".join(description[start:end].split())
            if snippet:
                result.answer_source = snippet
                return

    @classmethod
    def _fact_supports_answer(cls, fact: object, answer: str) -> bool:
        return fact_supports_answer(fact, answer)

    @classmethod
    def _answer_provenance(cls, project: Project, answer: str) -> str | None:
        text = answer.strip()
        if not text:
            return None
        for fact in project.facts.values():
            if cls._fact_supports_answer(fact, text):
                return "verified"
        return None

    @classmethod
    def _answer_in_evidence(cls, project: Project, answer: str) -> bool:
        return cls._answer_provenance(project, answer) == "verified"

    @staticmethod
    def _valid_fact_id(project: Project, value: object) -> int | None:
        if isinstance(value, int) and value in project.facts:
            return value
        return None

    def _execute_intents(self, project: Project, intents: list[Intent]) -> Iterable[IntentExecutionResult]:
        max_workers = min(1 if project.task_type == "crypto" else self.max_explore_workers, len(intents))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._run_intent_with_slot,
                    project,
                    intent,
                    DispatchMode.EXPLORE,
                    f"intent {intent.id}",
                ): intent
                for intent in intents
            }
            for future in as_completed(futures):
                intent = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = self._intent_failure_result(project, f"intent {intent.id}", exc)
                yield IntentExecutionResult(intent=intent, result=result)

    def _run_intent_with_slot(
        self,
        project: Project,
        intent: Intent,
        mode: DispatchMode,
        scope: str,
    ) -> WorkerResult:
        crypto_acquired = False
        if project.task_type == "crypto":
            while not self._stop_event.is_set():
                if self._crypto_compute_slots.acquire(timeout=0.2):
                    crypto_acquired = True
                    break
            if not crypto_acquired:
                return WorkerResult(retryable=True)
        if self._stop_event.is_set() or not self._acquire_model_slot():
            if crypto_acquired:
                self._crypto_compute_slots.release()
            return WorkerResult(retryable=True)
        try:
            if self._stop_event.is_set():
                return WorkerResult(retryable=True)
            with self._state_lock:
                intent.mark_running()
                self.client.save_project(project)
            if mode == DispatchMode.EXPLORE:
                self._log(
                    project.id,
                    f"explore intent {intent.id} started",
                    "info",
                    {"intent_id": intent.id},
                )
            return self._execute_with_retry(project, intent, mode, scope)
        finally:
            self._model_slots.release()
            if crypto_acquired:
                self._crypto_compute_slots.release()

    def _acquire_model_slot(self) -> bool:
        while not self._stop_event.is_set():
            if self._model_slots.acquire(timeout=0.2):
                return True
        return False

    def _execute_with_retry(
        self,
        project: Project,
        intent: Intent,
        mode: DispatchMode,
        scope: str,
    ) -> WorkerResult:
        try:
            return self._execute_attempt(project, intent, mode, scope)
        except Exception as exc:
            if self._is_fatal_upstream_failure(exc):
                self._note_fatal_upstream(project, scope, exc)
                return self._intent_failure_result(project, scope, exc)
            if self._stop_event.is_set():
                return self._intent_failure_result(project, scope, exc)
            if not self._is_retryable_failure(exc):
                self._log(
                    project.id,
                    f"{scope} execution failed: {type(exc).__name__}: {exc}",
                    "error",
                    {"intent_id": intent.id},
                )
                return self._intent_failure_result(project, scope, exc)
            self._log(
                project.id,
                f"{scope} hit a retryable failure; retrying once",
                "warning",
                {"intent_id": intent.id, "error": str(exc)},
            )
            try:
                return self._execute_attempt(project, intent, mode, scope)
            except Exception as retry_exc:
                if self._is_fatal_upstream_failure(retry_exc):
                    self._note_fatal_upstream(project, scope, retry_exc)
                    return self._intent_failure_result(project, scope, retry_exc)
                if self._stop_event.is_set() or self._is_retryable_failure(retry_exc):
                    if self._stop_event.is_set():
                        return self._intent_failure_result(project, scope, retry_exc)
                    self._log(
                        project.id,
                        f"{scope} retry failed; intent returned to active",
                        "warning",
                        {"intent_id": intent.id, "error": str(retry_exc)},
                    )
                    return WorkerResult(retryable=True, detail=str(retry_exc))
                self._log(
                    project.id,
                    f"{scope} retry failed: {type(retry_exc).__name__}: {retry_exc}",
                    "error",
                    {"intent_id": intent.id},
                )
                return self._intent_failure_result(project, scope, retry_exc)

    def _execute_attempt(
        self, project: Project, intent: Intent, mode: DispatchMode, scope: str,
    ) -> WorkerResult:
        if self._stop_event.is_set():
            return self._intent_failure_result(project, scope, RuntimeError("execution cancelled"))
        if project.task_type == "crypto":
            metadata = {**intent.crypto, "input_fact_ids": intent.crypto.get(
                "input_fact_ids", [intent.from_fact_id] if intent.from_fact_id is not None else [])}
            if not premises_current(project, metadata):
                raise ValueError("crypto premises changed or have no historical snapshot; replan this intent")
        if project.task_type == "crypto" and intent.crypto.get("resume_from") is not None:
            verifier = getattr(self.client, "verify_crypto_artifact", None)
            if callable(verifier):
                verifier(project.id, intent.crypto["resume_from"])
        if project.task_type == "crypto":
            validate_checkpoint_resume(project, intent.crypto)
        result = self._execute_intent(project, intent, mode)
        if project.task_type == "crypto" and self._stop_event.is_set():
            result.crypto["exit_reason"] = "cancelled"
            result.outcome = "error"
            result.timed_out = False
        return result

    def _intent_failure_result(self, project: Project, scope: str, exc: Exception) -> WorkerResult:
        if project.task_type != "crypto":
            return self._failure_result(scope, exc)
        reason = ("cancelled" if self._stop_event.is_set() else
                  exc.reason if isinstance(exc, CryptoExecutionLimit) else "tool_error")
        return WorkerResult(outcome="error", detail=self._execution_failed_fact(scope, exc),
                            crypto={"exit_reason": reason, "covered_scope": {}})

    def _failure_result(self, scope: str, exc: Exception) -> WorkerResult:
        if self._stop_event.is_set() or self._is_retryable_failure(exc):
            return WorkerResult(retryable=True, detail=str(exc))
        return WorkerResult(outcome="error", detail=self._execution_failed_fact(scope, exc))

    @staticmethod
    def _is_retryable_failure(exc: Exception) -> bool:
        if isinstance(exc, UpstreamFatalError):
            return False
        text = str(exc)
        # The output tail is captured worker output, not an error message. A timeout whose tail
        # happens to contain "reconnecting" or "429" must not be retried as a transient failure.
        if Dispatcher._OUTPUT_TAIL_MARKER in text:
            text = text.split(Dispatcher._OUTPUT_TAIL_MARKER, 1)[0]
        text = text.lower()
        markers = (
            "database is locked",
            "database locked",
            "sqlite_busy",
            "sqlite busy",
            "too many requests",
            "rate limit",
            "ratelimit",
            "stream idle",
            "stream_idle",
            "idle timeout",
            "connection reset",
            "connection refused",
            "temporarily unavailable",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "high demand",
            "overloaded",
            "stream disconnected",
            "reconnecting",
            "disconnected before completion",
            "does not contain a json object",
            "transient upstream failure",
        )
        if any(marker in text for marker in markers):
            return True
        tokens = text.replace(":", " ").replace(",", " ").split()
        return any(code in tokens for code in ("429", "502", "503", "504"))

    @staticmethod
    def _execution_failed_fact(scope: str, exc: Exception) -> str:
        text = str(exc)
        tail = ""
        marker = Dispatcher._OUTPUT_TAIL_MARKER
        if marker in text:
            text, tail = text.split(marker, 1)
            tail = strip_prompt_template_text(tail).strip()
            if len(tail) > Dispatcher._PARTIAL_OUTPUT_LIMIT:
                tail = tail[-Dispatcher._PARTIAL_OUTPUT_LIMIT:]
        if "no-output timed out" in text:
            message = (
                f"{scope} 执行超时：Codex 长时间无 stdout/stderr 输出，可能卡在长期运行或无输出命令；需要 reason 重新规划更小的验证步骤"
            )
        elif len(text) > 300:
            message = f"{scope} execution failed: {type(exc).__name__}: {text[:297]}..."
        else:
            message = f"{scope} execution failed: {type(exc).__name__}: {text}"
        if tail:
            return f"{message}{marker}{tail}"
        return message

    @staticmethod
    def _apply_intent_result(project: Project, item: IntentExecutionResult) -> None:
        intent = item.intent
        before_facts = set(project.facts)
        try:
            Dispatcher._apply_intent_result_body(project, item)
        finally:
            produced = [fact_id for fact_id in project.facts if fact_id not in before_facts]
            if produced:
                intent.set_targets(produced)

    @staticmethod
    def _apply_intent_result_body(project: Project, item: IntentExecutionResult) -> None:
        result = item.result
        intent = item.intent
        Dispatcher._backfill_answer_source(project, result)
        if result.retryable:
            if intent.status == IntentStatus.RUNNING:
                intent.release()
            return
        if project.task_type == "crypto":
            Dispatcher._apply_crypto_intent_result(project, item)
            return
        if result.timed_out:
            Dispatcher._store_worker_observations(project, result, intent)
            if intent.status == IntentStatus.RUNNING:
                intent.mark_failed(
                    IntentOutcome.ERROR,
                    note="execution timed out after partial facts were saved; intent left incomplete",
                )
            return
        existing_fact_ids = set(project.facts)
        priors = result.prior_results
        if result.outcome != "error":
            priors = Dispatcher._without_duplicate_answers(priors, result)
        for prior in priors:
            Dispatcher._add_prior_fact(project, prior, intent)
        if result.outcome == "error":
            Dispatcher._persist_json_facts_from_detail(project, result.detail, intent)
            Dispatcher._record_partial_output(project, result.detail)
            if intent.status == IntentStatus.RUNNING:
                intent.mark_failed(IntentOutcome.ERROR, note=result.detail or "execution failed")
            return
        if result.outcome == "refuted":
            fact_text = Dispatcher._worker_result_fact_text(result)
            if not fact_text:
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.EMPTY, note="refuted result missing fact")
                return
            fact = project.add_fact(
                fact_text, kind=FactKind.REFUTED,
                crypto=Dispatcher._crypto_fact_metadata(project, intent),
            )
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.REFUTED)
            return
        new_facts = [project.facts[fact_id] for fact_id in project.facts if fact_id not in existing_fact_ids]
        if Dispatcher._final_answer_already_recorded(result, new_facts):
            if intent.status == IntentStatus.RUNNING and new_facts:
                intent.mark_completed(to_fact_id=new_facts[-1].id, outcome=IntentOutcome.VERIFIED)
            return
        fact_text = Dispatcher._worker_result_fact_text(result)
        if fact_text:
            fact = project.add_fact(
                fact_text, kind=FactKind.EVIDENCE,
                crypto=Dispatcher._crypto_fact_metadata(project, intent),
            )
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.VERIFIED)
            return
        if intent.status == IntentStatus.RUNNING:
            intent.mark_failed(IntentOutcome.EMPTY, note="intent returned no fact or answer")

    @staticmethod
    def _apply_crypto_intent_result(project: Project, item: IntentExecutionResult) -> None:
        result, intent = item.result, item.intent
        declared_reason = result.crypto.get("exit_reason")
        allowed_reasons = {"completed", "range_exhausted", "timeout", "budget_exhausted", "solver_unknown", "tool_error", "cancelled"}
        exit_reason = "timeout" if result.timed_out else declared_reason
        if not isinstance(exit_reason, str) or exit_reason not in allowed_reasons:
            exit_reason = "tool_error"
            result.detail = result.detail or "crypto result missing or invalid exit_reason"
        covered_scope = result.crypto.get("covered_scope", {})
        if not isinstance(covered_scope, dict):
            covered_scope = {}
            exit_reason = "tool_error"
            result.detail = result.detail or "crypto covered_scope must be an object"
        if exit_reason == "range_exhausted" and not covered_scope:
            exit_reason = "tool_error"
            result.detail = result.detail or "range_exhausted needs covered_scope"
        execution = {
            "intent_id": intent.id,
            "compute_id": f"crypto-{project.id}-{intent.id}",
            "exit_reason": exit_reason,
            "planned_scope": intent.crypto.get("scope", {}),
            "covered_scope": covered_scope,
            "input_ids": intent.crypto.get("input_ids", []),
        }
        if result.rejected:
            execution["rejected_output_objects"] = result.rejected[:20]
        try:
            execution["budget"] = resolve_crypto_budget(
                project.crypto.get("default_budget"), intent.crypto.get("budget")
            ).to_dict()
        except ValueError:
            execution["exit_reason"] = "tool_error"
            exit_reason = "tool_error"
        if result.crypto.get("checkpoint_ref") is not None:
            execution["checkpoint_ref"] = result.crypto["checkpoint_ref"]
        if result.crypto.get("checkpoint_error"):
            execution["checkpoint_error"] = str(result.crypto["checkpoint_error"])
        try:
            project_crypto({"schema_version": 1, "inputs": [], "executions": [execution]})
        except ValueError as exc:
            execution["exit_reason"] = "tool_error"
            execution["covered_scope"] = {}
            execution["invalid_scope"] = str(exc)
            execution.pop("checkpoint_ref", None)
            exit_reason = "tool_error"
        project.crypto.setdefault("executions", []).append(execution)
        verification = result.crypto.get("verification")
        if intent.crypto.get("method") == "verify.candidate":
            # A cancelled/aborted verification must never leave a record behind, even a passing one.
            if exit_reason != "completed":
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note=f"independent verification {exit_reason}")
                return
            expected_id = intent.crypto.get("params", {}).get("candidate_fact_id")
            valid_record = (
                isinstance(verification, dict)
                and verification.get("candidate_fact_id") == expected_id
                and verification.get("method") == CRYPTO_VERIFY_VERSION
                and verification.get("status") in {"pass", "fail", "inconclusive"}
            )
            if not valid_record:
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note="invalid or missing independent verification record")
                return
            record = {**verification, "intent_id": intent.id}
            project.crypto.setdefault("verifications", []).append(record)
            if record["status"] == "pass" and result.outcome != "error":
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_completed(outcome=IntentOutcome.VERIFIED)
            elif record["status"] == "pass":
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(
                        IntentOutcome.ERROR,
                        note="independent verification pass record conflicted with an error outcome",
                    )
            else:
                checks = str(record.get("checks", {}))[:600]
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(
                        IntentOutcome.EMPTY,
                        note=f"independent verification {record['status']}: {checks}",
                    )
            return
        rejected_observations = 0
        for observation in result.prior_results:
            try:
                Dispatcher._add_prior_fact(project, observation, intent)
            except (KeyError, ValueError) as exc:
                rejected_observations += 1
                execution.setdefault("observation_errors", []).append(str(exc)[:600])
                Dispatcher._record_crypto_pending(project, intent, observation.fact_description or observation.answer,
                                                  f"invalid crypto observation: {exc}")
        if rejected_observations:
            execution["rejected_observations"] = rejected_observations
        supplied_fact = result.crypto.get("fact", {})
        if Dispatcher._is_crypto_candidate(result.answer, result.crypto):
            try:
                candidate_id = Dispatcher._save_crypto_candidate(project, intent, result.answer, result.fact_description, result.crypto)
            except (KeyError, ValueError) as exc:
                execution["exit_reason"] = "tool_error"
                execution["candidate_error"] = str(exc)[:600]
                Dispatcher._record_crypto_pending(project, intent, result.fact_description or result.answer,
                                                  f"candidate not saved: {exc}")
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note=f"candidate not saved: {exc}")
                return
            if intent.status == IntentStatus.RUNNING:
                if exit_reason in {"completed", "range_exhausted"} and result.outcome != "error":
                    intent.mark_completed(outcome=IntentOutcome.EMPTY)
                    intent.note = f"candidate saved as fact {candidate_id}; independent verification required"
                else:
                    intent.mark_failed(IntentOutcome.ERROR, note=f"{exit_reason}; candidate saved as fact {candidate_id}, not verified")
            return
        if exit_reason in {"timeout", "budget_exhausted", "solver_unknown", "tool_error", "cancelled"} or result.outcome == "error":
            if (result.outcome == "verified" and isinstance(supplied_fact, dict)
                    and supplied_fact.get("confirmed") is True
                    and supplied_fact.get("claim_status") == "observed"
                    and result.fact_description):
                try:
                    project.add_fact(
                        result.fact_description, kind=FactKind.EVIDENCE,
                        crypto=Dispatcher._crypto_fact_metadata(project, intent, result.crypto),
                    )
                except (KeyError, ValueError) as exc:
                    execution["rejected_current_observation"] = str(exc)
            elif result.fact_description:
                Dispatcher._record_crypto_pending(project, intent, result.fact_description,
                                                  f"{exit_reason}: observation is not explicitly confirmed")
            if intent.status == IntentStatus.RUNNING:
                intent.mark_failed(IntentOutcome.ERROR, note=f"{exit_reason}: {result.detail or 'no conclusive result'}")
            return
        if not isinstance(supplied_fact, dict) or supplied_fact.get("claim_status") != "observed":
            reason = "unverified hypothesis" if isinstance(supplied_fact, dict) and supplied_fact.get("claim_status") == "hypothesis" else "crypto.fact.claim_status=observed is required for evidence"
            Dispatcher._record_crypto_pending(project, intent, result.fact_description, reason)
            execution["observation_error"] = reason
            if intent.status == IntentStatus.RUNNING:
                intent.mark_failed(IntentOutcome.EMPTY, note=reason)
            return
        fact_text = Dispatcher._worker_result_fact_text(result)
        if fact_text and exit_reason == "range_exhausted":
            fact_text = f"{fact_text}\ncovered_scope: {json.dumps(covered_scope, ensure_ascii=False, sort_keys=True)}"
        if result.outcome == "refuted":
            if not fact_text or not covered_scope:
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note="crypto refutation needs a fact and covered_scope")
                return
            scoped_text = fact_text if exit_reason == "range_exhausted" else f"{fact_text}\ncovered_scope: {json.dumps(covered_scope, ensure_ascii=False, sort_keys=True)}"
            metadata = Dispatcher._crypto_fact_metadata(project, intent, result.crypto)
            kind = FactKind.EVIDENCE if metadata.get("observation_kind") == "evidence" else FactKind.REFUTED
            try:
                fact = project.add_fact(scoped_text, kind=kind, crypto=metadata)
            except (KeyError, ValueError) as exc:
                execution["exit_reason"] = "tool_error"
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note=f"invalid crypto fact: {exc}")
                return
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.REFUTED)
            return
        if fact_text:
            try:
                fact = project.add_fact(
                    fact_text, kind=FactKind.EVIDENCE,
                    crypto=Dispatcher._crypto_fact_metadata(project, intent, result.crypto),
                )
            except (KeyError, ValueError) as exc:
                execution["exit_reason"] = "tool_error"
                if intent.status == IntentStatus.RUNNING:
                    intent.mark_failed(IntentOutcome.ERROR, note=f"invalid crypto fact: {exc}")
                return
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.VERIFIED)
            return
        if intent.status == IntentStatus.RUNNING:
            intent.mark_failed(IntentOutcome.EMPTY, note="crypto intent returned no confirmed observation")



    @staticmethod
    def _answer_covered_by_facts(answer: str | None, fact_texts: list[str]) -> bool:
        if not answer:
            return False
        needle = answer.strip()
        if not needle:
            return False
        return any(needle in text for text in fact_texts)

    @staticmethod
    def _without_duplicate_answers(
        priors: list[WorkerObservation],
        result: WorkerResult,
    ) -> list[WorkerObservation]:
        fact_texts: list[str] = []
        for prior in priors:
            text = _optional_fact_description(prior.fact_description)
            if text:
                fact_texts.append(text)
        final_text = _optional_fact_description(result.fact_description)
        if final_text:
            fact_texts.append(final_text)
        kept: list[WorkerObservation] = []
        for prior in priors:
            if (
                not _optional_fact_description(prior.fact_description)
                and prior.answer
                and prior.outcome != "refuted"
                and Dispatcher._answer_covered_by_facts(prior.answer, fact_texts)
            ):
                continue
            kept.append(prior)
        return kept

    @staticmethod
    def _final_answer_already_recorded(result: WorkerResult, new_facts: list[object]) -> bool:
        if _optional_fact_description(result.fact_description) or not result.answer:
            return False
        if result.outcome == "refuted":
            return False
        texts = [
            str(getattr(fact, "description", ""))
            for fact in new_facts
            if getattr(fact, "kind", None) == FactKind.EVIDENCE
        ]
        return Dispatcher._answer_covered_by_facts(result.answer, texts)

    @staticmethod
    def _store_worker_observations(
        project: Project,
        result: WorkerResult,
        intent: Intent | None = None,
    ) -> int:
        observations = list(result.prior_results)
        if result.fact_description or result.answer:
            observations.append(
                WorkerObservation(
                    answer=result.answer,
                    fact_description=result.fact_description,
                    outcome=result.outcome,
                    answer_source=result.answer_source,
                    crypto=result.crypto,
                )
            )
        before = len(project.facts)
        for observation in observations:
            Dispatcher._add_prior_fact(project, observation, intent)
        return len(project.facts) - before

    @classmethod
    def _persist_json_facts_from_detail(
        cls,
        project: Project,
        detail: str | None,
        intent: Intent | None = None,
    ) -> int:
        if not detail or cls._OUTPUT_TAIL_MARKER not in detail:
            return 0
        tail = detail.split(cls._OUTPUT_TAIL_MARKER, 1)[1]
        try:
            parsed = parse_worker_result(tail)
        except ValueError:
            return 0
        return cls._store_worker_observations(project, parsed, intent)

    @staticmethod
    def _reason_result_from_repair(exc: ReasonFormatError) -> ReasonResult | None:
        repaired = repair_reason_output(getattr(exc, "output", "") or "")
        if not repaired:
            return None
        try:
            return parse_reason_result(repaired)
        except (ReasonFormatError, ValueError):
            return None

    @staticmethod
    def _add_prior_fact(
        project: Project,
        observation: WorkerObservation,
        intent: Intent | None = None,
    ) -> object | None:
        supplied_fact = observation.crypto.get("fact", {}) if project.task_type == "crypto" else {}
        if project.task_type == "crypto" and Dispatcher._is_crypto_candidate(observation.answer, observation.crypto):
            Dispatcher._save_crypto_candidate(project, intent, observation.answer, observation.fact_description, observation.crypto)
            return None
        if project.task_type == "crypto" and (not isinstance(supplied_fact, dict) or supplied_fact.get("claim_status") != "observed"):
            Dispatcher._record_crypto_pending(project, intent, observation.fact_description,
                                              "prior observation lacks crypto.fact.claim_status=observed; not evidence")
            return None
        fact_text = _optional_fact_description(observation.fact_description)
        if not fact_text and observation.answer:
            fact_text = f"agent reported possible final answer: {observation.answer}"
        if not fact_text:
            return None
        if observation.answer_source:
            fact_text = f"{fact_text}\nsource: {observation.answer_source}"
        if observation.outcome == "refuted":
            crypto = Dispatcher._crypto_fact_metadata(project, intent, observation.crypto)
            if project.task_type == "crypto":
                covered_scope = observation.crypto.get("covered_scope", {})
                if not isinstance(covered_scope, dict) or not covered_scope:
                    return None
                fact_text = f"{fact_text}\ncovered_scope: {json.dumps(covered_scope, ensure_ascii=False, sort_keys=True)}"
            kind = FactKind.EVIDENCE if project.task_type == "crypto" and crypto.get("observation_kind") == "evidence" else FactKind.REFUTED
            return project.add_fact(fact_text, kind=kind, crypto=crypto)
        if observation.outcome == "error":
            return None
        return project.add_fact(
            fact_text,
            kind=FactKind.EVIDENCE,
            crypto=Dispatcher._crypto_fact_metadata(project, intent, observation.crypto),
        )

    def _execute_intent(self, project: Project, intent: Intent, mode: DispatchMode) -> WorkerResult:
        if project.task_type == "crypto" and intent.crypto.get("method") == "verify.candidate":
            candidate_id = intent.crypto.get("params", {}).get("candidate_fact_id")
            if type(candidate_id) is not int:
                return WorkerResult(outcome="error", detail="verification needs candidate_fact_id",
                                    crypto={"exit_reason": "tool_error", "covered_scope": {}})
            verifier = self.client.verify_crypto_candidate
            verification_id = uuid4().hex
            with self._verification_lock:
                self._active_verifications[project.id] = verification_id
            try:
                if callable_accepts_mode(verifier, parameter="verification_id"):
                    record = verifier(
                        project.id,
                        candidate_id,
                        verification_id=verification_id,
                        timeout_seconds=self._verification_timeout(project, intent),
                    )
                else:
                    record = verifier(project.id, candidate_id)
            finally:
                with self._verification_lock:
                    self._active_verifications.pop(project.id, None)
            return WorkerResult(
                outcome="verified" if record.get("status") == "pass" else "error",
                crypto={"exit_reason": "completed", "covered_scope": intent.crypto.get("scope", {}),
                        "verification": record},
            )
        with self._state_lock:
            snapshot = copy.deepcopy(project)
        if self._worker_accepts_mode and self._worker_accepts_log:
            return self.worker.execute_intent(snapshot, intent, mode, log=self._agent_log(project.id))  # type: ignore[call-arg]
        if self._worker_accepts_mode:
            return self.worker.execute_intent(snapshot, intent, mode)  # type: ignore[call-arg]
        if self._worker_accepts_log:
            return self.worker.execute_intent(snapshot, intent, log=self._agent_log(project.id))  # type: ignore[call-arg]
        return self.worker.execute_intent(snapshot, intent)

    def _recover_stranded_intents(self, project: Project) -> int:
        recovered = 0
        for intent in project.intents.values():
            if intent.status != IntentStatus.RUNNING:
                continue
            intent.release()
            recovered += 1
        return recovered

    def _commit_intent_result(
        self,
        project: Project,
        item: IntentExecutionResult,
        release_project: bool = False,
    ) -> None:
        self._consume_retry_budget(item)
        if project.task_type == "crypto" and item.result.crypto.get("checkpoint_ref") is not None:
            verifier = getattr(self.client, "verify_crypto_artifact", None)
            if callable(verifier):
                try:
                    verifier(project.id, item.result.crypto["checkpoint_ref"])
                except Exception as exc:
                    item.result.crypto.pop("checkpoint_ref", None)
                    item.result.crypto["checkpoint_error"] = f"checkpoint file verification failed: {exc}"
        with self._state_lock:
            self._apply_intent_result(project, item)
            self._persist_step(project, release_project=release_project)

    def _consume_retry_budget(self, item: IntentExecutionResult) -> None:
        if not item.result.retryable or self._stop_event.is_set():
            if not item.result.retryable:
                self._retry_counts.pop(item.intent.id, None)
            return
        count = self._retry_counts.get(item.intent.id, 0) + 1
        self._retry_counts[item.intent.id] = count
        if count < self.max_retryable_attempts:
            return
        self._retry_counts.pop(item.intent.id, None)
        detail = item.result.detail or "retry budget exhausted"
        item.result = WorkerResult(
            outcome="error",
            detail=self._execution_failed_fact(
                f"intent {item.intent.id}",
                RuntimeError(detail),
            ),
            crypto={"exit_reason": "tool_error", "covered_scope": {}} if item.intent.crypto else {},
        )

    @staticmethod
    def _is_fatal_upstream_failure(exc: Exception) -> bool:
        return isinstance(exc, UpstreamFatalError) or fatal_upstream_marker(str(exc)) is not None

    def _note_fatal_upstream(self, project: Project, scope: str, exc: Exception) -> None:
        message = str(exc)
        if self._fatal_upstream_error is not None:
            return
        self._fatal_upstream_error = message
        self._log(
            project.id,
            f"{scope} stopped by an upstream quota/auth failure; retrying cannot fix it. "
            "Top up the provider account or switch the API key/provider.",
            "error",
            {"error": message[:1200]},
        )

    def _fatal_upstream_result(self, project: Project) -> DispatchResult | None:
        if self._fatal_upstream_error is None:
            return None
        return DispatchResult(
            project.id,
            DispatchMode.SKIPPED,
            project.status,
            "upstream quota/auth failure; dispatcher will stop: "
            + self._fatal_upstream_error[:300],
        )

    def _reason_fatal_upstream(
        self,
        project: Project,
        exc: Exception,
        release_project: bool,
    ) -> DispatchResult:
        self._note_fatal_upstream(project, "reason", exc)
        self._persist_step(project, release_project=release_project)
        return DispatchResult(
            project.id,
            DispatchMode.SKIPPED,
            project.status,
            "upstream quota/auth failure; top up the account or switch provider: "
            + str(exc)[:300],
        )

    def _reason_failure_result(
        self,
        project: Project,
        exc: Exception,
        release_project: bool,
    ) -> DispatchResult:
        self._reason_failures += 1
        self._log(
            project.id,
            f"reason failed: {type(exc).__name__}: {exc}",
            "error" if self._reason_failures >= self.max_retryable_attempts else "warning",
            {"attempt": self._reason_failures, "error": str(exc)},
        )
        self._persist_step(project, release_project=release_project)
        if self._reason_failures >= self.max_retryable_attempts and not self._stop_event.is_set():
            return DispatchResult(
                project.id,
                DispatchMode.SKIPPED,
                project.status,
                "reason failed repeatedly",
            )
        return DispatchResult(project.id, DispatchMode.REASON, project.status, "reason failed; will retry")

    def _call_reasoner(self, project: Project) -> ReasonResult:
        if self._reasoner_accepts_log:
            return self.reasoner.plan(project, log=self._agent_log(project.id))  # type: ignore[call-arg]
        return self.reasoner.plan(project)

    @staticmethod
    def _is_timeout_failure(exc: Exception) -> bool:
        text = str(exc).lower()
        return "timed out" in text or "without stdout/stderr" in text

    def _reason_timeout_failure(
        self,
        project: Project,
        exc: Exception,
        release_project: bool,
    ) -> DispatchResult:
        self._reason_salvage_failures += 1
        self._persist_step(project, release_project=release_project)
        self._log(
            project.id,
            f"reason timed out "
            f"({self._reason_salvage_failures}/{self.max_reason_salvage_failures}); will retry",
            "warning",
            {"error": str(exc)},
        )
        if self._reason_salvage_failures >= self.max_reason_salvage_failures:
            self._log(project.id, "reason kept timing out; dispatcher will stop", "error")
            return DispatchResult(
                project.id,
                DispatchMode.SKIPPED,
                project.status,
                "reason timed out repeatedly without producing a plan",
            )
        return DispatchResult(
            project.id,
            DispatchMode.REASON,
            project.status,
            "reason timed out; will retry",
        )

    def _reason_format_failure(
        self,
        project: Project,
        exc: Exception,
        release_project: bool,
    ) -> DispatchResult:
        self._reason_format_failures += 1
        fatal = self._reason_format_failures >= self.max_retryable_attempts
        self._log(
            project.id,
            f"reason output was not a JSON object (format failure "
            f"{self._reason_format_failures}/{self.max_retryable_attempts})",
            "error" if fatal else "warning",
            {"attempt": self._reason_format_failures, "error": str(exc)},
        )
        self._persist_step(project, release_project=release_project)
        if fatal and not self._stop_event.is_set():
            return DispatchResult(
                project.id,
                DispatchMode.SKIPPED,
                project.status,
                f"reason output was never a valid JSON object: {exc}",
            )
        return DispatchResult(
            project.id,
            DispatchMode.REASON,
            project.status,
            "reason output was not JSON; will retry",
        )

    def _is_duplicate_intent(
        self,
        project: Project,
        description: str,
        from_fact_id: int | None,
        action_key: str = "",
    ) -> bool:
        for intent in project.intents.values():
            if intent.from_fact_id != from_fact_id:
                continue
            if self._repeats_intent(intent, description, action_key):
                return True
        return False

    @staticmethod
    def _repeats_intent(intent: Intent, description: str, action_key: str) -> bool:
        same_text = descriptions_equivalent(description, intent.description)
        if intent.outcome in {IntentOutcome.ERROR, IntentOutcome.EMPTY}:
            return same_text
        return same_text or action_keys_equivalent(action_key, intent.action_key)

    _INITIAL_PROBE_DESCRIPTION = "初始探测目标并提取可继续分析的关键事实"

    @staticmethod
    def _is_initial_probe_intent(intent: Intent) -> bool:
        return intent.description == Dispatcher._INITIAL_PROBE_DESCRIPTION

    @staticmethod
    def _create_initial_intent(project: Project) -> Intent:
        if project.task_type == "crypto":
            input_ids = [reference["id"] for reference in project.crypto.get("inputs", [])]
            return project.add_intent(
                description="核对离线输入字节与哈希，提取明确参数并执行低成本格式/数学检查",
                from_fact_id=0 if 0 in project.facts else None,
                to_fact_id=1 if 1 in project.facts else None,
                action_key="inspect-crypto-inputs",
                crypto={"method": "inspect.inputs", "input_ids": input_ids,
                        "input_fact_ids": [0] if 0 in project.facts else [],
                        "scope": {"input_ids": input_ids, "checks": ["hash", "format", "cheap-math"]}},
            )
        if 0 in project.facts and 1 in project.facts:
            return project.add_intent(
                description=Dispatcher._INITIAL_PROBE_DESCRIPTION,
                from_fact_id=0,
                to_fact_id=1,
            )
        return project.add_intent(description=Dispatcher._INITIAL_PROBE_DESCRIPTION)

    @staticmethod
    def _complete_intent_with_fact(project: Project, intent: Intent, fact_id: int) -> None:
        intent.mark_completed(to_fact_id=fact_id)

    @staticmethod
    def _worker_result_fact_text(result: WorkerResult) -> str | None:
        fact_description = _optional_fact_description(result.fact_description)
        if fact_description:
            text = fact_description
        elif result.answer:
            text = f"agent reported possible final answer: {result.answer}"
        else:
            return None
        if result.answer_source:
            text = f"{text}\nsource: {result.answer_source}"
        return text

    _GOAL_EDGE_DESCRIPTION = "得到最终结果"

    @staticmethod
    def _set_end_fact_result(project: Project, answer: str) -> None:
        if 1 not in project.facts:
            return
        project.facts[1].description = f"result\nanswer: {answer}"

    @staticmethod
    def _set_finding_result(project: Project, answer: str | None, facts: list[object]) -> None:
        if 1 not in project.facts:
            return
        labels = "、".join(f"F{fact.id}" for fact in facts)
        summary = str(answer or "").strip() or f"已确认 {len(facts)} 个漏洞"
        project.facts[1].description = f"accepted_facts: {labels}\nanswer: {summary}"

    @staticmethod
    def _accepted_finding_facts(
        project: Project,
        fact_ids: tuple[int, ...] | list[int],
        required: int,
    ) -> tuple[list[object] | None, str]:
        submitted = list(fact_ids or ())
        if not submitted:
            return None, "未提交 accepted_fact_ids"
        accepted: list[object] = []
        problems: list[str] = []
        seen: set[int] = set()
        for fact_id in submitted:
            if fact_id in seen:
                continue
            seen.add(fact_id)
            fact = project.facts.get(fact_id)
            if fact is None:
                problems.append(f"F{fact_id} 不存在")
                continue
            if fact.id in {0, 1} or fact.kind != FactKind.EVIDENCE:
                problems.append(f"F{fact_id} 不是 evidence")
                continue
            description = fact.description or ""
            if description.startswith(PARTIAL_OUTPUT_PREFIX):
                problems.append(f"F{fact_id} 是未完成摘录")
                continue
            if description.startswith(ANSWER_WRAPPER_PREFIX):
                problems.append(f"F{fact_id} 是候选答案包装")
                continue
            accepted.append(fact)
        if problems or len(accepted) < required:
            detail = f"有效 evidence 只有 {len(accepted)} 条，需要至少 {required} 条"
            if problems:
                detail += "；" + "；".join(problems)
            return None, detail
        return accepted, ""

    @staticmethod
    def _record_rejected_findings(
        project: Project,
        required: int,
        fact_ids: tuple[int, ...] | list[int],
        reason: str,
    ) -> None:
        marker = (
            f"完成被拒绝：目的要求至少 {required} 个 evidence 漏洞事实，"
            f"accepted_fact_ids={list(fact_ids or ())} 未通过：{reason}。"
            "单条事实中的“第N个漏洞”不能代替这些 id。"
        )
        if any(fact.description == marker for fact in project.facts.values()):
            return
        project.add_fact(marker, kind=FactKind.REFUTED)

    @staticmethod
    def _connect_fact_to_goal(project: Project, fact_id: int) -> None:
        if 1 not in project.facts or fact_id in {0, 1} or fact_id not in project.facts:
            return
        if any(
            intent.from_fact_id == fact_id
            and intent.description == Dispatcher._GOAL_EDGE_DESCRIPTION
            and 1 in intent.to_fact_ids
            for intent in project.intents.values()
        ):
            return
        intent = project.add_intent(
            description=Dispatcher._GOAL_EDGE_DESCRIPTION,
            from_fact_id=fact_id,
            to_fact_id=1,
        )
        intent.mark_running()
        intent.mark_completed()

    @staticmethod
    def _connect_answer_fact_to_goal(project: Project, answer: str) -> None:
        if 1 not in project.facts:
            return
        answer_text = str(answer).strip()
        if not answer_text:
            return
        source_fact = next(
            (
                fact
                for fact in sorted(project.facts.values(), key=lambda item: item.id, reverse=True)
                if Dispatcher._fact_supports_answer(fact, answer_text)
            ),
            None,
        )
        if source_fact is None:
            return
        Dispatcher._connect_fact_to_goal(project, source_fact.id)

    @staticmethod
    def _dispatch_mode(project: Project) -> DispatchMode:
        if project.is_first_run:
            return DispatchMode.FIRST_RUN
        if project.active_intents():
            return DispatchMode.EXPLORE
        return DispatchMode.REASON

    @staticmethod
    def _enter_running(project: Project) -> DispatchMode:
        if project.status == ProjectStatus.ACTIVE:
            project.start()
        elif project.status == ProjectStatus.STOPED:
            project.start()
        return Dispatcher._dispatch_mode(project)

    @staticmethod
    def _complete_project(project: Project) -> None:
        if project.status == ProjectStatus.COMPLETED:
            return
        if project.status == ProjectStatus.INACTIVE:
            project.activate()
        if project.status in {ProjectStatus.ACTIVE, ProjectStatus.STOPED}:
            project.start()
        if project.status == ProjectStatus.RUNNING:
            project.complete()

    def _persist_step(self, project: Project, release_project: bool = False) -> None:
        if release_project or self._stop_event.is_set():
            self._release_project(project)
        self.client.save_project(project)

    @staticmethod
    def _release_project(project: Project) -> None:
        if project.status == ProjectStatus.RUNNING:
            project.stop()

    def _append_trailing_explore(
        self,
        project_id: int,
        result: DispatchResult,
        results: list[DispatchResult],
    ) -> None:
        if not step_requests_trailing_explore(result) or self._stop_event.is_set():
            return
        project = self.client.get_project(project_id)
        if not project.active_intents():
            return
        results.append(self._run_project_step(project, release_project=False))

    def _observe_target(self, result: WorkerResult) -> None:
        parts: list[str] = []
        for value in (result.fact_description, result.detail, result.answer):
            if value:
                parts.append(value)
        for prior in result.prior_results:
            for value in (prior.fact_description, prior.answer):
                if value:
                    parts.append(value)
        text = " ".join(parts).strip().lower()
        if not text:
            return
        if "target not found" in text:
            self._target_offline_streak += 1
            return
        self._target_offline_streak = 0

    def _target_offline_result(self, project: Project) -> DispatchResult | None:
        if self._target_offline_streak < 3:
            return None
        message = "target appears offline: target not found repeated 3 times"
        self._log(project.id, message, "error", {"streak": self._target_offline_streak})
        return DispatchResult(project.id, DispatchMode.SKIPPED, project.status, message)

    def _release_project_if_running(self, project_id: int) -> None:
        cas_status = getattr(self.client, "cas_project_status", None)
        if callable(cas_status):
            changed = cas_status(project_id, ProjectStatus.RUNNING, ProjectStatus.STOPED)
            if changed:
                self._log(project_id, "project released to stoped", "info")
            return
        project = self.client.get_project(project_id)
        if project.status == ProjectStatus.RUNNING:
            self._release_project(project)
            self.client.save_project(project)
            self._log(project.id, "project released to stoped", "info")

    def _ensure_first_run_failure_fact(
        self,
        project: Project,
        intent: Intent,
        result: WorkerResult,
        before_facts: set[int],
        release_project: bool,
    ) -> None:
        if not self._failed_first_run_waits_for_reason(project, intent, result):
            return
        if self._new_fact_ids(project, before_facts):
            return
        text = (intent.note or result.detail or "first-run execution failed").strip()
        fact = project.add_fact(text, kind=FactKind.ERROR)
        intent.set_targets([fact.id])
        self._persist_step(project, release_project=release_project)

    @staticmethod
    def _failed_first_run_waits_for_reason(project: Project, intent: Intent, result: WorkerResult) -> bool:
        return (
            project.task_type != "crypto"
            and not result.retryable
            and intent.status == IntentStatus.FAILED
        )

    @staticmethod
    def _new_fact_ids(project: Project, before: set[int]) -> list[int]:
        return [fact_id for fact_id in project.facts if fact_id not in before]

    @staticmethod
    def _crypto_fact_metadata(project: Project, intent: Intent | None, output_crypto: dict[str, Any] | None = None) -> dict[str, object]:
        if project.task_type != "crypto" or intent is None:
            return {}
        dependencies = intent.crypto.get("input_fact_ids")
        if dependencies is None:
            dependencies = [intent.from_fact_id] if intent.from_fact_id is not None else []
        metadata: dict[str, object] = {
            "producer_intent_id": intent.id,
            "depends_on_fact_ids": dependencies,
        }
        if intent.crypto.get("input_ids"):
            metadata["source_input_ids"] = intent.crypto["input_ids"]
        supplied = (output_crypto or {}).get("fact", {})
        if isinstance(supplied, dict):
            for key in ("source_input_ids", "typed_value", "artifact_refs", "observation_kind", "claim_status"):
                if key in supplied:
                    metadata[key] = supplied[key]
        return metadata

    @staticmethod
    def _is_crypto_candidate(answer: str | None, output_crypto: dict[str, Any]) -> bool:
        fact = output_crypto.get("fact", {})
        return bool(answer) or "candidate" in output_crypto or (isinstance(fact, dict) and fact.get("claim_status") == "candidate")

    @staticmethod
    def _record_crypto_pending(project: Project, intent: Intent | None, text: str | None, reason: str) -> None:
        if not text:
            return
        metadata = Dispatcher._crypto_fact_metadata(project, intent)
        metadata.update({"claim_status": "hypothesis", "diagnostic": reason[:600]})
        project.add_fact(f"待核实观察：{text}\n诊断：{reason}", kind=FactKind.ERROR, crypto=metadata)

    @staticmethod
    def _save_crypto_candidate(project: Project, intent: Intent | None, answer: str | None,
                               description: str | None, output_crypto: dict[str, Any]) -> int:
        if project.task_type != "crypto" or intent is None:
            raise ValueError("candidate save requires a crypto project and producer intent")
        candidate = output_crypto.get("candidate")
        if not isinstance(candidate, dict):
            candidate = {"answer": answer} if answer else {}
        if not isinstance(candidate.get("answer"), str) or not candidate["answer"]:
            raise ValueError("candidate.answer must be a nonempty string")
        if answer is not None and candidate["answer"] != answer:
            raise ValueError("answer and crypto.candidate.answer disagree")
        metadata = Dispatcher._crypto_fact_metadata(project, intent, output_crypto)
        # Candidate identity is expressed by claim_status; observation_kind has no
        # candidate value in the contract.  Normalize instead of losing the answer.
        metadata["observation_kind"] = "evidence"
        metadata["claim_status"] = "candidate"
        metadata["candidate"] = candidate
        fact = project.add_fact(description or f"candidate answer: {candidate['answer']}",
                                kind=FactKind.CANDIDATE, crypto=metadata)
        return fact.id

    @staticmethod
    def _ensure_crypto_candidate(project: Project, answer: str) -> object | None:
        text = answer.strip()
        if not text:
            return None
        for fact in sorted(project.facts.values(), key=lambda item: item.id, reverse=True):
            if fact.kind == FactKind.CANDIDATE and fact.crypto.get("candidate", {}).get("answer") == text:
                return fact
        input_ids = [ref["id"] for ref in project.crypto.get("inputs", [])]
        return project.add_fact(f"candidate answer: {text}", kind=FactKind.CANDIDATE,
                                crypto={"claim_status": "candidate", "source_input_ids": input_ids,
                                        "candidate": {"answer": text, "scheme": "auto"}})

    def _crypto_answer_verified(self, project: Project, answer: str) -> bool:
        return self._crypto_completion_source(project, answer) is not None

    def _crypto_completion_source(self, project: Project, answer: str) -> str | None:
        verifier = getattr(self.client, "verify_crypto_candidate", None)
        if not callable(verifier):
            return None
        return completion_source(project, answer, lambda fact_id: verifier(project.id, fact_id))

    @staticmethod
    def _connect_crypto_answer_to_goal(project: Project, answer: str) -> None:
        if 1 not in project.facts:
            return
        source = None
        description = "候选答案通过原始输入独立校验"
        for record in reversed(project.crypto.get("verifications", [])):
            if record.get("status") == "pass" and record.get("answer") == answer:
                source = record["candidate_fact_id"]
                break
        if source is None:
            for record in reversed(project.crypto.get("acceptances", [])):
                if record.get("answer") == answer and record.get("candidate_fact_id") in project.facts:
                    source = record["candidate_fact_id"]
                    description = "候选答案获得平台回执"
                    break
        if source is None:
            for fact in sorted(project.facts.values(), key=lambda item: item.id, reverse=True):
                if fact_supports_answer(fact, answer):
                    source = fact.id
                    description = "证据事实包含最终答案"
                    break
        if source is None:
            return
        if not any(i.from_fact_id == source and i.to_fact_id == 1 for i in project.intents.values()):
            intent = project.add_intent(description, from_fact_id=source, to_fact_id=1)
            intent.mark_running()
            intent.mark_completed()

    def _log(
        self,
        project_id: int,
        message: str,
        level: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> None:
        add_log = getattr(self.client, "add_project_log", None)
        if add_log is None:
            return
        try:
            add_log(project_id, message, level=level, metadata=metadata or {})
        except Exception:
            return

    def _agent_log(self, project_id: int):
        def log(message: str, level: str = "info", metadata: dict[str, object] | None = None) -> None:
            self._log(project_id, message, level=level, metadata=metadata or {})

        return log
