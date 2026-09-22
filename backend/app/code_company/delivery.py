"""Delivery facade for Code Company artifacts and evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..services.artifacts import ArtifactService


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    run_id: str
    artifact_count: int
    archive_path: str | None
    status: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"runId": self.run_id, "artifactCount": self.artifact_count, "archivePath": self.archive_path, "status": self.status, "message": self.message}


class DeliveryManager:
    """Keep materialization, secret scan and ZIP delivery in one boundary."""

    def __init__(self, artifact_service: ArtifactService) -> None:
        self.artifact_service = artifact_service

    async def package(self, run_id: str, *, strict: bool = True) -> DeliveryResult:
        try:
            archive = await asyncio.to_thread(self.artifact_service.create_archive, run_id, strict=strict)
        except FileNotFoundError as exc:
            return DeliveryResult(run_id, 0, None, "blocked", str(exc))
        except ValueError as exc:
            return DeliveryResult(run_id, len(self.artifact_service.list_public(run_id)), None, "blocked", str(exc))
        return DeliveryResult(run_id, len(self.artifact_service.list_public(run_id)), str(Path(archive)), "ready", "Artifacts packaged after allowlist and secret validation.")
