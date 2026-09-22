"""Core workflow models.

这些模型只描述业务数据，不包含调度逻辑。这样 YAML、API、执行器和前端
都可以共享同一份清晰的数据契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepType(str, Enum):
    AGENT = "agent"
    APPROVAL = "approval"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(slots=True)
class StepDefinition:
    id: str
    type: StepType = StepType.AGENT
    agent_id: str | None = None
    task_template: str = ""
    output: str | None = None
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: float = 120
    retry_count: int = 2
    max_tokens: int | None = None
    thinking: str = "auto"
    output_format: str = "text"
    failure_policy: str = "fail"
    runtime_plan: bool = False
    generation_mode: str = "single"
    enforce_artifact_contract: bool = False
    # Candidate prompt Skills are selected at Run time.  They remain optional
    # so existing workflows continue to execute unchanged.
    skills: list[str] = field(default_factory=list)
    skill_mode: str = "auto"
    # Optional deterministic post-step validation.  The configuration is
    # deliberately generic so future company templates can select their own
    # validation profile without changing the executor.
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentDefinition:
    id: str
    name: str
    description: str
    system_prompt: str


@dataclass(slots=True)
class WorkflowDefinition:
    id: str
    name: str
    concurrency: int = 3
    inputs: dict[str, Any] = field(default_factory=dict)
    steps: list[StepDefinition] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    runtime_defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StepResult:
    step_id: str
    status: StepStatus
    output: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0
    retry_count: int = 0
    error_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class WorkflowResult:
    run_id: str
    workflow_id: str
    status: RunStatus
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0
    error_message: str | None = None
    final_report: str | None = None
