"""Architecture validation, contract compilation, and blueprint freezing."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..code_company.contract_compiler import ContractCompiler
from ..code_company.runtime import CodeCompanyRuntime
from ..llm.base import LLMError, LLMResponse
from ..repositories.sqlite import SQLiteRepository
from .adaptive import build_adaptive_policy
from .architecture_repair import (
    ArchitectureContractError,
    ArchitectureRepairCoordinator,
    architecture_patch_issue,
)
from .architecture_validator import ArchitectureValidator
from .capability_router import CapabilityRouter
from .delivery_contract import build_delivery_contract, contract_hash
from .events import WorkflowEventBus
from .model_invocation import ModelInvocationService
from .models import StepDefinition
from .models import StepType, utc_now
from .platform_contracts import build_project_blueprint
from .requirements import RequirementSpec
from .run_state import RunState


class ArchitectureContractService:
    def __init__(
        self,
        validator: ArchitectureValidator,
        compiler: ContractCompiler,
        runtime: CodeCompanyRuntime,
        repository: SQLiteRepository,
        event_bus: WorkflowEventBus | None = None,
        capability_router: CapabilityRouter | None = None,
        repair_coordinator: ArchitectureRepairCoordinator | None = None,
        model_invocation: ModelInvocationService | None = None,
        contract_builder: Callable[..., dict[str, Any]] = build_delivery_contract,
        blueprint_builder: Callable[..., dict[str, Any]] = build_project_blueprint,
    ) -> None:
        self.validator = validator
        self.compiler = compiler
        self.runtime = runtime
        self.repository = repository
        self.event_bus = event_bus
        self.capability_router = capability_router
        self.repair_coordinator = repair_coordinator
        self.model_invocation = model_invocation
        self.contract_builder = contract_builder
        self.blueprint_builder = blueprint_builder

    def freeze(self, state: RunState) -> None:
        blueprint = state.context.snapshot().get("project_blueprint")
        if not isinstance(blueprint, dict):
            return
        frozen = dict(blueprint)
        frozen["status"] = "FROZEN"
        frozen["frozen_at"] = utc_now()
        frozen = self.compiler.enrich_blueprint(frozen)
        state.blueprint = frozen
        state.context.set("project_blueprint", frozen)
        state.context.set("compiled_contract", self.compiler.compile(frozen))
        state.context.set(
            "execution_plan",
            self.runtime.plan(
                str(state.context.snapshot().get("requirement") or ""),
                blueprint=frozen,
                available_steps=[
                    item.id
                    for item in state.workflow.steps
                    if item.type == StepType.AGENT
                ],
            ),
        )
        blueprint_policy = self.compiler.execution_policy(frozen)
        state.adaptive_policy = {**state.adaptive_policy, **blueprint_policy}
        state.policy_skipped_steps = set(blueprint_policy["skip_steps"])
        self.repository.update_run(
            state.run_id, blueprint_json=json.dumps(frozen, ensure_ascii=False)
        )

    def validate_candidate(
        self, candidate: dict[str, Any], requirement_spec: RequirementSpec
    ) -> dict[str, Any]:
        """Compile the complete contract before approval, without publishing it."""

        validated = self.validator.validate(candidate, requirement_spec)
        if validated is None:
            raise ArchitectureContractError(
                "Architecture decision is not a valid JSON object"
            )
        decision = validated.model_dump(mode="json")
        contract = self.contract_builder(
            requirement_spec.raw_requirement,
            decision,
            requirement_spec=requirement_spec.model_dump(mode="json"),
        )
        if decision.get("backend_required") and contract.get("crud_required"):
            entities = {
                str(item.get("name") or item.get("id") or "").lower()
                for item in contract.get("entities", [])
                if isinstance(item, dict)
            }
            expected = {name.lower() for name in requirement_spec.primary_entities}
            if expected and not expected <= entities:
                raise ArchitectureContractError(
                    "实体合同缺少原始需求实体：" + "、".join(sorted(expected - entities))
                )
            api_rows = [
                row
                for row in contract.get("api_contract", [])
                if isinstance(row, dict)
            ]
            if not api_rows:
                raise ArchitectureContractError("API 合同缺少 CRUD 路由")
            for name in expected or entities:
                matching = [
                    row
                    for row in api_rows
                    if str(row.get("entity_id") or "").lower() == name
                ]
                if not matching:
                    raise ArchitectureContractError(
                        f"API 合同缺少实体 {name} 的 CRUD 路由或 entity_id"
                    )
                if not any(
                    {"GET", "POST", "PUT", "DELETE"}
                    <= set(row.get("methods") or [])
                    for row in matching
                ):
                    raise ArchitectureContractError(
                        f"API 合同缺少实体 {name} 的完整 CRUD 方法"
                    )
            named_paths = set(
                re.findall(r"/api/[A-Za-z][\w-]*", requirement_spec.raw_requirement)
            )
            actual_paths = {
                str(row.get("collection_path") or row.get("path") or "")
                for row in api_rows
            }
            if named_paths and not named_paths <= actual_paths:
                raise ArchitectureContractError(
                    "API 合同缺少原始需求明确指定的路由："
                    + "、".join(sorted(named_paths - actual_paths))
                )
        blueprint = self.blueprint_builder(
            requirement_spec.raw_requirement,
            requirement_spec.model_dump(mode="json"),
            decision,
            contract,
        )
        blueprint = self.compiler.enrich_blueprint(blueprint)
        role_contracts = blueprint.get("role_contracts") or {}
        required_roles = []
        if bool((blueprint.get("backend") or {}).get("required")):
            required_roles.append("backend")
        if bool((blueprint.get("frontend") or {}).get("required")):
            required_roles.append("frontend")
        if str((blueprint.get("database") or {}).get("mode") or "none") != "none":
            required_roles.append("database")
        missing_plans = [
            owner
            for owner in required_roles
            if not isinstance(role_contracts.get(owner), dict)
            or not role_contracts[owner].get("file_plan")
        ]
        if missing_plans:
            raise ArchitectureContractError(
                "必需 Agent 缺少成果物文件计划：" + "、".join(missing_plans)
            )
        return {
            "decision": decision,
            "contract": contract,
            "blueprint": blueprint,
            "compiled_contract": self.compiler.compile(blueprint),
            "contract_hash": contract_hash(contract),
        }

    async def process_step(
        self,
        state: RunState,
        step: StepDefinition,
        response: LLMResponse,
        *,
        effective_system_prompt: str,
        agent_id: str,
        runtime: dict[str, Any],
        provider_attempts: list[dict[str, Any]],
        total_input_tokens: int,
        total_output_tokens: int,
        last_provider_record: dict[str, Any],
        persist_state: Callable[[RunState], None],
        validate_candidate: Callable[[dict[str, Any], RequirementSpec], dict[str, Any]],
    ) -> tuple[LLMResponse, int, int, dict[str, Any]]:
        """Validate, repair, and publish one Architecture Agent response."""

        if not all(
            (
                self.event_bus,
                self.capability_router,
                self.repair_coordinator,
                self.model_invocation,
            )
        ):
            raise RuntimeError("Architecture step runtime dependencies are not configured")

        raw_decision = self.validator.parse(response.text)
        requirement_spec = state.requirement_spec or self.capability_router.route(
            str(state.context.snapshot().get("requirement", ""))
        )
        state.requirement_spec = requirement_spec
        state.context.set("requirement_spec", requirement_spec.model_dump(mode="json"))
        validated_decision = self.validator.validate(raw_decision, requirement_spec)
        if validated_decision is None:
            raise LLMError(
                "Architecture output validation failed: expected a valid JSON object",
                retryable=True,
                response_metadata=last_provider_record,
            )

        async def emit_repair(event_type: str, payload: dict[str, Any]) -> None:
            fact = payload.get("failureFact")
            if isinstance(fact, dict):
                prior = state.context.snapshot().get("failure_facts")
                facts = (
                    [item for item in prior if isinstance(item, dict)]
                    if isinstance(prior, list)
                    else []
                )
                facts = [
                    item
                    for item in facts
                    if item.get("failure_id") != fact.get("failure_id")
                ]
                facts.append(fact)
                state.context.set("failure_facts", facts)
                self.repository.update_run(
                    state.run_id,
                    failure_facts_json=json.dumps(facts, ensure_ascii=False),
                )
            if event_type == "architecture.target_gate_completed" and payload.get("passed"):
                prior = state.context.snapshot().get("failure_facts")
                if isinstance(prior, list):
                    resolved = [
                        {**item, "resolved": True}
                        if isinstance(item, dict)
                        and item.get("stage") == "architecture_contract"
                        else item
                        for item in prior
                    ]
                    state.context.set("failure_facts", resolved)
                    self.repository.update_run(
                        state.run_id,
                        failure_facts_json=json.dumps(resolved, ensure_ascii=False),
                    )
            state.context.set(
                "architecture_repair_state",
                {
                    "attempt": int(payload.get("repairAttempt") or 0),
                    "event": event_type,
                    "fingerprint": fact.get("fingerprint") if isinstance(fact, dict) else "",
                },
            )
            persist_state(state)
            await self.event_bus.emit(
                event_type, state.run_id, {"stepId": step.id, **payload}
            )

        async def repair_architecture(
            candidate: dict[str, Any], fact: dict[str, Any], attempt: int
        ) -> dict[str, Any]:
            nonlocal total_input_tokens, total_output_tokens, last_provider_record
            cache_key = f"{attempt}:{fact['fingerprint']}"
            cached = state.context.snapshot().get("architecture_repair_responses")
            if isinstance(cached, dict) and isinstance(cached.get(cache_key), dict):
                return cached[cache_key]
            feedbacks = state.context.snapshot().get("architecture_repair_feedback")
            previous_key = f"{attempt - 1}:{fact['fingerprint']}"
            previous_feedback = (
                feedbacks.get(previous_key, "") if isinstance(feedbacks, dict) else ""
            )
            entity_names = [
                str(row.get("name") or row.get("id") or "")
                for row in (candidate.get("delivery_contract") or {}).get("entities", [])
                if isinstance(row, dict)
            ]
            entity_link_rule = (
                "本次失败是 API 缺少实体关联。返回的每条 api_contract 必须显式包含 "
                "entity_id，值必须与已有实体名称完全一致；可用实体名称："
                f"{json.dumps(entity_names, ensure_ascii=False)}。即使只有一个实体也不能省略；"
                "不得只写 path、identity_field 或依赖 URL 自动关联。"
                "如果无法确定对应实体，不得编造映射。\n"
                if fact["code"] == "ARCH_API_ENTITY_ID_REQUIRED"
                else ""
            )
            feedback_line = (
                f"上一次修复结果未被接受的原因：{previous_feedback}\n"
                if previous_feedback
                else ""
            )
            repair_scope = list(fact.get("repair_scope") or [])
            contract_shape_scope = [
                item
                for item in repair_scope
                if item
                in {
                    "backend_stack",
                    "frontend_stack",
                    "page_mode",
                    "database_mode",
                    "production_database",
                    "validation_database",
                    "entrypoints",
                }
            ]
            if contract_shape_scope:
                allowed_instruction = (
                    "本次只允许修正交付形态合同。返回 JSON 对象并在 delivery_contract 中显式提供："
                    f"{json.dumps(contract_shape_scope, ensure_ascii=False)}。"
                    "技术栈必须使用平台规范标识，例如 springboot、python、vue、react、html、none；"
                    "entrypoints 必须是字符串数组。不得修改 entities、api_contract、原始需求或能力范围。"
                )
            elif "artifact_ownership" in repair_scope:
                allowed_instruction = (
                    "本次只允许修正顶层 artifact_ownership 映射；不得修改实体、API、技术栈、"
                    "项目类型或原始需求。"
                )
            else:
                allowed_instruction = (
                    "只修正以下允许范围内的实体、字段或 API 合同；"
                    "不得改动技术栈、项目类型、能力路由或原始需求。"
                    "只返回 JSON 对象，包含允许修改的 entities 和/或 api_contract 数组，"
                    "不得省略其他已有实体与路由。"
                )
            repair_prompt = (
                "架构合同预检失败。"
                f"{allowed_instruction}\n"
                f"{entity_link_rule}{feedback_line}"
                f"允许范围：{json.dumps(repair_scope, ensure_ascii=False)}\n"
                f"原始用户需求：{requirement_spec.raw_requirement}\n"
                f"失败规则：{fact['code']}\n失败证据：{fact['message']}\n"
                f"当前架构草案：{json.dumps(candidate, ensure_ascii=False)}"
            )
            repair_response = await self.model_invocation.generate(
                effective_system_prompt,
                repair_prompt,
                {
                    "agent_id": agent_id,
                    "generation_phase": "architecture_contract_repair",
                    "max_tokens": runtime["max_tokens"],
                    "effective_thinking": "off",
                    "thinking_type": "disabled",
                    "reasoning_effort": "off",
                    "connect_timeout": 10,
                    "read_timeout": 120,
                    "write_timeout": 30,
                    "pool_timeout": 10,
                },
                step.timeout_seconds,
            )
            total_input_tokens += repair_response.input_tokens
            total_output_tokens += repair_response.output_tokens
            last_provider_record = repair_response.provider_record()
            provider_attempts.append(last_provider_record)
            self.model_invocation.persist_attempts(
                state.run_id, step.id, provider_attempts
            )
            patch_text = repair_response.text.strip()
            if patch_text.startswith("```"):
                patch_text = re.sub(
                    r"^```[^\n]*\n|\n?```$", "", patch_text
                ).strip()
            try:
                patch = json.loads(patch_text)
            except (ValueError, TypeError):
                patch = {}
            if not isinstance(patch, dict):
                patch = {}
            issue = architecture_patch_issue(candidate, patch, fact)
            if issue:
                feedback = dict(feedbacks or {}) if isinstance(feedbacks, dict) else {}
                feedback[cache_key] = issue
                state.context.set("architecture_repair_feedback", feedback)
                await emit_repair(
                    "architecture.repair_rejected",
                    {
                        "repairAttempt": attempt,
                        "failureFact": fact,
                        "reason": issue,
                    },
                )
                patch = {}
            responses = dict(cached or {}) if isinstance(cached, dict) else {}
            responses[cache_key] = patch
            state.context.set("architecture_repair_responses", responses)
            persist_state(state)
            return patch

        if state.workflow.meta.get("delivery_contract"):
            contract_outcome = await self.repair_coordinator.coordinate(
                run_id=f"{state.run_id}:{state.checkpoint_thread_id or state.run_id}",
                candidate=raw_decision,
                max_attempts=max(
                    0,
                    min(
                        3,
                        int(
                            state.workflow.meta.get(
                                "architecture_contract_repair_attempts", 2
                            )
                        ),
                    ),
                ),
                validate=lambda candidate: validate_candidate(candidate, requirement_spec),
                repair=repair_architecture,
                emit=emit_repair,
            )
        else:
            contract_outcome = {
                "passed": True,
                "result": {"decision": validated_decision.model_dump(mode="json")},
            }
        state.context.set(
            "architecture_repair_history",
            list(contract_outcome.get("history") or []),
        )
        if not contract_outcome.get("passed"):
            fact = contract_outcome.get("failure") or {}
            raise ArchitectureContractError(
                f"{fact.get('summary') or '架构合同预检未通过'} 详情：{fact.get('message') or ''}"
            )

        prepared = contract_outcome["result"]
        decision = prepared["decision"]
        if decision:
            state.adaptive_policy = build_adaptive_policy(decision)
            state.policy_skipped_steps = set(
                state.adaptive_policy.get("skip_steps", [])
            )
            state.context.set("architecture_raw", response.text)
            state.context.set("architecture_decision", decision)
            if state.workflow.meta.get("delivery_contract"):
                contract = prepared["contract"]
                blueprint = prepared["blueprint"]
                blueprint_policy = self.compiler.execution_policy(blueprint)
                state.adaptive_policy = {**state.adaptive_policy, **blueprint_policy}
                state.policy_skipped_steps = set(blueprint_policy["skip_steps"])
                state.context.set("delivery_contract", contract)
                state.context.set("delivery_contract_hash", contract_hash(contract))
                blueprint["contract_hash"] = contract_hash(contract)
                state.blueprint = blueprint
                state.context.set("project_blueprint", blueprint)
                self.repository.update_run(
                    state.run_id,
                    blueprint_json=json.dumps(blueprint, ensure_ascii=False),
                    decision_log_json=json.dumps(
                        [{"source": "architecture", "decision": decision}],
                        ensure_ascii=False,
                    ),
                )
                decision["delivery_contract"] = contract
                state.context.set("architecture_decision", decision)
                serialized = json.dumps(decision, ensure_ascii=False)
                response = replace(
                    response, text=serialized, message_content=serialized
                )
                await self.event_bus.emit(
                    "workflow.contract_validated",
                    state.run_id,
                    {"contract": contract, "contractHash": contract_hash(contract)},
                )
                await self.event_bus.emit(
                    "workflow.blueprint_created",
                    state.run_id,
                    {"blueprint": blueprint, "status": blueprint.get("status")},
                )
            state.context.set(
                "architecture_doc", json.dumps(decision, ensure_ascii=False)
            )
            await self.event_bus.emit(
                "workflow.policy_decided",
                state.run_id,
                {"decision": decision, "policy": state.adaptive_policy},
            )
        return (
            response,
            total_input_tokens,
            total_output_tokens,
            last_provider_record,
        )
