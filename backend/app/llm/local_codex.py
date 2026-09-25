"""Local Codex CLI provider.

This provider intentionally does not read or transmit Codex credential files.
It delegates to the locally installed Codex CLI, which uses the user's
existing local login session and returns machine-readable JSONL events.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, LLMTimeoutError


class LocalCodexProvider(LLMProvider):
    """Run a non-interactive Codex CLI turn with the local login."""

    def __init__(
        self,
        model_name: str = "codex-default",
        *,
        executable: str | None = None,
        working_directory: str | None = None,
    ) -> None:
        self.model_name = model_name.strip() or "codex-default"
        self.executable = executable or os.getenv("CODEX_EXECUTABLE", "codex.exe")
        self.working_directory = working_directory or os.getenv("CODEX_WORKDIR") or str(Path.cwd())

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "chatgpt_local",
            "transport": "codex_cli",
            "modelFamily": "codex",
            "model": self.model_name,
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
            "capabilitySource": "local_codex_cli",
            "authentication": "local_login",
            "tokenControl": "soft_prompt_only",
        }

    async def check_login(self) -> None:
        executable = self._resolve_executable()
        _, _, returncode = await _run_process(
            [executable, "login", "status"],
            cwd=self.working_directory,
            timeout=10,
            input_data=None,
        )
        if returncode != 0:
            raise LLMError("本地 Codex 未登录，请先在本机执行 codex login。")

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> LLMResponse:
        settings = config or {}
        executable = self._resolve_executable()
        max_tokens = _positive_int(settings.get("max_tokens"), 6000)
        total_timeout = _positive_float(
            settings.get("total_timeout") or settings.get("request_timeout"),
            180,
        )
        prompt = _build_prompt(system_prompt, user_prompt, max_tokens)
        command = [
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
        ]
        if self.model_name not in {"", "codex-default", "local"}:
            command.extend(["--model", self.model_name])

        schema = settings.get("response_schema")
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        last_message_path: Path | None = None
        if isinstance(schema, dict):
            temp_dir = tempfile.TemporaryDirectory(prefix="agent-team-codex-output-")
            schema_path = Path(temp_dir.name) / "response-schema.json"
            last_message_path = Path(temp_dir.name) / "last-message.txt"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command.extend(["--output-schema", str(schema_path), "--output-last-message", str(last_message_path)])

        final_message = ""
        try:
            stdout, stderr, returncode = await _run_process(
                command,
                cwd=self.working_directory,
                timeout=total_timeout,
                input_data=prompt.encode("utf-8"),
                timeout_message=f"本地 Codex 执行超时（{total_timeout:g}s）",
            )
            if last_message_path and last_message_path.is_file():
                final_message = last_message_path.read_text(encoding="utf-8", errors="replace")
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()

        text, reasoning, usage, finish_reason, event_error = _parse_jsonl(stdout)
        if final_message.strip():
            # The JSONL stream can contain intermediate assistant messages.
            # Prefer Codex's dedicated final-message file for one protocol turn.
            text = final_message.strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if returncode != 0:
            detail = event_error or stderr_text or "本地 Codex 返回非零退出码"
            raise LLMError(
                f"本地 Codex 执行失败：{_compact(detail)}",
                retryable=False,
            )
        if not text.strip():
            raise LLMError(
                "本地 Codex 返回空输出",
                retryable=True,
                response_metadata={"reasoning_content": reasoning, "usage": usage},
            )

        return LLMResponse(
            text=text,
            input_tokens=_usage_int(usage, "input_tokens"),
            output_tokens=_usage_int(usage, "output_tokens"),
            model=self.model_name,
            finish_reason=finish_reason or "stop",
            message_content=text,
            reasoning_content=reasoning or None,
            usage=usage,
            request_parameters={
                "provider": "chatgpt_local",
                "transport": "codex_cli",
                "model": self.model_name,
                "execution": "local_subprocess",
                "sandbox": "read-only",
                "ephemeral": True,
                "requested_max_tokens": max_tokens,
                "token_control": "soft_prompt_only",
                "structured_output": "codex_output_schema" if isinstance(schema, dict) else "none",
            },
        )

    def _resolve_executable(self) -> str:
        candidate = self.executable.strip()
        if Path(candidate).is_file():
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

        # A GUI-launched backend can inherit a shorter PATH than the shell
        # where `codex` was installed. Discover the official per-user Windows
        # installation without reading any login or credential files.
        local_app_data = os.getenv("LOCALAPPDATA")
        if os.name == "nt" and local_app_data:
            install_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            if install_root.is_dir():
                discovered = sorted(
                    install_root.rglob("codex.exe"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                if discovered:
                    return str(discovered[0])

        raise LLMError(
            f"未找到本地 Codex CLI：{candidate}。请确认 Codex 已安装，或配置 CODEX_EXECUTABLE。"
        )


def _build_prompt(system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    return (
        "你正在作为 Agent Team 中的一个 Agent 工作。\n"
        "请遵守以下系统职责，并只返回最终答复，不要执行命令，不要修改本地文件。\n\n"
        f"系统职责：\n{system_prompt}\n\n"
        f"用户任务：\n{user_prompt}\n\n"
        f"输出预算提示：请尽量将最终答复控制在约 {max_tokens} 个输出 Token 以内；"
        "如果任务很长，请优先保证结构完整和关键结论，不要输出无关解释。"
    )


def _parse_jsonl(raw: bytes) -> tuple[str, str, dict[str, Any], str | None, str]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    plain_parts: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    event_error = ""

    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            plain_parts.append(stripped)
            continue
        if not isinstance(event, dict):
            continue

        event_type = str(event.get("type", "")).lower()
        if event_type == "error":
            error = event.get("error")
            event_error = _content_to_text(error) or str(event.get("message", ""))
        if isinstance(event.get("usage"), dict):
            usage.update(event["usage"])
        if event_type in {"turn.completed", "response.completed"}:
            finish_reason = str(event.get("finish_reason") or event.get("status") or "") or finish_reason

        item = event.get("item") if isinstance(event.get("item"), dict) else event
        item_type = str(item.get("type", "")).lower()
        item_text = _content_to_text(item.get("text") or item.get("content") or item.get("message"))
        if not item_text:
            continue
        if item_type in {"reasoning", "reasoning_summary"}:
            reasoning_parts.append(item_text)
        elif item_type in {"agent_message", "message", "assistant_message"}:
            text_parts.append(item_text)

    final_text = "".join(text_parts).strip() or "\n".join(plain_parts).strip()
    reasoning = "".join(reasoning_parts).strip()
    return final_text, reasoning, usage, finish_reason, _compact(event_error)


async def _run_process(
    command: list[str],
    *,
    cwd: str,
    timeout: float,
    input_data: bytes | None,
    timeout_message: str = "本地 Codex 操作超时",
) -> tuple[bytes, bytes, int]:
    """Run Codex without relying on Windows' Proactor event loop.

    Uvicorn reload workers may use a Selector event loop, where
    ``asyncio.create_subprocess_exec`` raises ``NotImplementedError`` on
    Windows. Popen runs in the OS process layer, while communicate is moved
    to a worker thread. Cancellation still kills the complete process tree
    and waits for it before the caller can retry.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise LLMError(
            f"无法启动本地 Codex CLI：{_compact(str(exc))}",
            retryable=False,
        ) from exc

    communicate_task = asyncio.create_task(
        asyncio.to_thread(process.communicate, input_data)
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communicate_task),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        await _terminate_process(process)
        await _drain_communicate(communicate_task)
        raise LLMTimeoutError(timeout_message) from exc
    except asyncio.CancelledError:
        await _terminate_process(process)
        await _drain_communicate(communicate_task)
        raise

    return stdout, stderr, int(process.returncode or 0)


async def _drain_communicate(task: asyncio.Task[tuple[bytes, bytes]]) -> None:
    """Wait briefly for the pipe reader after the child process is killed."""
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except asyncio.TimeoutError:
        # The OS process tree has already been terminated. The reader thread
        # will finish when its pipe handles close; do not block the event loop.
        pass


async def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
    except asyncio.TimeoutError:
        pass


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_to_text(item) for item in value)
    if isinstance(value, dict):
        return _content_to_text(value.get("text") or value.get("content") or value.get("message"))
    return str(value)


def _usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


def _compact(value: str) -> str:
    return " ".join(str(value).split())[:300]
