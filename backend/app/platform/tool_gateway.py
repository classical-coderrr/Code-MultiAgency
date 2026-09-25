"""Policy-controlled tools for the Coding Agent Loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
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
    mutable_path_globs: tuple[str, ...] | None = None
    denied_write_path_globs: tuple[str, ...] = ()
    exact_write_paths: frozenset[str] = frozenset()
    require_expected_sha256_for_patch: bool = False
    max_read_lines: int | None = None
    denied_globs: tuple[str, ...] = (
        ".env*", ".git/**", "**/.env*", "**/*.db", "**/*.db-wal", "**/*.db-shm",
        "**/logs/**", "**/checkpoint*/**", "**/private-cache/**", "**/workspace/**",
    )
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    command_timeout_seconds: float = 120.0
    verification_gates: frozenset[str] = frozenset()
    verification_dependencies: frozenset[str] = frozenset()
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

    @classmethod
    def coding_loop(
        cls,
        *,
        mutable_path_globs: tuple[str, ...] = (),
        denied_write_path_globs: tuple[str, ...] = (),
        exact_write_paths: frozenset[str] = frozenset(),
        verification_gates: frozenset[str] = frozenset(),
        verification_dependencies: frozenset[str] = frozenset(),
    ) -> "ToolPolicy":
        """Least-privilege policy for the autonomous Plan/Act loop.

        The loop cannot choose a command or argv. An owner may opt into fixed
        verification gates while the outer workflow retains final validation.
        """
        return cls(
            allowed_tools=frozenset({
                "repo.tree", "repo.search", "repo.read", "fs.create", "fs.patch", "fs.append",
                "git.status", "git.diff", "git.log",
            } | ({"verify.run"} if verification_gates else set())),
            allowed_path_globs=("**", "*"),
            mutable_path_globs=tuple(mutable_path_globs),
            denied_write_path_globs=tuple(denied_write_path_globs),
            exact_write_paths=frozenset(path.replace("\\", "/").casefold() for path in exact_write_paths),
            require_expected_sha256_for_patch=True,
            max_read_lines=400,
            max_output_bytes=32 * 1024,
            verification_gates=verification_gates,
            verification_dependencies=verification_dependencies,
        )

    def permits(self, tool: str) -> bool:
        return tool in self.allowed_tools

    def permits_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return True
        if normalized.startswith("/") or ".." in normalized.split("/"):
            return False
        if any(_path_glob_matches(normalized, pattern) for pattern in self.denied_globs):
            return False
        return any(_path_glob_matches(normalized, pattern) for pattern in self.allowed_path_globs)

    def permits_write_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not self.permits_path(normalized):
            return False
        if normalized.casefold() in self.exact_write_paths:
            return True
        if any(_path_glob_matches(normalized, pattern) for pattern in self.denied_write_path_globs):
            return False
        patterns = self.mutable_path_globs
        if patterns is None:
            patterns = self.allowed_path_globs
        return any(_path_glob_matches(normalized, pattern) for pattern in patterns)

    def permits_command(self, command: list[str]) -> bool:
        return bool(command) and Path(command[0]).name.lower() in {item.lower() for item in self.allowed_commands}


def _path_glob_matches(path: str, pattern: str) -> bool:
    """Match path globs without allowing a single '*' to cross directories."""
    value = str(path or "").replace("\\", "/").casefold()
    rule = str(pattern or "").replace("\\", "/").casefold()
    pieces: list[str] = []
    index = 0
    while index < len(rule):
        character = rule[index]
        if character == "*" and index + 1 < len(rule) and rule[index + 1] == "*":
            index += 2
            if index < len(rule) and rule[index] == "/":
                pieces.append("(?:.*/)?")
                index += 1
            else:
                pieces.append(".*")
            continue
        if character == "*":
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
        index += 1
    return re.fullmatch("".join(pieces), value) is not None


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
                bounded = self._bounded(call.tool, self._read(workspace, call.arguments))
                path = self._path(workspace, str(call.arguments.get("path") or ""))
                result = ToolResult(
                    call.tool,
                    True,
                    output=bounded.output,
                    truncated=bounded.truncated,
                    metadata={
                        "path": str(call.arguments.get("path") or ""),
                        "sha256": self._file_sha256(path),
                        "bytes": path.stat().st_size,
                    },
                )
            elif call.tool.startswith("git."):
                result = self._bounded(call.tool, self._git(workspace, call.tool))
            elif call.tool in {"fs.create", "fs.write", "fs.patch", "fs.append", "fs.delete", "fs.rename", "fs.move"}:
                result = self._fs(workspace, call)
            elif call.tool in {"shell.execute", "build.run", "test.run"}:
                result = self._command(workspace, call)
            elif call.tool == "verify.run":
                command = self._verification_command(workspace, call.arguments)
                setup = self._verification_setup(workspace, call.arguments)
                prepared = self.sandbox.run(workspace, setup, timeout_seconds=self.policy.command_timeout_seconds) if setup else None
                if prepared and prepared.success:
                    command = self._verification_command(workspace, call.arguments)
                result = self._verification_result(prepared, stage="dependency-install") if prepared and not prepared.success else self._verification_result(
                    self.sandbox.run(workspace, command, timeout_seconds=self.policy.command_timeout_seconds)
                )
            else:
                result = ToolResult(call.tool, False, error=f"Unsupported tool: {call.tool}")
        except (OSError, ValueError, PermissionError, WorkspacePathError, subprocess.SubprocessError) as exc:
            result = ToolResult(call.tool, False, error=str(exc))
        return self._record(workspace, call, result)

    async def aexecute(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        if call.tool == "verify.run" and self.policy.permits(call.tool):
            try:
                command = self._verification_command(workspace, call.arguments)
                setup = self._verification_setup(workspace, call.arguments)
                if setup:
                    prepared = await self.sandbox.arun(workspace, setup, timeout_seconds=self.policy.command_timeout_seconds)
                    if not prepared.success:
                        return self._record(workspace, call, self._verification_result(prepared, stage="dependency-install"))
                    command = self._verification_command(workspace, call.arguments)
                run = await self.sandbox.arun(workspace, command, timeout_seconds=self.policy.command_timeout_seconds)
                return self._record(workspace, call, self._verification_result(run))
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, PermissionError, WorkspacePathError) as exc:
                return self._record(workspace, call, ToolResult(call.tool, False, error=str(exc)))
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
        if self.policy.max_read_lines is not None and path.stat().st_size > self.policy.max_file_bytes:
            raise ValueError("repo.read file exceeds Tool Gateway size limit")
        content = path.read_text(encoding="utf-8")
        max_lines = self.policy.max_read_lines
        if max_lines is None:
            return content
        lines = content.splitlines()
        total_lines = len(lines)
        start_line = max(1, int(arguments.get("start_line", 1)))
        requested_end = arguments.get("end_line")
        end_line = int(requested_end) if requested_end is not None else start_line + max_lines - 1
        end_line = max(start_line, min(end_line, start_line + max_lines - 1, total_lines or 1))
        selected = lines[start_line - 1:end_line]
        header = f"[lines {start_line}-{start_line + len(selected) - 1} of {total_lines}]"
        return header + ("\n" + "\n".join(selected) if selected else "")

    def coding_action_intent(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> dict[str, Any]:
        """Validate a coding-loop action and return a safe, content-free journal intent."""
        if not self.policy.permits(call.tool):
            raise PermissionError(f"Tool is not allowed: {call.tool}")
        if call.tool == "verify.run":
            gate = self._verification_gate(call.arguments)
            return {"operation": "verify", "gate": gate}
        if call.tool in {"fs.create", "fs.patch", "fs.append"}:
            relative = str(call.arguments.get("path") or "")
            if not relative or not self.policy.permits_write_path(relative):
                raise PermissionError("Path is outside the Agent's writable ownership")
            path = self._path(workspace, relative)
            if call.tool == "fs.create":
                amendment_reason = self._plan_amendment_reason(relative, call.arguments)
                content = call.arguments.get("content")
                if not isinstance(content, str):
                    raise ValueError("fs.create requires string content")
                if len(content.encode("utf-8")) > self.policy.max_file_bytes:
                    raise ValueError("file exceeds Tool Gateway size limit")
                if path.exists():
                    raise FileExistsError(relative)
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                intent = {
                    "operation": "create",
                    "path": relative,
                    "beforeSha256": None,
                    "afterSha256": content_hash,
                    "bytes": len(content.encode("utf-8")),
                }
                if amendment_reason:
                    intent["planAmendmentReason"] = amendment_reason
                return intent
            expected_hash = str(call.arguments.get("expected_sha256") or "")
            if not path.is_file():
                raise FileNotFoundError(relative)
            current_hash = self._file_sha256(path)
            current = path.read_text(encoding="utf-8")
            if self.policy.require_expected_sha256_for_patch and not expected_hash:
                raise ValueError(f"{call.tool} requires expected_sha256 from the latest repo.read result")
            if expected_hash and current_hash != expected_hash:
                raise ValueError(f"{call.tool} expected_sha256 does not match the current file")
            if call.tool == "fs.append":
                addition = call.arguments.get("content")
                if not isinstance(addition, str) or not addition:
                    raise ValueError("fs.append requires non-empty string content")
                updated = current + addition
            else:
                old_text = call.arguments.get("old_text")
                new_text = call.arguments.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str):
                    raise ValueError("fs.patch requires old_text and new_text")
                if current.count(old_text) != 1:
                    raise ValueError("fs.patch old_text must match exactly once")
                updated = current.replace(old_text, new_text, 1)
            if len(updated.encode("utf-8")) > self.policy.max_file_bytes:
                raise ValueError("patched file exceeds Tool Gateway size limit")
            return {
                "operation": "append" if call.tool == "fs.append" else "patch",
                "path": relative,
                "beforeSha256": current_hash,
                "afterSha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
                "bytes": len(updated.encode("utf-8")),
            }
        if call.tool == "repo.read":
            relative = str(call.arguments.get("path") or "")
            return {"operation": "read", "path": relative}
        return {"operation": call.tool}

    def _plan_amendment_reason(self, relative: str, arguments: dict[str, Any]) -> str:
        """Require an auditable reason for an unplanned owner-owned file."""
        normalized = relative.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not self.policy.exact_write_paths or normalized.casefold() in self.policy.exact_write_paths:
            return ""
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 240:
            raise ValueError("Creating a file outside the frozen plan requires reason (8-240 characters)")
        if any(marker in reason.casefold() for marker in ("sk-", "bearer ", "api_key", "password=")):
            raise ValueError("Plan amendment reason must not contain credentials")
        return reason.strip()

    def reconcile_coding_action(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        intent: dict[str, Any],
    ) -> tuple[str, ToolResult]:
        """Reconcile a journaled file action after interruption; never replay it blindly."""
        operation = str(intent.get("operation") or "")
        path_text = str(intent.get("path") or "")
        if operation not in {"create", "patch", "append"} or not path_text:
            return "UNKNOWN", ToolResult(
                "coding_loop.recovery", False,
                error="上次工具动作中断，无法确认结果；请重新检查文件状态后再决定下一步。",
            )
        try:
            path = self._path(workspace, path_text)
            current_hash = self._file_sha256(path)
        except (OSError, ValueError, PermissionError, WorkspacePathError) as exc:
            return "UNKNOWN", ToolResult("coding_loop.recovery", False, error=str(exc))
        expected_after = str(intent.get("afterSha256") or "")
        expected_before = intent.get("beforeSha256")
        if current_hash and current_hash == expected_after:
            return "COMPLETED", ToolResult(
                f"fs.{operation}", True, output=path_text,
                metadata={"operation": operation, "path": path_text, "afterSha256": current_hash, "reconciled": True},
            )
        if current_hash == expected_before:
            reason = "上次文件动作未留下预期变更；没有自动重放，请根据当前内容重新决定。"
        else:
            reason = "文件内容与动作前、动作后的指纹均不匹配；为避免覆盖并发修改，已停止自动重放。"
        return "UNKNOWN", ToolResult(
            f"fs.{operation}", False, error=reason,
            metadata={"operation": operation, "path": path_text, "currentSha256": current_hash, "reconciled": True},
        )

    def _fs(self, workspace: WorkspaceRef | dict[str, Any], call: ToolCall) -> ToolResult:
        relative = str(call.arguments.get("path") or "")
        path = self._path(workspace, relative)
        content = call.arguments.get("content")
        if call.tool in {"fs.create", "fs.write", "fs.patch", "fs.append", "fs.delete", "fs.rename", "fs.move"}:
            if not self.policy.permits_write_path(relative):
                raise PermissionError("Path is outside the Agent's writable ownership")
        if call.tool in {"fs.create", "fs.write"}:
            if not isinstance(content, str):
                raise ValueError(f"{call.tool} requires string content")
            amendment_reason = self._plan_amendment_reason(relative, call.arguments) if call.tool == "fs.create" else ""
            if len(content.encode("utf-8")) > self.policy.max_file_bytes:
                raise ValueError("file exceeds Tool Gateway size limit")
            if call.tool == "fs.create" and path.exists():
                raise FileExistsError(relative)
            previous_hash = self._file_sha256(path) if path.is_file() else None
            self._atomic_create(path, content) if call.tool == "fs.create" else self._atomic_write(path, content)
            metadata = {
                "operation": "create" if call.tool == "fs.create" else "update",
                "path": relative,
                "bytes": len(content.encode("utf-8")),
                "beforeSha256": previous_hash,
                "afterSha256": self._file_sha256(path),
            }
            if amendment_reason:
                metadata["planAmendmentReason"] = amendment_reason
            return ToolResult(
                call.tool,
                True,
                output=relative,
                metadata=metadata,
            )
        if call.tool in {"fs.patch", "fs.append"}:
            if call.tool == "fs.append":
                addition = call.arguments.get("content")
                if not isinstance(addition, str) or not addition:
                    raise ValueError("fs.append requires non-empty string content")
                if not path.is_file():
                    raise FileNotFoundError(relative)
                current = path.read_text(encoding="utf-8")
                previous_hash = self._file_sha256(path)
                if self.policy.require_expected_sha256_for_patch and not call.arguments.get("expected_sha256"):
                    raise ValueError("fs.append requires expected_sha256 from the latest repo.read result")
                if call.arguments.get("expected_sha256") and previous_hash != str(call.arguments.get("expected_sha256")):
                    raise ValueError("fs.append expected_sha256 does not match the current file")
                updated = current + addition
                if len(updated.encode("utf-8")) > self.policy.max_file_bytes:
                    raise ValueError("appended file exceeds Tool Gateway size limit")
                self._atomic_write(path, updated)
                return ToolResult(
                    call.tool, True, output=relative,
                    metadata={
                        "operation": "update", "path": relative,
                        "bytes": len(updated.encode("utf-8")),
                        "beforeSha256": previous_hash, "afterSha256": self._file_sha256(path),
                    },
                )
            old_text = call.arguments.get("old_text")
            new_text = call.arguments.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                raise ValueError("fs.patch requires old_text and new_text")
            if self.policy.require_expected_sha256_for_patch and not call.arguments.get("expected_sha256"):
                raise ValueError("fs.patch requires expected_sha256 from the latest repo.read result")
            current = path.read_text(encoding="utf-8")
            if self.policy.require_expected_sha256_for_patch:
                current_hash = self._file_sha256(path)
                if current_hash != str(call.arguments.get("expected_sha256")):
                    raise ValueError("fs.patch expected_sha256 does not match the current file")
                if current.count(old_text) != 1:
                    raise ValueError("fs.patch old_text must match exactly once")
            elif old_text not in current:
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

    def _verification_gate(self, arguments: dict[str, Any]) -> str:
        if set(arguments) != {"gate"} or not isinstance(arguments.get("gate"), str):
            raise ValueError("verify.run only accepts a named gate; command, path and timeout are not accepted")
        gate = arguments["gate"]
        if gate not in self.policy.verification_gates:
            raise PermissionError(f"Verification gate is not assigned to this Agent: {gate}")
        return gate

    def _verification_command(self, workspace: WorkspaceRef | dict[str, Any], arguments: dict[str, Any]) -> list[str]:
        gate = self._verification_gate(arguments)
        root = self.workspace_service.resolve_relative(workspace, "")
        if gate in {"backend-compile", "backend-test"}:
            if not (root / "pom.xml").is_file():
                raise ValueError("Backend verification requires pom.xml in the workspace root")
            executable = shutil.which("mvn.cmd" if os.name == "nt" else "mvn")
            if not executable:
                raise ValueError("Maven is unavailable; verification was not run")
            return [executable, "-B", "-Dstyle.color=never", "-DskipTests", "compile"] if gate == "backend-compile" else [executable, "-B", "-Dstyle.color=never", "test"]
        if gate == "frontend-build":
            if not (root / "package.json").is_file():
                raise ValueError("Frontend verification requires package.json in the workspace root")
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if not isinstance(scripts, dict) or str(scripts.get("build") or "").strip() != "vite build":
                raise ValueError("Frontend verification only runs the frozen 'vite build' script; other scripts require the outer Tester")
            executable = shutil.which("node.exe" if os.name == "nt" else "node")
            if not executable:
                raise ValueError("Node.js is unavailable; verification was not run")
            vite_entry = root / "node_modules" / "vite" / "bin" / "vite.js"
            if vite_entry.exists() and not vite_entry.resolve().is_relative_to(root.resolve()):
                raise PermissionError("Frontend Vite entrypoint must stay inside the workspace")
            # npm run would execute prebuild/postbuild scripts even when build is frozen.
            return [executable, str(vite_entry), "build"]
        raise ValueError(f"Unsupported verification gate: {gate}")

    def _verification_setup(self, workspace: WorkspaceRef | dict[str, Any], arguments: dict[str, Any]) -> list[str] | None:
        if self._verification_gate(arguments) != "frontend-build":
            return None
        root = self.workspace_service.resolve_relative(workspace, "")
        if (root / "node_modules").is_dir():
            return None
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        if any(key in package for key in ("optionalDependencies", "overrides", "resolutions", "workspaces", "bundledDependencies")):
            raise PermissionError("Frontend verification cannot install packages with dynamic dependency overrides or workspaces")
        declared: set[str] = set()
        for field_name in ("dependencies", "devDependencies"):
            values = package.get(field_name) or {}
            if not isinstance(values, dict):
                raise ValueError(f"package.json {field_name} must be an object")
            for name, version in values.items():
                if not isinstance(version, str) or not re.fullmatch(r"[~^]?\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?", version.strip()):
                    raise PermissionError("Frontend verification will not install non-registry or unpinned dependency specs")
                declared.add(str(name))
        if not declared or not declared <= self.policy.verification_dependencies:
            raise PermissionError("Dependency installation needs a frozen allowlist matching package.json; outer Tester will handle unsupported dependencies")
        executable = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not executable:
            raise ValueError("npm is unavailable; verification was not run")
        return [executable, "install", "--package-lock=false", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"]

    def _verification_result(self, result: Any, *, stage: str = "gate") -> ToolResult:
        bounded = self._bounded("verify.run", result.stdout + ("\n" + result.stderr if result.stderr else ""))
        return ToolResult(
            "verify.run", result.success, output=bounded.output,
            error=None if result.success else (
                "Verification timed out" if result.timed_out else f"{stage} failed (exit {result.exit_code})"
            ),
            truncated=bounded.truncated or result.truncated,
            metadata={"command": list(result.command), "exitCode": result.exit_code, "timedOut": result.timed_out, "stage": stage},
        )

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
    def _atomic_create(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

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
