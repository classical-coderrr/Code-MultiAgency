"""Policy-controlled tools for the Coding Agent Loop."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import LocalSandbox, SandboxPolicy
from .workspace import LocalWorkspaceService, WorkspacePathError, WorkspaceRef


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    success: bool
    output: str = ""
    error: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Permission boundary; Skills describe behavior but cannot grant tools."""

    allowed_tools: frozenset[str] = frozenset({
        "repo.tree", "repo.search", "repo.read", "git.status", "git.diff", "git.log",
    })
    allowed_path_globs: tuple[str, ...] = ("**", "*")
    denied_globs: tuple[str, ...] = (
        ".env*", ".git/**", "**/.env*", "**/*.db", "**/*.db-wal", "**/*.db-shm",
        "**/logs/**", "**/checkpoint*/**", "**/private-cache/**", "**/workspace/**",
    )
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    command_timeout_seconds: float = 120.0
    allowed_commands: frozenset[str] = frozenset({
        "git", "java", "mvn", "mvn.cmd", "mvnw", "mvnw.cmd", "node", "npm", "npm.cmd",
        "npx", "npx.cmd", "python", "python.exe", "py", "pytest", "pytest.exe",
    })

    @classmethod
    def coding_default(cls) -> "ToolPolicy":
        return cls(allowed_tools=frozenset({
            "repo.tree", "repo.search", "repo.read", "fs.create", "fs.write", "fs.patch", "fs.delete",
            "fs.rename", "fs.move",
            "shell.execute", "build.run", "test.run", "git.status", "git.diff", "git.log",
        }))

    def permits(self, tool: str) -> bool:
        return tool in self.allowed_tools

    def permits_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/").lstrip("./")
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in self.denied_globs):
            return False
        return any(fnmatch.fnmatch(normalized, pattern) for pattern in self.allowed_path_globs)

    def permits_command(self, command: list[str]) -> bool:
        return bool(command) and Path(command[0]).name.lower() in {item.lower() for item in self.allowed_commands}


class LocalToolGateway:
    """Execute tools inside one Workspace and keep an audit record."""

    def __init__(
        self,
        workspace_service: LocalWorkspaceService,
        policy: ToolPolicy | None = None,
        sandbox: LocalSandbox | None = None,
    ) -> None:
        self.workspace_service = workspace_service
        self.policy = policy or ToolPolicy()
        self.sandbox = sandbox or LocalSandbox(
            workspace_service,
            policy=SandboxPolicy(
                allowed_commands=self.policy.allowed_commands,
                timeout_seconds=self.policy.command_timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
            ),
        )
        self.audit_log: list[dict[str, Any]] = []

    def execute(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        if not self.policy.permits(call.tool):
            return self._record(workspace, call, ToolResult(call.tool, False, error=f"Tool is not allowed: {call.tool}"))
        try:
            if call.tool == "repo.tree":
                result = self._bounded(call.tool, self._tree(workspace, call.arguments))
            elif call.tool == "repo.search":
                result = self._bounded(call.tool, self._search(workspace, call.arguments))
            elif call.tool == "repo.read":
                result = self._bounded(call.tool, self._read(workspace, call.arguments))
            elif call.tool.startswith("git."):
                result = self._bounded(call.tool, self._git(workspace, call.tool))
            elif call.tool in {"fs.create", "fs.write", "fs.patch", "fs.delete", "fs.rename", "fs.move"}:
                result = self._fs(workspace, call)
            elif call.tool in {"shell.execute", "build.run", "test.run"}:
                result = self._command(workspace, call)
            else:
                result = ToolResult(call.tool, False, error=f"Unsupported tool: {call.tool}")
        except (OSError, ValueError, PermissionError, WorkspacePathError, subprocess.SubprocessError) as exc:
            result = ToolResult(call.tool, False, error=str(exc))
        return self._record(workspace, call, result)

    async def aexecute(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        if call.tool in {"shell.execute", "build.run", "test.run"} and self.policy.permits(call.tool):
            try:
                command = self._command_argv(call)
                result = await self.sandbox.arun(workspace, command, timeout_seconds=self._timeout(call.arguments))
                output = self._bounded(call.tool, result.stdout + ("\n" + result.stderr if result.stderr else ""))
                value = ToolResult(call.tool, result.success, output=output.output, error=None if result.success else self._command_error(result), truncated=output.truncated, metadata=result.as_dict())
                return self._record(workspace, call, value)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, PermissionError, WorkspacePathError) as exc:
                return self._record(workspace, call, ToolResult(call.tool, False, error=str(exc)))
        return await asyncio.to_thread(self.execute, workspace, call)

    def _tree(self, workspace: WorkspaceRef | dict[str, Any], arguments: dict[str, Any]) -> str:
        relative = str(arguments.get("path") or "")
        root = self._path(workspace, relative)
        if not root.exists():
            return ""
        max_depth = max(0, min(12, int(arguments.get("max_depth", 4))))
        rows: list[str] = []
        base = root if root.is_dir() else root.parent
        for item in sorted(base.rglob("*")):
            rel = item.relative_to(root if root.is_dir() else base).as_posix()
            child_rel = str(Path(relative) / rel)
            if not self.policy.permits_path(child_rel) or not self.workspace_service.can_read(workspace, child_rel):
                continue
            if len(Path(rel).parts) > max_depth:
                continue
            rows.append(f"{rel}{'/' if item.is_dir() else ''}")
        return "\n".join(rows)

    def _search(self, workspace: WorkspaceRef | dict[str, Any], arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("repo.search requires query")
        relative = str(arguments.get("path") or "")
        root = self._path(workspace, relative)
        if not root.exists():
            return ""
        max_results = max(1, min(200, int(arguments.get("max_results", 50))))
        workspace_root = Path(str(workspace.get("worktree_path") if isinstance(workspace, dict) else workspace.worktree_path))
        files = [root] if root.is_file() else (path for path in root.rglob("*") if path.is_file())
        results: list[str] = []
        for path in files:
            rel = path.relative_to(workspace_root).as_posix()
            if not self.policy.permits_path(rel) or not self.workspace_service.can_read(workspace, rel):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if query.lower() in line.lower():
                    results.append(f"{rel}:{line_no}:{line.strip()}")
                    if len(results) >= max_results:
                        return "\n".join(results)
        return "\n".join(results)

    def _read(self, workspace: WorkspaceRef | dict[str, Any], arguments: dict[str, Any]) -> str:
        path = self._path(workspace, str(arguments.get("path") or ""))
        if not path.is_file():
            raise ValueError("repo.read path is not a file")
        return path.read_text(encoding="utf-8")

    def _fs(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        relative = str(call.arguments.get("path") or "")
        path = self._path(workspace, relative)
        content = call.arguments.get("content")
        if call.tool in {"fs.create", "fs.write"}:
            if not isinstance(content, str):
                raise ValueError(f"{call.tool} requires string content")
            if len(content.encode("utf-8")) > self.policy.max_file_bytes:
                raise ValueError("file exceeds Tool Gateway size limit")
            if call.tool == "fs.create" and path.exists():
                raise FileExistsError(relative)
            previous_hash = self._file_sha256(path) if path.is_file() else None
            self._atomic_write(path, content)
            return ToolResult(
                call.tool,
                True,
                output=relative,
                metadata={
                    "operation": "create" if call.tool == "fs.create" else "update",
                    "path": relative,
                    "bytes": len(content.encode("utf-8")),
                    "beforeSha256": previous_hash,
                    "afterSha256": self._file_sha256(path),
                },
            )
        if call.tool == "fs.patch":
            old_text = call.arguments.get("old_text")
            new_text = call.arguments.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                raise ValueError("fs.patch requires old_text and new_text")
            current = path.read_text(encoding="utf-8")
            if old_text not in current:
                raise ValueError("fs.patch old_text was not found")
            updated = current.replace(old_text, new_text, 1 if not call.arguments.get("replace_all") else -1)
            if len(updated.encode("utf-8")) > self.policy.max_file_bytes:
                raise ValueError("patched file exceeds Tool Gateway size limit")
            previous_hash = self._file_sha256(path)
            self._atomic_write(path, updated)
            return ToolResult(
                call.tool,
                True,
                output=relative,
                metadata={
                    "operation": "update",
                    "path": relative,
                    "changed": updated != current,
                    "beforeSha256": previous_hash,
                    "afterSha256": self._file_sha256(path),
                },
            )
        if call.tool in {"fs.rename", "fs.move"}:
            destination_relative = str(call.arguments.get("destination") or "")
            if not destination_relative:
                raise ValueError(f"{call.tool} requires destination")
            destination = self._path(workspace, destination_relative)
            if not path.exists():
                raise FileNotFoundError(relative)
            if not path.is_file():
                raise IsADirectoryError(relative)
            if destination.exists() and not bool(call.arguments.get("overwrite")):
                raise FileExistsError(destination_relative)
            if destination.exists() and not destination.is_file():
                raise IsADirectoryError(destination_relative)
            digest = self._file_sha256(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            return ToolResult(
                call.tool,
                True,
                output=destination_relative,
                metadata={
                    "operation": "rename" if call.tool == "fs.rename" else "move",
                    "source": relative,
                    "destination": destination_relative,
                    "sha256": digest,
                },
            )
        if not path.exists():
            raise FileNotFoundError(relative)
        if path.is_dir():
            raise IsADirectoryError(relative)
        size_bytes = path.stat().st_size
        digest = self._file_sha256(path)
        path.unlink()
        return ToolResult(
            call.tool,
            True,
            output=relative,
            metadata={
                "operation": "delete",
                "path": relative,
                "bytes": size_bytes,
                "beforeSha256": digest,
            },
        )

    def _command(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        command = self._command_argv(call)
        result = self.sandbox.run(workspace, command, timeout_seconds=self._timeout(call.arguments))
        output = self._bounded(call.tool, result.stdout + ("\n" + result.stderr if result.stderr else ""))
        return ToolResult(call.tool, result.success, output=output.output, error=None if result.success else self._command_error(result), truncated=output.truncated, metadata=result.as_dict())

    def _command_argv(self, call: ToolCall) -> list[str]:
        raw = call.arguments.get("command")
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("Command tools require an argv list")
        command = list(raw)
        if not self.policy.permits_command(command):
            raise PermissionError(f"Sandbox command is not allowed: {command[0] if command else ''}")
        return command

    def _git(self, workspace: WorkspaceRef | dict[str, Any], tool: str) -> str:
        command = {
            "git.status": ["git", "status", "--short"],
            "git.diff": ["git", "diff", "--no-ext-diff", "--binary"],
            "git.log": ["git", "log", "-n", "20", "--oneline", "--decorate"],
        }[tool]
        result = self.sandbox.run(workspace, command, timeout_seconds=min(10, self.policy.command_timeout_seconds))
        if not result.success:
            raise RuntimeError(self._command_error(result))
        return result.stdout

    def _path(self, workspace: WorkspaceRef | dict[str, Any], relative: str) -> Path:
        normalized = relative.replace("\\", "/").lstrip("/")
        if not self.policy.permits_path(normalized):
            raise PermissionError("Path is denied by Tool Gateway policy")
        return self.workspace_service.resolve_relative(workspace, normalized)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _file_sha256(path: Path) -> str | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _bounded(self, tool: str, output: str) -> ToolResult:
        encoded = output.encode("utf-8")
        if len(encoded) <= self.policy.max_output_bytes:
            return ToolResult(tool, True, output=output)
        clipped = encoded[: self.policy.max_output_bytes].decode("utf-8", errors="ignore")
        return ToolResult(tool, True, output=clipped, truncated=True)

    @staticmethod
    def _timeout(arguments: dict[str, Any]) -> float:
        try:
            return max(0.1, float(arguments.get("timeout_seconds", 120)))
        except (TypeError, ValueError):
            return 120.0

    @staticmethod
    def _command_error(result: Any) -> str:
        if getattr(result, "timed_out", False):
            return "Sandbox command timed out"
        return (getattr(result, "stderr", "") or getattr(result, "stdout", "") or f"process exited with {getattr(result, 'exit_code', None)}").strip()

    def _record(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall, result: ToolResult) -> ToolResult:
        self.audit_log.append({
            "tool": call.tool,
            "arguments": {key: value for key, value in call.arguments.items() if key not in {"content", "api_key", "old_text", "new_text"}},
            "run_id": workspace.get("run_id") if isinstance(workspace, dict) else workspace.run_id,
            "success": result.success,
            "error": result.error,
        })
        return result
