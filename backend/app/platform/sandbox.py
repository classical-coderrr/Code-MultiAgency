"""Bounded local execution for Code Company tools.

This is the first portable Sandbox adapter. It deliberately uses argv-only
process creation, a workspace cwd, a short allowlist and sanitized env vars.
It is suitable for local MVP execution; a container/worker adapter can replace
it later without changing ToolGateway callers.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .workspace import LocalWorkspaceService, WorkspaceRef


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    allowed_commands: frozenset[str] = frozenset({
        "git", "java", "mvn", "mvn.cmd", "mvnw", "mvnw.cmd", "node", "npm", "npm.cmd",
        "npx", "npx.cmd", "python", "python.exe", "py", "pytest", "pytest.exe",
    })
    timeout_seconds: float = 120.0
    max_output_bytes: int = 128 * 1024
    network_enabled: bool = False

    def permits(self, command: Sequence[str]) -> bool:
        if not command:
            return False
        executable = Path(str(command[0])).name.lower()
        return executable in {item.lower() for item in self.allowed_commands}


@dataclass(frozen=True, slots=True)
class SandboxResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exitCode": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timedOut": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "success": self.success,
        }


class LocalSandbox:
    def __init__(self, workspace_service: LocalWorkspaceService, policy: SandboxPolicy | None = None) -> None:
        self.workspace_service = workspace_service
        self.policy = policy or SandboxPolicy()

    def run(self, workspace: WorkspaceRef | dict[str, Any], command: Sequence[str], *, timeout_seconds: float | None = None) -> SandboxResult:
        argv = self._validate(command)
        cwd = self.workspace_service.resolve_relative(workspace, "")
        timeout = max(0.1, min(float(timeout_seconds or self.policy.timeout_seconds), self.policy.timeout_seconds))
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=self._sanitized_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._text(exc.stdout)
            stderr = self._text(exc.stderr)
            stdout, stderr, truncated = self._bound(stdout, stderr)
            return SandboxResult(tuple(argv), None, stdout, stderr, timed_out=True, truncated=truncated)
        stdout, stderr, truncated = self._bound(completed.stdout or "", completed.stderr or "")
        return SandboxResult(tuple(argv), completed.returncode, stdout, stderr, truncated=truncated)

    async def arun(self, workspace: WorkspaceRef | dict[str, Any], command: Sequence[str], *, timeout_seconds: float | None = None) -> SandboxResult:
        argv = self._validate(command)
        cwd = self.workspace_service.resolve_relative(workspace, "")
        timeout = max(0.1, min(float(timeout_seconds or self.policy.timeout_seconds), self.policy.timeout_seconds))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=self._sanitized_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            self._terminate(process)
            await process.wait()
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout, stderr, truncated = self._bound(self._text(stdout_bytes), self._text(stderr_bytes))
            return SandboxResult(tuple(argv), None, stdout, stderr, timed_out=True, truncated=truncated)
        except asyncio.CancelledError:
            self._terminate(process)
            await process.wait()
            # The cancellation is intentionally re-raised after the child is
            # reaped, so a retry cannot start while the old process is alive.
            raise
        stdout, stderr, truncated = self._bound(self._text(stdout_bytes), self._text(stderr_bytes))
        return SandboxResult(tuple(argv), process.returncode, stdout, stderr, truncated=truncated)

    def _validate(self, command: Sequence[str]) -> list[str]:
        argv = [str(item) for item in command]
        if not argv or any("\x00" in item for item in argv):
            raise ValueError("Sandbox command must be a non-empty argv list")
        if not self.policy.permits(argv):
            raise PermissionError(f"Sandbox command is not allowed: {argv[0]}")
        return argv

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    def _bound(self, stdout: str, stderr: str) -> tuple[str, str, bool]:
        limit = max(1024, int(self.policy.max_output_bytes))
        encoded = (stdout + stderr).encode("utf-8")
        if len(encoded) <= limit:
            return stdout, stderr, False
        remaining = max(0, limit - len(stderr.encode("utf-8")))
        stdout = stdout.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        stderr = stderr.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        return stdout, stderr, True

    @staticmethod
    def _sanitized_env() -> dict[str, str]:
        blocked = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH")
        return {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in blocked)
        }
