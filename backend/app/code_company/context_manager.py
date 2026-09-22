"""Layered context for Code Company Agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..workflow.context import WorkflowContext
from ..workflow.context_packer import ContextPacket, ContextPacker


@dataclass(frozen=True, slots=True)
class ContextLayers:
    task: dict[str, Any]
    project: dict[str, Any]
    repository: dict[str, Any]
    runtime: dict[str, Any]
    evidence: list[dict[str, Any]]
    memory: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "project": self.project,
            "repository": self.repository,
            "runtime": self.runtime,
            "evidence": self.evidence,
            "memory": self.memory,
        }


class CodeContextManager:
    """Select only the context a task needs; the full Run state stays durable."""

    def __init__(self, packer: ContextPacker | None = None) -> None:
        self.packer = packer or ContextPacker()

    def layers(self, context: WorkflowContext | dict[str, Any]) -> ContextLayers:
        snapshot = context.snapshot() if isinstance(context, WorkflowContext) else dict(context)
        return ContextLayers(
            task={key: snapshot.get(key) for key in ("requirement", "requirement_spec", "task", "acceptance_criteria") if key in snapshot},
            project={key: snapshot.get(key) for key in ("project_blueprint", "compiled_contract", "delivery_contract") if key in snapshot},
            repository={key: snapshot.get(key) for key in ("repo_index", "workspace") if key in snapshot},
            runtime={key: snapshot.get(key) for key in ("runtime", "current_diff", "tool_result", "build_error", "test_error") if key in snapshot},
            evidence=list(snapshot.get("evidence") or []) if isinstance(snapshot.get("evidence"), list) else [],
            memory=list(snapshot.get("memory") or []) if isinstance(snapshot.get("memory"), list) else [],
        )

    def packet(self, template: str, context: WorkflowContext, max_input_tokens: int, *, preserve_keys: tuple[str, ...] = ()) -> ContextPacket:
        return self.packer.pack(template, context, max_input_tokens, preserve_keys=preserve_keys)
