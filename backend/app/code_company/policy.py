"""Code Company policy boundary: permission is separate from Agent skills."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    name: str
    allowed_paths: tuple[str, ...] = ("**", "*")
    denied_paths: tuple[str, ...] = (".env*", "**/.env*", ".git/**", "**/.git/**", "**/*.db", "**/logs/**", "**/workspace/**", "**/checkpoint*/**")
    allowed_tools: frozenset[str] = frozenset()
    max_attempts: int = 3
    timeout_seconds: float = 120.0
    network_enabled: bool = False
    require_human_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def permits_path(self, path: str) -> bool:
        normalized = str(path or "").replace("\\", "/").lstrip("/")
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in self.denied_paths):
            return False
        return any(fnmatch.fnmatch(normalized, pattern) for pattern in self.allowed_paths)

    def permits_tool(self, tool: str) -> bool:
        return not self.allowed_tools or str(tool or "") in self.allowed_tools


class PolicyEngine:
    def __init__(self, policies: dict[str, AgentPolicy] | None = None) -> None:
        self.policies = dict(policies or {})

    def for_agent(self, agent_id: str) -> AgentPolicy:
        return self.policies.get(str(agent_id), AgentPolicy(name=str(agent_id or "default")))

    def check(self, agent_id: str, *, tool: str | None = None, path: str | None = None) -> tuple[bool, str]:
        policy = self.for_agent(agent_id)
        if tool and not policy.permits_tool(tool):
            return False, f"Policy denied tool: {tool}"
        if path is not None and not policy.permits_path(path):
            return False, f"Policy denied path: {path}"
        return True, "allowed"
