"""Provider-neutral bounded coding loop."""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from ..llm.base import LLMProvider, LLMResponse
from ..platform.tool_gateway import LocalToolGateway, ToolCall, ToolResult
from ..platform.workspace import WorkspaceRef

Emit = Callable[[str, dict[str, Any]], Awaitable[None]] | None
class CodingLoopError(RuntimeError):
    pass

@dataclass(frozen=True, slots=True)
class CodingLoopConfig:
    max_iterations: int = 8
    timeout_seconds: float = 120.0
    # Total output budget across this Agent's loop; each call has its own cap.
    max_tokens: int = 4000
    response_max_tokens: int | None = None
    thinking: str = "low"
    max_tool_actions: int = 8
    max_observation_chars: int = 3000
    require_successful_mutation_before_final: bool = True
    required_artifacts: tuple[str, ...] = ()

@dataclass(slots=True)
class CodingLoopResult:
    output: str
    responses: list[LLMResponse] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    iterations: int = 0
    verified: bool = False

class CodingAgentLoop:
    def __init__(self, provider: LLMProvider, gateway: LocalToolGateway, repository: Any | None = None) -> None:
        self.provider = provider
        self.gateway = gateway
        self.repository = repository

    async def run(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        task: str,
        *,
        system_prompt: str = "",
        config: CodingLoopConfig | None = None,
        emit: Emit = None,
        run_id: str | None = None,
        step_id: str | None = None,
        on_response: Callable[[LLMResponse], None] | None = None,
    ) -> CodingLoopResult:
        settings = config or CodingLoopConfig()
        max_iterations = max(1, min(32, int(settings.max_iterations)))
        max_actions = max(0, min(32, int(settings.max_tool_actions)))
        output_budget = max(1, int(settings.max_tokens))
        response_budget = max(1, int(settings.response_max_tokens or settings.max_tokens))
        required_artifacts = tuple(dict.fromkeys(
            str(path).replace("\\", "/").strip("/")
            for path in settings.required_artifacts
            if str(path).strip()
        ))
        observation_limit = max(256, min(12_000, int(settings.max_observation_chars)))
        deadline = asyncio.get_running_loop().time() + max(0.1, float(settings.timeout_seconds))
        journal = bool(self.repository and run_id and step_id)
        records: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        responses: list[LLMResponse] = []
        tool_results: list[ToolResult] = []
        output_tokens = 0
        final_output = ""
        sequence = 1
        action_count = 0
        has_successful_mutation = False
        rejected_final_count = 0
        incomplete_final_count = 0
        consecutive_protocol_failures = 0
        turn_records: list[dict[str, Any]] = []

        if journal:
            records = self.repository.list_coding_actions(str(run_id), str(step_id))
            records = await self._reconcile_pending(workspace, records, str(run_id), str(step_id), emit)
            turn_records = self.repository.list_coding_turns(str(run_id), str(step_id))
            for turn in turn_records:
                if str(turn.get("status") or "").upper() == "RUNNING":
                    # The provider may have completed after the worker lost its
                    # response. Charge the reserved ceiling rather than replaying
                    # an unaccounted model turn on recovery.
                    self.repository.finish_coding_turn(
                        run_id=str(run_id), step_id=str(step_id),
                        turn_no=int(turn["turn_no"]), status="INTERRUPTED",
                        output_tokens=int(turn.get("requested_max_tokens") or 0),
                    )
            turn_records = self.repository.list_coding_turns(str(run_id), str(step_id))
            output_tokens = sum(max(0, int(item.get("output_tokens") or 0)) for item in turn_records)
            for record in records:
                tool_results.append(self._record_result(record))
                history.append(self._history_observation(record, observation_limit))
                has_successful_mutation = has_successful_mutation or (
                    bool((record.get("result") or {}).get("success", False))
                    and str((record.get("intent") or {}).get("operation") or "") in {"create", "patch", "append"}
                )
            if records:
                sequence = max(int(item["sequence_no"]) for item in records) + 1
            action_count = sum(
                1 for item in records
                if str(item.get("status") or "").upper() in {"COMPLETED", "FAILED", "UNKNOWN"}
            )
        turn_no = max((int(item.get("turn_no") or 0) for item in turn_records), default=0)

        failed = {
            (str(item.get("tool") or ""), str(item.get("arguments_hash") or ""))
            for item in records
            if str(item.get("status") or "").upper() in {"COMPLETED", "FAILED"}
            and not bool((item.get("result") or {}).get("success", False))
        }
        read_tools = {"repo.tree", "repo.read"}
        repeated_read_results: set[tuple[str, str, str]] = set()
        for record in records:
            tool = str(record.get("tool") or "")
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            if (
                tool in read_tools
                and str(record.get("status") or "").upper() == "COMPLETED"
                and bool(result.get("success", False))
            ):
                output_hash = hashlib.sha256(str(result.get("output") or "").encode("utf-8")).hexdigest()
                repeated_read_results.add((tool, str(record.get("arguments_hash") or ""), output_hash))
        role = system_prompt.strip() or "You are the coding Agent responsible for this task."
        composed_system = self._system_prompt(
            role, self.gateway.policy.allowed_tools, self.gateway.policy.verification_gates,
        )
        if self.gateway.policy.exact_write_paths:
            composed_system += (
                "\nCreating an owner-owned helper file outside the frozen file plan requires "
                "fs.create.arguments.reason (8-240 characters). Explain the internal need; "
                "do not put credentials in the reason or alter the frozen API/database contract."
            )

        for iteration in range(turn_no + 1, max_iterations + 1):
            remaining_time = deadline - asyncio.get_running_loop().time()
            remaining_tokens = output_budget - output_tokens
            if remaining_time <= 0:
                raise CodingLoopError("编码循环达到总耗时上限；未将超时误报为完成。")
            if remaining_tokens <= 0:
                missing_required = self._missing_required_artifacts(workspace, required_artifacts)
                if missing_required:
                    if not has_successful_mutation:
                        raise CodingLoopError(
                            "Frozen file plan is incomplete and no source change was staged: "
                            + ", ".join(missing_required[:20])
                        )
                    final_output = "Budget exhausted; required files remain for the outer validation and repair gates: " + ", ".join(missing_required[:20])
                    if emit:
                        await emit("coding_loop.final_pending_validation", {
                            "iteration": iteration - 1, "verified": False,
                            "missingFiles": missing_required, "message": final_output,
                        })
                    break
                if has_successful_mutation:
                    final_output = "已提交工作区动作；可见输出预算已用尽，尚未通过外层成果物验证。"
                    if emit:
                        await emit("coding_loop.final_pending_validation", {
                            "iteration": iteration - 1, "verified": False, "message": final_output,
                        })
                    break
                raise CodingLoopError("编码循环已用完本 Agent 的可见输出 Token 预算。")
            if emit:
                await emit("coding_loop.iteration_started", {
                    "iteration": iteration, "maxIterations": max_iterations,
                    "remainingOutputTokens": remaining_tokens,
                })
            request_tokens = min(remaining_tokens, response_budget)
            if journal:
                self.repository.start_coding_turn(
                    run_id=str(run_id), step_id=str(step_id), turn_no=iteration,
                    requested_max_tokens=request_tokens,
                )
            try:
                response = await asyncio.wait_for(
                    self.provider.generate(
                        composed_system,
                        self._prompt(task, history, iteration, max_iterations),
                        {
                            "agent_id": "coding_agent_loop",
                            "max_tokens": request_tokens,
                            "effective_thinking": settings.thinking,
                            "reasoning_effort": settings.thinking,
                            "request_timeout": remaining_time,
                            "response_schema": self._response_schema(self.gateway.policy.allowed_tools),
                        },
                    ),
                    timeout=remaining_time,
                )
            except asyncio.TimeoutError as exc:
                if journal:
                    self.repository.finish_coding_turn(
                        run_id=str(run_id), step_id=str(step_id), turn_no=iteration,
                        status="INTERRUPTED", output_tokens=request_tokens,
                        finish_reason="timeout",
                    )
                output_tokens += request_tokens
                raise CodingLoopError("编码 Agent 的总任务时限已到，未确认动作将在恢复时核对。") from exc
            except asyncio.CancelledError:
                if journal:
                    self.repository.finish_coding_turn(
                        run_id=str(run_id), step_id=str(step_id), turn_no=iteration,
                        status="INTERRUPTED", output_tokens=request_tokens,
                        finish_reason="cancelled",
                    )
                raise
            except Exception:
                if journal:
                    self.repository.finish_coding_turn(
                        run_id=str(run_id), step_id=str(step_id), turn_no=iteration,
                        status="INTERRUPTED", output_tokens=request_tokens,
                        finish_reason="provider_error",
                    )
                output_tokens += request_tokens
                raise
            responses.append(response)
            usage_tokens = max(0, int(response.output_tokens or 0))
            generated_text = str(response.text or "") + str(getattr(response, "reasoning_content", "") or "")
            ascii_chars = sum(1 for character in generated_text if ord(character) < 128)
            non_ascii_chars = len(generated_text) - ascii_chars
            estimated_tokens = (ascii_chars + 3) // 4 + non_ascii_chars
            consumed_tokens = max(usage_tokens, estimated_tokens)
            output_tokens += consumed_tokens
            if journal:
                self.repository.finish_coding_turn(
                    run_id=str(run_id), step_id=str(step_id), turn_no=iteration,
                    status="COMPLETED", input_tokens=int(response.input_tokens or 0),
                    output_tokens=consumed_tokens, finish_reason=response.finish_reason,
                )
            if on_response:
                on_response(response)
            if self._is_truncated(response.finish_reason):
                reason = "模型响应达到输出长度上限；本轮响应不作为完成结果。"
                history.append({"type": "protocol_error", "error": reason})
                if emit:
                    await emit("coding_loop.protocol_error", {"iteration": iteration, "reason": reason})
                continue

            instruction = self._parse(response.text)
            if instruction is None:
                consecutive_protocol_failures += 1
                reason = "响应不是有效的单个 JSON 对象；请仅返回一个 tool_call 或 final。"
                history.append({"type": "protocol_error", "error": reason})
                if emit:
                    await emit("coding_loop.protocol_error", {
                        "iteration": iteration, "reason": reason,
                        "consecutiveFailures": consecutive_protocol_failures,
                    })
                if consecutive_protocol_failures >= 2:
                    breaker_reason = "Agent 连续两轮未遵守编码协议，已停止无进展循环。"
                    if emit:
                        await emit("coding_loop.no_progress", {
                            "iteration": iteration, "reason": breaker_reason,
                        })
                    raise CodingLoopError(breaker_reason)
                continue
            action, error = self._normalize_instruction(instruction)
            if error:
                consecutive_protocol_failures += 1
                history.append({"type": "protocol_error", "error": error})
                if emit:
                    await emit("coding_loop.protocol_error", {
                        "iteration": iteration, "reason": error,
                        "consecutiveFailures": consecutive_protocol_failures,
                    })
                if consecutive_protocol_failures >= 2:
                    breaker_reason = "Agent 连续两轮未遵守编码协议，已停止无进展循环。"
                    if emit:
                        await emit("coding_loop.no_progress", {
                            "iteration": iteration, "reason": breaker_reason,
                        })
                    raise CodingLoopError(breaker_reason)
                continue
            consecutive_protocol_failures = 0
            if action["type"] == "final":
                if not action["content"]:
                    history.append({"type": "protocol_error", "error": "final.content 不能为空。"})
                    continue
                missing_required = self._missing_required_artifacts(workspace, required_artifacts)
                if missing_required:
                    incomplete_final_count += 1
                    reason = (
                        "Frozen file plan is incomplete; create or finish these owned files before final: "
                        + ", ".join(missing_required[:20])
                    )
                    history.append({"type": "required_files_missing", "paths": missing_required[:20], "error": reason})
                    if emit:
                        await emit("coding_loop.required_files_missing", {
                            "iteration": iteration,
                            "missingFiles": missing_required,
                            "finalRejected": True,
                        })
                    if incomplete_final_count >= 3:
                        if has_successful_mutation:
                            final_output = reason
                            if emit:
                                await emit("coding_loop.final_pending_validation", {
                                    "iteration": iteration, "verified": False,
                                    "missingFiles": missing_required, "message": final_output,
                                })
                            break
                        raise CodingLoopError(reason)
                    continue
                if settings.require_successful_mutation_before_final and not has_successful_mutation:
                    rejected_final_count += 1
                    reason = "尚无成功的受控文件写入；生成任务必须先用文件工具提交源码，再汇报完成。"
                    history.append({"type": "protocol_error", "error": reason})
                    if emit:
                        await emit("coding_loop.protocol_error", {
                            "iteration": iteration,
                            "reason": reason,
                            "finalRejected": True,
                        })
                    if rejected_final_count >= 2:
                        raise CodingLoopError("Agent 连续提交 final 但没有成功写入文件，已停止无进展循环。")
                    continue
                final_output = action["content"]
                if emit:
                    await emit("coding_loop.final_pending_validation", {
                        "iteration": iteration, "verified": False,
                        "message": "Agent 已提交结果，仍需通过外层成果物与运行验证。",
                    })
                break

            call = ToolCall(action["tool"], action["arguments"])
            args_json = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            args_hash = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
            fingerprint = (call.tool, args_hash)
            if fingerprint in failed:
                reason = "相同工具和参数此前已失败，重复动作没有进展，已熔断。"
                if emit:
                    await emit("coding_loop.no_progress", {"iteration": iteration, "tool": call.tool, "reason": reason})
                raise CodingLoopError(reason)
            if action_count >= max_actions:
                reason = f"工具动作达到本步上限（{max_actions}）。"
                history.append({"type": "tool_observation", "tool": call.tool, "success": False, "error": reason})
                if emit:
                    await emit("coding_loop.action_limit", {"iteration": iteration, "maxToolActions": max_actions})
                continue
            try:
                intent = self.gateway.coding_action_intent(workspace, call)
            except (OSError, ValueError, PermissionError) as exc:
                result = self._safe_result(ToolResult(call.tool, False, error=str(exc)), observation_limit)
                failed.add(fingerprint)
                history.append({"type": "tool_observation", **result.as_dict()})
                if emit:
                    await emit("coding_loop.action_rejected", {
                        "iteration": iteration, "tool": call.tool, "reason": result.error,
                    })
                continue

            action_id = hashlib.sha256(
                f"{run_id or ''}|{step_id or ''}|{sequence}|{call.tool}|{args_hash}".encode("utf-8")
            ).hexdigest()
            if journal:
                record = self.repository.start_coding_action(
                    run_id=str(run_id), step_id=str(step_id), action_id=action_id,
                    sequence_no=sequence, tool=call.tool, arguments_hash=args_hash, intent=intent,
                )
                if str(record.get("status")) != "RUNNING":
                    history.append(self._history_observation(record, observation_limit))
                    tool_results.append(self._record_result(record))
                    has_successful_mutation = has_successful_mutation or (
                        bool((record.get("result") or {}).get("success", False))
                        and str((record.get("intent") or {}).get("operation") or "") in {"create", "patch", "append"}
                    )
                    sequence += 1
                    continue
            current_sequence = sequence
            sequence += 1
            if emit:
                await emit("coding_loop.action_started", {
                    "iteration": iteration, "sequence": current_sequence,
                    "actionId": action_id, "tool": call.tool,
                })
            task_result = asyncio.create_task(self.gateway.aexecute(workspace, call))
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(task_result),
                    timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
                )
            except asyncio.TimeoutError as exc:
                # Drain the worker-thread action before a workflow retry can start.
                result = await task_result
                safe = self._safe_result(result, observation_limit)
                if journal:
                    stored = self.repository.finish_coding_action(
                        run_id=str(run_id), step_id=str(step_id), action_id=action_id,
                        status="COMPLETED" if result.success else "FAILED",
                        result=self._stored_result(safe),
                    )
                    records.append(stored)
                if emit:
                    await emit("coding_loop.action_completed", self._action_event(current_sequence, action_id, safe))
                raise CodingLoopError("工具动作超过剩余总耗时；动作已结束并记录。") from exc
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(task_result)
                finally:
                    raise

            safe = self._safe_result(result, observation_limit)
            action_count += 1
            has_successful_mutation = has_successful_mutation or (safe.success and call.tool in {"fs.create", "fs.patch", "fs.append"})
            if journal:
                stored = self.repository.finish_coding_action(
                    run_id=str(run_id), step_id=str(step_id), action_id=action_id,
                    status="COMPLETED" if result.success else "FAILED",
                    result=self._stored_result(safe),
                )
                records.append(stored)
            tool_results.append(safe)
            history.append({"type": "tool_observation", "sequence": current_sequence, **safe.as_dict()})
            if not safe.success:
                failed.add(fingerprint)
            elif call.tool in {"fs.create", "fs.patch", "fs.append"}:
                incomplete_final_count = 0
                # A build failure belongs to the prior source revision. Once
                # source changes, the same named gate is meaningful again.
                failed = {item for item in failed if item[0] != "verify.run"}
            read_signature: tuple[str, str, str] | None = None
            repeated_read = False
            if safe.success and call.tool in read_tools:
                output_hash = hashlib.sha256(str(safe.output or "").encode("utf-8")).hexdigest()
                read_signature = (call.tool, args_hash, output_hash)
                repeated_read = read_signature in repeated_read_results
                repeated_read_results.add(read_signature)
            if emit:
                await emit("coding_loop.action_completed", self._action_event(current_sequence, action_id, safe))
                await emit("coding_loop.iteration_completed", {
                    "iteration": iteration, "sequence": current_sequence, "success": safe.success,
                })
            if repeated_read:
                reason = "相同目录或文件读取再次返回相同结果，未产生新信息；为避免空转已熔断。"
                if emit:
                    await emit("coding_loop.no_progress", {
                        "iteration": iteration, "tool": call.tool, "reason": reason,
                    })
                raise CodingLoopError(reason)

        if not final_output:
            missing_required = self._missing_required_artifacts(workspace, required_artifacts)
            if missing_required:
                if not has_successful_mutation:
                    raise CodingLoopError(
                        "Coding loop reached its iteration limit without completing frozen files: "
                        + ", ".join(missing_required[:20])
                    )
                final_output = "Iteration limit reached; required files remain for the outer validation and repair gates: " + ", ".join(missing_required[:20])
                if emit:
                    await emit("coding_loop.final_pending_validation", {
                        "iteration": max_iterations, "verified": False,
                        "missingFiles": missing_required, "message": final_output,
                    })
            if has_successful_mutation:
                final_output = "已提交工作区动作；轮数已用尽，结果仍需外层成果物验证。"
                if emit:
                    await emit("coding_loop.final_pending_validation", {
                        "iteration": max_iterations, "verified": False, "message": final_output,
                    })
            else:
                raise CodingLoopError("达到轮数上限，未收到符合协议的 final；中间输出不作为成功。")
        return CodingLoopResult(final_output, responses, tool_results, max(turn_no, iteration if 'iteration' in locals() else 0), False)

    def _missing_required_artifacts(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        required_paths: tuple[str, ...],
    ) -> list[str]:
        missing: list[str] = []
        for relative in required_paths:
            try:
                path = self.gateway.workspace_service.resolve_relative(workspace, relative)
                if not path.is_file() or path.stat().st_size == 0:
                    missing.append(relative)
            except (OSError, ValueError, PermissionError):
                missing.append(relative)
        return missing

    async def _reconcile_pending(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        records: list[dict[str, Any]],
        run_id: str,
        step_id: str,
        emit: Emit,
    ) -> list[dict[str, Any]]:
        result_records: list[dict[str, Any]] = []
        for record in records:
            if str(record.get("status") or "").upper() != "RUNNING":
                result_records.append(record)
                continue
            status, result = self.gateway.reconcile_coding_action(workspace, record.get("intent") or {})
            safe = self._safe_result(result, 3000)
            resolved = self.repository.finish_coding_action(
                run_id=run_id, step_id=step_id, action_id=str(record["action_id"]),
                status=status, result=self._stored_result(safe),
            )
            result_records.append(resolved)
            if emit:
                await emit("coding_loop.action_reconciled", {
                    "sequence": int(record["sequence_no"]), "actionId": str(record["action_id"]),
                    "tool": str(record["tool"]), "status": status, "success": safe.success,
                    "reason": safe.error, "metadata": safe.metadata,
                })
        return result_records

    @staticmethod
    def _system_prompt(role: str, allowed_tools: frozenset[str], verification_gates: frozenset[str] = frozenset()) -> str:
        return (
            role
            + "\n\n[受控编码循环协议：必须遵守]\n"
            + "保留上面的 Agent 职责。每次只返回一个 JSON 对象：工具操作格式为 "
            + '{"type":"tool_call","tool":"工具名","arguments":{...},"content":""}；'
            + '结束格式为 {"type":"final","tool":"","arguments":{},"content":"简短结果摘要"}。'
            + "可用工具：" + json.dumps(sorted(allowed_tools), ensure_ascii=False)
            + "。repo.read 可用 start_line/end_line 读取较长文件的局部。每次最多一个工具动作，先观察前一动作结果再继续。fs.patch 与 fs.append 必须提供最近 repo.read 的 metadata.sha256 作为 expected_sha256，old_text 必须唯一匹配；新文件用 fs.create，长文件可先分块 fs.create，再按当前文件哈希分块 fs.append。"
            + ("当前允许的 verify.run 档位：" + json.dumps(sorted(verification_gates), ensure_ascii=False) + "。" if verification_gates else "")
            + "文件工具是由平台执行的 JSON 协议，不是要求你在自身 CLI 沙箱里直接写文件；即使本地沙箱只读，也必须返回 fs.create/fs.patch/fs.append 的 tool_call。repo.tree 或 repo.read 的结果未变化时不要重复读取，直接开始第一个交付文件。编码任务先检查目录和现有实现，再按冻结合同逐步创建或精确修改文件；优先最小差异，不要重复生成整套项目。首轮生成也必须通过文件工具提交源码，不要把源码塞进 final。至少一次受控文件写入成功后，才能 final；完成所有本 Agent 负责的文件后再汇报实际改动，不声称已经构建或测试。"
            + "可用 verify.run 时仅传 {\"gate\":\"backend-compile|backend-test|frontend-build\"} 中当前 Agent 获授权的档位；不得传命令、路径或超时。验证失败后根据真实输出修复相关文件并重新验证。没有该权限或依赖缺失时如实说明。不得声称已验证或编造执行结果；最终完整验收仍由外层工作流负责。Shell 和删除由外层工作流负责。"
        )

    @staticmethod
    def _prompt(task: str, history: list[dict[str, Any]], iteration: int, maximum: int) -> str:
        recent = json.dumps(history[-6:], ensure_ascii=False, separators=(",", ":"), default=str)
        return f"Task:\n{task}\nIteration: {iteration}/{maximum}\nRecent observations:\n{recent}\nReturn exactly one JSON object."

    @staticmethod
    def _response_schema(allowed_tools: Any) -> dict[str, Any]:
        tools = sorted(str(tool) for tool in allowed_tools if str(tool).strip())
        tool_schema: dict[str, Any] = {"type": "string"}
        if tools:
            tool_schema["enum"] = [*tools, ""]
        return {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["tool_call", "final"]},
                "tool": tool_schema,
                "arguments": {"type": "object"},
                "content": {"type": "string"},
            },
            "required": ["type", "tool", "arguments", "content"],
            "additionalProperties": False,
        }

    @staticmethod
    def _parse(value: str) -> dict[str, Any] | None:
        text = str(value or "").strip()
        fence = chr(96) * 3
        if text.startswith(fence):
            lines = text.splitlines()
            if len(lines) < 3 or lines[-1].strip() != fence:
                return None
            text = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _normalize_instruction(value: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        kind = str(value.get("type") or "").strip().lower()
        if kind == "final":
            content = value.get("content")
            if not isinstance(content, str):
                return {}, "final.content 必须是字符串。"
            return {"type": "final", "content": content.strip()}, None
        if kind == "tool_call":
            tool, arguments = value.get("tool"), value.get("arguments", {})
            if not isinstance(tool, str) or not tool.strip() or not isinstance(arguments, dict):
                return {}, "tool_call 必须提供工具名和对象类型 arguments。"
            return {"type": "tool_call", "tool": tool.strip(), "arguments": arguments}, None
        if kind == "actions":
            actions = value.get("actions")
            if not isinstance(actions, list) or len(actions) != 1:
                return {}, "每轮只允许一个工具动作；请拆分后观察结果再继续。"
            if not isinstance(actions[0], dict):
                return {}, "actions[0] 必须是对象。"
            return CodingAgentLoop._normalize_instruction({
                "type": "tool_call",
                "tool": actions[0].get("tool"),
                "arguments": actions[0].get("arguments", {}),
            })
        return {}, "type 只允许 tool_call、单项 actions 或 final。"

    @staticmethod
    def _is_truncated(reason: str | None) -> bool:
        return str(reason or "").strip().lower() in {
            "length", "max_tokens", "max_output_tokens", "content_filter_truncated",
        }

    @staticmethod
    def _safe_text(value: str) -> str:
        def mask_assignment(match: re.Match[str]) -> str:
            return match.group(1) + match.group(2) + "[REDACTED]"

        value = re.sub(
            r"(?i)\b(api[_-]?key|access[_-]?key|private[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|token|client[_-]?secret)\b(\s*[:=]\s*)([^\s,;]+)",
            mask_assignment,
            value,
        )
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", value)

    @classmethod
    def _safe_result(cls, result: ToolResult, max_chars: int) -> ToolResult:
        output = cls._safe_text(str(result.output or ""))
        error = cls._safe_text(str(result.error or "")) or None
        clipped = len(output) > max_chars
        if clipped:
            output = output[:max_chars] + "\n[observation clipped; use a narrower read range]"
        if error and len(error) > 1000:
            error = error[:1000] + "…"
        allowed_keys = {
            "operation", "path", "bytes", "beforeSha256", "afterSha256", "sha256",
            "changed", "reconciled", "currentSha256", "planAmendmentReason",
        }
        metadata = {
            key: item for key, item in (result.metadata or {}).items()
            if key in allowed_keys and isinstance(item, (str, int, float, bool, type(None)))
        }
        return ToolResult(result.tool, result.success, output, error, result.truncated or clipped, metadata)

    @classmethod
    def _stored_result(cls, result: ToolResult) -> dict[str, Any]:
        return cls._safe_result(result, 3000).as_dict()

    @classmethod
    def _record_result(cls, record: dict[str, Any]) -> ToolResult:
        raw = record.get("result") if isinstance(record.get("result"), dict) else {}
        result = ToolResult(
            str(raw.get("tool") or record.get("tool") or "coding_loop.recovery"),
            bool(raw.get("success", False)),
            str(raw.get("output") or ""),
            str(raw.get("error")) if raw.get("error") else None,
            bool(raw.get("truncated", False)),
            raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )
        return cls._safe_result(result, 3000)

    @classmethod
    def _history_observation(cls, record: dict[str, Any], maximum: int) -> dict[str, Any]:
        result = cls._safe_result(cls._record_result(record), maximum)
        return {
            "type": "tool_observation",
            "sequence": int(record.get("sequence_no") or 0),
            "status": str(record.get("status") or "UNKNOWN"),
            **result.as_dict(),
        }

    @staticmethod
    def _action_event(sequence: int, action_id: str, result: ToolResult) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "actionId": action_id,
            "tool": result.tool,
            "success": result.success,
            "error": result.error,
            "metadata": result.metadata,
        }
