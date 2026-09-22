"""Typed Architecture decision parsing and routing validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .adaptive import parse_architecture_decision, reconcile_architecture_decision
from .requirements import RequirementSpec


class ArchitectureDecision(BaseModel):
    """The only Architecture output allowed to reach the executor."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1.0"
    project_type: Literal["static_html", "web_app", "backend_service", "unknown"]
    backend_required: bool
    complexity: Literal["low", "medium", "high"]
    summary: str = Field(default="", max_length=500)
    frontend_plan: list[str] = Field(default_factory=list, max_length=8)
    backend_reason: str = Field(default="", max_length=500)
    risks: list[str] = Field(default_factory=list, max_length=8)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    optional_capabilities: list[str] = Field(default_factory=list, max_length=32)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list, max_length=8)
    routing_guard: dict[str, Any] = Field(default_factory=dict)
    delivery_contract: dict[str, Any] = Field(default_factory=dict)

    @field_validator("frontend_plan", "risks", "required_capabilities", "optional_capabilities", "evidence", "clarification_questions", mode="before")
    @classmethod
    def normalize_lists(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]


class ArchitectureValidator:
    """Validate model output against the typed RequirementSpec contract."""

    VERSION = "1.0"

    def parse(self, text: str) -> dict[str, Any] | None:
        return parse_architecture_decision(text)

    def validate(
        self,
        decision: Any,
        requirement: RequirementSpec,
    ) -> ArchitectureDecision | None:
        # Provider output is untrusted. A model may return a JSON string,
        # quoted JSON, or a list even when the prompt asks for an object. Do
        # not let that malformed value reach routing code that calls `.get()`.
        if not isinstance(decision, dict):
            return None
        routing = {
            "backend_required": requirement.backend_required,
            "business_data_signal": requirement.business_data_signal,
            "crud_management_signal": requirement.crud_business_signal,
            "explicit_backend": requirement.explicit_backend,
            "explicit_pure_frontend": requirement.explicit_pure_frontend,
            "ambiguous": requirement.ambiguous,
            "matched_signals": requirement.matched_signals,
            "reason": requirement.reason,
            "capability_spec": requirement.model_dump(mode="json"),
        }
        reconciled = reconcile_architecture_decision(
            decision,
            requirement.raw_requirement,
            routing_override=routing,
        )
        if not reconciled:
            return None
        try:
            return ArchitectureDecision.model_validate(reconciled)
        except ValueError:
            return None

    def fallback(self, requirement: RequirementSpec) -> ArchitectureDecision:
        """Create a conservative decision when the model cannot return JSON.

        This is intentionally a routing-safe minimum, not a replacement for a
        successful Architecture response.  It keeps the implementation branch
        for ambiguous/business requirements and lets the human approval node
        review the boundary before downstream Agents do expensive work.
        """
        backend_required = bool(requirement.backend_required or not requirement.explicit_pure_frontend)
        project_type = "static_html" if requirement.display_only and not backend_required else "web_app"
        complexity = "low" if project_type == "static_html" else "medium"
        required_capabilities = list(requirement.required_capabilities)
        if "frontend" not in required_capabilities:
            required_capabilities.insert(0, "frontend")
        if backend_required and "backend" not in required_capabilities:
            required_capabilities.insert(0, "backend")
        fallback = {
            "project_type": project_type,
            "backend_required": backend_required,
            "complexity": complexity,
            "summary": "Architecture 未返回可解析正文，已依据原始需求能力路由生成保守架构；请在人工确认节点核对范围。",
            "frontend_plan": ["保留前端界面与交互实现"] if "frontend" in required_capabilities else [],
            "backend_reason": (
                "原始需求未明确允许纯前端，按安全默认值保留 Backend 分支。"
                if backend_required
                else "原始需求明确为纯前端/本地存储或纯展示页面。"
            ),
            "risks": [
                "Architecture Agent 未生成可解析 JSON",
                "当前方案由本地能力路由兜底，需人工确认实现边界",
            ],
            "required_capabilities": required_capabilities,
            "optional_capabilities": list(requirement.optional_capabilities),
            "evidence": [*requirement.evidence, "Architecture 输出异常，启用本地保守兜底"],
            "needs_clarification": True,
            "clarification_questions": list(requirement.clarification_questions)
            or ["是否确认按当前原始需求保留 Frontend 与 Backend 实现分支？"],
        }
        decision = self.validate(fallback, requirement)
        if decision is None:
            raise ValueError("Unable to build a valid conservative Architecture fallback")
        return decision
