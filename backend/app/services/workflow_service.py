"""Workflow loading, validation and GraphDTO conversion."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from dataclasses import replace

from ..workflow.dag import build_dag
from ..workflow.models import StepType, WorkflowDefinition
from ..workflow.parser import parse_workflow_file


class WorkflowService:
    def __init__(self, workflows_dir: str | Path) -> None:
        self.workflows_dir = Path(workflows_dir)

    def list_workflows(self) -> list[WorkflowDefinition]:
        return [self.get_workflow(path.stem) for path in sorted(self.workflows_dir.glob("*.yaml"))]

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        path = self.workflows_dir / f"{workflow_id}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Workflow not found: {workflow_id}")
        workflow = parse_workflow_file(path, workflow_id)
        build_dag(workflow)
        return workflow

    def get_workflow_for_run(self, run: dict[str, Any]) -> WorkflowDefinition:
        """Prefer the immutable workflow snapshot captured when a Run started."""
        snapshot = run.get("workflow_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("id"):
            return self.from_snapshot(snapshot)
        return self.get_workflow(str(run.get("workflow_id") or ""))

    @staticmethod
    def from_snapshot(snapshot: dict[str, Any]) -> WorkflowDefinition:
        """Rehydrate a validated WorkflowDefinition without reading mutable YAML."""
        raw_steps = snapshot.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("Workflow snapshot does not contain steps")
        steps: list[Any] = []
        from ..workflow.models import StepDefinition

        for raw in raw_steps:
            if not isinstance(raw, dict) or not raw.get("id"):
                raise ValueError("Workflow snapshot contains an invalid step")
            steps.append(
                StepDefinition(
                    id=str(raw["id"]),
                    type=StepType(str(raw.get("type", StepType.AGENT.value))),
                    agent_id=raw.get("agent_id", raw.get("agentId")),
                    task_template=str(raw.get("task_template", raw.get("taskTemplate", ""))),
                    output=raw.get("output"),
                    depends_on=[str(item) for item in raw.get("depends_on", raw.get("dependsOn", []))],
                    timeout_seconds=float(raw.get("timeout_seconds", raw.get("timeoutSeconds", 120))),
                    retry_count=int(raw.get("retry_count", raw.get("retryCount", 2))),
                    max_tokens=raw.get("max_tokens", raw.get("maxTokens")),
                    thinking=str(raw.get("thinking", "auto")),
                    output_format=str(raw.get("output_format", raw.get("outputFormat", "text"))),
                    failure_policy=str(raw.get("failure_policy", raw.get("failurePolicy", "fail"))),
                    runtime_plan=bool(raw.get("runtime_plan", raw.get("runtimePlan", False))),
                    generation_mode=str(raw.get("generation_mode", raw.get("generationMode", "single"))),
                    enforce_artifact_contract=bool(raw.get("enforce_artifact_contract", raw.get("enforceArtifactContract", False))),
                    skills=[str(item) for item in raw.get("skills", []) if str(item).strip()] if isinstance(raw.get("skills", []), list) else [str(raw.get("skill"))] if raw.get("skill") else [],
                    skill_mode=str(raw.get("skill_mode", raw.get("skillMode", "auto"))),
                    validation=raw.get("validation", {}) if isinstance(raw.get("validation", {}), dict) else {},
                )
            )
        workflow = WorkflowDefinition(
            id=str(snapshot.get("id")),
            name=str(snapshot.get("name", snapshot.get("id"))),
            concurrency=int(snapshot.get("concurrency", 3)),
            inputs=snapshot.get("inputs", {}) if isinstance(snapshot.get("inputs", {}), dict) else {},
            steps=steps,
            meta=snapshot.get("meta", {}) if isinstance(snapshot.get("meta", {}), dict) else {},
            runtime_defaults=snapshot.get("runtime_defaults", snapshot.get("runtimeDefaults", {})) if isinstance(snapshot.get("runtime_defaults", snapshot.get("runtimeDefaults", {})), dict) else {},
        )
        build_dag(workflow)
        return workflow

    def graph_dto(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        dag = build_dag(workflow)
        layout = workflow.meta.get("layout", {})
        nodes: list[dict[str, Any]] = []
        for index, step in enumerate(workflow.steps):
            level = next((level_index for level_index, ids in enumerate(dag.levels) if step.id in ids), 0)
            default_x = 88 + (index % max(1, len(dag.levels[level]))) * 220
            default_y = 76 + level * 160
            position = layout.get(step.id, {"x": default_x, "y": default_y})
            nodes.append({
                "id": step.id,
                "type": "approval" if step.type == StepType.APPROVAL else "agent",
                "position": position,
                "data": {
                    "label": step.id.replace("_", " ").title(),
                    "agentId": step.agent_id,
                    "output": step.output,
                    "runtime": {"maxTokens": step.max_tokens, "retry": step.retry_count, "thinking": step.thinking},
                    "outputFormat": step.output_format,
                    "failurePolicy": step.failure_policy,
                    "runtimePlan": step.runtime_plan,
                    "generationMode": step.generation_mode,
                    "enforceArtifactContract": step.enforce_artifact_contract,
                    "skills": list(step.skills),
                    "skillMode": step.skill_mode,
                    "validation": step.validation,
                },
            })
        # 用 Workflow 中声明的依赖顺序生成边，Graph 往返后不会产生无意义的顺序抖动。
        edges = [{"id": f"{source}-{step.id}", "source": source, "target": step.id} for step in workflow.steps for source in step.depends_on]
        return {"id": workflow.id, "name": workflow.name, "nodes": nodes, "edges": edges, "runtimeDefaults": workflow.runtime_defaults}

    def graph_to_workflow(self, workflow: WorkflowDefinition, graph: dict[str, Any]) -> WorkflowDefinition:
        """Apply graph edits back to a workflow without mixing UI position into execution rules."""
        node_ids = {str(node.get("id")) for node in graph.get("nodes", []) if isinstance(node, dict)}
        known_ids = {step.id for step in workflow.steps}
        if node_ids != known_ids:
            raise ValueError("Graph nodes must match workflow step ids")
        dependencies = {step.id: [] for step in workflow.steps}
        for edge in graph.get("edges", []):
            source, target = edge.get("source"), edge.get("target")
            if source not in known_ids or target not in known_ids or source == target:
                raise ValueError("Graph contains an invalid edge")
            dependencies[target].append(source)
        layout = {str(node["id"]): node.get("position", {"x": 0, "y": 0}) for node in graph.get("nodes", [])}
        steps = [replace(step, depends_on=dependencies[step.id]) for step in workflow.steps]
        return replace(workflow, steps=steps, meta={**workflow.meta, "layout": layout})
