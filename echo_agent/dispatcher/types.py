from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from echo_agent.models import Intent, Project, ProjectStatus


class DispatchMode(str, Enum):
    SKIPPED = "skipped"
    FIRST_RUN = "first_run"
    EXPLORE = "explore"
    REASON = "reason"
    COMPLETED = "completed"


@dataclass(slots=True)
class WorkerObservation:
    answer: str | None = None
    fact_description: str | None = None
    outcome: str | None = None
    answer_source: str | None = None
    crypto: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerResult:
    answer: str | None = None
    fact_description: str | None = None
    retryable: bool = False
    detail: str | None = None
    outcome: str | None = None
    prior_results: list[WorkerObservation] = field(default_factory=list)
    answer_source: str | None = None
    timed_out: bool = False
    crypto: dict[str, Any] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)

    @property
    def has_answer(self) -> bool:
        return self.answer is not None


@dataclass(slots=True)
class ReasonResult:
    completed: bool = False
    next_intent_descriptions: list[str] | None = None
    next_intents: list[dict[str, object]] | None = None
    answer: str | None = None
    stop_reason: str = ""
    raw_output: str = ""
    salvaged: bool = False


@dataclass(slots=True)
class DispatchResult:
    project_id: int
    mode: DispatchMode
    status: ProjectStatus
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["mode"] = self.mode.value
        data["status"] = self.status.value
        return data


@dataclass(slots=True)
class IntentExecutionResult:
    intent: Intent
    result: WorkerResult


@dataclass(slots=True)
class AgentRunResult:
    output: str
    salvaged: bool = False
    timed_out: bool = False
    raw_output: str = ""
    limit_reason: str | None = None


AgentLogCallback = Callable[[str, str, dict[str, object] | None], None]


class WorkerAdapter(Protocol):
    def execute_intent(self, project: Project, intent: Intent) -> WorkerResult:
        raise NotImplementedError


class Reasoner(Protocol):
    def plan(self, project: Project) -> ReasonResult:
        raise NotImplementedError


class AgentAdapter(Protocol):
    def run(self, project: Project, prompt: str, mode: DispatchMode) -> AgentRunResult:
        raise NotImplementedError
