"""Evidence-first verification boundary for Code Company.

The existing validators remain the implementation.  This facade is the seam
where build/startup/browser gates can be added without growing the executor's
dependency list or changing Agent YAML.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..workflow.artifact_validator import ArtifactValidationResult, ArtifactValidator, ValidationCheck
from ..workflow.integration_gate import IntegrationGate


class VerificationEngine:
    def __init__(
        self,
        artifact_validator: ArtifactValidator | None = None,
        integration_gate: IntegrationGate | None = None,
    ) -> None:
        self.artifact_validator = artifact_validator or ArtifactValidator()
        self.integration_gate = integration_gate or IntegrationGate()

    async def validate_artifacts(
        self,
        context: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ArtifactValidationResult:
        """Run deterministic Artifact checks and return their evidence result."""
        try:
            return await self.artifact_validator.validate(context, config, emit=emit)
        except Exception as exc:
            # Unexpected validator exceptions are platform defects. They must
            # retain evidence and must never trigger source-Agent rewrites.
            check = ValidationCheck(
                "validator-error", "platform", "验证器内部错误", "failed",
                f"确定性验证器异常：{type(exc).__name__}；请检查平台验证逻辑。",
                output=f"{type(exc).__name__}: {str(exc)[:2000]}",
                evidence={"exceptionType": type(exc).__name__},
            )
            return ArtifactValidationResult("failed", check.message, (), (check,))

    def normalize_checks(self, checks: list[dict[str, Any]], *, gate: str = "integration") -> dict[str, Any]:
        """Convert raw checks into the platform evidence/failure contract."""
        return self.integration_gate.normalize(checks, gate=gate)
