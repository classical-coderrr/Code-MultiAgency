"""Capability-based requirement routing boundary."""

from __future__ import annotations

import re

from .adaptive import infer_requirement_capabilities
from .requirement_gaps import RequirementGapAnalyzer
from .requirements import RequirementSpec


class CapabilityRouter:
    """Convert an untrusted user request into a versioned RequirementSpec."""

    VERSION = "1.0"

    def __init__(self, gap_analyzer: RequirementGapAnalyzer | None = None) -> None:
        self.gap_analyzer = gap_analyzer or RequirementGapAnalyzer()

    def route(self, requirement: str) -> RequirementSpec:
        raw = str(requirement or "").strip()
        inferred = infer_requirement_capabilities(raw)
        gap_analysis = self.gap_analyzer.analyze(raw, inferred)
        normalized = re.sub(r"\s+", " ", raw).strip()
        stacks = gap_analysis["requested_stacks"]
        complete_stack_boundary = (
            stacks.get("backend") not in {None, "none", "unspecified"}
            and stacks.get("frontend") not in {None, "none", "unspecified"}
        )
        needs_clarification = bool(
            gap_analysis["impactful_gaps"]
            or (inferred.get("ambiguous", False) and not complete_stack_boundary and not inferred.get("explicit_pure_frontend"))
        )
        return RequirementSpec.model_validate({
            "schema_version": "1.0",
            "router_version": self.VERSION,
            "source": "rules+heuristics",
            "raw_requirement": raw,
            "normalized_requirement": normalized,
            **inferred,
            **gap_analysis,
            "needs_clarification": needs_clarification,
            "state": "USER_CLARIFICATION_REQUIRED" if needs_clarification else "ASSUMPTION_ALLOWED" if gap_analysis["gaps"] else "CLEAR",
            "matched_signals": inferred.get("evidence", []),
            "reason": self._reason(inferred),
        })

    @staticmethod
    def _reason(inferred: dict[str, object]) -> str:
        if inferred.get("explicit_pure_frontend"):
            return "用户明确允许纯前端或本地存储。"
        if inferred.get("backend_required"):
            return "需求能力包含服务端、业务数据或需要后端保护的能力。"
        if inferred.get("ambiguous"):
            return "需求范围不明确，保留实现分支并交由人工确认。"
        return "未检测到必须保留 Backend 的能力。"
