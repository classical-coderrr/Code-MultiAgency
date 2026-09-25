from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.platform.workspace import LocalWorkspaceService


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for worktree integration tests")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _git_repo(path: Path, *, tracked_env: bool = False) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Agent Team Tests")
    _git(path, "config", "user.email", "agent-team-tests@example.invalid")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    if tracked_env:
        (path / ".env").write_text("MODEL_API_KEY=must-not-enter-worktree\n", encoding="utf-8")
    _git(path, "add", "--all")
    _git(path, "commit", "-m", "initial")
    return path


def test_clean_existing_repository_gets_private_stable_owner_and_candidate_worktrees(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    original_sha = _git(source, "rev-parse", "HEAD")
    service = LocalWorkspaceService(tmp_path / "service" / "workspaces")
    service.git_worktree_root = (tmp_path / "private-git-worktrees").resolve()

    stable = service.create_for_run("run_worktree", {"repo_path": str(source)})
    backend = service.create_for_owner(stable, "backend")
    frontend = service.create_for_owner(stable, "frontend")

    assert stable.backend_kind == "git_worktree_v1"
    assert stable.base_commit_sha == original_sha
    assert backend.working_branch != frontend.working_branch
    assert Path(backend.worktree_path).resolve() != Path(frontend.worktree_path).resolve()
    (Path(backend.worktree_path) / "backend.txt").write_text("private\n", encoding="utf-8")
    assert not (Path(frontend.worktree_path) / "backend.txt").exists()

    candidate = service.create_candidate(stable, "repair_1", ["backend"])
    service.stage_candidate(candidate, [{"name": "src/App.java", "content": "old", "step_id": "backend"}])
    service.stage_candidate(candidate, [{"name": "src/App.java", "content": "fixed", "step_id": "backend"}])
    promoted = service.promote_candidate(candidate)

    assert promoted.status == "STABLE"
    assert (Path(stable.worktree_path) / "src" / "App.java").read_text(encoding="utf-8") == "fixed"
    assert _git(source, "rev-parse", "HEAD") == original_sha
    assert _git(source, "status", "--porcelain") == ""
    assert (source / "README.md").read_text(encoding="utf-8") == "base\n"


def test_dirty_or_secret_bearing_repository_falls_back_to_secret_filtered_snapshot(tmp_path: Path) -> None:
    dirty_source = _git_repo(tmp_path / "dirty-source")
    (dirty_source / "README.md").write_text("user changes\n", encoding="utf-8")
    (dirty_source / ".env").write_text("MODEL_API_KEY=private\n", encoding="utf-8")
    service = LocalWorkspaceService(tmp_path / "dirty-workspaces")

    dirty = service.create_for_run("run_dirty", {"repo_path": str(dirty_source)})

    assert dirty.backend_kind == "snapshot_v1"
    assert dirty.workspace_warning and "uncommitted" in dirty.workspace_warning
    assert (Path(dirty.worktree_path) / "README.md").read_text(encoding="utf-8") == "user changes\n"
    assert not (Path(dirty.worktree_path) / ".env").exists()
    assert not (Path(dirty.worktree_path) / ".git").exists()
    assert _git(dirty_source, "status", "--porcelain")

    secret_source = _git_repo(tmp_path / "secret-source", tracked_env=True)
    secret = service.create_for_run("run_secret", {"repo_path": str(secret_source)})
    assert secret.backend_kind == "snapshot_v1"
    assert secret.workspace_warning and "secret-like" in secret.workspace_warning
    assert not (Path(secret.worktree_path) / ".env").exists()
