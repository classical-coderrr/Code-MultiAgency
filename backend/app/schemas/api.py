from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuntimeOverride(BaseModel):
    max_tokens: int | None = Field(default=None, ge=1, le=128000)
    retry: int | None = Field(default=None, ge=0, le=10)
    thinking: Literal["auto", "off", "low", "high", "max"] | None = None
    skill_mode: Literal["auto", "on", "off"] | None = None


class RunRuntimeRequest(BaseModel):
    budget_mode: Literal["auto", "manual"] = "auto"
    defaults: RuntimeOverride = Field(default_factory=RuntimeOverride)
    steps: dict[str, RuntimeOverride] = Field(default_factory=dict)
    repo_path: str | None = Field(default=None, max_length=1000)
    repo_url: str | None = Field(default=None, max_length=1000)
    base_branch: str | None = Field(default=None, max_length=200)
    base_commit_sha: str | None = Field(default=None, max_length=200)
    working_branch: str | None = Field(default=None, max_length=200)
    coding_loop: bool = False
    max_tool_steps: int = Field(default=8, ge=1, le=32)


class RunCreateRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=5000)
    runtime: RunRuntimeRequest = Field(default_factory=RunRuntimeRequest)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class ClarificationRequestBody(BaseModel):
    answers: dict[str, object] = Field(default_factory=dict)


class ProviderConfigRequest(BaseModel):
    provider: str = Field(default="qwen", min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model_name: str = Field(min_length=1, max_length=120)
