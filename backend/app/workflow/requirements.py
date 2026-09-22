"""Versioned, typed requirement contract used by the workflow router."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


RequirementConfidence = Literal["low", "medium", "high"]
RequirementState = Literal["CLEAR", "ASSUMPTION_ALLOWED", "USER_CLARIFICATION_REQUIRED"]
RequirementSupport = Literal["SUPPORTED", "SUPPORTED_WITH_DEFAULTS", "CLARIFICATION_REQUIRED", "PARTIALLY_SUPPORTED", "UNSUPPORTED"]


class RequirementSpec(BaseModel):
    """Normalized requirement facts shared by routing and architecture.

    This model deliberately describes capabilities rather than an industry.
    It is safe to persist in a Run context: it contains no provider secret or
    generated prompt content beyond the user's own requirement.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    router_version: str = "1.0"
    source: str = "rules+heuristics"
    raw_requirement: str = Field(default="", max_length=100_000)
    normalized_requirement: str = Field(default="", max_length=100_000)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    optional_capabilities: list[str] = Field(default_factory=list, max_length=32)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    matched_signals: list[str] = Field(default_factory=list, max_length=16)
    confidence: RequirementConfidence = "low"
    backend_required: bool = False
    business_data_signal: bool = False
    crud_business_signal: bool = False
    crud_requested: bool = False
    explicit_backend: bool = False
    explicit_pure_frontend: bool = False
    display_only: bool = False
    ambiguous: bool = False
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=500)
    state: RequirementState = "CLEAR"
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    impactful_gaps: list[str] = Field(default_factory=list, max_length=32)
    gaps: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    safe_defaults: dict[str, Any] = Field(default_factory=dict)
    primary_entities: list[str] = Field(default_factory=list, max_length=32)
    requested_stacks: dict[str, str] = Field(default_factory=dict)
    capability_profile: dict[str, bool] = Field(default_factory=dict)
    support_status: RequirementSupport = "SUPPORTED"

    @field_validator(
        "required_capabilities",
        "optional_capabilities",
        "evidence",
        "matched_signals",
        "clarification_questions",
        "assumptions",
        "impactful_gaps",
        "primary_entities",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]
