"""Artifact completeness and durable materialization services."""

from __future__ import annotations

import asyncio
from typing import Any

from ..services.artifacts import ArtifactService
from .events import WorkflowEventBus
from .models import StepStatus, StepType
from .run_state import RunState
from .runtime_policy import RuntimePolicyResolver


class ArtifactDeliveryService:
    def __init__(
        self,
        artifact_service: ArtifactService | None,
        event_bus: WorkflowEventBus,
        runtime_policy: RuntimePolicyResolver,
    ) -> None:
        self.artifact_service = artifact_service
        self.event_bus = event_bus
        self.runtime_policy = runtime_policy

    async def materialize(self, state: RunState, final_report: str) -> str | None:
        """Persist current artifacts without masking the original run error."""

        if self.artifact_service is None:
            return None
        try:
            artifacts = await asyncio.to_thread(
                self.artifact_service.materialize,
                state.run_id,
                state.workflow.id,
                state.context.snapshot(),
                final_report,
            )
            for artifact in artifacts:
                await self.event_bus.emit(
                    "artifact.created", state.run_id, {"artifact": artifact}
                )
            return None
        except Exception as exc:
            message = str(exc) or "Artifact materialization failed"
            await self.event_bus.emit(
                "artifact.failed", state.run_id, {"error": message}
            )
            return message

    def completeness_gaps(self, state: RunState) -> list[str]:
        """Check that every successful producer owns usable, unique files."""

        raw_files = state.context.snapshot().get("__artifact_files__")
        files = (
            [item for item in raw_files if isinstance(item, dict)]
            if isinstance(raw_files, list)
            else []
        )
        producers = [
            step
            for step in state.workflow.steps
            if step.type == StepType.AGENT
            and state.results.get(step.id) == StepStatus.SUCCESS
            and step.id not in state.policy_skipped_steps
            and self.runtime_policy.generation_mode(state, step) == "artifacts"
        ]
        if not producers:
            return []

        gaps: list[str] = []
        files_by_step: dict[str, list[dict[str, Any]]] = {}
        names_by_owner: dict[str, set[str]] = {}
        for item in files:
            owner = str(item.get("step_id") or item.get("owner_step") or "").strip()
            name = str(item.get("name") or item.get("path") or "").strip()
            content = item.get("content")
            if owner and name and isinstance(content, str) and content.strip():
                files_by_step.setdefault(owner, []).append(item)
                names_by_owner.setdefault(name, set()).add(owner)

        for step in producers:
            if not files_by_step.get(step.id):
                gaps.append(f"{step.id} 未返回可交付文件")
        for name, owners in sorted(names_by_owner.items()):
            if len(owners) > 1:
                gaps.append(
                    f"文件 {name} 被多个 Agent 重复生成（{', '.join(sorted(owners))}）"
                )
        return gaps

