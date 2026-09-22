"""Generic artifact-first generation for long or multi-file Agent outputs."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from math import ceil
from pathlib import PurePath
from typing import Any, Awaitable, Callable

from ..llm.base import LLMError, LLMResponse
from ..code_company.artifact_contracts import enforce_artifact_plan
from ..code_company.dependency_manifest import render_managed_artifact
from ..services.artifact_paths import is_safe_artifact_path, normalize_artifact_path
from .output_inspector import inspect_response
from .maven_contract import normalize_h2_flyway_dependency


RequestCall = Callable[[str, str, dict[str, Any], float], Awaitable[LLMResponse]]
RecordResponse = Callable[[LLMResponse], None]
EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LANGUAGE_BY_SUFFIX = {
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".vue": "vue",
    ".py": "python",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
    ".md": "markdown",
    ".txt": "text",
    ".svg": "svg",
    ".xml": "xml",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".sh": "bash",
    ".ps1": "powershell",
}
_LANGUAGE_ALIASES = {
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "py": "python",
    "yml": "yaml",
    "md": "markdown",
    "sh": "bash",
    "shell": "bash",
    "htm": "html",
}

# Artifact mode is intentionally file-scoped.  A runtime planner budget is a
# planning hint for the whole Agent step; it must not become a tiny hard cap
# for every independent source file.  The floor is an upper-bound request,
# not a guaranteed spend: providers still charge for tokens actually emitted.
ARTIFACT_FILE_MIN_TOKENS = 2048
ARTIFACT_FILE_MARGIN = 1.5
ARTIFACT_REPAIR_MARGIN = 1.35
ARTIFACT_PART_SPLIT_THRESHOLD = 1600
ARTIFACT_PART_MIN_TOKENS = 2048
ARTIFACT_PART_MARGIN = 2.0
ARTIFACT_MAX_PARTS = 8
ARTIFACT_CONTINUATION_MAX_ATTEMPTS = 2
ARTIFACT_PLAN_MIN_TOKENS = 1200
ARTIFACT_PLAN_COMPLEX_TOKENS = 1600
ARTIFACT_PLAN_RETRY_TOKENS = 2000


@dataclass(frozen=True, slots=True)
class ArtifactPartSpec:
    id: str
    purpose: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ArtifactFileSpec:
    name: str
    language: str
    purpose: str
    estimated_tokens: int
    parts: tuple[ArtifactPartSpec, ...] = ()
    owner: str = ""
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    depends_on_files: tuple[str, ...] = ()


@dataclass(slots=True)
class ArtifactGenerationResult:
    summary: str
    files: list[dict[str, str]]
    last_response: LLMResponse
    input_tokens: int
    output_tokens: int
    repair_count: int
    continuation_count: int = 0
    split_count: int = 0


@dataclass(slots=True)
class _ContentGenerationResult:
    content: str
    reason: str | None
    responses: list[LLMResponse]
    repair_count: int = 0
    continuation_count: int = 0


async def generate_artifacts(
    *,
    system_prompt: str,
    original_prompt: str,
    base_config: dict[str, Any],
    request_timeout: float,
    max_tokens: int,
    provider_max_tokens: int,
    request: RequestCall,
    record_response: RecordResponse,
    emit: EmitEvent,
) -> ArtifactGenerationResult:
    """Plan and generate independent files without replaying a whole response.

    The planner only returns a small manifest. Each file then gets its own
    request and can be repaired independently when the provider truncates or
    returns invalid structured content. The caller owns persistence and
    cancellation of the underlying request.
    """

    raw_request = request

    async def request_with_transport_retry(
        system: str, user: str, config: dict[str, Any], timeout: float,
    ) -> LLMResponse:
        # A file-level retry preserves the already planned and generated
        # files. The provider call has returned/raised before another starts;
        # the executor waits for cancellation cleanup on its timeout path.
        for attempt in range(2):
            try:
                return await raw_request(system, user, config, timeout)
            except LLMError as exc:
                reason = str(exc).lower()
                transport = any(marker in reason for marker in (
                    "timeout", "timed out", "network error", "connection", "rate limit", "429", "502", "503", "504",
                ))
                if attempt or not exc.retryable or not transport:
                    raise
                await emit("step.artifact_request_retrying", {
                    "phase": str(config.get("generation_phase") or "unknown"),
                    "fileName": str(config.get("artifact_name") or ""),
                    "attempt": 2,
                    "reason": str(exc)[:300],
                })
                await asyncio.sleep(0.5)
        raise AssertionError("unreachable transport retry state")

    request = request_with_transport_retry

    safe_max_tokens = max(1, min(int(max_tokens), int(provider_max_tokens)))
    planning_responses: list[LLMResponse] = []
    plan_budget = _artifact_plan_budget(original_prompt, safe_max_tokens, provider_max_tokens)
    plan_response = await request(
        _planning_system_prompt(),
        _planning_prompt(original_prompt),
        {
            **base_config,
            "generation_phase": "artifact_plan",
            "continuation": False,
            "max_tokens": plan_budget,
            "effective_thinking": "off",
            "thinking_type": "disabled",
            "reasoning_effort": "off",
        },
        request_timeout,
    )
    record_response(plan_response)
    planning_responses.append(plan_response)
    plan = parse_artifact_manifest(plan_response.text)
    if plan is None:
        retry_budget = max(
            plan_budget,
            min(int(provider_max_tokens), ARTIFACT_PLAN_RETRY_TOKENS),
        )
        await emit(
            "step.artifact_plan_recovering",
            {
                "reason": "成果物规划 JSON 不完整，正在使用精简清单自动重试",
                "finishReason": plan_response.finish_reason,
                "previousMaxTokens": plan_budget,
                "nextMaxTokens": retry_budget,
            },
        )
        plan_response = await request(
            _planning_system_prompt(),
            _planning_retry_prompt(original_prompt),
            {
                **base_config,
                "generation_phase": "artifact_plan_retry",
                "continuation": False,
                "max_tokens": retry_budget,
                "effective_thinking": "off",
                "thinking_type": "disabled",
                "reasoning_effort": "off",
            },
            request_timeout,
        )
        record_response(plan_response)
        planning_responses.append(plan_response)
        plan = parse_artifact_manifest(plan_response.text)

    plan_source = "provider"
    if plan is None:
        contract = base_config.get("delivery_contract")
        scope = _contract_technology_scope(contract) if isinstance(contract, dict) else original_prompt
        plan = _fallback_artifact_manifest(
            scope,
            str(base_config.get("agent_id") or ""),
            contract=base_config.get("delivery_contract"),
        )
        plan_source = "deterministic_fallback"
        await emit(
            "step.artifact_plan_fallback",
            {
                "reason": "Provider 两次未返回完整文件清单，已启用本地保守规划",
                "fileCount": len(plan),
            },
        )

    if bool(base_config.get("enforce_artifact_contract", False)):
        plan = _complete_agent_manifest(
            plan,
            original_prompt,
            str(base_config.get("agent_id") or ""),
            contract=base_config.get("delivery_contract"),
        )
        agent_name = str(base_config.get("agent_id") or "").lower()
        owner = next((name for name in ("database", "backend", "frontend") if name in agent_name), agent_name)
        plan, denied_files = enforce_artifact_plan(
            plan,
            owner=owner,
            compiled_contract=base_config.get("compiled_contract"),
            missing_factory=lambda row: ArtifactFileSpec(
                name=str(row["path"]),
                language=_LANGUAGE_BY_SUFFIX.get(PurePath(str(row["path"])).suffix.lower(), "text"),
                purpose=f"冻结文件计划要求的 {', '.join(row.get('provides') or [str(row['path'])])}",
                estimated_tokens=1024,
                owner=owner,
                provides=tuple(str(value) for value in row.get("provides") or []),
                requires=tuple(str(value) for value in row.get("requires") or []),
                depends_on_files=tuple(str(value) for value in row.get("depends_on_files") or []),
            ),
        )
        if denied_files:
            await emit("step.artifact_ownership_denied", {
                "owner": owner,
                "files": denied_files,
                "reason": "文件不属于当前 Agent 的冻结 Artifact Ownership，已在生成前拒绝",
            })

    await emit(
        "step.artifact_planned",
        {
            "files": [
                {
                    "name": item.name,
                    "purpose": item.purpose,
                    "estimatedTokens": item.estimated_tokens,
                    "owner": item.owner,
                    "provides": list(item.provides),
                    "requires": list(item.requires),
                    "dependsOnFiles": list(item.depends_on_files),
                    "parts": [
                        {"id": part.id, "purpose": part.purpose, "estimatedTokens": part.estimated_tokens}
                        for part in item.parts
                    ],
                }
                for item in plan
            ],
            "fileCount": len(plan),
            "planSource": plan_source,
        },
    )

    all_responses = planning_responses
    files: list[dict[str, str]] = []
    repair_count = 0
    continuation_count = 0
    split_count = 0
    generation_original_prompt = original_prompt
    for index, spec in enumerate(plan, start=1):
        hidden_reasoning_observed = _has_hidden_reasoning(all_responses)
        base_config["hidden_reasoning_observed"] = hidden_reasoning_observed
        file_budget = provider_max_tokens if hidden_reasoning_observed else _file_budget(spec, safe_max_tokens, provider_max_tokens)
        managed_content = render_managed_artifact(
            spec.name,
            (base_config.get("compiled_contract") or {}).get("dependency_manifest") or {},
        )
        if managed_content is not None:
            files.append(_artifact_file_record(spec, managed_content))
            await emit("step.artifact_manifest_compiled", {
                "fileName": spec.name,
                "fileIndex": index,
                "fileCount": len(plan),
                "source": "dependency_manifest_compiler",
            })
            continue
        completed_source = "\n".join(
            f"--- {item['name']} ---\n{item['content']}" for item in files
        )
        original_prompt = generation_original_prompt
        if completed_source:
            original_prompt += (
                "\n\n以下为本次已生成的真实兄弟文件。其依赖、包名、类型和公开方法签名是约束，"
                "必须按这些源码调用，禁止重新猜测接口：\n" + completed_source[:32000]
            )
        parts = spec.parts
        split_attempted = bool(parts)
        if not parts and _should_pre_split(spec, provider_max_tokens):
            split_response, planned_parts = await _request_split_plan(
                system_prompt=system_prompt,
                original_prompt=original_prompt,
                spec=spec,
                base_config=base_config,
                request_timeout=request_timeout,
                provider_max_tokens=provider_max_tokens,
                request=request,
            )
            record_response(split_response)
            all_responses.append(split_response)
            split_attempted = True
            if planned_parts:
                parts = planned_parts
                split_count += 1
                await emit(
                    "step.artifact_split_planned",
                    {
                        "fileName": spec.name,
                        "fileIndex": index,
                        "fileCount": len(plan),
                        "parts": [
                            {"id": part.id, "purpose": part.purpose, "estimatedTokens": part.estimated_tokens}
                            for part in parts
                        ],
                    },
                )

        if parts:
            part_result = await _generate_artifact_parts(
                system_prompt=system_prompt,
                original_prompt=original_prompt,
                plan=plan,
                spec=spec,
                parts=parts,
                file_index=index,
                file_count=len(plan),
                agent_max_tokens=safe_max_tokens,
                provider_max_tokens=provider_max_tokens,
                request_timeout=request_timeout,
                base_config=base_config,
                request=request,
                record_response=record_response,
                emit=emit,
            )
            all_responses.extend(part_result.responses)
            repair_count += part_result.repair_count
            continuation_count += part_result.continuation_count
            content, reason = part_result.content, part_result.reason
            response_for_error = part_result.responses[-1] if part_result.responses else plan_response
        else:
            allocation = file_budget
            await emit(
                "step.artifact_generating",
                {
                    "fileName": spec.name,
                    "fileIndex": index,
                    "fileCount": len(plan),
                    "maxTokens": allocation,
                    "agentMaxTokens": safe_max_tokens,
                    "budgetSource": "file_estimate",
                    "budgetRaised": allocation > safe_max_tokens,
                },
            )
            response = await request(
                system_prompt,
                _file_prompt(original_prompt, plan, spec),
                {
                    **base_config,
                    "generation_phase": "artifact_file",
                    "artifact_name": spec.name,
                    "artifact_index": index,
                    "artifact_total": len(plan),
                    "continuation": False,
                    "max_tokens": allocation,
                },
                request_timeout,
            )
            record_response(response)
            all_responses.append(response)
            base_config["hidden_reasoning_observed"] = _has_hidden_reasoning(all_responses)
            content, reason = _validated_file_content(response, spec)
            response_for_error = response

        if reason and not split_attempted:
            split_response, planned_parts = await _request_split_plan(
                system_prompt=system_prompt,
                original_prompt=original_prompt,
                spec=spec,
                base_config=base_config,
                request_timeout=request_timeout,
                provider_max_tokens=provider_max_tokens,
                request=request,
            )
            record_response(split_response)
            all_responses.append(split_response)
            split_attempted = True
            if planned_parts:
                parts = planned_parts
                split_count += 1
                await emit(
                    "step.artifact_split_planned",
                    {
                        "fileName": spec.name,
                        "fileIndex": index,
                        "fileCount": len(plan),
                        "parts": [
                            {"id": part.id, "purpose": part.purpose, "estimatedTokens": part.estimated_tokens}
                            for part in parts
                        ],
                    },
                )
                part_result = await _generate_artifact_parts(
                    system_prompt=system_prompt,
                    original_prompt=original_prompt,
                    plan=plan,
                    spec=spec,
                    parts=parts,
                    file_index=index,
                    file_count=len(plan),
                    agent_max_tokens=safe_max_tokens,
                    provider_max_tokens=provider_max_tokens,
                    request_timeout=request_timeout,
                    base_config=base_config,
                    request=request,
                    record_response=record_response,
                    emit=emit,
                )
                all_responses.extend(part_result.responses)
                repair_count += part_result.repair_count
                continuation_count += part_result.continuation_count
                content, reason = part_result.content, part_result.reason
                response_for_error = part_result.responses[-1] if part_result.responses else response_for_error

        if reason:
            allocation = provider_max_tokens if base_config.get("hidden_reasoning_observed") else file_budget
            repair_attempt = 1
            repair_count += 1
            suffix_repair = bool(content.strip()) and "输出上限" in reason
            await emit(
                "step.artifact_repairing",
                {
                    "fileName": spec.name,
                    "repairAttempt": repair_attempt,
                    "reason": reason,
                    "scope": "suffix" if suffix_repair else "file",
                },
            )
            response = await request(
                system_prompt,
                _suffix_repair_prompt(original_prompt, spec, content, reason) if suffix_repair else _repair_prompt(original_prompt, spec, content, reason),
                {
                    **base_config,
                    "generation_phase": "artifact_suffix_repair" if suffix_repair else "artifact_repair",
                    "artifact_name": spec.name,
                    "artifact_repair_attempt": repair_attempt,
                    "artifact_repair_scope": "suffix" if suffix_repair else "file",
                    "continuation": False,
                    "max_tokens": _repair_budget(allocation, provider_max_tokens),
                },
                request_timeout,
            )
            record_response(response)
            all_responses.append(response)
            if suffix_repair:
                suffix = _strip_code_fence(response.text)
                merged_content = f"{content}{suffix}" if suffix else content
                content, reason = _validated_file_content(
                    LLMResponse(text=merged_content, finish_reason="stop", message_content=merged_content),
                    spec,
                )
                if str(response.finish_reason or "").strip().lower() == "length":
                    reason = reason or "Provider 达到文件尾部补齐上限"
            else:
                content, reason = _validated_file_content(response, spec)
            response_for_error = response

        if reason:
            continuation_result = await _continue_artifact_content(
                system_prompt=system_prompt,
                original_prompt=original_prompt,
                spec=spec,
                content=content,
                reason=reason,
                allocation=provider_max_tokens if base_config.get("hidden_reasoning_observed") else file_budget,
                base_config=base_config,
                request_timeout=request_timeout,
                provider_max_tokens=provider_max_tokens,
                request=request,
                record_response=record_response,
                emit=emit,
            )
            all_responses.extend(continuation_result.responses)
            continuation_count += continuation_result.continuation_count
            content, reason = continuation_result.content, continuation_result.reason
            response_for_error = continuation_result.responses[-1] if continuation_result.responses else response_for_error

        if reason:
            raise LLMError(
                f"成果物 {spec.name} 定点修复和分块后仍不完整：{reason}",
                retryable=False,
                response_metadata=response_for_error.provider_record(),
            )
        if spec.name == "pom.xml":
            content, normalized = normalize_h2_flyway_dependency(content)
            if normalized:
                await emit("step.artifact_dependency_normalized", {
                    "fileName": spec.name,
                    "reason": "已将错误的 flyway-database-h2 声明纠正为 flyway-core，保留其他依赖。",
                })
        if PurePath(spec.name).suffix.lower() == ".java":
            content = _complete_spring_web_annotation_imports(content)
        files.append(_artifact_file_record(spec, content))
        await emit("step.artifact_validated", {"fileName": spec.name, "fileIndex": index, "fileCount": len(plan)})

    if "frontend" in str(base_config.get("agent_id") or "").lower():
        compiled = base_config.get("compiled_contract") or {}
        api_paths = list(((compiled.get("openapi") or {}).get("paths") or {})) if isinstance(compiled, dict) else []
        bootstrap = _ensure_vue_vite_config(files, api_paths=api_paths)
        if bootstrap:
            await emit("step.artifact_bootstrap_completed", {"fileName": bootstrap})

    summary = _summary(files, plan_response)
    return ArtifactGenerationResult(
        summary=summary,
        files=files,
        last_response=all_responses[-1],
        input_tokens=sum(item.input_tokens for item in all_responses),
        output_tokens=sum(item.output_tokens for item in all_responses),
        repair_count=repair_count,
        continuation_count=continuation_count,
        split_count=split_count,
    )


def parse_artifact_manifest(text: str) -> list[ArtifactFileSpec] | None:
    raw = _extract_json_object(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        return None
    result: list[ArtifactFileSpec] = []
    seen: set[str] = set()
    for item in raw["files"][:12]:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name") or item.get("path") or "").strip()
        raw_name = normalize_artifact_path(raw_name)
        if not raw_name or not is_safe_artifact_path(raw_name):
            continue
        if raw_name in seen:
            continue
        language = _normalize_language(str(item.get("language") or ""), raw_name)
        purpose = " ".join(str(item.get("purpose") or "").split())[:240] or "按用户需求生成的成果物文件"
        try:
            estimated = max(256, min(128000, int(item.get("estimated_tokens", 1024))))
        except (TypeError, ValueError):
            estimated = 1024
        parts = _parse_artifact_parts(item.get("parts"))
        provides = tuple(str(value) for value in item.get("provides") or [] if str(value).strip())
        requires = tuple(str(value) for value in item.get("requires") or [] if str(value).strip())
        dependencies = tuple(str(value) for value in item.get("depends_on_files") or [] if str(value).strip())
        result.append(ArtifactFileSpec(raw_name, language, purpose, estimated, parts, str(item.get("owner") or ""), provides, requires, dependencies))
        seen.add(raw_name)
    return result or None


def _artifact_file_record(spec: ArtifactFileSpec, content: str) -> dict[str, Any]:
    return {
        "name": spec.name,
        "language": spec.language,
        "purpose": spec.purpose,
        "content": content,
        "owner": spec.owner,
        "provides": list(spec.provides),
        "requires": list(spec.requires),
        "depends_on_files": list(spec.depends_on_files),
    }


def _parse_artifact_parts(raw_parts: Any) -> tuple[ArtifactPartSpec, ...]:
    """Parse optional logical source sections from the planner contract."""
    if not isinstance(raw_parts, list):
        return ()
    result: list[ArtifactPartSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_parts[:ARTIFACT_MAX_PARTS], start=1):
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or item.get("name") or f"part-{index}").strip()
        if not _SAFE_NAME.fullmatch(raw_id) or raw_id in seen:
            raw_id = f"part-{index}"
        try:
            estimated = max(256, min(128000, int(item.get("estimated_tokens", 768))))
        except (TypeError, ValueError):
            estimated = 768
        purpose = " ".join(str(item.get("purpose") or "").split())[:240] or f"第 {index} 个连续源码片段"
        result.append(ArtifactPartSpec(raw_id, purpose, estimated))
        seen.add(raw_id)
    return tuple(result) if len(result) >= 2 else ()


def _extract_json_object(text: str) -> Any:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(value):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _normalize_language(language: str, name: str) -> str:
    normalized = _LANGUAGE_ALIASES.get(language.strip().lower(), language.strip().lower())
    return normalized or _LANGUAGE_BY_SUFFIX.get(PurePath(name).suffix.lower(), "text")


async def _request_split_plan(
    *,
    system_prompt: str,
    original_prompt: str,
    spec: ArtifactFileSpec,
    base_config: dict[str, Any],
    request_timeout: float,
    provider_max_tokens: int,
    request: RequestCall,
) -> tuple[LLMResponse, tuple[ArtifactPartSpec, ...]]:
    response = await request(
        system_prompt,
        _split_prompt(original_prompt, spec),
        {
            **base_config,
            "generation_phase": "artifact_split_plan",
            "artifact_name": spec.name,
            "continuation": False,
            "max_tokens": min(700, provider_max_tokens),
            "effective_thinking": "off",
            "thinking_type": "disabled",
            "reasoning_effort": "off",
        },
        request_timeout,
    )
    raw = _extract_json_object(response.text)
    parts = _parse_artifact_parts(raw.get("parts") if isinstance(raw, dict) else None)
    return response, parts


async def _generate_artifact_parts(
    *,
    system_prompt: str,
    original_prompt: str,
    plan: list[ArtifactFileSpec],
    spec: ArtifactFileSpec,
    parts: tuple[ArtifactPartSpec, ...],
    file_index: int,
    file_count: int,
    agent_max_tokens: int,
    provider_max_tokens: int,
    request_timeout: float,
    base_config: dict[str, Any],
    request: RequestCall,
    record_response: RecordResponse,
    emit: EmitEvent,
) -> _ContentGenerationResult:
    responses: list[LLMResponse] = []
    contents: list[str] = []
    repair_count = 0
    continuation_count = 0
    for part_index, part in enumerate(parts, start=1):
        allocation = provider_max_tokens if base_config.get("hidden_reasoning_observed") else _part_budget(part, provider_max_tokens)
        await emit(
            "step.artifact_generating",
            {
                "fileName": spec.name,
                "fileIndex": file_index,
                "fileCount": file_count,
                "partId": part.id,
                "partIndex": part_index,
                "partCount": len(parts),
                "maxTokens": allocation,
                "agentMaxTokens": agent_max_tokens,
                "budgetSource": "part_estimate",
                "budgetRaised": allocation > agent_max_tokens,
            },
        )
        response = await request(
            system_prompt,
            _part_prompt(original_prompt, plan, spec, parts, part_index, part),
            {
                **base_config,
                "generation_phase": "artifact_part",
                "artifact_name": spec.name,
                "artifact_part_id": part.id,
                "artifact_part_index": part_index,
                "artifact_part_total": len(parts),
                "continuation": False,
                "max_tokens": allocation,
            },
            request_timeout,
        )
        record_response(response)
        responses.append(response)
        content, reason = _validated_part_content(response)
        if reason:
            repair_count += 1
            await emit(
                "step.artifact_repairing",
                {
                    "fileName": spec.name,
                    "partId": part.id,
                    "repairAttempt": repair_count,
                    "reason": reason,
                    "scope": "part",
                },
            )
            response = await request(
                system_prompt,
                _part_repair_prompt(original_prompt, spec, part, content, reason),
                {
                    **base_config,
                    "generation_phase": "artifact_part_repair",
                    "artifact_name": spec.name,
                    "artifact_part_id": part.id,
                    "artifact_part_repair_attempt": 1,
                    "continuation": False,
                    "max_tokens": _repair_budget(allocation, provider_max_tokens),
                },
                request_timeout,
            )
            record_response(response)
            responses.append(response)
            content, reason = _validated_part_content(response)
        if reason:
            continuation_result = await _continue_artifact_content(
                system_prompt=system_prompt,
                original_prompt=original_prompt,
                spec=spec,
                content=content,
                reason=reason,
                allocation=allocation,
                base_config=base_config,
                request_timeout=request_timeout,
                provider_max_tokens=provider_max_tokens,
                request=request,
                record_response=record_response,
                emit=emit,
                part=part,
            )
            responses.extend(continuation_result.responses)
            continuation_count += continuation_result.continuation_count
            content, reason = continuation_result.content, continuation_result.reason
        if reason:
            return _ContentGenerationResult(
                content="\n".join([*contents, content]).strip(),
                reason=reason,
                responses=responses,
                repair_count=repair_count,
                continuation_count=continuation_count,
            )
        contents.append(content)

    merged = "\n".join(contents).strip()
    inspection_content, reason = _validated_file_content(
        LLMResponse(text=merged, finish_reason="stop", message_content=merged),
        spec,
    )
    return _ContentGenerationResult(
        content=inspection_content,
        reason=reason,
        responses=responses,
        repair_count=repair_count,
        continuation_count=continuation_count,
    )


async def _continue_artifact_content(
    *,
    system_prompt: str,
    original_prompt: str,
    spec: ArtifactFileSpec,
    content: str,
    reason: str,
    allocation: int,
    base_config: dict[str, Any],
    request_timeout: float,
    provider_max_tokens: int,
    request: RequestCall,
    record_response: RecordResponse,
    emit: EmitEvent,
    part: ArtifactPartSpec | None = None,
) -> _ContentGenerationResult:
    current = content.strip()
    responses: list[LLMResponse] = []
    last_reason = reason
    for attempt in range(1, ARTIFACT_CONTINUATION_MAX_ATTEMPTS + 1):
        await emit(
            "step.artifact_continuing",
            {
                "fileName": spec.name,
                "partId": part.id if part else None,
                "continuationAttempt": attempt,
                "reason": last_reason,
            },
        )
        response = await request(
            system_prompt,
            _continuation_prompt(original_prompt, spec, current, last_reason, part),
            {
                **base_config,
                "generation_phase": "artifact_continuation",
                "artifact_name": spec.name,
                "artifact_part_id": part.id if part else None,
                "artifact_continuation_attempt": attempt,
                "continuation": True,
                "max_tokens": min(provider_max_tokens, max(allocation, ARTIFACT_PART_MIN_TOKENS)),
            },
            request_timeout,
        )
        record_response(response)
        responses.append(response)
        addition = _strip_code_fence(response.text)
        if addition.strip():
            current = f"{current}\n{addition}".strip() if current else addition.strip()
        if not addition.strip():
            last_reason = "续写没有返回新增内容"
            break
        inspection_response = LLMResponse(text=current, finish_reason="stop", message_content=current)
        if part:
            inspection_content, last_reason = _validated_part_content(inspection_response)
        else:
            inspection_content, last_reason = _validated_file_content(inspection_response, spec)
        current = inspection_content
        if not last_reason and str(response.finish_reason or "").strip().lower() != "length":
            return _ContentGenerationResult(current, None, responses, continuation_count=attempt)
        last_reason = last_reason or "Provider 续写仍达到输出上限"
    inspection_response = LLMResponse(text=current, finish_reason="stop", message_content=current)
    if part:
        inspection_content, final_reason = _validated_part_content(inspection_response)
    else:
        inspection_content, final_reason = _validated_file_content(inspection_response, spec)
    return _ContentGenerationResult(
        inspection_content,
        final_reason or last_reason,
        responses,
        continuation_count=len(responses),
    )


def _has_hidden_reasoning(responses: list[LLMResponse]) -> bool:
    for response in responses:
        if str(response.reasoning_content or "").strip():
            return True
        usage = response.usage if isinstance(response.usage, dict) else {}
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
        if isinstance(details, dict) and int(details.get("reasoning_tokens") or 0) > 0:
            return True
    return False


def _part_budget(part: ArtifactPartSpec, provider_max_tokens: int) -> int:
    # A split planner's estimates are often optimistic for framework files
    # such as App.vue.  A generous per-part ceiling does not force the model
    # to consume it, but prevents avoidable repair/continuation calls.
    estimated_with_margin = ceil(max(256, part.estimated_tokens) * ARTIFACT_PART_MARGIN)
    return max(1, min(provider_max_tokens, max(ARTIFACT_PART_MIN_TOKENS, estimated_with_margin)))


def _should_pre_split(spec: ArtifactFileSpec, provider_max_tokens: int) -> bool:
    """Split only when a whole file is unlikely to fit one provider response.

    Splitting medium files merely because they exceed a small fixed threshold
    makes framework syntax span multiple independent responses.  Prefer one
    complete file request whenever the estimate plus safety margin fits the
    provider window; a real `finish_reason=length` can still trigger the
    existing split fallback afterward.
    """
    provider_cap = max(1, int(provider_max_tokens))
    projected = ceil(max(256, spec.estimated_tokens) * ARTIFACT_FILE_MARGIN)
    safety_boundary = max(ARTIFACT_PART_SPLIT_THRESHOLD, int(provider_cap * 0.85))
    return projected >= safety_boundary


def _file_budget(spec: ArtifactFileSpec, agent_max_tokens: int, provider_max_tokens: int) -> int:
    """Choose a cap for one file, independent from the Agent step budget.

    ``agent_max_tokens`` is retained for diagnostics only.  In artifact mode,
    each file is a separate provider request, so constraining every file to
    the planner's small whole-step recommendation would recreate the exact
    truncation problem this mode is designed to prevent.
    """
    del agent_max_tokens
    estimated_with_margin = ceil(max(256, spec.estimated_tokens) * ARTIFACT_FILE_MARGIN)
    return max(1, min(provider_max_tokens, max(ARTIFACT_FILE_MIN_TOKENS, estimated_with_margin)))


def _repair_budget(allocation: int, provider_max_tokens: int) -> int:
    """Give a targeted repair at least the original file budget."""
    return max(1, min(provider_max_tokens, max(ARTIFACT_FILE_MIN_TOKENS, allocation, ceil(allocation * ARTIFACT_REPAIR_MARGIN))))


def _validated_part_content(response: LLMResponse) -> tuple[str, str | None]:
    content = _strip_code_fence(response.text)
    if not content.strip():
        return "", "Agent 未返回源码片段"
    if str(response.finish_reason or "").strip().lower() == "length":
        return content, "Provider 达到单片段输出上限"
    return content, None


def _validated_file_content(response: LLMResponse, spec: ArtifactFileSpec) -> tuple[str, str | None]:
    content = _strip_code_fence(response.text)
    if PurePath(spec.name).suffix.lower() in {
        ".java", ".js", ".mjs", ".ts", ".tsx", ".vue", ".css",
        ".json", ".yaml", ".yml", ".xml", ".sql", ".py",
    }:
        # A split/continued source file can retain an orphan Markdown closing
        # fence even when the whole response is not a valid fenced block.
        content = re.sub(r"(?:\r?\n)?\s*```\s*$", "", content).strip()
        content = re.sub(r"^```[^\r\n]*\r?\n", "", content).strip()
    if not content.strip():
        return "", "Agent 未返回文件内容"
    if str(response.finish_reason or "").strip().lower() == "length":
        return content, "Provider 达到单文件输出上限"
    output_format = "html" if PurePath(spec.name).suffix.lower() in {".html", ".htm"} else "json" if PurePath(spec.name).suffix.lower() == ".json" else "text"
    inspection = inspect_response(
        LLMResponse(
            text=content,
            finish_reason=response.finish_reason,
            message_content=content,
        ),
        output_format,
    )
    if not inspection.complete:
        return content, inspection.reason or "文件结构校验未通过"
    return content, None


def _strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    match = re.fullmatch(r"```[^\r\n]*\r?\n(?P<body>.*?)\r?\n?```", value, flags=re.DOTALL)
    return match.group("body").strip() if match else value


def _ensure_vue_vite_config(files: list[dict[str, str]], *, api_paths: list[str] | None = None) -> str | None:
    """Complete standard Vue/Vite wiring omitted by a provider manifest.

    The Vite plugin is an installation-time/build-time contract, not business
    code.  Do not overwrite a custom config or guess a dependency version.
    """
    by_name = {item["name"]: item for item in files}
    if "src/App.vue" not in by_name or any(
        name in by_name for name in ("vite.config.js", "vite.config.mjs", "vite.config.ts")
    ):
        return None
    package_file = by_name.get("package.json")
    if package_file is None:
        return None
    try:
        package = json.loads(package_file["content"])
    except (TypeError, ValueError):
        return None
    if not isinstance(package, dict):
        return None
    dependencies = package.get("dependencies") or {}
    dev_dependencies = package.get("devDependencies") or {}
    if not isinstance(dependencies, dict) or not isinstance(dev_dependencies, dict):
        return None
    if "vue" not in dependencies or "vite" not in dev_dependencies:
        return None
    if "@vitejs/plugin-vue" not in {**dependencies, **dev_dependencies}:
        vite_version = str(dev_dependencies["vite"])
        vite_major = re.search(r"\d+", vite_version)
        if vite_major is None or vite_major.group() not in {"5", "6"}:
            return None
        dev_dependencies["@vitejs/plugin-vue"] = "^5.0.4"
        package["devDependencies"] = dev_dependencies
        package_file["content"] = json.dumps(package, ensure_ascii=False, indent=2) + "\n"
    proxy_paths = sorted({
        "/" + path.strip("/").split("/", 1)[0]
        for path in (api_paths or ["/api"])
        if isinstance(path, str) and path.startswith("/") and path != "/"
    })
    proxy_entries = "\n".join(
        f"      '{prefix}': process.env.VITE_API_PROXY || 'http://127.0.0.1:2198',"
        for prefix in proxy_paths
    )
    files.append({
        "name": "vite.config.js",
        "language": "javascript",
        "purpose": "Vue SFC plugin and local API proxy",
        "content": (
            "import { defineConfig } from 'vite';\n"
            "import vue from '@vitejs/plugin-vue';\n\n"
            "export default defineConfig({\n"
            "  plugins: [vue()],\n"
            f"  server: {{ proxy: {{\n{proxy_entries}\n  }} }},\n"
            "});\n"
        ),
    })
    return "vite.config.js"


def _complete_spring_web_annotation_imports(content: str) -> str:
    """Add only unambiguous Spring Web annotation imports used in Java source."""
    annotation_names = {
        "RestController", "RequestMapping", "GetMapping", "PostMapping",
        "PutMapping", "PatchMapping", "DeleteMapping", "RequestBody",
        "PathVariable", "RequestParam", "CrossOrigin",
    }
    if "import org.springframework.web.bind.annotation.*;" in content:
        return content
    used = set(re.findall(r"(?<![\w.])@([A-Za-z][A-Za-z0-9]*)\b", content))
    missing = sorted(
        name for name in used & annotation_names
        if f"import org.springframework.web.bind.annotation.{name};" not in content
    )
    if not missing:
        return content
    imports = "".join(
        f"import org.springframework.web.bind.annotation.{name};\n" for name in missing
    )
    package = re.search(r"(?m)^\s*package\s+[A-Za-z_][\w.]*\s*;\s*\n", content)
    if package:
        return content[:package.end()] + "\n" + imports + content[package.end():]
    return imports + "\n" + content


def _artifact_plan_budget(original_prompt: str, safe_max_tokens: int, provider_max_tokens: int) -> int:
    """Allocate a small but realistic control budget for a file manifest."""
    normalized = str(original_prompt or "").lower()
    complexity_signals = (
        "springboot",
        "spring boot",
        "vue",
        "react",
        "前后端",
        "full-stack",
        "full stack",
        "crud",
        "curd",
        "增删改查",
        "数据库",
    )
    signal_count = sum(1 for signal in complexity_signals if signal in normalized)
    target = (
        ARTIFACT_PLAN_COMPLEX_TOKENS
        if signal_count >= 2 or len(normalized) >= 6000
        else ARTIFACT_PLAN_MIN_TOKENS
    )
    provider_cap = max(1, int(provider_max_tokens))
    task_cap = max(1, int(safe_max_tokens))
    planning_floor = min(provider_cap, ARTIFACT_PLAN_MIN_TOKENS)
    return max(1, min(provider_cap, max(planning_floor, min(task_cap, target))))


def _planning_system_prompt() -> str:
    return (
        "你是成果物文件清单规划器。你的唯一职责是返回一个简短、完整、可解析的 JSON 对象。"
        "禁止输出源码、Markdown、解释、标题、代码围栏或 JSON 之外的任何文字。"
        "不要在总清单中拆分源码片段；复杂文件会由后续步骤单独拆分。"
    )


def _bounded_planning_context(original_prompt: str) -> str:
    value = str(original_prompt or "").strip()
    if len(value) <= 12000:
        return value
    return f"{value[:6000]}\n\n[中间上下文已压缩]\n\n{value[-6000:]}"


def _planning_prompt(original_prompt: str) -> str:
    return (
        f"{_bounded_planning_context(original_prompt)}\n\n"
        "只规划当前 Agent 负责的可交付文件，输出："
        '{"files":[{"name":"src/App.vue","language":"vue","purpose":"主页面与交互","estimated_tokens":1800}]}。'
        "只允许 files 数组；每项只允许 name、language、purpose、estimated_tokens。"
        "文件名必须是安全的相对路径（例如 src/main.js），禁止绝对路径、.. 和路径穿越；purpose 不超过 30 个字，只列真正需要的文件，最多 12 个。"
        "不要输出 parts；不要生成源码正文。"
    )


def _planning_retry_prompt(original_prompt: str) -> str:
    return (
        f"{_bounded_planning_context(original_prompt)}\n\n"
        "上一次文件清单没有形成完整 JSON。现在只输出最小文件清单，最多 12 项："
        '{"files":[{"name":"src/App.vue","language":"vue","estimated_tokens":1800}]}。'
        "每项仅保留 name、language、estimated_tokens；name 可以是安全的相对路径；不要 purpose、parts、源码、Markdown 或解释。"
    )


def _fallback_artifact_manifest(original_prompt: str, agent_id: str, *, contract: dict[str, Any] | None = None) -> list[ArtifactFileSpec]:
    """Build a conservative manifest so planner formatting cannot fail a run."""
    normalized = str(original_prompt or "").lower()
    normalized_agent = str(agent_id or "").lower()

    if "frontend" in normalized_agent:
        if "vue" in normalized:
            return [
                ArtifactFileSpec("package.json", "json", "Vue 项目依赖与脚本", 700),
                ArtifactFileSpec("index.html", "html", "Vue 页面入口", 500),
                ArtifactFileSpec("src/main.js", "javascript", "Vue 应用启动入口", 600),
                ArtifactFileSpec("src/App.vue", "vue", "主要页面和 CRUD 交互", 2400),
                ArtifactFileSpec("src/style.css", "css", "页面布局与视觉样式", 1200),
            ]
        return [
            ArtifactFileSpec("index.html", "html", "页面结构", 1200),
            ArtifactFileSpec("style.css", "css", "页面样式", 900),
            ArtifactFileSpec("script.js", "javascript", "页面交互", 1200),
        ]

    if "database" in normalized_agent:
        if "springboot" in normalized or "spring boot" in normalized:
            return [
                ArtifactFileSpec("src/main/resources/schema.sql", "sql", "Spring Boot schema", 900),
                ArtifactFileSpec("src/main/resources/data.sql", "sql", "Optional seed data", 700),
            ]
        return [
            ArtifactFileSpec("database/schema.sql", "sql", "Database schema", 1200),
            ArtifactFileSpec("database/README.md", "markdown", "Database model notes", 700),
        ]

    if "backend" in normalized_agent:
        if "springboot" in normalized or "spring boot" in normalized:
            entity = "Student" if "学生" in normalized or "student" in normalized else "DomainEntity"
            if contract and contract.get("entities"):
                proposed = str(contract["entities"][0].get("name", ""))
                if re.fullmatch(r"[A-Z][A-Za-z0-9_]*", proposed):
                    entity = proposed
            return [
                ArtifactFileSpec("pom.xml", "xml", "Spring Boot 依赖、H2 测试数据库与构建配置", 1000),
                ArtifactFileSpec("src/main/java/com/example/studentmanagement/Application.java", "java", "应用启动入口", 500),
                ArtifactFileSpec(f"src/main/java/com/example/studentmanagement/{entity}.java", "java", "领域实体", 900),
                ArtifactFileSpec(f"src/main/java/com/example/studentmanagement/{entity}Repository.java", "java", "数据访问接口", 700),
                ArtifactFileSpec(f"src/main/java/com/example/studentmanagement/{entity}Service.java", "java", "CRUD 业务逻辑", 1600),
                ArtifactFileSpec(f"src/main/java/com/example/studentmanagement/{entity}Controller.java", "java", "REST CRUD 接口", 1500),
                ArtifactFileSpec("src/main/resources/application.yml", "yaml", "可由 H2 联调参数覆盖的运行与数据库配置", 600),
            ]
        return [
            ArtifactFileSpec("main.py", "python", "后端应用入口与 API", 1800),
            ArtifactFileSpec("models.py", "python", "数据模型", 1000),
            ArtifactFileSpec("service.py", "python", "业务逻辑", 1400),
            ArtifactFileSpec("requirements.txt", "text", "运行依赖", 400),
        ]

    return [ArtifactFileSpec("implementation.md", "markdown", "当前 Agent 的交付结果", 1600)]


def _contract_technology_scope(contract: dict[str, Any]) -> str:
    return " ".join([str(contract.get("backend_stack", "")), str(contract.get("frontend_stack", "")), "CRUD" if contract.get("crud_required") else ""])


def _complete_agent_manifest(
    plan: list[ArtifactFileSpec],
    original_prompt: str,
    agent_id: str,
    *, contract: dict[str, Any] | None = None,
) -> list[ArtifactFileSpec]:
    """Normalize a provider manifest into a minimally runnable agent contract.

    A planner is allowed to optimize the file list, but it is not allowed to
    omit the bootstrap files needed by the technology it just selected or to
    assign another Agent's files to the current Agent.  This is deliberately
    capability/technology based; the executor does not contain an industry
    specific workflow.
    """
    normalized = (_contract_technology_scope(contract) if isinstance(contract, dict) else str(original_prompt or "")).lower()
    normalized_agent = str(agent_id or "").lower()
    current = list(plan)
    if isinstance(contract, dict) and "frontend" in normalized_agent and contract.get("page_mode") in {"static", "static_rest"}:
        root = "src/main/resources/static/" if contract["page_mode"] == "static_rest" else ""
        plain = []
        for item in current:
            name = item.name.removeprefix("frontend/")
            if name.endswith(".md"):
                plain.append(item)
                continue
            if name in {"package.json", "package-lock.json", "pnpm-lock.yaml"} or "vite.config" in name or name.startswith(("tests/", "test/")) or name.endswith((".vue", ".jsx", ".tsx", ".ts")):
                continue
            if root and not name.startswith(root):
                name = root + name
            plain.append(replace(item, name=name))
        if not any(item.name == root + "index.html" for item in plain):
            plain.insert(0, ArtifactFileSpec(root + "index.html", "html", "合同指定的页面入口与交互", 1600))
        current = plain

    def normalize_agent_path(item: ArtifactFileSpec) -> ArtifactFileSpec:
        name = item.name.replace("\\", "/")
        lowered = name.lower()
        if "frontend" in normalized_agent:
            if lowered.startswith("frontend/"):
                name = name[len("frontend/") :]
            if name.lower() == "readme.md":
                name = "frontend/README.md"
        elif "database" in normalized_agent:
            if "spring boot" in normalized or "springboot" in normalized:
                if lowered.startswith("database/"):
                    name = name[len("database/") :]
                if PurePath(name).name.lower() in {"schema.sql", "data.sql"}:
                    name = f"src/main/resources/{PurePath(name).name}"
                elif PurePath(name).name.lower() == "readme.md":
                    name = "database/README.md"
            elif lowered in {"schema.sql", "data.sql"}:
                name = f"database/{name}"
            elif PurePath(name).name.lower() == "readme.md" and not lowered.startswith("database/"):
                name = "database/README.md"
        elif "backend" in normalized_agent:
            if lowered.startswith("backend/"):
                name = name[len("backend/") :]
            if name.lower() == "readme.md":
                name = "backend/README.md"
        return replace(item, name=name)

    current = [normalize_agent_path(item) for item in current]

    frontend_extensions = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".vue"}
    backend_runtime_config_names = {
        "application.yml",
        "application.yaml",
        "application.properties",
    }
    frontend_project_manifests = {
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "tsconfig.json", "vite.config.json",
    }

    def is_cross_agent_file(item: ArtifactFileSpec) -> bool:
        lowered_name = item.name.lower()
        suffix = PurePath(item.name).suffix.lower()
        if "frontend" in normalized_agent:
            return lowered_name.startswith("backend/") or suffix in {".java", ".kt", ".properties", ".yml", ".yaml", ".sql", ".xml"}
        if "database" in normalized_agent:
            # Database owns schema, seed data and migrations. Spring Boot
            # runtime configuration belongs to Backend even when it contains
            # datasource settings. Letting both Agents own application.yml
            # creates two registrations for the same project path.
            return (
                lowered_name.startswith("frontend/")
                or PurePath(lowered_name).name in frontend_project_manifests
                or PurePath(lowered_name).name in backend_runtime_config_names
                or suffix in {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".vue", ".java", ".kt", ".py"}
            )
        if "backend" in normalized_agent:
            return (
                lowered_name.startswith("frontend/")
                or (("spring boot" in normalized or "springboot" in normalized)
                    and PurePath(lowered_name).name in frontend_project_manifests)
                or suffix in frontend_extensions
                or ("database contract files" in normalized and suffix == ".sql")
            )
        return False

    current = [item for item in current if not is_cross_agent_file(item)]
    if "frontend" in normalized_agent and "vue" in normalized:
        frontend_root_map = {
            "main.js": "src/main.js",
            "main.ts": "src/main.ts",
            "App.vue": "src/App.vue",
            "style.css": "src/style.css",
        }
        current = [replace(item, name=frontend_root_map.get(item.name, item.name)) for item in current]
    if "backend" in normalized_agent and ("spring boot" in normalized or "springboot" in normalized):
        backend_root = "src/main/java/com/example/studentmanagement"
        current = [
            replace(
                item,
                name=(
                    f"{backend_root}/{PurePath(item.name).name}"
                    if PurePath(item.name).suffix.lower() == ".java" and "/" not in item.name
                    else f"src/main/resources/{PurePath(item.name).name}"
                    if PurePath(item.name).name.lower() in {"application.yml", "application.yaml", "application.properties"} and "/" not in item.name
                    else item.name
                ),
            )
            for item in current
        ]

    # Provider manifests can contain both `frontend/package.json` and
    # `package.json` (or the backend equivalents).  Normalize first, then keep
    # only one owner for each path so parallel Agents cannot create duplicate
    # Artifact registrations.
    deduplicated: list[ArtifactFileSpec] = []
    seen_names: set[str] = set()
    for item in current:
        if item.name in seen_names:
            continue
        deduplicated.append(item)
        seen_names.add(item.name)
    current = deduplicated
    names = {item.name for item in current}

    def add(name: str, language: str, purpose: str, estimated_tokens: int) -> None:
        if name not in names:
            current.append(ArtifactFileSpec(name, language, purpose, estimated_tokens))
            names.add(name)

    if "frontend" in normalized_agent and "vue" in normalized:
        add("package.json", "json", "Vue 项目依赖与启动脚本", 700)
        add("index.html", "html", "前端页面入口", 500)
        add("src/main.js", "javascript", "Vue 应用启动入口", 600)
        add("src/App.vue", "vue", "主页面与用户交互", 2400)
        add("src/style.css", "css", "页面布局与视觉样式", 1200)

    if "backend" in normalized_agent and ("spring boot" in normalized or "springboot" in normalized):
        add("pom.xml", "xml", "Spring Boot 依赖、H2 测试数据库与构建配置", 1000)
        if not any(PurePath(name).suffix.lower() == ".java" and "application" in PurePath(name).stem.lower() for name in names):
            package_root = _manifest_java_root(current)
            add(f"{package_root}/Application.java", "java", "Spring Boot 应用启动入口", 500)

        crud_signals = ("crud", "curd", "增删改查", "管理系统", "student management", "学生管理")
        if any(signal in normalized for signal in crud_signals):
            entity = _manifest_entity_name(current)
            package_root = _manifest_java_root(current)
            java_stems = {
                PurePath(name).stem.lower()
                for name in names
                if PurePath(name).suffix.lower() == ".java"
            }
            required_java = (
                (entity, "领域实体与数据字段", 900),
                (f"{entity}Repository", "数据访问接口", 700),
                (f"{entity}Service", "CRUD 业务逻辑", 1600),
                (f"{entity}Controller", "REST CRUD 接口", 1500),
            )
            for stem, purpose, estimated_tokens in required_java:
                if stem.lower() not in java_stems:
                    add(_manifest_java_target_path(current, package_root, stem, entity), "java", purpose, estimated_tokens)
                    java_stems.add(stem.lower())

        if not any(PurePath(name).name.lower() in {"application.yml", "application.yaml", "application.properties"} for name in names):
            add("src/main/resources/application.yml", "yaml", "可由 H2 联调参数覆盖的运行与数据源配置", 600)

    if "database" in normalized_agent:
        if "spring boot" in normalized or "springboot" in normalized:
            add("src/main/resources/schema.sql", "sql", "Spring Boot database schema", 1200)
        else:
            add("database/schema.sql", "sql", "Database schema and constraints", 1200)

    return current


def _manifest_entity_name(plan: list[ArtifactFileSpec]) -> str:
    """Reuse the provider's domain entity name when it supplied one."""
    excluded = ("application", "controller", "service", "repository", "config", "dto")
    for item in plan:
        path = PurePath(item.name)
        if path.suffix.lower() != ".java":
            continue
        stem = path.stem
        if stem and not any(stem.lower().endswith(suffix) for suffix in excluded):
            return stem
    return "DomainEntity"


def _manifest_java_root(plan: list[ArtifactFileSpec]) -> str:
    """Keep generated Spring files in the package root chosen by the planner."""
    marker = ("src", "main", "java")
    java_parents: list[PurePath] = []
    for item in plan:
        path = PurePath(item.name)
        if path.suffix.lower() != ".java":
            continue
        parts = path.parts
        for index in range(len(parts) - len(marker)):
            if tuple(part.lower() for part in parts[index : index + len(marker)]) == marker:
                parent = PurePath(*parts[: index + len(marker)], *parts[index + len(marker) : -1])
                if "application" in path.stem.lower():
                    return parent.as_posix()
                java_parents.append(parent)
                break
    layer_names = {"entity", "model", "repository", "service", "controller", "dto", "config"}
    for parent in java_parents:
        if parent.name.lower() in layer_names:
            return parent.parent.as_posix()
    if java_parents:
        return java_parents[0].as_posix()
    return "src/main/java/com/example/studentmanagement"


def _manifest_java_target_path(
    plan: list[ArtifactFileSpec],
    package_root: str,
    stem: str,
    entity: str,
) -> str:
    """Follow an existing layered Spring package layout when adding a missing role."""
    layer_names = {"entity", "model", "repository", "service", "controller"}
    uses_layers = any(
        PurePath(item.name).suffix.lower() == ".java"
        and PurePath(item.name).parent.name.lower() in layer_names
        for item in plan
    )
    if not uses_layers:
        return f"{package_root}/{stem}.java"
    lowered = stem.lower()
    if lowered == entity.lower():
        layer = "entity"
    elif lowered.endswith("repository"):
        layer = "repository"
    elif lowered.endswith("service"):
        layer = "service"
    elif lowered.endswith("controller"):
        layer = "controller"
    else:
        layer = "model"
    return f"{package_root}/{layer}/{stem}.java"


def _file_prompt(original_prompt: str, plan: list[ArtifactFileSpec], spec: ArtifactFileSpec) -> str:
    manifest = ", ".join(item.name for item in plan)
    return (
        f"{original_prompt}\n\n"
        f"当前处于成果物分块生成阶段。请只输出文件 `{spec.name}` 的完整原文，不要 Markdown 代码围栏、解释、标题或省略号。"
        f"该文件职责：{spec.purpose}。本次成果物文件清单：{manifest}。"
        "必须在本次响应内完成当前文件，并保持与其他文件的引用名称一致。"
    )


def _split_prompt(original_prompt: str, spec: ArtifactFileSpec) -> str:
    return (
        f"{original_prompt}\n\n"
        f"当前文件 `{spec.name}` 预计约 {spec.estimated_tokens} Token，单次生成可能过长。"
        "请只输出一个 JSON 对象，将这个文件拆成 2 到 8 个按顺序拼接的连续源码片段："
        '{"parts":[{"id":"part-1","purpose":"...","estimated_tokens":800}]}。'
        "每个片段必须有清晰职责和合理边界；第一段包含必要的开头声明，最后一段包含必要的收尾。"
        "片段会按顺序直接合并，不能互相重复，也不能把同一个函数、规则或标签拆在不明确的位置。"
    )


def _part_prompt(
    original_prompt: str,
    plan: list[ArtifactFileSpec],
    spec: ArtifactFileSpec,
    parts: tuple[ArtifactPartSpec, ...],
    part_index: int,
    part: ArtifactPartSpec,
) -> str:
    manifest = ", ".join(item.name for item in plan)
    part_manifest = ", ".join(item.id for item in parts)
    return (
        f"{original_prompt}\n\n"
        f"当前处于成果物分块任务阶段。只输出文件 `{spec.name}` 的连续源码片段 `{part.id}`，"
        "不要 Markdown 代码围栏、解释、标题或省略号。"
        f"这是第 {part_index} / {len(parts)} 段；当前文件片段顺序为：{part_manifest}。"
        f"本片段职责：{part.purpose}。本次文件清单：{manifest}。"
        "片段会按顺序与同一文件的其他片段直接合并，因此不要重复其他片段的内容；"
        "第一段负责必要的开头声明，最后一段负责必要的收尾。"
    )


def _part_repair_prompt(
    original_prompt: str,
    spec: ArtifactFileSpec,
    part: ArtifactPartSpec,
    content: str,
    reason: str,
) -> str:
    return (
        f"{original_prompt}\n\n"
        f"当前处于成果物片段 `{spec.name}` / `{part.id}` 的定点修复阶段。"
        "请只重新输出这个连续源码片段的完整内容，不要 Markdown 代码围栏、解释或省略号。"
        f"上一次片段未通过检查，原因：{reason}。不要复制截断标记。\n"
        f"<previous_part>\n{content}\n</previous_part>"
    )


def _continuation_prompt(
    original_prompt: str,
    spec: ArtifactFileSpec,
    content: str,
    reason: str,
    part: ArtifactPartSpec | None,
) -> str:
    scope = f"文件 `{spec.name}` 的片段 `{part.id}`" if part else f"文件 `{spec.name}`"
    return (
        f"{original_prompt}\n\n"
        f"这是最后一级兜底续写。{scope}之前已经生成了一部分内容，但仍未通过完整性检查：{reason}。"
        "请只输出紧接在已有内容末尾之后的缺失源码，不要重复已有内容，不要 Markdown 围栏、解释或省略号。"
        "如果已经到达正确结尾，也只输出缺失的收尾内容。\n"
        f"<generated_so_far>\n{content}\n</generated_so_far>"
    )


def _repair_prompt(original_prompt: str, spec: ArtifactFileSpec, content: str, reason: str) -> str:
    return (
        f"{original_prompt}\n\n"
        f"当前处于成果物定点修复阶段。请重新输出文件 `{spec.name}` 的完整原文，不要 Markdown 代码围栏、解释或省略号。"
        f"上一次文件未通过检查，原因：{reason}。请修复后一次性输出完整文件。"
        f"上一次已生成内容仅用于定位问题，不要复制截断标记：\n<previous_file>\n{content}\n</previous_file>"
    )


def _suffix_repair_prompt(original_prompt: str, spec: ArtifactFileSpec, content: str, reason: str) -> str:
    tail = content[-12000:]
    omitted = max(0, len(content) - len(tail))
    return (
        f"{original_prompt}\n\n"
        f"当前处于成果物 `{spec.name}` 的尾部补齐阶段。上一次响应已经生成文件前半部分，"
        f"但在输出上限处中断（{reason}）。请只输出从中断位置开始缺失的后缀内容，绝对不要重新输出整个文件。"
        "不要 Markdown 代码围栏、解释、标题或省略号；不要重复已有内容；请补齐所有未闭合的标签、括号和结尾。"
        f"以下是已有文件末尾 {len(tail)} 个字符（前面省略 {omitted} 个字符），请从它的最后一个字符继续：\n"
        f"<generated_tail>\n{tail}\n</generated_tail>"
    )


def _summary(files: list[dict[str, str]], plan_response: LLMResponse) -> str:
    names = "、".join(item["name"] for item in files)
    return f"成果物已按文件独立生成并完成结构校验：{names}。规划阶段输出 {plan_response.output_tokens} Token。"
