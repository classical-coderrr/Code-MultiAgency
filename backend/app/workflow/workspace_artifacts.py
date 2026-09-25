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
        ownership = (state.blueprint or {}).get("artifact_ownership") or {}
        compiled = state.context.snapshot().get("compiled_contract")
        file_plan = compiled.get("file_plan") if isinstance(compiled, dict) else None
        return self.collect_workspace(workspace, step_id, ownership=ownership, file_plan=file_plan)

    def collect_workspace(
        self,
        workspace: dict[str, Any],
        step_id: str,
        *,
        ownership: dict[str, Any] | None = None,
        file_plan: list[Any] | None = None,
    ) -> list[dict[str, str]]:
        """Collect one Agent's owned files from a normal or candidate workspace."""
        root_value = workspace.get("worktree_path") if isinstance(workspace, dict) else None
        if not root_value:
            return []
        root = Path(str(root_value)).resolve()
        if not root.is_dir():
            return []
        suffixes = {
            ".java", ".kt", ".xml", ".html", ".htm", ".css", ".js",
            ".ts", ".tsx", ".vue", ".json", ".yaml", ".yml", ".sql",
            ".properties", ".md", ".txt", ".py", ".toml", ".gradle",
            ".conf", ".ini", ".sh", ".ps1", ".bat", ".csv", ".lock",
            ".go", ".rs", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
            ".hpp", ".swift", ".dart", ".svelte", ".astro", ".graphql",
            ".proto", ".kts", ".mjs", ".cjs", ".jsx",
            ".scss", ".sass", ".less", ".svg",
        }
        known_text_names = {
            "dockerfile", "makefile", "procfile", ".gitignore", ".gitattributes",
            ".editorconfig", "justfile", "gemfile", "rakefile",
        }
        languages = {
            ".java": "java", ".kt": "kotlin", ".html": "html",
            ".htm": "html", ".css": "css", ".js": "javascript",
            ".ts": "typescript", ".tsx": "tsx", ".vue": "vue",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".sql": "sql", ".properties": "text", ".md": "markdown",
            ".txt": "text", ".py": "python", ".xml": "xml",
            ".toml": "toml", ".gradle": "gradle", ".conf": "text",
            ".ini": "ini", ".sh": "bash", ".ps1": "powershell",
            ".bat": "batch", ".csv": "csv", ".lock": "text",
            ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
            ".cs": "csharp", ".cpp": "cpp", ".c": "c", ".h": "c",
            ".hpp": "cpp", ".swift": "swift", ".dart": "dart",
            ".svelte": "svelte", ".astro": "astro", ".graphql": "graphql",
            ".proto": "protobuf", ".kts": "kotlin", ".mjs": "javascript",
            ".cjs": "javascript", ".jsx": "javascript",
        }
        special_languages = {
            "dockerfile": "dockerfile", "makefile": "makefile", "procfile": "text",
            ".gitignore": "text", ".gitattributes": "text", ".editorconfig": "ini",
            "justfile": "makefile", "gemfile": "ruby", "rakefile": "ruby",
        }
        result: list[dict[str, str]] = []
        ownership = ownership if isinstance(ownership, dict) else {}
        planned_owners: dict[str, str] = {}
        for row in file_plan or []:
            if not isinstance(row, dict):
                continue
            path_value = str(row.get("path") or "").replace("\\", "/").lstrip("/")
            plan_owner = str(row.get("owner") or "").strip().casefold()
            if path_value and plan_owner:
                key = path_value.casefold()
                previous = planned_owners.get(key)
                if previous and previous != plan_owner:
                    raise ValueError(f"Frozen file plan assigns {path_value} to multiple owners")
                planned_owners[key] = plan_owner
        policy = self.runtime.tool_gateway.policy
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            basename = path.name.lower()
            if suffix not in suffixes and basename not in known_text_names:
                continue
            explicit_owner = planned_owners.get(relative.casefold())
            if explicit_owner and step_id.casefold() != explicit_owner:
                continue
            if not explicit_owner and ownership:
                claiming_owners = {
                    str(owner).strip().casefold()
                    for owner in ownership
                    if path_allowed(str(owner), relative, ownership)
                }
                if len(claiming_owners) > 1:
                    continue
            if not explicit_owner and step_id.casefold() in {str(owner).casefold() for owner in ownership} and not path_allowed(step_id, relative, ownership):
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
                    "language": languages.get(suffix, special_languages.get(basename, "text")),
                    "purpose": "Coding Agent Loop output",
                    "content": content,
                }
            )
        return result

    @staticmethod
    def merge_owner_artifacts(
        original_files: list[dict[str, Any]],
        collected_files: list[dict[str, Any]],
        owner: str,
        *,
        file_plan: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Replace one owner's files without importing overlapping foreign files."""
        owner = str(owner or "").strip().casefold()
        planned_owners: dict[str, str] = {}
        for row in file_plan or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("path") or "").replace("\\", "/").lstrip("/")
            plan_owner = str(row.get("owner") or "").strip().casefold()
            if name and plan_owner:
                key = name.casefold()
                previous = planned_owners.get(key)
                if previous and previous != plan_owner:
                    raise ValueError(f"Frozen file plan assigns {name} to multiple owners")
                planned_owners[key] = plan_owner

        by_path: dict[str, dict[str, Any]] = {}
        original_by_path: dict[str, dict[str, Any]] = {}
        for item in original_files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("path") or "").replace("\\", "/").lstrip("/")
            if not name:
                continue
            key = name.casefold()
            item_owner = str(item.get("step_id") or item.get("owner_step") or "").strip().casefold()
            expected_owner = planned_owners.get(key)
            if expected_owner and item_owner != expected_owner:
                continue
            current = original_by_path.get(key)
            if current is not None:
                current_owner = str(current.get("step_id") or current.get("owner_step") or "").strip().casefold()
                if current_owner != item_owner and not (
                    expected_owner and expected_owner in {current_owner, item_owner}
                ):
                    raise ValueError(f"Artifact path has conflicting owners: {name}")
                if expected_owner and item_owner != expected_owner:
                    continue
                old_revision = int(current.get("owner_revision") or 0)
                new_revision = int(item.get("owner_revision") or 0)
                if new_revision >= old_revision:
                    original_by_path[key] = {**item, "name": name}
                continue
            original_by_path[key] = {**item, "name": name}

        for key, item in original_by_path.items():
            item_owner = str(item.get("step_id") or item.get("owner_step") or "").strip().lower()
            if item_owner != owner:
                by_path[key] = item

        collected_by_name: dict[str, dict[str, Any]] = {}
        for item in collected_files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").replace("\\", "/").lstrip("/")
            if not name:
                continue
            key = name.casefold()
            assigned = planned_owners.get(key)
            if assigned and owner != assigned:
                continue
            current = collected_by_name.get(key)
            if current is not None and str(current.get("name")) != name:
                raise ValueError(f"Artifact paths collide on a case-insensitive filesystem: {current.get('name')} and {name}")
            collected_by_name[key] = {**item, "name": name, "step_id": owner}

        for key, item in collected_by_name.items():
            assigned = planned_owners.get(key)
            if assigned and owner != assigned:
                continue
            current = by_path.get(key)
            if current is not None:
                current_owner = str(current.get("step_id") or current.get("owner_step") or "").strip().lower()
                if current_owner != owner and owner != assigned:
                    raise ValueError(f"Artifact path is already owned by {current_owner}: {item['name']}")
            by_path[key] = item
        return list(by_path.values())
