"""Run-scoped Workspace identity and safe local path handling.

This is deliberately smaller than a Git worktree manager.  It establishes the
Project -> Repository -> Workspace -> Task -> Run contract first, while the
existing ArtifactService continues to own materialization and versioning.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DENIED_NAMES = {
    ".env",
    ".git",
    "logs",
    "workspace",
    "checkpoint",
    "checkpoints",
    "private-cache",
}
_DENIED_SUFFIXES = {".db", ".db-wal", ".db-shm"}


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """The immutable identity of the files a Coding Agent is allowed to see."""

    workspace_id: str
    project_id: str
    repository_id: str
    repo_url: str | None
    base_branch: str | None
    base_commit_sha: str | None
    working_branch: str | None
    worktree_path: str
    sandbox_id: str | None
    task_id: str
    run_id: str
    status: str = "ATTACHED"
    mode: str = "greenfield"
    source_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateWorkspaceRef:
    candidate_id: str
    run_id: str
    base_workspace_path: str
    worktree_path: str
    owners: tuple[str, ...]
    status: str = "CANDIDATE"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["owners"] = list(self.owners)
        return value


class WorkspacePathError(ValueError):
    """Raised when a tool attempts to leave its assigned workspace."""


class LocalWorkspaceService:
    """Create deterministic, run-isolated local workspace references.

    The service does not clone repositories or create Git worktrees yet.  A
    later Existing Repo adapter can fill in the repository fields without
    changing the RunState contract.  The current path is the same per-run
    directory already used by ArtifactService, so existing artifacts remain
    compatible.
    """

    def __init__(self, root: str | Path = "data/workspaces") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_for_run(
        self,
        run_id: str,
        runtime: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> WorkspaceRef:
        self._validate_id(run_id, "run_id")
        values = runtime if isinstance(runtime, dict) else {}
        project_id = self._safe_value(values.get("project_id"), f"project_{run_id}")
        repository_id = self._safe_value(values.get("repository_id"), f"repo_{run_id}")
        resolved = (self.root / run_id).resolve()
        if not self._within_root(resolved):
            raise WorkspacePathError("Workspace path escapes configured root")
        resolved.mkdir(parents=True, exist_ok=True)
        source_path = self._optional_path(values.get("repo_path") or values.get("existing_repo_path"))
        mode = "existing_repo" if source_path else "greenfield"
        if source_path and source_path != resolved:
            self._attach_existing_repo(source_path, resolved)
        return WorkspaceRef(
            workspace_id=f"ws_{run_id}",
            project_id=project_id,
            repository_id=repository_id,
            repo_url=self._optional_string(values.get("repo_url")),
            base_branch=self._optional_string(values.get("base_branch")),
            base_commit_sha=self._optional_string(values.get("base_commit_sha")),
            working_branch=self._optional_string(values.get("working_branch")),
            worktree_path=str(resolved),
            sandbox_id=self._optional_string(values.get("sandbox_id")),
            task_id=task_id or f"task_{run_id}",
            run_id=run_id,
            mode=mode,
            source_path=str(source_path) if source_path else None,
        )

    def create_for_owner(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        owner: str,
    ) -> WorkspaceRef:
        """Create an isolated branch workspace for a parallel Coding Agent."""
        owner_id = str(owner or "").strip().lower()
        self._validate_id(owner_id, "owner")
        base = workspace if isinstance(workspace, WorkspaceRef) else WorkspaceRef(**workspace)
        destination = (self.root / ".agent-workspaces" / base.run_id / owner_id).resolve()
        if not self._within_root(destination):
            raise WorkspacePathError("Agent workspace escapes configured root")
        destination.mkdir(parents=True, exist_ok=True)
        source = Path(base.worktree_path).resolve()
        if source.is_dir() and not any(destination.iterdir()):
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".env", ".env.*", ".git", "node_modules", "target", "dist",
                    "*.db", "*.db-wal", "*.db-shm", "logs", "checkpoints", "private-cache",
                ),
            )
        return replace(
            base,
            workspace_id=f"{base.workspace_id}_{owner_id}",
            worktree_path=str(destination),
            task_id=f"{base.task_id}_{owner_id}",
            working_branch=f"{base.working_branch or 'run'}/{owner_id}",
        )

    def create_candidate(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        candidate_id: str,
        owners: list[str] | tuple[str, ...],
    ) -> CandidateWorkspaceRef:
        """Create a repair workspace that cannot overwrite Stable files."""
        self._validate_id(candidate_id, "candidate_id")
        base_path = Path(str(workspace.worktree_path if isinstance(workspace, WorkspaceRef) else workspace.get("worktree_path", ""))).resolve()
        if not self._within_root(base_path):
            raise WorkspacePathError("Base workspace is outside configured root")
        run_id = str(workspace.run_id if isinstance(workspace, WorkspaceRef) else workspace.get("run_id", ""))
        self._validate_id(run_id, "run_id")
        candidate_path = (self.root / ".candidates" / run_id / candidate_id).resolve()
        if not self._within_root(candidate_path):
            raise WorkspacePathError("Candidate workspace escapes configured root")
        candidate_path.mkdir(parents=True, exist_ok=True)
        return CandidateWorkspaceRef(
            candidate_id=candidate_id,
            run_id=run_id,
            base_workspace_path=str(base_path),
            worktree_path=str(candidate_path),
            owners=tuple(dict.fromkeys(str(owner) for owner in owners if str(owner).strip())),
        )

    def stage_candidate(self, candidate: CandidateWorkspaceRef, files: list[dict[str, Any]]) -> CandidateWorkspaceRef:
        """Replace the Candidate tree with one exact structured Artifact set."""
        root = Path(candidate.worktree_path).resolve()
        boundary = (self.root / ".candidates" / candidate.run_id).resolve()
        if not self._within_root(root, boundary) or root == boundary:
            raise WorkspacePathError("Invalid candidate workspace boundary")
        manifest_path = self._candidate_manifest_path(candidate)
        previous_manifest = self._read_candidate_manifest(manifest_path)
        current_paths: list[str] = []
        normalized_files: list[tuple[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            raw_name = str(item.get("name") or item.get("path") or "").replace("\\", "/")
            normalized = self._candidate_relative_path(raw_name)
            if normalized in current_paths:
                raise WorkspacePathError(f"Candidate Artifact path is duplicated: {normalized}")
            current_paths.append(normalized)
            normalized_files.append((normalized, str(item["content"])))
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in normalized_files:
            destination = (root / Path(*PurePosixPath(relative).parts)).resolve()
            if not self._within_root(destination, root):
                raise WorkspacePathError("Candidate Artifact escapes workspace")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8", newline="")
        manifest = {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "owners": list(candidate.owners),
            "base_paths": previous_manifest.get("base_paths", current_paths),
            "current_paths": current_paths,
        }
        self._atomic_json_write(manifest_path, manifest)
        return candidate

    def promote_candidate(self, candidate: CandidateWorkspaceRef) -> CandidateWorkspaceRef:
        """Atomically promote the complete managed Candidate file set.

        Files removed or renamed inside the Candidate are removed from Stable;
        an interrupted promotion restores every touched path from the rollback
        snapshot so cross-Agent changes cannot be partially committed.
        """
        source = Path(candidate.worktree_path).resolve()
        destination = Path(candidate.base_workspace_path).resolve()
        if not self._within_root(source) or not self._within_root(destination):
            raise WorkspacePathError("Candidate promotion leaves configured root")
        manifest_path = self._candidate_manifest_path(candidate)
        manifest = self._read_candidate_manifest(manifest_path)
        if not manifest:
            raise WorkspacePathError("Candidate manifest is missing")
        base_paths = {
            self._candidate_relative_path(str(item))
            for item in manifest.get("base_paths", [])
        }
        current_paths = {
            self._candidate_relative_path(str(item))
            for item in manifest.get("current_paths", [])
        }
        affected_paths = sorted(base_paths | current_paths)
        rollback_root = (source.parent / f".{candidate.candidate_id}.rollback").resolve()
        boundary = (self.root / ".candidates" / candidate.run_id).resolve()
        if not self._within_root(rollback_root, boundary) or rollback_root == boundary:
            raise WorkspacePathError("Candidate rollback path escapes boundary")
        if rollback_root.exists():
            shutil.rmtree(rollback_root)
        rollback_root.mkdir(parents=True, exist_ok=True)
        existed: set[str] = set()
        try:
            for relative in affected_paths:
                target = (destination / Path(*PurePosixPath(relative).parts)).resolve()
                if not self._within_root(target, destination):
                    raise WorkspacePathError("Candidate promotion path escapes Stable workspace")
                if target.exists() and not target.is_file():
                    raise WorkspacePathError(f"Stable managed path is not a file: {relative}")
                if target.is_file():
                    if relative in current_paths - base_paths:
                        raise WorkspacePathError(f"Candidate would overwrite an unmanaged file: {relative}")
                    backup = rollback_root / Path(*PurePosixPath(relative).parts)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    existed.add(relative)

            for relative in sorted(current_paths):
                candidate_file = (source / Path(*PurePosixPath(relative).parts)).resolve()
                target = (destination / Path(*PurePosixPath(relative).parts)).resolve()
                if not self._within_root(candidate_file, source) or not candidate_file.is_file():
                    raise WorkspacePathError(f"Candidate file is missing: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{candidate.candidate_id}.tmp")
                shutil.copy2(candidate_file, temporary)
                os.replace(temporary, target)

            for relative in sorted(base_paths - current_paths):
                target = (destination / Path(*PurePosixPath(relative).parts)).resolve()
                if target.is_file():
                    target.unlink()
        except Exception:
            for relative in affected_paths:
                target = (destination / Path(*PurePosixPath(relative).parts)).resolve()
                backup = rollback_root / Path(*PurePosixPath(relative).parts)
                if relative in existed and backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{candidate.candidate_id}.restore")
                    shutil.copy2(backup, temporary)
                    os.replace(temporary, target)
                elif target.is_file():
                    target.unlink()
            raise
        finally:
            if rollback_root.exists():
                shutil.rmtree(rollback_root)
        return replace(candidate, status="STABLE")

    def discard_candidate(self, candidate: CandidateWorkspaceRef) -> CandidateWorkspaceRef:
        root = Path(candidate.worktree_path).resolve()
        boundary = (self.root / ".candidates" / candidate.run_id).resolve()
        if not self._within_root(root, boundary) or root == boundary:
            raise WorkspacePathError("Invalid candidate workspace boundary")
        if root.exists():
            shutil.rmtree(root)
        manifest_path = self._candidate_manifest_path(candidate)
        manifest_path.unlink(missing_ok=True)
        return replace(candidate, status="REJECTED")

    def _candidate_manifest_path(self, candidate: CandidateWorkspaceRef) -> Path:
        root = Path(candidate.worktree_path).resolve()
        boundary = (self.root / ".candidates" / candidate.run_id).resolve()
        manifest = (root.parent / f".{candidate.candidate_id}.manifest.json").resolve()
        if not self._within_root(manifest, boundary) or manifest == boundary:
            raise WorkspacePathError("Candidate manifest path escapes boundary")
        return manifest

    @staticmethod
    def _candidate_relative_path(value: str) -> str:
        raw = str(value or "").replace("\\", "/")
        pure = PurePosixPath(raw)
        if not raw or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise WorkspacePathError("Candidate Artifact path is invalid")
        return pure.as_posix()

    @staticmethod
    def _read_candidate_manifest(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspacePathError("Candidate manifest is invalid") from exc
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
            newline="",
        )
        os.replace(temporary, path)

    def resolve_relative(self, workspace: WorkspaceRef | dict[str, Any], relative_path: str = "") -> Path:
        root = Path(str(workspace.worktree_path if isinstance(workspace, WorkspaceRef) else workspace.get("worktree_path", ""))).resolve()
        if not self._within_root(root):
            raise WorkspacePathError("Workspace root is outside configured root")
        candidate = (root / relative_path).resolve()
        if not self._within_root(candidate, root):
            raise WorkspacePathError("Path escapes assigned workspace")
        if self._is_denied(candidate, root):
            raise WorkspacePathError("Path is not available to Agent tools")
        return candidate

    def can_read(self, workspace: WorkspaceRef | dict[str, Any], relative_path: str) -> bool:
        try:
            self.resolve_relative(workspace, relative_path)
        except (OSError, ValueError, WorkspacePathError):
            return False
        return True

    def _within_root(self, path: Path, root: Path | None = None) -> bool:
        boundary = (root or self.root).resolve()
        try:
            path.resolve().relative_to(boundary)
            return True
        except ValueError:
            return False

    @classmethod
    def _is_denied(cls, path: Path, root: Path) -> bool:
        try:
            parts = {part.lower() for part in path.relative_to(root).parts}
        except ValueError:
            return True
        if parts & _DENIED_NAMES:
            return True
        name = path.name.lower()
        return name in _DENIED_NAMES or any(name.endswith(suffix) for suffix in _DENIED_SUFFIXES)

    @staticmethod
    def _validate_id(value: str, name: str) -> None:
        if not _SAFE_ID.fullmatch(str(value or "")):
            raise ValueError(f"Invalid {name}")

    @classmethod
    def _safe_value(cls, value: Any, default: str) -> str:
        candidate = str(value or default).strip()
        if not _SAFE_ID.fullmatch(candidate):
            return default
        return candidate

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        candidate = str(value).strip() if value is not None else ""
        return candidate or None

    @staticmethod
    def _optional_path(value: Any) -> Path | None:
        candidate = str(value or "").strip()
        if not candidate:
            return None
        path = Path(candidate).expanduser().resolve()
        if not path.is_dir():
            raise WorkspacePathError("Existing repository path is not a directory")
        return path

    @staticmethod
    def _attach_existing_repo(source: Path, destination: Path) -> None:
        if source == destination or source in destination.parents or destination in source.parents:
            raise WorkspacePathError("Existing repository cannot overlap the run workspace")
        if any(destination.iterdir()):
            return
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".env", ".env.*", "*.db", "*.db-wal", "*.db-shm", "logs", "workspace", "checkpoints", "private-cache",
            ),
        )
