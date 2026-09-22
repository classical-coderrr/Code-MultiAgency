"""Collect bounded artifacts from an opt-in Coding Agent workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..code_company.artifact_contracts import path_allowed
from ..code_company.runtime import CodeCompanyRuntime
from .run_state import RunState


class WorkspaceArtifactCollector:
    def __init__(self, runtime: CodeCompanyRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def workspace_for_step(state: RunState, step_id: str) -> dict[str, Any]:
        branches = state.context.snapshot().get("agent_workspaces")
        if isinstance(branches, dict) and isinstance(branches.get(step_id), dict):
            return dict(branches[step_id])
        return dict(state.workspace or {})

    def collect(self, state: RunState, step_id: str) -> list[dict[str, str]]:
        workspace = self.workspace_for_step(state, step_id)
        root_value = workspace.get("worktree_path") if isinstance(workspace, dict) else None
        if not root_value:
            return []
        root = Path(str(root_value)).resolve()
        if not root.is_dir():
            return []
        suffixes = {
            ".java", ".kt", ".xml", ".html", ".htm", ".css", ".js",
            ".ts", ".tsx", ".vue", ".json", ".yaml", ".yml", ".sql",
            ".properties", ".md", ".txt", ".py",
        }
        languages = {
            ".java": "java", ".kt": "kotlin", ".html": "html",
            ".htm": "html", ".css": "css", ".js": "javascript",
            ".ts": "typescript", ".tsx": "tsx", ".vue": "vue",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".sql": "sql", ".properties": "text", ".md": "markdown",
            ".txt": "text", ".py": "python", ".xml": "xml",
        }
        result: list[dict[str, str]] = []
        ownership = (state.blueprint or {}).get("artifact_ownership") or {}
        policy = self.runtime.tool_gateway.policy
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            relative = path.relative_to(root).as_posix()
            if step_id in ownership and not path_allowed(step_id, relative, ownership):
                continue
            if not policy.permits_path(relative):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not content.strip() or len(content.encode("utf-8")) > policy.max_file_bytes:
                continue
            result.append(
                {
                    "step_id": step_id,
                    "name": relative,
                    "language": languages.get(path.suffix.lower(), "text"),
                    "purpose": "Coding Agent Loop output",
                    "content": content,
                }
            )
        return result

