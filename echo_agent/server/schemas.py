from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from echo_agent.models import ProjectStatus


class ProjectCreateRequest(BaseModel):
    id: int | None = None
    name: str | None = None
    docker: str | None = None
    target_url: str | None = None
    hint: str = ""
    objective: str = ""


class ProjectDetailsUpdate(BaseModel):
    name: str
    target_url: str | None = None
    hint: str = ""
    objective: str = ""


class ProjectSaveRequest(BaseModel):
    id: int | None = None
    name: str
    status: ProjectStatus | str = ProjectStatus.INACTIVE
    facts: list[dict[str, Any]] = Field(default_factory=list)
    intents: list[dict[str, Any]] = Field(default_factory=list)
    docker: str | None = None
    target_url: str | None = None
    hint: str = ""
    objective: str = ""


class ProjectStatusRequest(BaseModel):
    status: ProjectStatus | str


class FactCreateRequest(BaseModel):
    id: int | None = None
    description: str


class IntentCreateRequest(BaseModel):
    id: int | None = None
    from_fact_id: int | None = Field(default=None, alias="from")
    to_fact_id: int | None = Field(default=None, alias="to")
    description: str = ""

    model_config = {"populate_by_name": True}


class ProjectLogCreateRequest(BaseModel):
    level: str = "info"
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectRunRequest(BaseModel):
    interval_seconds: float | None = None
    max_steps: int | None = None
    max_workers: int | None = None


class ProjectBatchRunRequest(BaseModel):
    project_ids: list[int] | None = None
    interval_seconds: float | None = None
    max_steps: int | None = None
    max_workers: int | None = None


class ConsoleSettingsUpdate(BaseModel):
    max_concurrent_projects: int = Field(ge=1)
    codex_max_slots: int = Field(ge=1)
    explore_max_workers: int = Field(ge=1)
    base_url: str
    model: str
    api_key: str | None = None
    clear_api_key: bool = False
