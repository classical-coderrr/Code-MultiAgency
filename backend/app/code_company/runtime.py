"""Code Company runtime boundary.

The workflow executor can keep orchestrating legacy YAML while Code Company
specific workspace and tool behavior moves behind this small facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from dataclasses import replace

from .context_manager import CodeContextManager
from .planner import DynamicPlanner
from .policy import PolicyEngine
from .repository_intelligence import RepositoryIndexer
from ..platform.tool_gateway import LocalToolGateway, ToolPolicy
from ..platform.workspace import CandidateWorkspaceRef, LocalWorkspaceService, WorkspaceRef


class CodeCompanyRuntime:
    def __init__(self, workspace_service: LocalWorkspaceService, tool_gateway: LocalToolGateway | None = None) -> None:
        self.workspace_service = workspace_service
        self.tool_gateway = tool_gateway or LocalToolGateway(workspace_service, ToolPolicy.coding_default())
        self.repository_indexer = RepositoryIndexer(workspace_service)
        self.context_manager = CodeContextManager()
        self.planner = DynamicPlanner()
        self.policy_engine = PolicyEngine()

    @classmethod
    def from_root(cls, root: str | Path) -> "CodeCompanyRuntime":
        workspace_service = LocalWorkspaceService(root)
        return cls(workspace_service)

    def prepare_run(self, run_id: str, runtime: dict[str, Any] | None = None) -> WorkspaceRef:
        return self.workspace_service.create_for_run(run_id, runtime)

    def prepare_agent_workspaces(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        owners: list[str] | tuple[str, ...] = ("backend", "frontend"),
    ) -> dict[str, dict[str, Any]]:
        return {
            owner: self.workspace_service.create_for_owner(workspace, owner).as_dict()
            for owner in owners
        }

    def prepare_candidate(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        candidate_id: str,
        owners: list[str] | tuple[str, ...],
        files: list[dict[str, Any]],
    ) -> CandidateWorkspaceRef:
        candidate = self.workspace_service.create_candidate(workspace, candidate_id, owners)
        return self.workspace_service.stage_candidate(candidate, files)

    def update_candidate(self, candidate: CandidateWorkspaceRef, files: list[dict[str, Any]]) -> None:
        self.workspace_service.stage_candidate(candidate, files)

    def promote_candidate(self, candidate: CandidateWorkspaceRef) -> CandidateWorkspaceRef:
        return self.workspace_service.promote_candidate(candidate)

    def discard_candidate(self, candidate: CandidateWorkspaceRef) -> CandidateWorkspaceRef:
        return self.workspace_service.discard_candidate(candidate)

    def index_workspace(self, workspace: WorkspaceRef | dict[str, Any]) -> dict[str, Any]:
        return self.repository_indexer.index(workspace).as_dict()

    def plan(self, requirement: str, *, blueprint: dict[str, Any] | None = None, available_steps: list[str] | None = None) -> dict[str, Any]:
        return self.planner.plan(requirement, blueprint=blueprint, available_steps=available_steps or []).as_dict()

    def capabilities(self) -> dict[str, Any]:
        return {
            "workspace": {"mode": "run_isolated", "existing_repo": True, "candidate": True, "parallel_agent_branches": True},
            "tools": sorted(self.tool_gateway.policy.allowed_tools),
            "write_tools_enabled": True,
            "shell_enabled": True,
            "repository_intelligence": ["directory_tree", "symbol_index", "import_graph", "dependency_summary", "relevant_files"],
            "context_layers": ["task", "project", "repository", "runtime", "evidence", "memory"],
            "dynamic_planner": True,
            "policy_engine": True,
            "delivery": ["artifact_allowlist", "secret_scan", "zip"],
        }

    def gateway_for_owner(self, owner: str, blueprint: dict[str, Any] | None) -> LocalToolGateway:
        """Create an Agent-scoped gateway from the frozen ownership contract."""
        ownership = (blueprint or {}).get("artifact_ownership") or {}
        patterns = ownership.get(owner) or []
        if isinstance(patterns, str):
            patterns = [patterns]
        base = ToolPolicy.coding_default()
        policy = replace(base, allowed_path_globs=tuple(str(item) for item in patterns) or base.allowed_path_globs)
        return LocalToolGateway(self.workspace_service, policy)
