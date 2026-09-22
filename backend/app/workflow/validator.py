"""Workflow structural validation."""

from __future__ import annotations

from .models import StepType, WorkflowDefinition


class WorkflowValidationError(ValueError):
    pass


def validate_workflow(workflow: WorkflowDefinition) -> None:
    if not workflow.steps:
        raise WorkflowValidationError("Workflow must contain at least one step")
    if not 1 <= workflow.concurrency <= 10:
        raise WorkflowValidationError("Workflow concurrency must be between 1 and 10")

    step_ids = [step.id for step in workflow.steps]
    if len(step_ids) != len(set(step_ids)):
        duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
        raise WorkflowValidationError(f"Duplicate step id: {', '.join(duplicates)}")

    known_ids = set(step_ids)
    for step in workflow.steps:
        if step.id in step.depends_on:
            raise WorkflowValidationError(f"Step cannot depend on itself: {step.id}")
        missing = [dependency for dependency in step.depends_on if dependency not in known_ids]
        if missing:
            raise WorkflowValidationError(
                f"Step {step.id} depends on missing step(s): {', '.join(missing)}"
            )
        if step.type == StepType.AGENT and not step.agent_id:
            raise WorkflowValidationError(f"Agent step {step.id} requires an agent")
        if step.runtime_plan and step.type != StepType.AGENT:
            raise WorkflowValidationError(f"Runtime plan step {step.id} must be an Agent step")
        if step.runtime_plan and step.output_format != "json":
            raise WorkflowValidationError(f"Runtime plan step {step.id} must use JSON output")
        if step.runtime_plan and not step.output:
            raise WorkflowValidationError(f"Runtime plan step {step.id} requires an output variable")
