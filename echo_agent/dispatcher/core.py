from __future__ import annotations

import copy
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Mapping

from echo_agent.dispatcher.agents import CodexDockerAgent, CodexReasoner, CodexWorker
from echo_agent.dispatcher.client import ProjectClient
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
    }:
        return False
    return True


def step_requests_trailing_explore(result: DispatchResult) -> bool:
    message = result.message or ""
    return message.startswith("planned ") and not message.startswith("planned 0")


class Dispatcher:
    _OUTPUT_TAIL_MARKER = "\noutput_tail:\n"
    _PARTIAL_OUTPUT_PREFIX = "未完成执行的输出摘录："
    _PARTIAL_OUTPUT_MIN_CHARS = 40
    _PARTIAL_OUTPUT_LIMIT = 4000

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
        self._stop_event = stop_event or threading.Event()
        self._state_lock = threading.Lock()
        self._retry_counts: dict[int, int] = {}
        self._reason_failures = 0
        self._reason_format_failures = 0
        self._reason_salvage_failures = 0
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
                if result.mode in {DispatchMode.COMPLETED, DispatchMode.SKIPPED} or self._stop_event.is_set():
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
        offline = self._target_offline_result(project)
        if offline is not None:
            return offline
        return DispatchResult(project.id, DispatchMode.FIRST_RUN, project.status, "initial intent executed")

    def _explore(
        self,
        project: Project,
        intents: list[Intent],
        mode: DispatchMode,
        release_project: bool,
    ) -> DispatchResult:
        self._log(
            project.id,
            f"explore started with {len(intents)} active intents",
            "info",
            {"intent_ids": [intent.id for intent in intents], "codex_slots": self.max_codex_slots},
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
                if self._is_timeout_failure(exc):
                    return self._reason_timeout_failure(project, exc, release_project=release_project)
                return self._reason_failure_result(project, exc, release_project=release_project)
        finally:
            self._model_slots.release()
        self._reason_failures = 0
        self._reason_format_failures = 0
        completion_rejected = False
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
            if result.answer and self._answer_provenance(project, result.answer) == "verified":
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
            self._log(
                project.id,
                "reason completion rejected because the answer is not in evidence",
                "warning",
                {"has_answer": bool(result.answer)},
            )
            completion_rejected = True
            self._record_rejected_answer(project, result.answer)

        planned_intents = self._planned_intents_from_reason_result(project, result)
        if completion_rejected and not planned_intents and result.answer:
            # 模型声称完成但缺少证据、又没规划下一步：自动生成一条针对性验证，
            # 避免运行停在原地（BUG-016）。action_key 带答案哈希，避免不同候选
            # 被同一条 intent 去重掉（BUG-018）。
            digest = hashlib.sha1(str(result.answer).encode("utf-8")).hexdigest()[:8]
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
        skipped_repeat = 0
        skipped_from = 0
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
            if to_fact_id is None and 1 in project.facts:
                to_fact_id = 1
            if from_fact_id is None:
                skipped_from += 1
                continue
            action_key = str(item.get("action_key") or "")
            if self._is_duplicate_intent(project, description, from_fact_id, action_key):
                skipped_repeat += 1
                continue
            intent = project.add_intent(
                description=description,
                from_fact_id=from_fact_id,
                to_fact_id=to_fact_id,
                action_key=action_key,
            )
            planned_intent_ids.append(intent.id)
            planned_count += 1
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
        self._log(
            project.id,
            f"reason planned {planned_count} new intents",
            "info",
            {
                "intent_ids": planned_intent_ids,
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
                }
            )
        return normalized

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
    def _record_partial_output(cls, project: Project, detail: str | None) -> None:
        if not detail or cls._OUTPUT_TAIL_MARKER not in detail:
            return
        tail = strip_prompt_template_text(detail.split(cls._OUTPUT_TAIL_MARKER, 1)[1]).strip()
        tail = strip_result_json_text(tail).strip()
        if len(tail) < cls._PARTIAL_OUTPUT_MIN_CHARS:
            return
        if len(tail) > cls._PARTIAL_OUTPUT_LIMIT:
            tail = tail[-cls._PARTIAL_OUTPUT_LIMIT:]
        project.add_fact(f"{cls._PARTIAL_OUTPUT_PREFIX}\n{tail}", kind=FactKind.ERROR)

    @staticmethod
    def _is_goal_or_evidence(project: Project, fact_id: int) -> bool:
        fact = project.facts.get(fact_id)
        if fact is None:
            return False
        return fact.kind in {FactKind.GOAL, FactKind.EVIDENCE}

    _ANSWER_WRAPPER_PREFIX = "agent reported possible final answer:"
    _ANSWER_SOURCE_CONTEXT_CHARS = 240

    @staticmethod
    def _answer_source_line(description: str) -> str:
        for line in description.splitlines():
            if line.startswith("source:"):
                return line.split(":", 1)[1].strip()
        return ""

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
        kind = getattr(fact, "kind", None)
        description = str(getattr(fact, "description", ""))
        if kind != FactKind.EVIDENCE or answer not in description:
            return False
        if description.startswith(cls._PARTIAL_OUTPUT_PREFIX):
            return False
        if description.startswith(cls._ANSWER_WRAPPER_PREFIX):
            source = cls._answer_source_line(description)
            return bool(source and answer in source)
        return True

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
        max_workers = min(self.max_explore_workers, len(intents))
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
                    result = self._failure_result(f"intent {intent.id}", exc)
                yield IntentExecutionResult(intent=intent, result=result)

    def _run_intent_with_slot(
        self,
        project: Project,
        intent: Intent,
        mode: DispatchMode,
        scope: str,
    ) -> WorkerResult:
        if self._stop_event.is_set() or not self._acquire_model_slot():
            return WorkerResult(retryable=True)
        try:
            if self._stop_event.is_set():
                return WorkerResult(retryable=True)
            with self._state_lock:
                intent.mark_running()
                self.client.save_project(project)
            return self._execute_with_retry(project, intent, mode, scope)
        finally:
            self._model_slots.release()

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
            return self._execute_intent(project, intent, mode)
        except Exception as exc:
            if self._stop_event.is_set():
                return WorkerResult(retryable=True)
            if not self._is_retryable_failure(exc):
                self._log(
                    project.id,
                    f"{scope} execution failed: {type(exc).__name__}: {exc}",
                    "error",
                    {"intent_id": intent.id},
                )
                return self._failure_result(scope, exc)
            self._log(
                project.id,
                f"{scope} hit a retryable failure; retrying once",
                "warning",
                {"intent_id": intent.id, "error": str(exc)},
            )
            try:
                return self._execute_intent(project, intent, mode)
            except Exception as retry_exc:
                if self._stop_event.is_set() or self._is_retryable_failure(retry_exc):
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
                return self._failure_result(scope, retry_exc)

    def _failure_result(self, scope: str, exc: Exception) -> WorkerResult:
        if self._stop_event.is_set() or self._is_retryable_failure(exc):
            return WorkerResult(retryable=True, detail=str(exc))
        return WorkerResult(outcome="error", detail=self._execution_failed_fact(scope, exc))

    @staticmethod
    def _is_retryable_failure(exc: Exception) -> bool:
        text = str(exc).lower()
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
        marker = "\noutput_tail:\n"
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
        if result.timed_out:
            Dispatcher._store_worker_observations(project, result, intent.from_fact_id)
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
            Dispatcher._add_prior_fact(project, prior, intent.from_fact_id)
        if result.outcome == "error":
            Dispatcher._persist_json_facts_from_detail(project, result.detail, intent.from_fact_id)
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
            fact = project.add_fact(fact_text, kind=FactKind.REFUTED)
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.REFUTED)
            return
        new_facts = [project.facts[fact_id] for fact_id in project.facts if fact_id not in existing_fact_ids]
        if Dispatcher._final_answer_already_recorded(result, new_facts):
            if intent.status == IntentStatus.RUNNING and new_facts:
                intent.mark_completed(to_fact_id=new_facts[-1].id, outcome=IntentOutcome.VERIFIED)
            return
        fact_text = Dispatcher._worker_result_fact_text(result)
        if fact_text:
            fact = project.add_fact(fact_text, kind=FactKind.EVIDENCE)
            intent.mark_completed(to_fact_id=fact.id, outcome=IntentOutcome.VERIFIED)
            return
        if intent.status == IntentStatus.RUNNING:
            intent.mark_failed(IntentOutcome.EMPTY, note="intent returned no fact or answer")



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
        from_fact_id: int | None = None,
    ) -> int:
        observations = list(result.prior_results)
        if result.fact_description or result.answer:
            observations.append(
                WorkerObservation(
                    answer=result.answer,
                    fact_description=result.fact_description,
                    outcome=result.outcome,
                    answer_source=result.answer_source,
                )
            )
        before = len(project.facts)
        for observation in observations:
            Dispatcher._add_prior_fact(project, observation, from_fact_id)
        return len(project.facts) - before

    @classmethod
    def _persist_json_facts_from_detail(
        cls,
        project: Project,
        detail: str | None,
        from_fact_id: int | None = None,
    ) -> int:
        if not detail or cls._OUTPUT_TAIL_MARKER not in detail:
            return 0
        tail = detail.split(cls._OUTPUT_TAIL_MARKER, 1)[1]
        try:
            parsed = parse_worker_result(tail)
        except ValueError:
            return 0
        return cls._store_worker_observations(project, parsed, from_fact_id)

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
        from_fact_id: int | None = None,
    ) -> object | None:
        fact_text = _optional_fact_description(observation.fact_description)
        if not fact_text and observation.answer:
            fact_text = f"agent reported possible final answer: {observation.answer}"
        if not fact_text:
            return
        if observation.answer_source:
            fact_text = f"{fact_text}\nsource: {observation.answer_source}"
        if observation.outcome == "refuted":
            fact = project.add_fact(fact_text, kind=FactKind.REFUTED)
        elif observation.outcome == "error":
            return
        else:
            fact = project.add_fact(fact_text, kind=FactKind.EVIDENCE)
        return fact

    def _execute_intent(self, project: Project, intent: Intent, mode: DispatchMode) -> WorkerResult:
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

    @staticmethod
    def _set_end_fact_result(project: Project, answer: str) -> None:
        if 1 not in project.facts:
            return
        project.facts[1].description = f"result\nanswer: {answer}"

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
        producers = [
            intent
            for intent in project.intents.values()
            if source_fact.id in intent.to_fact_ids and not Dispatcher._is_initial_probe_intent(intent)
        ]
        if producers:
            max(producers, key=lambda item: item.id).add_target(1)
            return
        if any(
            intent.from_fact_id == source_fact.id and 1 in intent.to_fact_ids
            for intent in project.intents.values()
        ):
            return
        intent = project.add_intent(
            description="得到最终结果",
            from_fact_id=source_fact.id,
            to_fact_id=1,
        )
        intent.mark_running()
        intent.mark_completed()

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
        project = self.client.get_project(project_id)
        if project.status == ProjectStatus.RUNNING:
            self._release_project(project)
            self.client.save_project(project)
            self._log(project.id, "project released to stoped", "info")

    @staticmethod
    def _new_fact_ids(project: Project, before: set[int]) -> list[int]:
        return [fact_id for fact_id in project.facts if fact_id not in before]

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
