"""Run-scoped Workspace identity and safe local path handling.

This is deliberately smaller than a Git worktree manager.  It establishes the
Project -> Repository -> Workspace -> Task -> Run contract first, while the
existing ArtifactService continues to own materialization and versioning.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
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
    backend_kind: str = "snapshot_v1"
    git_root: str | None = None
    git_common_dir: str | None = None
    workspace_warning: str | None = None

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
    backend_kind: str = "snapshot_v1"
    git_root: str | None = None
    git_common_dir: str | None = None
    working_branch: str | None = None
    base_commit_sha: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["owners"] = list(self.owners)
        return value


class WorkspacePathError(ValueError):
    """Raised when a tool attempts to leave its assigned workspace."""


class LocalWorkspaceService:
    """Create run workspaces with safe snapshots and optional real Git worktrees."""

    def __init__(self, root: str | Path = "data/workspaces") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        root_key = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:16]
        app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
        app_data_root = Path(app_data).expanduser() if app_data else Path.home() / ".local" / "share"
        self.git_worktree_root = (app_data_root / "Agent-Team" / "worktrees" / root_key).resolve()

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
        snapshot_path = (self.root / run_id).resolve()
        if not self._within_root(snapshot_path):
            raise WorkspacePathError("Workspace path escapes configured root")
        source_path = self._optional_path(values.get("repo_path") or values.get("existing_repo_path"))
        mode = "existing_repo" if source_path else "greenfield"
        backend_kind = "snapshot_v1"
        git_root: Path | None = None
        git_common_dir: Path | None = None
        base_sha = self._optional_string(values.get("base_commit_sha"))
        base_branch = self._optional_string(values.get("base_branch"))
        working_branch = self._optional_string(values.get("working_branch"))
        warning: str | None = None
        resolved = snapshot_path
        workspace_backend = str(values.get("workspace_backend") or "auto").strip().lower()
        if workspace_backend not in {"auto", "snapshot", "git_worktree"}:
            workspace_backend = "auto"

        if source_path and source_path != snapshot_path:
            git_info, git_reason = self._clean_git_repository(source_path)
            if workspace_backend in {"auto", "git_worktree"} and git_info is not None:
                git_root, git_common_dir, detected_sha, detected_branch = git_info
                # A tracked environment file must never be copied into an Agent worktree.
                if self._has_tracked_secrets(git_root):
                    git_info = None
                    git_reason = "repository tracks a secret-like .env file"
                    git_root = None
                    git_common_dir = None
                else:
                    base_sha = detected_sha
                    base_branch = base_branch or detected_branch
                    branch = self._run_branch_name(run_id)
                    resolved = (self.git_worktree_root / run_id / "stable").resolve()
                    self._ensure_git_worktree(git_root, git_common_dir, resolved, branch, base_sha)
                    working_branch = branch
                    backend_kind = "git_worktree_v1"
            if backend_kind != "git_worktree_v1":
                warning = git_reason or "Git worktree is unavailable; using a protected repository snapshot"
                resolved.mkdir(parents=True, exist_ok=True)
                self._attach_existing_repo(source_path, resolved)
        else:
            resolved.mkdir(parents=True, exist_ok=True)
        return WorkspaceRef(
            workspace_id=f"ws_{run_id}",
            project_id=project_id,
            repository_id=repository_id,
            repo_url=self._optional_string(values.get("repo_url")),
            base_branch=base_branch,
            base_commit_sha=base_sha,
            working_branch=working_branch,
            worktree_path=str(resolved),
            sandbox_id=self._optional_string(values.get("sandbox_id")),
            task_id=task_id or f"task_{run_id}",
            run_id=run_id,
            mode=mode,
            source_path=str(source_path) if source_path else None,
            backend_kind=backend_kind,
            git_root=str(git_root) if git_root else None,
            git_common_dir=str(git_common_dir) if git_common_dir else None,
            workspace_warning=warning,
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
        if base.backend_kind == "git_worktree_v1":
            if not base.git_root or not base.git_common_dir or not base.base_commit_sha:
                raise WorkspacePathError("Git worktree reference is missing pinned repository metadata")
            destination = (self.git_worktree_root / base.run_id / "owners" / owner_id).resolve()
            branch = self._owner_branch_name(base.run_id, owner_id)
            self._ensure_git_worktree(
                Path(base.git_root), Path(base.git_common_dir), destination,
                branch, base.base_commit_sha,
            )
            return replace(
                base,
                workspace_id=f"{base.workspace_id}_{owner_id}",
                worktree_path=str(destination),
                task_id=f"{base.task_id}_{owner_id}",
                working_branch=branch,
            )

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
        base = workspace if isinstance(workspace, WorkspaceRef) else WorkspaceRef(**workspace)
        base_path = Path(base.worktree_path).resolve()
        git_mode = base.backend_kind == "git_worktree_v1"
        if not (self._is_valid_git_worktree(base_path, base.git_common_dir) if git_mode else self._within_root(base_path)):
            raise WorkspacePathError("Base workspace is outside configured root")
        run_id = str(base.run_id)
        self._validate_id(run_id, "run_id")
        if git_mode:
            if not base.git_root or not base.git_common_dir or not base.base_commit_sha:
                raise WorkspacePathError("Git worktree reference is missing pinned repository metadata")
            candidate_path = (self.git_worktree_root / run_id / "candidates" / candidate_id).resolve()
            branch = self._candidate_branch_name(run_id, candidate_id)
            self._ensure_git_worktree(
                Path(base.git_root), Path(base.git_common_dir), candidate_path,
                branch, base.base_commit_sha,
            )
        else:
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
            backend_kind=base.backend_kind,
            git_root=base.git_root,
            git_common_dir=base.git_common_dir,
            working_branch=branch if git_mode else None,
            base_commit_sha=base.base_commit_sha,
        )

    def stage_candidate(self, candidate: CandidateWorkspaceRef, files: list[dict[str, Any]]) -> CandidateWorkspaceRef:
        """Replace the Candidate tree with one exact structured Artifact set."""
        root = Path(candidate.worktree_path).resolve()
        boundary = (
            self.git_worktree_root / candidate.run_id / "candidates"
            if candidate.backend_kind == "git_worktree_v1"
            else self.root / ".candidates" / candidate.run_id
        ).resolve()
        root_valid = (
            self._is_valid_git_worktree(root, candidate.git_common_dir)
            if candidate.backend_kind == "git_worktree_v1"
            else self._within_root(root, boundary)
        )
        if not root_valid or root == boundary:
            raise WorkspacePathError("Invalid candidate workspace boundary")
        manifest_path = self._candidate_manifest_path(candidate)
        previous_manifest = self._read_candidate_manifest(manifest_path)
        current_paths: list[str] = []
        current_keys: set[str] = set()
        normalized_files: list[tuple[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            raw_name = str(item.get("name") or item.get("path") or "").replace("\\", "/")
            normalized = self._candidate_relative_path(raw_name)
            key = normalized.casefold()
            if key in current_keys:
                raise WorkspacePathError(f"Candidate Artifact path is duplicated: {normalized}")
            current_keys.add(key)
            current_paths.append(normalized)
            normalized_files.append((normalized, str(item["content"])))

        if candidate.backend_kind == "git_worktree_v1":
            current_set = set(current_paths)
            for old_value in previous_manifest.get("current_paths", []):
                old_relative = self._candidate_relative_path(str(old_value))
                if old_relative in current_set:
                    continue
                old_path = (root / Path(*PurePosixPath(old_relative).parts)).resolve()
                if not self._within_root(old_path, root):
                    raise WorkspacePathError("Previous candidate path escapes workspace")
                if old_path.is_file():
                    old_path.unlink()
        elif root.exists():
            shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
        else:
            root.mkdir(parents=True, exist_ok=True)
        for relative, content in normalized_files:
            destination = (root / Path(*PurePosixPath(relative).parts)).resolve()
            if not self._within_root(destination, root):
                raise WorkspacePathError("Candidate Artifact escapes workspace")
            if destination.exists() and not destination.is_file():
                raise WorkspacePathError(f"Candidate Artifact path is not a file: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{candidate.candidate_id}.tmp")
            temporary.write_text(content, encoding="utf-8", newline="")
            os.replace(temporary, destination)
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
        if candidate.backend_kind == "git_worktree_v1":
            source_valid = self._is_valid_git_worktree(source, candidate.git_common_dir)
            destination_valid = self._is_valid_git_worktree(destination, candidate.git_common_dir)
        else:
            source_valid = self._within_root(source)
            destination_valid = self._within_root(destination)
        if not source_valid or not destination_valid:
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
        boundary = (
            self.git_worktree_root / candidate.run_id / "candidates"
            if candidate.backend_kind == "git_worktree_v1"
            else self.root / ".candidates" / candidate.run_id
        ).resolve()
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
        boundary = (
            self.git_worktree_root / candidate.run_id / "candidates"
            if candidate.backend_kind == "git_worktree_v1"
            else self.root / ".candidates" / candidate.run_id
        ).resolve()
        valid = (
            self._is_valid_git_worktree(root, candidate.git_common_dir)
            if candidate.backend_kind == "git_worktree_v1"
            else self._within_root(root, boundary)
        )
        if not valid or root == boundary:
            raise WorkspacePathError("Invalid candidate workspace boundary")
        if candidate.backend_kind == "git_worktree_v1":
            self._git(Path(candidate.git_root or ""), "worktree", "remove", "--force", str(root))
            if candidate.working_branch and candidate.working_branch.startswith("agent-team/candidates/"):
                self._git(Path(candidate.git_root or ""), "branch", "-D", candidate.working_branch, check=False)
        elif root.exists():
            shutil.rmtree(root)
        manifest_path = self._candidate_manifest_path(candidate)
        manifest_path.unlink(missing_ok=True)
        return replace(candidate, status="REJECTED")

    def _candidate_manifest_path(self, candidate: CandidateWorkspaceRef) -> Path:
        root = Path(candidate.worktree_path).resolve()
        boundary = (
            self.git_worktree_root / candidate.run_id / "candidates"
            if candidate.backend_kind == "git_worktree_v1"
            else self.root / ".candidates" / candidate.run_id
        ).resolve()
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
        if any(part.casefold() in _DENIED_NAMES for part in pure.parts):
            raise WorkspacePathError("Candidate Artifact path targets a protected directory or secret")
        if any(pure.name.casefold().endswith(suffix) for suffix in _DENIED_SUFFIXES):
            raise WorkspacePathError("Candidate Artifact path targets a protected database file")
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
        backend_kind = workspace.backend_kind if isinstance(workspace, WorkspaceRef) else str(workspace.get("backend_kind") or "snapshot_v1")
        git_common_dir = workspace.git_common_dir if isinstance(workspace, WorkspaceRef) else workspace.get("git_common_dir")
        root_valid = (
            self._is_valid_git_worktree(root, git_common_dir)
            if backend_kind == "git_worktree_v1"
            else self._within_root(root)
        )
        if not root_valid:
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

    def _is_valid_git_worktree(self, path: Path, expected_common_dir: str | Path | None) -> bool:
        root = path.resolve()
        if not self._within_root(root, self.git_worktree_root) or not root.is_dir():
            return False
        if not (root / ".git").exists():
            return False
        try:
            top = Path(self._git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
            common = self._resolve_git_common_dir(root, self._git(root, "rev-parse", "--git-common-dir").stdout.strip())
        except (OSError, subprocess.SubprocessError, WorkspacePathError, ValueError):
            return False
        if top != root:
            return False
        if expected_common_dir:
            return common == Path(expected_common_dir).resolve()
        return True

    @staticmethod
    def _resolve_git_common_dir(cwd: Path, value: str) -> Path:
        path = Path(value.strip())
        return (cwd / path).resolve() if not path.is_absolute() else path.resolve()

    @staticmethod
    def _git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspacePathError(f"Git workspace operation failed: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:600]
            raise WorkspacePathError(f"Git workspace operation failed ({result.returncode}): {detail}")
        return result

    def _clean_git_repository(
        self, source: Path,
    ) -> tuple[tuple[Path, Path, str, str | None] | None, str | None]:
        try:
            root = Path(self._git(source, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
            common_raw = self._git(root, "rev-parse", "--git-common-dir").stdout.strip()
            common = self._resolve_git_common_dir(root, common_raw)
            commit = self._git(root, "rev-parse", "HEAD").stdout.strip()
            branch_result = self._git(root, "branch", "--show-current", check=False)
            branch = branch_result.stdout.strip() or None
            status = self._git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
        except (OSError, WorkspacePathError, ValueError) as exc:
            return None, f"Git worktree unavailable ({type(exc).__name__}); using protected repository snapshot"
        if source != root and root not in source.parents:
            return None, "Provided path is not inside its Git repository; using protected repository snapshot"
        if status.strip():
            return None, "Source Git working tree has uncommitted or untracked changes; preserved via snapshot mode"
        return (root, common, commit, branch), None

    def _has_tracked_secrets(self, git_root: Path) -> bool:
        tracked = self._git(git_root, "ls-files", "-z").stdout.split("\0")
        safe_templates = {".env.example", ".env.runtime.example"}
        for raw in tracked:
            parts = [part.casefold() for part in raw.replace("\\", "/").split("/")]
            if any(part == ".env" or (part.startswith(".env.") and part not in safe_templates) for part in parts):
                return True
        return False

    def _ensure_git_worktree(
        self,
        git_root: Path,
        git_common_dir: Path,
        destination: Path,
        branch: str,
        base_commit_sha: str,
    ) -> None:
        destination = destination.resolve()
        if not self._within_root(destination, self.git_worktree_root):
            raise WorkspacePathError("Git worktree destination escapes its private workspace root")
        if destination.exists():
            if not self._is_valid_git_worktree(destination, git_common_dir):
                raise WorkspacePathError("Git worktree destination exists but is not a registered Agent worktree")
            current_branch = self._git(destination, "branch", "--show-current", check=False).stdout.strip()
            if current_branch and current_branch != branch:
                raise WorkspacePathError("Existing Agent worktree is attached to a different branch")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        ref = f"refs/heads/{branch}"
        exists = self._git(git_root, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0
        if exists:
            self._git(git_root, "worktree", "add", str(destination), branch)
        else:
            self._git(git_root, "worktree", "add", "-b", branch, str(destination), base_commit_sha)
        if not self._is_valid_git_worktree(destination, git_common_dir):
            raise WorkspacePathError("Git did not register the requested Agent worktree")

    @staticmethod
    def _branch_run_key(run_id: str) -> str:
        return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]

    @classmethod
    def _run_branch_name(cls, run_id: str) -> str:
        return f"agent-team/runs/{cls._branch_run_key(run_id)}/stable"

    @classmethod
    def _owner_branch_name(cls, run_id: str, owner: str) -> str:
        owner_key = hashlib.sha256(owner.casefold().encode("utf-8")).hexdigest()[:10]
        return f"agent-team/runs/{cls._branch_run_key(run_id)}/owners/{owner_key}"

    @classmethod
    def _candidate_branch_name(cls, run_id: str, candidate_id: str) -> str:
        candidate_key = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:12]
        return f"agent-team/candidates/{cls._branch_run_key(run_id)}/{candidate_key}"

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
                ".env", ".env.*", ".git", "node_modules", "target", "dist", ".venv", "venv", "__pycache__",
                "*.db", "*.db-wal", "*.db-shm", "logs", "workspace", "checkpoints", "private-cache",
            ),
        )
