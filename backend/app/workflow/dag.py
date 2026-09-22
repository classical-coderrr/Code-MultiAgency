"""Small, dependency-aware DAG engine used by the executor."""

from __future__ import annotations

from dataclasses import dataclass

from .models import StepDefinition, WorkflowDefinition
from .validator import WorkflowValidationError, validate_workflow


@dataclass(slots=True)
class WorkflowDAG:
    levels: list[list[str]]
    dependencies: dict[str, set[str]]
    dependents: dict[str, set[str]]

    @property
    def topological_order(self) -> list[str]:
        return [step_id for level in self.levels for step_id in level]


def build_dag(workflow: WorkflowDefinition) -> WorkflowDAG:
    validate_workflow(workflow)
    dependencies = {step.id: set(step.depends_on) for step in workflow.steps}
    dependents = {step.id: set() for step in workflow.steps}
    for step_id, deps in dependencies.items():
        for dependency in deps:
            dependents[dependency].add(step_id)

    # Kahn 算法按层移除节点；最后仍无法移除的节点说明依赖图存在环。
    remaining = {step_id: set(deps) for step_id, deps in dependencies.items()}
    levels: list[list[str]] = []
    while remaining:
        ready = [step.id for step in workflow.steps if step.id in remaining and not remaining[step.id]]
        if not ready:
            cycle_nodes = ", ".join(sorted(remaining))
            raise WorkflowValidationError(f"Workflow contains a cycle involving: {cycle_nodes}")
        levels.append(ready)
        for step_id in ready:
            remaining.pop(step_id)
        for deps in remaining.values():
            deps.difference_update(ready)

    return WorkflowDAG(levels=levels, dependencies=dependencies, dependents=dependents)
