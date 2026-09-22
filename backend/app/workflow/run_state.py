"""Mutable state for one durable workflow run.

The state object is shared by the lifecycle and LangGraph adapters, so it
must not depend on ``WorkflowExecutor``.  Keeping it in its own module makes
the execution facade replaceable without changing checkpoint data.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .context import WorkflowContext
from .dag import WorkflowDAG
from .langgraph_runtime import LangGraphWorkflow
from .models import StepStatus, WorkflowDefinition
from .requirements import RequirementSpec


@dataclass(slots=True)
class RunState:
    """In-memory projection of a durable workflow run."""

    run_id: str
    workflow: WorkflowDefinition
    dag: WorkflowDAG
    context: WorkflowContext
    requirement_spec: RequirementSpec | None = None
    runtime: dict[str, Any] = field(default_factory=dict)
    current_level: int = 0
    waiting_step_id: str | None = None
    results: dict[str, StepStatus] = field(default_factory=dict)
    adaptive_policy: dict[str, Any] = field(default_factory=dict)
    runtime_budget_plan: dict[str, Any] = field(default_factory=dict)
    policy_skipped_steps: set[str] = field(default_factory=set)
    graph: LangGraphWorkflow | None = None
    checkpoint_thread_id: str | None = None
    recovery_count: int = 0
    recovery_mode: bool = False
    task: asyncio.Task[Any] | None = None
    resume_task: asyncio.Task[Any] | None = None
    started_perf: float = field(default_factory=time.perf_counter)
    approval_started_perf: float | None = None
    excluded_approval_ms: int = 0
    clarification_request: dict[str, Any] | None = None
    blueprint: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None
    execution_status: str = "PENDING"
    delivery_status: str = "NOT_EVALUATED"

