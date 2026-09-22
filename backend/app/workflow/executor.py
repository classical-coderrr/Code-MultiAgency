"""Workflow scheduler: state transitions, concurrency, retry and approval pause."""
from __future__ import annotations

import asyncio
import json
import posixpath
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt

from ..agents.registry import AgentRegistry
from ..llm.base import LLMError, LLMProvider, LLMTimeoutError, LLMResponse
from ..repositories.sqlite import SQLiteRepository
from ..services.artifacts import ArtifactService
from ..skills.registry import SkillRegistry
from ..skills.resolver import SkillResolver
from ..code_company.runtime import CodeCompanyRuntime
from ..code_company.coding_loop import CodingAgentLoop, CodingLoopConfig
from ..code_company.verification import VerificationEngine
from ..code_company.contract_compiler import ContractCompiler
from ..code_company.artifact_contracts import path_allowed
from ..code_company.repair import RepairEngine
from ..code_company.repair_coordinator import RepairCoordinator
from .context import WorkflowContext
from .maven_contract import normalize_h2_flyway_dependency
from .context_packer import ContextPacker
from .delivery_contract import build_delivery_contract, contract_hash
from .budget import TokenBudgetManager
from .dag import WorkflowDAG, build_dag
from .events import WorkflowEventBus
from .adaptive import (
    build_local_runtime_budget_plan,
    build_adaptive_policy,
    parse_runtime_budget_plan,
)
from .artifact_generation import ARTIFACT_FILE_MIN_TOKENS, ArtifactGenerationResult, generate_artifacts
from .artifact_validator import ArtifactValidationResult, ArtifactValidator
from .artifact_delivery import ArtifactDeliveryService
from .architecture_contract import ArchitectureContractService
from .architecture_validator import ArchitectureValidator
from .architecture_repair import ArchitectureContractError, ArchitectureRepairCoordinator, architecture_patch_issue
from .capability_router import CapabilityRouter
from .langgraph_runtime import LangGraphState, LangGraphWorkflow
from .models import RunStatus, StepDefinition, StepStatus, StepType, WorkflowDefinition, utc_now
from .model_invocation import ModelInvocationService
from .failure_recording import FailureFactRecorder
from .output_inspector import inspect_response
from .requirements import RequirementSpec
from .clarification import RequirementClarificationService
from .collaboration import CollaborationCoordinator
from .platform_contracts import (
    ClarificationRequest,
    build_project_blueprint,
    evidence_from_check,
    failure_fact_from_check,
    normalize_failure_fact,
    request_blueprint_change,
)
from .integration_gate import IntegrationGate
from .platform_runtime import PlatformRuntime
from .run_state import RunState
from .runtime_policy import RuntimePolicyResolver
from .workspace_artifacts import WorkspaceArtifactCollector


class WorkflowAlreadyRunningError(RuntimeError):
    pass


class WorkflowExecutor:
    def __init__(
        self,
        registry: AgentRegistry,
        provider: LLMProvider,
        event_bus: WorkflowEventBus,
        repository: SQLiteRepository,
        checkpointer: BaseCheckpointSaver | None = None,
        artifact_service: ArtifactService | None = None,
        skill_registry: SkillRegistry | None = None,
        artifact_validator: ArtifactValidator | None = None,
        code_company_runtime: CodeCompanyRuntime | None = None,
        max_active_runs: int = 4,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.event_bus = event_bus
        self.repository = repository
        self.model_invocation = ModelInvocationService(provider, repository)
        self.failure_recorder = FailureFactRecorder(repository)
        self.runtime_policy = RuntimePolicyResolver()
        self.checkpointer = checkpointer or InMemorySaver()
        self.artifact_service = artifact_service
        self.artifact_validator = artifact_validator or ArtifactValidator()
        self.token_budget_manager = TokenBudgetManager()
        self.context_packer = ContextPacker()
        self.capability_router = CapabilityRouter()
        self.clarification_service = RequirementClarificationService()
        self.integration_gate = IntegrationGate()
        self.verification_engine = VerificationEngine(self.artifact_validator, self.integration_gate)
        self.contract_compiler = ContractCompiler()
        self.repair_engine = RepairEngine()
        self.repair_coordinator = RepairCoordinator(self.repair_engine)
        self.collaboration_coordinator = CollaborationCoordinator(repository)
        self.platform_runtime = PlatformRuntime(self.token_budget_manager)
        self.architecture_validator = ArchitectureValidator()
        self.architecture_repair = ArchitectureRepairCoordinator(self.checkpointer)
        self.skill_resolver = SkillResolver(skill_registry or SkillRegistry())
        workspace_root = getattr(artifact_service, "workspace_root", Path("data/workspaces"))
        self.code_company_runtime = code_company_runtime or CodeCompanyRuntime.from_root(workspace_root)
        self.workspace_artifacts = WorkspaceArtifactCollector(self.code_company_runtime)
        self.artifact_delivery = ArtifactDeliveryService(
            artifact_service, event_bus, self.runtime_policy
        )
        self.architecture_contract = ArchitectureContractService(
            self.architecture_validator,
            self.contract_compiler,
            self.code_company_runtime,
            repository,
            event_bus=event_bus,
            capability_router=self.capability_router,
            repair_coordinator=self.architecture_repair,
            model_invocation=self.model_invocation,
            contract_builder=build_delivery_contract,
            blueprint_builder=build_project_blueprint,
        )
        self._active: dict[str, RunState] = {}
        self._queued_runs: set[str] = set()
        self._queued_workflows: dict[str, str] = {}
        self._run_slots = asyncio.Semaphore(max(1, int(max_active_runs)))
        self._starting_workflows: set[str] = set()

    def _platform_context(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        """Expose only non-secret runtime metadata to configurable planner Agents."""
        return {
            "provider_capabilities": self.provider.capabilities(),
            "workflow_steps": [
                {
                    "id": step.id,
                    "agent_id": step.agent_id,
                    "output_format": step.output_format,
                    "runtime_plan": step.runtime_plan,
                    "generation_mode": step.generation_mode,
                    "skills": list(step.skills),
                    "validation": WorkflowExecutor._safe_snapshot_value(step.validation),
                }
                for step in workflow.steps
                if step.type == StepType.AGENT
            ],
            "code_company_runtime": self.code_company_runtime.capabilities(),
        }

    def is_active(self, workflow_id: str) -> bool:
        return (
            any(state.workflow.id == workflow_id for state in self._active.values())
            or workflow_id in self._starting_workflows
            or workflow_id in self._queued_workflows.values()
        )

    def active_run_for_workflow(self, workflow_id: str) -> dict[str, str] | None:
        """Return the durable in-memory run that currently owns a workflow."""
        for state in self._active.values():
            if state.workflow.id != workflow_id:
                continue
            status = (RunStatus.WAITING_CLARIFICATION.value if state.waiting_step_id == "__clarification__"
                      else RunStatus.WAITING_APPROVAL.value if state.waiting_step_id else RunStatus.RUNNING.value)
            requirement = state.context.snapshot().get("requirement")
            return {
                "runId": state.run_id,
                "status": status,
                "requirement": str(requirement or ""),
            }
        return None

    @staticmethod
    def _workflow_snapshot(workflow: WorkflowDefinition) -> dict[str, Any]:
        return {
            "id": workflow.id,
            "name": workflow.name,
            "concurrency": workflow.concurrency,
            "inputs": workflow.inputs,
            "meta": workflow.meta,
            "runtime_defaults": workflow.runtime_defaults,
            "steps": [
                {
                    "id": step.id,
                    "type": step.type.value,
                    "agent_id": step.agent_id,
                    "task_template": step.task_template,
                    "output": step.output,
                    "depends_on": list(step.depends_on),
                    "timeout_seconds": step.timeout_seconds,
                    "retry_count": step.retry_count,
                    "max_tokens": step.max_tokens,
                    "thinking": step.thinking,
                    "output_format": step.output_format,
                    "failure_policy": step.failure_policy,
                    "runtime_plan": step.runtime_plan,
                    "generation_mode": step.generation_mode,
                    "enforce_artifact_contract": step.enforce_artifact_contract,
                    "skills": list(step.skills),
                    "skill_mode": step.skill_mode,
                    "validation": WorkflowExecutor._safe_snapshot_value(step.validation),
                }
                for step in workflow.steps
            ],
        }

    @classmethod
    def _safe_snapshot_value(cls, value: Any, key: str = "") -> Any:
        """Keep recovery snapshots useful without persisting credential fields."""
        sensitive = {"api_key", "apikey", "authorization", "cookie", "password", "secret", "token"}
        if key.lower() in sensitive:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): cls._safe_snapshot_value(item_value, str(item_key)) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [cls._safe_snapshot_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _persist_state(self, state: RunState) -> None:
        snapshot = {
            "version": 1,
            "run_id": state.run_id,
            "context": self._safe_snapshot_value(state.context.snapshot()),
            "results": {key: value.value for key, value in state.results.items()},
            "requirement_spec": state.requirement_spec.model_dump(mode="json") if state.requirement_spec else None,
            "runtime": self._safe_snapshot_value(state.runtime),
            "current_level": state.current_level,
            "waiting_step_id": state.waiting_step_id,
            "adaptive_policy": self._safe_snapshot_value(state.adaptive_policy),
            "runtime_budget_plan": self._safe_snapshot_value(state.runtime_budget_plan),
            "policy_skipped_steps": sorted(state.policy_skipped_steps),
            "checkpoint_thread_id": state.checkpoint_thread_id or state.run_id,
            "recovery_count": state.recovery_count,
            "clarification_request": self._safe_snapshot_value(state.clarification_request),
            "blueprint": self._safe_snapshot_value(state.blueprint),
            "workspace": self._safe_snapshot_value(state.workspace),
            "execution_status": state.execution_status,
            "delivery_status": state.delivery_status,
        }
        self.repository.save_run_snapshot(state.run_id, snapshot, heartbeat_at=utc_now())

    def _state_from_run(
        self,
        run: dict[str, Any],
        workflow: WorkflowDefinition,
        *,
        reset_inflight: bool = False,
    ) -> RunState:
        """Rebuild a Run from the durable snapshot, with a legacy fallback."""
        run_id = str(run.get("id") or "")
        dag = build_dag(workflow)
        snapshot = run.get("state") if isinstance(run.get("state"), dict) else {}
        persisted_inputs = run.get("inputs") if isinstance(run.get("inputs"), dict) else {}
        step_rows = {
            str(item.get("step_id")): item
            for item in run.get("steps", [])
            if isinstance(item, dict) and item.get("step_id")
        }

        context_data = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else None
        legacy_runtime_budget_plan: dict[str, Any] = {}
        if context_data is None:
            context_data = {**workflow.inputs}
            context_data.update({key: value for key, value in persisted_inputs.items() if key != "runtime"})
            context_data.update(self._platform_context(workflow))
            requirement = str(context_data.get("requirement", ""))
            for step in workflow.steps:
                row = step_rows.get(step.id, {})
                if row.get("status") != StepStatus.SUCCESS.value or not step.output:
                    continue
                output = row.get("output") or ""
                if step.runtime_plan:
                    parsed_plan = parse_runtime_budget_plan(
                        str(output),
                        {item.id for item in workflow.steps if item.type == StepType.AGENT},
                        self.token_budget_manager.provider_max_tokens(self.provider.capabilities()),
                    )
                    context_data[step.output] = parsed_plan or output
                    if parsed_plan:
                        legacy_runtime_budget_plan = parsed_plan
                else:
                    context_data[step.output] = output
            context_data["requirement"] = context_data.get("requirement", requirement)
        else:
            context_data = dict(context_data)
            context_data.setdefault("workflow_steps", self._platform_context(workflow)["workflow_steps"])
            context_data.setdefault("provider_capabilities", self.provider.capabilities())

        requirement_data = snapshot.get("requirement_spec") or context_data.get("requirement_spec")
        try:
            requirement_spec = RequirementSpec.model_validate(requirement_data) if isinstance(requirement_data, dict) else self.capability_router.route(str(context_data.get("requirement", "")))
        except Exception:
            requirement_spec = self.capability_router.route(str(context_data.get("requirement", "")))
        context_data["requirement_spec"] = requirement_spec.model_dump(mode="json")

        # Runs created before durable snapshots still need the architecture
        # policy reconstructed so a post-restart approval does not accidentally
        # execute a branch that the original routing decision skipped.
        legacy_adaptive_policy: dict[str, Any] = {}
        if not snapshot:
            architecture_output = context_data.get("architecture_doc")
            raw_decision = self.architecture_validator.parse(str(architecture_output or ""))
            validated_decision = self.architecture_validator.validate(raw_decision, requirement_spec)
            if validated_decision:
                decision = validated_decision.model_dump(mode="json")
                context_data["architecture_decision"] = decision
                context_data["architecture_doc"] = json.dumps(decision, ensure_ascii=False)
                legacy_adaptive_policy = build_adaptive_policy(decision)

        raw_results = snapshot.get("results") if isinstance(snapshot.get("results"), dict) else {}
        results: dict[str, StepStatus] = {}
        for step in workflow.steps:
            raw_status = raw_results.get(step.id, step_rows.get(step.id, {}).get("status", StepStatus.PENDING.value))
            try:
                status = StepStatus(str(raw_status))
            except ValueError:
                status = StepStatus.PENDING
            if reset_inflight and status in {StepStatus.RUNNING, StepStatus.FAILED}:
                status = StepStatus.PENDING
                self.repository.upsert_step(
                    run_id,
                    step.id,
                    status=StepStatus.PENDING.value,
                    error_message="节点在进程重启前未完成，已回收到可重试状态",
                )
            results[step.id] = status

        adaptive_policy = snapshot.get("adaptive_policy") if isinstance(snapshot.get("adaptive_policy"), dict) else legacy_adaptive_policy
        runtime_budget_plan = snapshot.get("runtime_budget_plan") if isinstance(snapshot.get("runtime_budget_plan"), dict) else legacy_runtime_budget_plan
        waiting_step_id = snapshot.get("waiting_step_id")
        if not waiting_step_id:
            waiting_step_id = next(
                (step.id for step in workflow.steps if results.get(step.id) == StepStatus.WAITING_APPROVAL and step.type == StepType.APPROVAL),
                None,
            )
        workspace = snapshot.get("workspace") if isinstance(snapshot.get("workspace"), dict) else context_data.get("workspace")
        if not isinstance(workspace, dict):
            # Backward-compatible recovery for runs created before the
            # Workspace contract existed. The same run id keeps the recovered
            # files attached to the original run directory.
            workspace = self.code_company_runtime.prepare_run(
                run_id,
                snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else persisted_inputs.get("runtime", {}),
            ).as_dict()
            context_data["workspace"] = workspace
        snapshot_blueprint = snapshot.get("blueprint") if isinstance(snapshot.get("blueprint"), dict) else None
        context_blueprint = context_data.get("project_blueprint") if isinstance(context_data.get("project_blueprint"), dict) else None
        persisted_blueprint = run.get("blueprint") if isinstance(run.get("blueprint"), dict) else None
        blueprint = self._authoritative_blueprint(
            snapshot_blueprint,
            context_blueprint,
            persisted_blueprint,
        )
        if blueprint:
            context_data["project_blueprint"] = blueprint
        if blueprint and str(blueprint.get("status") or "") == "CHANGE_REQUESTED":
            for stale_key in (
                "compiled_contract",
                "delivery_contract",
                "delivery_contract_hash",
                "execution_plan",
                "artifact_validation",
                "delivery_gate",
            ):
                context_data.pop(stale_key, None)

        state = RunState(
            run_id=run_id,
            workflow=workflow,
            dag=dag,
            context=WorkflowContext(context_data),
            requirement_spec=requirement_spec,
            runtime=snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else (persisted_inputs.get("runtime", {}) if isinstance(persisted_inputs.get("runtime"), dict) else {}),
            current_level=int(snapshot.get("current_level", 0) or 0),
            waiting_step_id=str(waiting_step_id) if waiting_step_id else None,
            results=results,
            adaptive_policy=adaptive_policy,
            runtime_budget_plan=runtime_budget_plan,
            policy_skipped_steps=set(snapshot.get("policy_skipped_steps", adaptive_policy.get("skip_steps", []))),
            checkpoint_thread_id=str(snapshot.get("checkpoint_thread_id") or run_id),
            recovery_count=int(run.get("recovery_count") or snapshot.get("recovery_count") or 0),
            recovery_mode=reset_inflight,
            started_perf=time.perf_counter() - max(0, int(run.get("active_duration_ms") or 0)) / 1000,
            approval_started_perf=time.perf_counter() if waiting_step_id else None,
            clarification_request=snapshot.get("clarification_request") if isinstance(snapshot.get("clarification_request"), dict) else None,
            blueprint=blueprint,
            workspace=workspace,
            execution_status=str(snapshot.get("execution_status") or run.get("execution_status") or run.get("status") or RunStatus.PENDING.value),
            delivery_status=str(snapshot.get("delivery_status") or run.get("delivery_status") or "NOT_EVALUATED"),
        )
        state.graph = self._build_graph(workflow)
        return state

    @staticmethod
    def _authoritative_blueprint(
        snapshot_blueprint: dict[str, Any] | None,
        context_blueprint: dict[str, Any] | None,
        persisted_blueprint: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Choose the newest durable Blueprint and reject stale frozen copies."""
        candidates = [
            (0, context_blueprint),
            (1, snapshot_blueprint),
            (2, persisted_blueprint),
        ]
        valid = [(source, item) for source, item in candidates if isinstance(item, dict)]
        if not valid:
            return None
        status_rank = {"DRAFT": 0, "FROZEN": 1, "CHANGE_REQUESTED": 2}
        _, selected = max(
            valid,
            key=lambda row: (
                int(row[1].get("version") or 0),
                status_rank.get(str(row[1].get("status") or ""), -1),
                row[0],
            ),
        )
        return dict(selected)

    def restore_waiting_run(self, run: dict[str, Any], workflow: WorkflowDefinition) -> bool:
        """Rebuild an approval-paused run after the process has restarted."""
        run_id = str(run.get("id") or "")
        if not run_id or str(run.get("status")) not in {RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_CLARIFICATION.value}:
            return False
        if run_id in self._active:
            return True

        state = self._state_from_run(run, workflow)
        if state.waiting_step_id is None:
            self.repository.update_run(
                run_id,
                status=RunStatus.FAILED.value,
                finished_at=utc_now(),
                error_message="Unable to restore approval state after backend restart",
            )
            return False
        state.current_level = next((index for index, level in enumerate(state.dag.levels) if state.waiting_step_id in level), 0)
        self._active[run_id] = state
        return True

    def _workflow_for_run(self, workflows: Any, run: dict[str, Any]) -> WorkflowDefinition:
        if hasattr(workflows, "get_workflow_for_run"):
            return workflows.get_workflow_for_run(run)
        return workflows.get_workflow(str(run.get("workflow_id") or ""))

    async def _run_recovered_state(self, state: RunState, previous_status: str) -> None:
        state.task = asyncio.current_task()
        await self.event_bus.emit(
            "workflow.recovered",
            state.run_id,
            {
                "workflowId": state.workflow.id,
                "previousStatus": previous_status,
                "recoveryCount": state.recovery_count,
                "checkpointThreadId": state.checkpoint_thread_id,
            },
        )
        await self._run_state(state)

    def restore_runs(self, workflows: Any) -> list[str]:
        """Restore approval pauses and unfinished Runs during application startup."""
        restored: list[str] = []
        for run in self.repository.list_recoverable_runs():
            run_id = str(run.get("id") or "")
            if not run_id or run_id in self._active:
                continue
            try:
                workflow = self._workflow_for_run(workflows, run)
                previous_status = str(run.get("status"))
                if previous_status in {RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_CLARIFICATION.value}:
                    if self.restore_waiting_run(run, workflow):
                        self.repository.mark_recovery(run_id)
                        restored.append(run_id)
                    continue
                state = self._state_from_run(run, workflow, reset_inflight=True)
                recovery_count = self.repository.mark_recovery(run_id, status=RunStatus.RUNNING.value)
                state.recovery_count = recovery_count
                self._active[run_id] = state
                state.task = asyncio.create_task(self._run_recovered_state(state, previous_status))
                restored.append(run_id)
            except Exception as exc:
                self.repository.update_run(
                    run_id,
                    status=RunStatus.FAILED.value,
                    finished_at=utc_now(),
                    error_message=f"无法在后端重启后恢复 Run：{exc}",
                )
        return restored

    def restore_waiting_runs(self, workflows: Any) -> list[str]:
        """Restore every durable approval pause known to the repository."""
        restored: list[str] = []
        for run in self.repository.list_runs(RunStatus.WAITING_APPROVAL.value):
            try:
                workflow = self._workflow_for_run(workflows, run)
                if self.restore_waiting_run(run, workflow):
                    restored.append(str(run["id"]))
            except Exception as exc:
                self.repository.update_run(
                    str(run.get("id")),
                    status=RunStatus.FAILED.value,
                    finished_at=utc_now(),
                    error_message=f"Unable to restore run after backend restart: {exc}",
                )
        return restored

    async def start(self, run_id: str, workflow: WorkflowDefinition, inputs: dict[str, Any], runtime: dict[str, Any] | None = None) -> None:
        if run_id in self._active or run_id in self._queued_runs:
            raise WorkflowAlreadyRunningError(f"Run is already running: {run_id}")
        queued = self.is_active(workflow.id)
        self._queued_runs.add(run_id)
        self._queued_workflows[run_id] = workflow.id
        if queued:
            self.repository.update_run(run_id, status=RunStatus.QUEUED.value, execution_status=RunStatus.QUEUED.value)
            await self.event_bus.emit("workflow.queued", run_id, {"workflowId": workflow.id, "status": RunStatus.QUEUED.value})
        try:
            async with self._run_slots:
                self._queued_runs.discard(run_id)
                self._queued_workflows.pop(run_id, None)
                dag = build_dag(workflow)
                initial_context = {**workflow.inputs, **inputs, **self._platform_context(workflow)}
                workspace = self.code_company_runtime.prepare_run(run_id, runtime or {})
                initial_context["workspace"] = workspace.as_dict()
                parallel_owners = [
                    step.id for step in workflow.steps
                    if step.id in {"database", "backend", "frontend"} and step.type == StepType.AGENT
                ]
                initial_context["agent_workspaces"] = self.code_company_runtime.prepare_agent_workspaces(
                    workspace,
                    parallel_owners,
                ) if parallel_owners else {}
                initial_context["repo_index"] = self.code_company_runtime.index_workspace(workspace)
                initial_context["execution_plan"] = self.code_company_runtime.plan(
                    str(initial_context.get("requirement") or ""),
                    available_steps=[step.id for step in workflow.steps if step.type == StepType.AGENT],
                )
                requirement_spec = self.capability_router.route(str(initial_context.get("requirement", "")))
                initial_context["requirement_spec"] = requirement_spec.model_dump(mode="json")
                self.repository.update_run_inputs(
                    run_id,
                    {"requirement_spec": requirement_spec.model_dump(mode="json")},
                )
                state = RunState(
                    run_id=run_id,
                    workflow=workflow,
                    dag=dag,
                    context=WorkflowContext(initial_context),
                    requirement_spec=requirement_spec,
                    runtime=runtime or {},
                    workspace=workspace.as_dict(),
                    checkpoint_thread_id=run_id,
                    execution_status=RunStatus.PENDING.value,
                )
                state.results = {step.id: StepStatus.PENDING for step in workflow.steps}
                state.task = asyncio.current_task()
                self._active[run_id] = state
                self.repository.update_run(run_id, execution_status=RunStatus.RUNNING.value, status=RunStatus.RUNNING.value)
                self.repository.save_workflow_snapshot(run_id, self._workflow_snapshot(workflow))
                self._persist_state(state)
                state.graph = self._build_graph(workflow)
                await self._run_state(state)
        finally:
            self._queued_runs.discard(run_id)
            self._queued_workflows.pop(run_id, None)

    async def approve(self, run_id: str, decision: str) -> None:
        state = self._active.get(run_id)
        if not state or not state.waiting_step_id:
            raise ValueError("Run is not waiting for approval")
        if decision not in {"approve", "reject"}:
            raise ValueError("Approval decision must be approve or reject")
        if state.resume_task and not state.resume_task.done():
            raise ValueError("Approval is already being resolved")
        state.resume_task = asyncio.create_task(self._resume_graph(state, decision))

    async def clarify(self, run_id: str, answers: dict[str, Any]) -> None:
        state = self._active.get(run_id)
        if not state or state.waiting_step_id != "__clarification__":
            raise ValueError("Run is not waiting for requirement clarification")
        if state.resume_task and not state.resume_task.done():
            raise ValueError("Clarification is already being resolved")
        if state.requirement_spec is None or not state.clarification_request:
            raise ValueError("需求确认状态不完整，请刷新运行状态后重试。")
        submitted_request_id = str((answers or {}).get("request_id") or "").strip()
        current_request_id = str(state.clarification_request.get("request_id") or "").strip()
        if submitted_request_id and submitted_request_id != current_request_id:
            raise ValueError("需求确认请求已更新，请刷新后按当前问题重新提交。")
        # Reject an empty/invalid custom answer before scheduling graph resume.
        # Otherwise the HTTP endpoint reports success while the Run fails later.
        self.clarification_service.apply_answer(
            state.requirement_spec,
            ClarificationRequest.model_validate(state.clarification_request),
            dict(answers or {}),
        )
        state.resume_task = asyncio.create_task(self._resume_graph(state, dict(answers or {})))

    async def wait_for_control_resolution(self, run_id: str) -> None:
        """Wait until an approval/clarification resume reaches its next stable state."""
        state = self._active.get(run_id)
        task = state.resume_task if state else None
        if task is not None:
            await task

    @staticmethod
    def _begin_approval_wait(state: RunState) -> None:
        if state.approval_started_perf is None:
            state.approval_started_perf = time.perf_counter()

    @staticmethod
    def _end_approval_wait(state: RunState) -> None:
        if state.approval_started_perf is None:
            return
        state.excluded_approval_ms += max(
            0,
            int((time.perf_counter() - state.approval_started_perf) * 1000),
        )
        state.approval_started_perf = None

    @staticmethod
    def _approval_duration_ms(state: RunState) -> int:
        current_wait = (
            int((time.perf_counter() - state.approval_started_perf) * 1000)
            if state.approval_started_perf is not None
            else 0
        )
        return max(0, state.excluded_approval_ms + current_wait)

    @classmethod
    def _active_duration_ms(cls, state: RunState) -> int:
        wall_duration = int((time.perf_counter() - state.started_perf) * 1000)
        return max(0, wall_duration - cls._approval_duration_ms(state))

    async def stop(self, run_id: str) -> None:
        state = self._active.get(run_id)
        if not state:
            persisted = self.repository.get_run(run_id)
            if persisted and str(persisted.get("status") or "") == RunStatus.STOPPED.value:
                return
            raise ValueError("Run is not active")
        state.context.set("cancel_requested_at", utc_now())
        self._persist_state(state)
        await self.event_bus.emit(
            "workflow.cancelling",
            run_id,
            {"status": RunStatus.RUNNING.value, "reason": "user_requested"},
        )
        cancelled_tasks: list[asyncio.Task[Any]] = []
        for task in (state.task, state.resume_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                cancelled_tasks.append(task)
        for task in cancelled_tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
        await self._finish(state, RunStatus.STOPPED, "Stopped by user")

    async def abandon_for_lease_loss(self, run_id: str) -> None:
        """Detach local execution without changing the durable Run status.

        A fenced Redis lease may be lost while another Worker is taking over.
        Marking the Run STOPPED here would overwrite the successor's recovery
        state, so the old Worker only cancels its local tasks and persists the
        last stable snapshot.
        """
        state = self._active.get(run_id)
        if state is None:
            return
        for task in (state.task, state.resume_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._persist_state(state)
        self._active.pop(run_id, None)

    async def resume_persisted(self, run_id: str, workflow: WorkflowDefinition) -> int:
        """Resume a non-terminal Run from durable state and checkpoint."""
        if run_id in self._active:
            raise ValueError("Run is already active in this Worker")
        run = self.repository.get_run(run_id)
        if not run:
            raise ValueError("Run not found")
        previous_status = str(run.get("status") or "")
        if previous_status in {RunStatus.SUCCESS.value, RunStatus.FAILED.value, RunStatus.STOPPED.value}:
            raise ValueError("Terminal Run cannot be resumed")
        state = self._state_from_run(run, workflow, reset_inflight=True)
        if state.waiting_step_id:
            waiting_step = next((step for step in workflow.steps if step.id == state.waiting_step_id), None)
            waiting_status = (
                RunStatus.WAITING_APPROVAL.value
                if waiting_step is not None and waiting_step.type == StepType.APPROVAL
                else RunStatus.WAITING_CLARIFICATION.value
            )
            state.recovery_mode = True
            state.execution_status = waiting_status
            self.repository.update_run(
                run_id,
                status=waiting_status,
                execution_status=waiting_status,
                heartbeat_at=utc_now(),
            )
            self._active[run_id] = state
            await self.event_bus.emit(
                "workflow.recovered",
                run_id,
                {
                    "workflowId": workflow.id,
                    "previousStatus": previous_status,
                    "recoveryCount": state.recovery_count,
                    "checkpointThreadId": state.checkpoint_thread_id,
                    "paused": True,
                    "status": waiting_status,
                    "waitingStepId": state.waiting_step_id,
                },
            )
            return state.recovery_count
        state.recovery_count = self.repository.mark_recovery(run_id, status=RunStatus.RUNNING.value)
        self._active[run_id] = state
        state.task = asyncio.current_task()
        await self._run_recovered_state(state, previous_status)
        return state.recovery_count

    def _retry_plan(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
    ) -> tuple[dict[str, Any], RunState, set[str]]:
        """Validate a retry request and return its bounded reset set.

        The HTTP adapter calls this before a Redis job is accepted so an
        unrecoverable Run cannot appear to have started and then fail silently
        in a background Worker.
        """
        if run_id in self._active:
            raise ValueError("Run is still active")
        run = self.repository.get_run(run_id)
        if not run:
            raise ValueError("Run not found")
        status = str(run.get("status") or "")
        if status not in {RunStatus.FAILED.value, RunStatus.STOPPED.value}:
            raise ValueError("Only a failed or stopped Run can be retried")

        state = self._state_from_run(run, workflow)
        reset_ids = {step_id for step_id, step_status in state.results.items() if step_status == StepStatus.FAILED}
        fact_reset_ids, retry_blockers = self._failure_fact_retry_plan(run, workflow)
        reset_ids.update(fact_reset_ids)
        contract_failures = self._contract_reopen_failures(run)
        if contract_failures:
            reopen_count = int(state.context.snapshot().get("contract_reopen_count") or 0)
            max_reopens = max(
                1,
                min(3, int(workflow.meta.get("contract_reopen_attempts", 2) or 2)),
            )
            if reopen_count >= max_reopens:
                raise ValueError("冻结合同已达到最大重新打开次数，请人工检查需求与架构证据")
            architecture_step = next(
                (
                    step.id
                    for step in workflow.steps
                    if step.id == "architecture" or step.agent_id == "architect_agent"
                ),
                None,
            )
            if architecture_step is None:
                raise ValueError("冻结合同存在缺陷，但工作流没有可重新执行的 Architecture Agent")
            reset_ids.add(architecture_step)
        if retry_blockers:
            raise ValueError("当前失败不能自动重试：" + "；".join(retry_blockers[:3]))
        if status == RunStatus.STOPPED.value:
            reset_ids.update(step_id for step_id, step_status in state.results.items() if step_status == StepStatus.RUNNING)
        if not reset_ids and "成果物" in str(run.get("error_message") or ""):
            reset_ids = {
                step.id
                for step in workflow.steps
                if step.type == StepType.AGENT and (step.generation_mode == "artifacts" or step.output_format in {"html", "code"})
            }
        if not reset_ids:
            reset_ids = {
                step.id
                for step, step_status in ((item, state.results.get(item.id)) for item in workflow.steps)
                if step_status == StepStatus.SKIPPED
            }
        if not reset_ids:
            raise ValueError("Run has no recoverable failed node")

        changed = True
        while changed:
            changed = False
            for step in workflow.steps:
                if any(dependency in reset_ids for dependency in step.depends_on) and step.id not in reset_ids:
                    reset_ids.add(step.id)
                    changed = True
        return run, state, reset_ids

    @staticmethod
    def _latest_failure_facts(run: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the latest version of each FailureFact, including legacy rows."""
        facts = run.get("failure_facts")
        if not isinstance(facts, list):
            return []
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, fact in enumerate(facts):
            normalized = normalize_failure_fact(fact, index=index)
            if normalized is None:
                continue
            identity = str(
                normalized.get("failure_id")
                or normalized.get("fingerprint")
                or f"{normalized.get('code', 'UNCLASSIFIED')}:{normalized.get('stage', '')}:{normalized.get('owner', '')}"
            )
            latest[identity] = (index, normalized)
        return [item for _, item in sorted(latest.values(), key=lambda row: row[0])]

    @classmethod
    def _contract_reopen_failures(cls, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Return unresolved post-freeze defects that require Architecture replay."""
        return [
            fact
            for fact in cls._latest_failure_facts(run)
            if not bool(fact.get("resolved"))
            and (
                str(fact.get("repair_action") or "") == "request_blueprint_change"
                or (
                    str(fact.get("category") or "") == "contract_defect"
                    and str(fact.get("owner") or "") == "architecture"
                )
            )
        ]

    @classmethod
    def _failure_fact_retry_plan(
        cls,
        run: dict[str, Any],
        workflow: WorkflowDefinition,
    ) -> tuple[set[str], list[str]]:
        """Map unresolved FailureFacts to bounded workflow reset points.

        Unknown, platform, validator and environment failures are returned as
        blockers so Retry cannot blindly rewrite application source.
        """
        steps_by_id = {step.id: step for step in workflow.steps}
        agent_steps = {
            str(step.agent_id or "").lower(): step.id
            for step in workflow.steps
            if step.type == StepType.AGENT and step.agent_id
        }
        role_steps: dict[str, str] = {}
        for step in workflow.steps:
            role_steps.setdefault(str(step.id or "").lower(), step.id)
            if step.agent_id:
                role_steps.setdefault(str(step.agent_id).lower().removesuffix("_agent"), step.id)

        reset_ids: set[str] = set()
        blockers: list[str] = []
        for fact in cls._latest_failure_facts(run):
            if bool(fact.get("resolved")):
                continue
            category = str(fact.get("category") or "").strip().lower()
            owner = str(fact.get("owner") or "").strip().lower()
            stage = str(fact.get("stage") or "").strip()
            action = str(fact.get("repair_action") or "").strip().lower()
            summary = str(fact.get("summary") or fact.get("message") or fact.get("code") or "未分类失败")

            if action == "request_blueprint_change" or category == "contract_defect":
                architecture = role_steps.get("architecture") or agent_steps.get("architect_agent")
                if architecture:
                    reset_ids.add(architecture)
                else:
                    blockers.append("合同缺陷缺少可执行的 Architecture Agent")
                continue
            if category in {"validator_defect", "environment", "provider", "platform"} or owner in {"platform", "environment"}:
                blockers.append(summary)
                continue

            candidate = None
            if stage in steps_by_id:
                candidate = stage
            elif owner in steps_by_id:
                candidate = owner
            elif owner in agent_steps:
                candidate = agent_steps[owner]
            elif owner in role_steps:
                candidate = role_steps[owner]
            elif owner in {"integration_gate", "tester", "validation"}:
                candidate = role_steps.get("tester") or agent_steps.get("tester_agent")
            elif category in {"requirement", "requirement_routing", "capability_routing", "routing"}:
                candidate = role_steps.get("requirement") or agent_steps.get("requirement_agent")

            explicitly_retryable = bool(fact.get("repairable")) or bool(fact.get("retryable")) or action in {
                "dispatch_owner_repair",
                "create_missing_source_or_repair_reference",
                "repair_agent_output",
                "retry_agent",
            }
            legacy_fact = "repairable" not in fact and "retryable" not in fact and "repair_action" not in fact
            if candidate and (explicitly_retryable or legacy_fact):
                reset_ids.add(candidate)
            elif candidate:
                blockers.append(summary)
            else:
                blockers.append(f"责任不明确：{summary}")
        return reset_ids, list(dict.fromkeys(blockers))

    def _prepare_contract_reopen(
        self,
        state: RunState,
        run: dict[str, Any],
        reset_ids: set[str],
        failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Invalidate a frozen contract and its dependent outputs before replay.

        The old Blueprint is retained as an auditable change request, while the
        execution context is cleared so no downstream Agent can consume stale
        contracts or artifacts during the new Architecture/approval cycle.
        """
        snapshot = state.context.snapshot()
        reopen_count = int(snapshot.get("contract_reopen_count") or 0) + 1
        related_sections = list(
            dict.fromkeys(
                str(fact.get("related_contract") or "delivery_contract")
                for fact in failures
            )
        )
        reason = "；".join(
            str(fact.get("summary") or fact.get("message") or fact.get("code") or "合同缺陷")
            for fact in failures
        )[:2000]
        old_blueprint = state.blueprint or run.get("blueprint")
        change_request: dict[str, Any] = {}
        if isinstance(old_blueprint, dict) and str(old_blueprint.get("status") or "") == "FROZEN":
            change_request = request_blueprint_change(
                old_blueprint,
                source_agent="integration_gate",
                reason=reason,
                affected_sections=related_sections,
                proposed_changes={},
            )

        history = snapshot.get("contract_reopen_history")
        if not isinstance(history, list):
            history = []
        state.context.set("contract_reopen_count", reopen_count)
        state.context.set(
            "contract_reopen_history",
            [
                *history,
                {
                    "attempt": reopen_count,
                    "reason": reason,
                    "affected_sections": related_sections,
                    "failure_ids": [str(item.get("failure_id") or "") for item in failures],
                    "previous_contract_hash": snapshot.get("delivery_contract_hash"),
                    "change_request": change_request,
                },
            ],
        )
        stale_contract_keys = (
            "architecture_raw",
            "architecture_decision",
            "architecture_doc",
            "project_blueprint",
            "compiled_contract",
            "delivery_contract",
            "delivery_contract_hash",
            "execution_plan",
            "artifact_validation",
            "delivery_gate",
            "repair_last_candidate_validation",
        )
        for key in stale_contract_keys:
            state.context.delete(key)
        raw_files = snapshot.get("__artifact_files__")
        if isinstance(raw_files, list):
            state.context.set(
                "__artifact_files__",
                [
                    item
                    for item in raw_files
                    if not isinstance(item, dict)
                    or str(item.get("step_id") or item.get("owner_step") or "") not in reset_ids
                ],
            )
        state.blueprint = None
        state.adaptive_policy = {}
        state.policy_skipped_steps = set()
        state.delivery_status = "NOT_EVALUATED"
        resolved_facts = [
            {**fact, "resolved": True}
            if isinstance(fact, dict)
            and any(fact.get("failure_id") == item.get("failure_id") for item in failures)
            else fact
            for fact in (run.get("failure_facts") or [])
        ]
        state.context.set("failure_facts", resolved_facts)
        self.repository.update_run(
            state.run_id,
            blueprint_json=json.dumps(change_request or {}, ensure_ascii=False),
            failure_facts_json=json.dumps(resolved_facts, ensure_ascii=False),
            delivery_status="NOT_EVALUATED",
        )
        return {
            "attempt": reopen_count,
            "reason": reason,
            "affectedSections": related_sections,
            "resetSteps": sorted(reset_ids),
            "changeRequest": change_request,
        }

    def validate_retry(self, run_id: str, workflow: WorkflowDefinition) -> list[str]:
        """Fail fast before a retry job is sent to a background Worker."""
        _, _, reset_ids = self._retry_plan(run_id, workflow)
        return sorted(reset_ids)

    async def retry(self, run_id: str, workflow: WorkflowDefinition) -> int:
        """Retry a failed Run from its failed node and preserve good work.

        Successful predecessors remain immutable for this recovery attempt;
        only failed nodes and their downstream dependants are reset.  A new
        LangGraph thread is used so a terminal checkpoint from the old attempt
        cannot short-circuit the retry.
        """
        run, state, reset_ids = self._retry_plan(run_id, workflow)
        status = str(run.get("status") or "")
        steps_by_id = {step.id: step for step in workflow.steps}
        contract_failures = self._contract_reopen_failures(run)

        for step_id in reset_ids:
            step = steps_by_id[step_id]
            state.results[step_id] = StepStatus.PENDING
            if step.output:
                state.context.delete(step.output)
            self.repository.upsert_step(
                run_id,
                step_id,
                status=StepStatus.PENDING.value,
                output="",
                finished_at=None,
                duration_ms=0,
                error_message=None,
                input_tokens=0,
                output_tokens=0,
                finish_reason=None,
                message_content=None,
                reasoning_content=None,
                usage_json=json.dumps({}, ensure_ascii=False),
                provider_attempts_json=json.dumps([], ensure_ascii=False),
            )

        reopen_payload = None
        if contract_failures:
            reopen_payload = self._prepare_contract_reopen(
                state, run, reset_ids, contract_failures
            )
        else:
            self._invalidate_retry_outputs(state, reset_ids)

        state.waiting_step_id = None
        state.current_level = 0
        state.recovery_mode = True
        state.recovery_count = self.repository.mark_recovery(run_id, status=RunStatus.RUNNING.value)
        state.checkpoint_thread_id = f"{run_id}:recovery:{state.recovery_count}"
        self.repository.update_run(
            run_id,
            status=RunStatus.RUNNING.value,
            finished_at=None,
            error_message=None,
            final_report=None,
        )
        self._persist_state(state)
        self._active[run_id] = state
        if reopen_payload is not None:
            await self.event_bus.emit(
                "workflow.contract_reopened",
                run_id,
                reopen_payload,
            )
        state.task = asyncio.create_task(self._run_recovered_state(state, status))
        return state.recovery_count

    def _invalidate_retry_outputs(self, state: RunState, reset_ids: set[str]) -> None:
        """Remove only outputs made stale by the selected retry boundary."""
        state.context.delete("delivery_gate")
        state.context.delete("repair_last_candidate_validation")
        state.delivery_status = "NOT_EVALUATED"
        steps_by_id = {step.id: step for step in state.workflow.steps}
        generation_owners = {
            step_id
            for step_id in reset_ids
            if step_id in steps_by_id
            and steps_by_id[step_id].type == StepType.AGENT
            and (
                steps_by_id[step_id].generation_mode in {"artifacts", "coding_loop"}
                or steps_by_id[step_id].output_format in {"html", "code"}
            )
        }
        if reset_ids:
            state.context.delete("artifact_validation")
        raw_files = state.context.snapshot().get("__artifact_files__")
        if generation_owners and isinstance(raw_files, list):
            state.context.set(
                "__artifact_files__",
                [
                    item
                    for item in raw_files
                    if not isinstance(item, dict)
                    or str(item.get("step_id") or item.get("owner_step") or "")
                    not in generation_owners
                ],
            )

    def _build_graph(self, workflow: WorkflowDefinition) -> LangGraphWorkflow:
        return LangGraphWorkflow(workflow, self._graph_step_handler, self.checkpointer)

    def _graph_snapshot(self, state: RunState, step_id: str | None = None) -> dict[str, Any]:
        results = state.results if step_id is None else {step_id: state.results.get(step_id, StepStatus.PENDING)}
        return {
            "context": state.context.snapshot(),
            "results": {key: value.value for key, value in results.items()},
        }

    async def _graph_has_checkpoint(self, state: RunState) -> bool:
        """Check whether LangGraph has a resumable checkpoint for this run."""
        if not state.graph:
            return False
        try:
            snapshot = await state.graph.graph.aget_state(
                {"configurable": {"thread_id": state.checkpoint_thread_id or state.run_id}}
            )
        except Exception:
            return False
        values = getattr(snapshot, "values", None)
        return isinstance(values, dict) and values.get("run_id") == state.run_id

    async def _resolve_graph_approval(
        self,
        state: RunState,
        step: StepDefinition,
        decision: str,
    ) -> dict[str, Any]:
        """Apply a durable approval decision during checkpoint-less replay."""
        self._end_approval_wait(state)
        state.waiting_step_id = None
        self.repository.resolve_approval(state.run_id, step.id, decision, utc_now())
        if decision == "approve":
            self.architecture_contract.freeze(state)
            if state.blueprint:
                await self.event_bus.emit("workflow.blueprint_frozen", state.run_id, {"blueprint": state.blueprint})
                await self.event_bus.emit("workflow.contract_frozen", state.run_id, {"contract": state.context.snapshot().get("delivery_contract"), "contractHash": state.context.snapshot().get("delivery_contract_hash")})
            state.results[step.id] = StepStatus.SUCCESS
            self.repository.upsert_step(
                state.run_id,
                step.id,
                status=StepStatus.SUCCESS.value,
                finished_at=utc_now(),
            )
            self.repository.update_run(state.run_id, status=RunStatus.RUNNING.value)
            await self.event_bus.emit(
                "step.completed",
                state.run_id,
                {
                    "stepId": step.id,
                    "status": StepStatus.SUCCESS.value,
                    "output": "Architecture approved",
                    "durationMs": self._active_duration_ms(state),
                    "approvalDurationMs": self._approval_duration_ms(state),
                },
            )
        else:
            state.results[step.id] = StepStatus.FAILED
            self.repository.upsert_step(
                state.run_id,
                step.id,
                status=StepStatus.FAILED.value,
                error_message="Architecture approval rejected",
                finished_at=utc_now(),
            )
            await self.event_bus.emit(
                "step.failed",
                state.run_id,
                {
                    "stepId": step.id,
                    "status": StepStatus.FAILED.value,
                    "error": "Architecture approval rejected",
                },
            )
        self._persist_state(state)
        return self._graph_snapshot(state, step.id)

    async def _graph_step_handler(self, step: StepDefinition, graph_state: LangGraphState) -> dict[str, Any]:
        run_id = str(graph_state.get("run_id") or "")
        state = self._active.get(run_id)
        if not state:
            raise RuntimeError(f"Run is no longer active: {run_id}")

        for key, value in (graph_state.get("context") or {}).items():
            state.context.set(key, value)
        for step_id, status in (graph_state.get("results") or {}).items():
            try:
                state.results[step_id] = StepStatus(status)
            except ValueError:
                continue

        # A restored run may be reconstructed from SQLite while the original
        # LangGraph checkpoint is unavailable. Do not execute completed nodes
        # a second time during the fallback replay.
        if state.results.get(step.id) in {
            StepStatus.SUCCESS,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        }:
            return self._graph_snapshot(state, step.id)

        # One unified clarification gate sits before the first Agent call.
        # It pauses the graph before any expensive provider request and resumes
        # from the same checkpoint after the user answers.
        clarification_enabled = bool(state.workflow.meta.get("clarification_gate", state.workflow.meta.get("delivery_contract", False)))
        first_agent_step = next((item.id for item in state.workflow.steps if item.type == StepType.AGENT), "architecture")
        if step.id == first_agent_step and clarification_enabled and not state.context.snapshot().get("clarification_answers"):
            spec = state.requirement_spec or self.capability_router.route(
                str(state.context.snapshot().get("requirement", ""))
            )
            state.requirement_spec = spec
            requirement_state, request = self.clarification_service.assess(spec)
            state.context.set("requirement_state", requirement_state)
            state.context.set("requirement_spec", spec.model_dump(mode="json"))
            if request is None:
                # Downstream control nodes consume a stable confirmation field.
                # An empty object means no human clarification was required;
                # leaving the variable undefined would make strict template
                # rendering fail after Requirement has already succeeded.
                state.context.set("clarification_answers", {})
            if request is not None:
                state.clarification_request = request.model_dump(mode="json")
                state.waiting_step_id = "__clarification__"
                state.execution_status = RunStatus.WAITING_CLARIFICATION.value
                state.results[step.id] = StepStatus.WAITING_CLARIFICATION
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.WAITING_CLARIFICATION.value,
                    started_at=utc_now(),
                )
                self.repository.update_run(
                    state.run_id,
                    status=RunStatus.WAITING_CLARIFICATION.value,
                    execution_status=RunStatus.WAITING_CLARIFICATION.value,
                    clarification_json=json.dumps(state.clarification_request, ensure_ascii=False),
                )
                self._persist_state(state)
                await self.event_bus.emit(
                    "step.waiting_clarification",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.WAITING_CLARIFICATION.value,
                        "request": state.clarification_request,
                    },
                )
                await self.event_bus.emit(
                    "workflow.clarification_required",
                    state.run_id,
                    {"status": RunStatus.WAITING_CLARIFICATION.value, "request": state.clarification_request},
                )
                resume_payload = graph_state.get("resume_payload")
                if isinstance(resume_payload, dict):
                    answer = resume_payload
                else:
                    answer = interrupt(state.clarification_request)
                if not isinstance(answer, dict):
                    raise ValueError("Requirement clarification must be an object")
                updated_spec, answered_request, updates = self.clarification_service.apply_answer(spec, request, answer)
                state.requirement_spec = updated_spec
                state.clarification_request = answered_request.model_dump(mode="json")
                state.context.set("requirement_spec", updated_spec.model_dump(mode="json"))
                state.context.set("requirement_state", updates["requirement_state"])
                state.context.set("clarification_request", state.clarification_request)
                state.context.set("clarification_answers", updates["clarification_answers"])
                state.context.set("assumption_log", updates["assumption_log"])
                state.waiting_step_id = None
                state.execution_status = RunStatus.RUNNING.value
                self.repository.update_run(
                    state.run_id,
                    status=RunStatus.RUNNING.value,
                    execution_status=RunStatus.RUNNING.value,
                    clarification_json=json.dumps(state.clarification_request, ensure_ascii=False),
                    assumption_log_json=json.dumps(updates["assumption_log"], ensure_ascii=False),
                )
                await self.event_bus.emit(
                    "workflow.clarification_answered",
                    state.run_id,
                    {"status": RunStatus.RUNNING.value, "answers": updates["clarification_answers"]},
                )

        if step.type == StepType.APPROVAL:
            steps_by_id = {item.id: item for item in state.workflow.steps}
            if any(not self._dependency_satisfied(state, dependency, steps_by_id) for dependency in step.depends_on):
                state.results[step.id] = StepStatus.SKIPPED
                self.repository.upsert_step(state.run_id, step.id, status=StepStatus.SKIPPED.value, finished_at=utc_now(), error_message="上游失败，不能进行人工审批")
                await self.event_bus.emit("step.skipped", state.run_id, {"stepId": step.id, "status": StepStatus.SKIPPED.value, "reason": "上游失败，不能进行人工审批"})
                return self._graph_snapshot(state, step.id)
            state.results[step.id] = StepStatus.WAITING_APPROVAL
            resume_decision = graph_state.get("resume_decision")
            if resume_decision and graph_state.get("resume_step_id") == step.id:
                return await self._resolve_graph_approval(state, step, str(resume_decision))
            if state.waiting_step_id != step.id:
                self._begin_approval_wait(state)
                state.waiting_step_id = step.id
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.WAITING_APPROVAL.value,
                    started_at=utc_now(),
                )
                self.repository.create_approval(state.run_id, step.id, utc_now())
                self.repository.update_run(state.run_id, status=RunStatus.WAITING_APPROVAL.value)
                self._persist_state(state)
                await self.event_bus.emit(
                    "step.waiting_approval",
                    state.run_id,
                    {"stepId": step.id, "status": StepStatus.WAITING_APPROVAL.value},
                )
                await self.event_bus.emit(
                    "workflow.waiting_approval",
                    state.run_id,
                    {"stepId": step.id, "status": RunStatus.WAITING_APPROVAL.value, "durationMs": self._active_duration_ms(state), "approvalDurationMs": self._approval_duration_ms(state)},
                )

            decision = str(interrupt({"stepId": step.id, "message": "请确认该 Workflow 节点后继续"}))
            self._end_approval_wait(state)
            state.waiting_step_id = None
            self.repository.resolve_approval(state.run_id, step.id, decision, utc_now())
            if decision == "approve":
                self.architecture_contract.freeze(state)
                if state.blueprint:
                    await self.event_bus.emit("workflow.blueprint_frozen", state.run_id, {"blueprint": state.blueprint})
                    await self.event_bus.emit("workflow.contract_frozen", state.run_id, {"contract": state.context.snapshot().get("delivery_contract"), "contractHash": state.context.snapshot().get("delivery_contract_hash")})
                state.results[step.id] = StepStatus.SUCCESS
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.SUCCESS.value,
                    finished_at=utc_now(),
                )
                self.repository.update_run(state.run_id, status=RunStatus.RUNNING.value)
                await self.event_bus.emit(
                    "step.completed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.SUCCESS.value,
                        "output": "Architecture approved",
                        "durationMs": self._active_duration_ms(state),
                        "approvalDurationMs": self._approval_duration_ms(state),
                    },
                )
            else:
                state.results[step.id] = StepStatus.FAILED
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.FAILED.value,
                    error_message="Architecture approval rejected",
                    finished_at=utc_now(),
                )
                await self.event_bus.emit(
                    "step.failed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.FAILED.value,
                        "error": "Architecture approval rejected",
                    },
                )
            self._persist_state(state)
            return self._graph_snapshot(state, step.id)

        await self._execute_step(state, step)
        self._persist_state(state)
        return self._graph_snapshot(state, step.id)

    async def _finalize_graph(self, state: RunState, result: dict[str, Any] | None) -> None:
        if result:
            for key, value in (result.get("context") or {}).items():
                state.context.set(key, value)
            for step_id, status in (result.get("results") or {}).items():
                try:
                    state.results[step_id] = StepStatus(status)
                except ValueError:
                    continue
        failure_policy_by_step = {step.id: step.failure_policy for step in state.workflow.steps}
        final_report = state.context.snapshot().get("final_report")
        if any(
            status == StepStatus.FAILED and failure_policy_by_step.get(step_id, "fail") != "continue"
            for step_id, status in state.results.items()
        ):
            # Keep generated files available for diagnosis even when the
            # validation gate fails. A failed run must not erase the exact
            # inputs that the Tester Agent needs to explain and repair.
            raw_files = state.context.snapshot().get("__artifact_files__")
            if self.artifact_service and isinstance(raw_files, list) and raw_files:
                await self.artifact_delivery.materialize(state, str(final_report or ""))
            await self._finish(state, RunStatus.FAILED, "One or more workflow steps failed")
            return
        if self.artifact_service:
            artifact_gaps = self.artifact_delivery.completeness_gaps(state)
            if artifact_gaps:
                message = "成果物不完整：" + "；".join(artifact_gaps)
                await self.event_bus.emit(
                    "artifact.incomplete",
                    state.run_id,
                    {
                        "error": message,
                        "missing": artifact_gaps,
                    },
                )
                await self._finish(state, RunStatus.FAILED, message)
                return
            try:
                artifacts = await asyncio.to_thread(
                    self.artifact_service.materialize,
                    state.run_id,
                    state.workflow.id,
                    state.context.snapshot(),
                    str(final_report or ""),
                )
                for artifact in artifacts:
                    await self.event_bus.emit(
                        "artifact.created",
                        state.run_id,
                        {"artifact": artifact},
                    )
            except Exception as exc:
                message = str(exc) or "Artifact materialization failed"
                await self.event_bus.emit(
                    "artifact.failed",
                    state.run_id,
                    {"error": message},
                )
                await self._finish(state, RunStatus.FAILED, f"成果物写入失败：{message}")
                return
        if state.workflow.meta.get("delivery_contract"):
            from .delivery_gate import evaluate_delivery
            snapshot = state.context.snapshot()
            archive = None
            if self.artifact_service:
                archive = await asyncio.to_thread(self.artifact_service.create_archive, state.run_id, strict=True)
            proof = self._delivery_proof(state)
            state.context.set("artifact_validation", proof)
            gate = evaluate_delivery(snapshot, proof, archive)
            state.context.set("delivery_gate", gate)
            if self.artifact_service:
                await self.artifact_delivery.materialize(state, str(final_report or ""))
                archive = await asyncio.to_thread(self.artifact_service.create_archive, state.run_id, strict=True)
                gate = evaluate_delivery(state.context.snapshot(), proof, archive)
                state.context.set("delivery_gate", gate)
            if not gate["deliverable"]:
                gate = await self._attempt_delivery_gate_repair(state, gate, str(final_report or ""))
            await self._record_delivery_gate_evidence(state, gate)
            await self.event_bus.emit("workflow.delivery_checked", state.run_id, gate)
            if not gate["deliverable"]:
                await self._finish(state, RunStatus.FAILED, "交付门禁未通过：" + "；".join(gate["missing"]))
                return
        await self._finish(state, RunStatus.SUCCESS, final_report=final_report)

    @staticmethod
    def _delivery_proof(state: RunState) -> dict[str, Any]:
        proof = dict(state.context.snapshot().get("artifact_validation") or {})
        checks = [item for item in proof.get("checks", []) if isinstance(item, dict)]
        approval_steps = [step for step in state.workflow.steps if step.type == StepType.APPROVAL]
        if approval_steps and not any(item.get("id") == "capability-human_approval" for item in checks):
            checks.append(
                {
                    "id": "capability-human_approval",
                    "status": "passed"
                    if all(state.results.get(step.id) == StepStatus.SUCCESS for step in approval_steps)
                    else "blocked",
                    "message": "人工审批结果",
                }
            )
        proof["checks"] = checks
        return proof

    async def _attempt_delivery_gate_repair(
        self,
        state: RunState,
        gate: dict[str, Any],
        final_report: str,
    ) -> dict[str, Any]:
        """Re-enter the existing Tester repair loop for late delivery defects.

        A frozen contract defect is intentionally excluded: it must reopen the
        Architecture contract and obtain human approval instead of asking code
        Agents to implement around a bad contract.
        """
        max_attempts = max(
            0,
            min(2, int(state.workflow.meta.get("delivery_repair_attempts", 1) or 0)),
        )
        missing = {str(item) for item in gate.get("missing", [])}
        if {"delivery-contract", "delivery-api-contract"} & missing:
            return gate
        tester = next(
            (
                step
                for step in state.workflow.steps
                if step.type == StepType.AGENT
                and (step.id == "tester" or step.agent_id == "tester_agent")
                and isinstance(step.validation, dict)
                and bool(step.validation.get("enabled"))
            ),
            None,
        )
        from .delivery_gate import evaluate_delivery

        async def seal_delivery(candidate_gate: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
            """Write final metadata, rebuild ZIP, then verify that exact archive."""
            if not self.artifact_service:
                return candidate_gate
            state.context.set("delivery_gate", candidate_gate)
            await self.artifact_delivery.materialize(state, final_report)
            archive_path = await asyncio.to_thread(
                self.artifact_service.create_archive,
                state.run_id,
                strict=True,
            )
            sealed = evaluate_delivery(state.context.snapshot(), proof, archive_path)
            state.context.set("delivery_gate", sealed)
            if sealed.get("deliverable") and candidate_gate != sealed:
                # The first rebuilt archive necessarily contains the previous
                # gate in delivery-report.json. Seal once more so metadata and
                # the verified final decision describe the same snapshot.
                await self.artifact_delivery.materialize(state, final_report)
                archive_path = await asyncio.to_thread(
                    self.artifact_service.create_archive,
                    state.run_id,
                    strict=True,
                )
                sealed = evaluate_delivery(state.context.snapshot(), proof, archive_path)
                state.context.set("delivery_gate", sealed)
            return sealed

        # ZIP corruption, stale packaging and missing archive entries are
        # platform delivery defects. Re-materialize and verify once before
        # asking any source Agent to modify already validated code.
        if "delivery-archive" in missing and self.artifact_service:
            await self.event_bus.emit(
                "delivery.archive_rebuild_started",
                state.run_id,
                {"owner": "platform", "missing": sorted(missing)},
            )
            proof = self._delivery_proof(state)
            rebuilt = await seal_delivery(gate, proof)
            await self.event_bus.emit(
                "delivery.archive_rebuild_completed",
                state.run_id,
                {
                    "owner": "platform",
                    "passed": bool(rebuilt.get("deliverable")),
                    "missing": sorted(str(item) for item in rebuilt.get("missing", [])),
                },
            )
            gate = rebuilt
            missing = {str(item) for item in gate.get("missing", [])}
            if gate.get("deliverable") or missing == {"delivery-archive"}:
                return gate

        if not max_attempts or tester is None:
            return gate

        previous_missing = sorted(missing)
        for attempt in range(1, max_attempts + 1):
            await self.event_bus.emit(
                "delivery.repair_started",
                state.run_id,
                {
                    "stepId": tester.id,
                    "repairAttempt": attempt,
                    "missing": previous_missing,
                    "reason": "最终交付门禁发现了 Tester 需要重新验证的成果物或证据缺陷",
                },
            )
            state.results[tester.id] = StepStatus.PENDING
            await self._execute_step(state, tester)
            if state.results.get(tester.id) != StepStatus.SUCCESS:
                await self.event_bus.emit(
                    "delivery.repair_completed",
                    state.run_id,
                    {
                        "stepId": tester.id,
                        "repairAttempt": attempt,
                        "passed": False,
                        "madeProgress": False,
                        "missing": previous_missing,
                    },
                )
                return gate

            proof = self._delivery_proof(state)
            state.context.set("artifact_validation", proof)
            archive = None
            if self.artifact_service:
                await self.artifact_delivery.materialize(state, final_report)
                archive = await asyncio.to_thread(
                    self.artifact_service.create_archive,
                    state.run_id,
                    strict=True,
                )
            repaired_gate = evaluate_delivery(state.context.snapshot(), proof, archive)
            state.context.set("delivery_gate", repaired_gate)
            if repaired_gate.get("deliverable"):
                repaired_gate = await seal_delivery(repaired_gate, proof)
            repaired_missing = sorted(str(item) for item in repaired_gate.get("missing", []))
            made_progress = repaired_gate.get("deliverable") or repaired_missing != previous_missing
            await self.event_bus.emit(
                "delivery.repair_completed",
                state.run_id,
                {
                    "stepId": tester.id,
                    "repairAttempt": attempt,
                    "passed": bool(repaired_gate.get("deliverable")),
                    "madeProgress": bool(made_progress),
                    "missing": repaired_missing,
                },
            )
            gate = repaired_gate
            if repaired_gate.get("deliverable") or not made_progress:
                return repaired_gate
            previous_missing = repaired_missing
        return gate

    async def _record_delivery_gate_evidence(
        self,
        state: RunState,
        gate: dict[str, Any],
    ) -> None:
        delivery_checks = gate.get("checks") if isinstance(gate, dict) else []
        if not isinstance(delivery_checks, list):
            return
        delivery_evidence = [
            evidence_from_check(item, gate="delivery", index=index)
            for index, item in enumerate(delivery_checks)
            if isinstance(item, dict)
        ]
        delivery_failures = [
            failure_fact_from_check(
                item,
                gate="delivery",
                owner="platform" if str(item.get("id") or "") == "delivery-archive" else "integration_gate",
                index=index,
            )
            for index, item in enumerate(delivery_checks)
            if isinstance(item, dict) and str(item.get("status")) != "passed"
        ]
        snapshot = state.context.snapshot()
        current_evidence = snapshot.get("evidence")
        non_delivery_evidence = [
            item
            for item in (current_evidence if isinstance(current_evidence, list) else [])
            if not isinstance(item, dict) or str(item.get("gate") or "") != "delivery"
        ]
        all_evidence = [*non_delivery_evidence, *delivery_evidence]
        state.context.set("delivery_evidence", delivery_evidence)
        state.context.set("evidence", all_evidence)

        current_failures = snapshot.get("failure_facts")
        historical_failures = [
            item for item in (current_failures if isinstance(current_failures, list) else [])
            if isinstance(item, dict)
        ]
        current_failure_ids = {str(item.get("failure_id") or "") for item in delivery_failures}
        retained_failures: list[dict[str, Any]] = []
        for item in historical_failures:
            if str(item.get("gate") or "") != "delivery":
                retained_failures.append(item)
            elif str(item.get("failure_id") or "") not in current_failure_ids:
                retained_failures.append({**item, "resolved": True})
        all_failures = [*retained_failures, *delivery_failures]
        state.context.set("failure_facts", all_failures)
        self.repository.update_run(
            state.run_id,
            evidence_json=json.dumps(all_evidence, ensure_ascii=False),
            failure_facts_json=json.dumps(all_failures, ensure_ascii=False),
        )
        await self.event_bus.emit(
            "integration.evidence",
            state.run_id,
            {"evidence": delivery_evidence, "failureFacts": delivery_failures},
        )

    async def _resume_graph(self, state: RunState, decision: Any) -> None:
        try:
            if not state.graph:
                state.graph = self._build_graph(state.workflow)
            if await self._graph_has_checkpoint(state):
                result = await state.graph.resume(decision, run_id=state.checkpoint_thread_id or state.run_id)
            else:
                # Rebuild from durable SQLite state when the previous
                # checkpointer was in-memory and disappeared on restart.
                result = await state.graph.ainvoke(
                    {
                        "run_id": state.run_id,
                        **self._graph_snapshot(state),
                        "resume_decision": decision,
                        "resume_step_id": state.waiting_step_id or "",
                        "resume_payload": decision,
                    },
                    run_id=state.checkpoint_thread_id or state.run_id,
                )
            if LangGraphWorkflow.is_interrupted(result):
                return
            await self._finalize_graph(state, result)
        except GraphInterrupt:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish(state, RunStatus.FAILED, str(exc))

    async def _run_state(self, state: RunState) -> None:
        if not state.graph:
            state.graph = self._build_graph(state.workflow)
        if state.current_level == 0:
            self.repository.update_run(state.run_id, status=RunStatus.RUNNING.value)
            await self.event_bus.emit(
                "workflow.started",
                state.run_id,
                {"workflowId": state.workflow.id, "status": RunStatus.RUNNING.value, "engine": "langgraph"},
            )
            if state.requirement_spec:
                spec = state.requirement_spec
                await self.event_bus.emit(
                    "workflow.requirement_routed",
                    state.run_id,
                    {
                        "schemaVersion": spec.schema_version,
                        "routerVersion": spec.router_version,
                        "requiredCapabilities": spec.required_capabilities,
                        "optionalCapabilities": spec.optional_capabilities,
                        "evidence": spec.evidence,
                        "confidence": spec.confidence,
                        "needsClarification": spec.needs_clarification,
                        "state": spec.state,
                        "impactfulGaps": spec.impactful_gaps,
                        "clarificationQuestions": spec.clarification_questions,
                    },
                )
        try:
            result = await state.graph.ainvoke(
                {"run_id": state.run_id, **self._graph_snapshot(state)},
                run_id=state.checkpoint_thread_id or state.run_id,
            )
            if LangGraphWorkflow.is_interrupted(result):
                return
            await self._finalize_graph(state, result)
        except GraphInterrupt:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish(state, RunStatus.FAILED, str(exc))

    def _runtime_for_step(self, state: RunState, step: StepDefinition) -> dict[str, Any]:
        """Compatibility shim for extensions that called the former helper."""
        return self.runtime_policy.resolve(state, step)

    @staticmethod
    def _runtime_plan_target_step_ids(state: RunState, planner_step_id: str) -> set[str]:
        return RuntimePolicyResolver.runtime_plan_target_step_ids(state, planner_step_id)

    @staticmethod
    def _confirmed_requirement_for_budget(state: RunState) -> str:
        return RuntimePolicyResolver.confirmed_requirement_for_budget(state)

    @staticmethod
    def _generation_mode(state: RunState, step: StepDefinition) -> str:
        return RuntimePolicyResolver.generation_mode(state, step)

    async def _skip_by_policy(self, state: RunState, step: StepDefinition) -> None:
        skip_reason = str(
            state.adaptive_policy.get("skip_reasons", {}).get(step.id)
            or "Architecture 与需求能力路由确认该步骤不是必需能力。"
        )
        state.results[step.id] = StepStatus.SKIPPED
        if step.output:
            state.context.set(step.output, f"Not required: {skip_reason}")
            state.context.set(f"{step.output}_files", "[]")
        self.repository.upsert_step(
            state.run_id,
            step.id,
            agent_id=step.agent_id,
            status=StepStatus.SKIPPED.value,
            finished_at=utc_now(),
            error_message=f"Skipped by adaptive capability policy: {skip_reason}",
        )
        await self.event_bus.emit(
            "step.skipped",
            state.run_id,
            {
                "stepId": step.id,
                "status": StepStatus.SKIPPED.value,
                "reason": skip_reason,
            },
        )

    @staticmethod
    def _dependency_satisfied(state: RunState, dependency: str, steps: dict[str, StepDefinition]) -> bool:
        status = state.results.get(dependency)
        return status == StepStatus.SUCCESS or (status == StepStatus.SKIPPED and dependency in state.policy_skipped_steps) or (status == StepStatus.FAILED and steps[dependency].failure_policy == "continue")

    def _validate_architecture_candidate(self, candidate: dict[str, Any], requirement_spec: RequirementSpec) -> dict[str, Any]:
        """Backward-compatible entry point for architecture repair tests."""
        return self.architecture_contract.validate_candidate(candidate, requirement_spec)

    async def _execute_step(self, state: RunState, step: StepDefinition) -> None:
        if step.id in state.policy_skipped_steps and step.type == StepType.AGENT:
            await self._skip_by_policy(state, step)
            return
        # 可选控制节点允许失败后使用确定性兜底；普通失败仍会阻断下游。
        steps_by_id = {item.id: item for item in state.workflow.steps}
        if any(
            not self._dependency_satisfied(state, dependency, steps_by_id)
            for dependency in step.depends_on
        ):
            state.results[step.id] = StepStatus.SKIPPED
            self.repository.upsert_step(state.run_id, step.id, status=StepStatus.SKIPPED.value, finished_at=utc_now(), error_message="Dependency failed or was skipped")
            await self.event_bus.emit("step.skipped", state.run_id, {"stepId": step.id, "status": StepStatus.SKIPPED.value})
            return

        state.results[step.id] = StepStatus.RUNNING
        started_at = utc_now()
        started_perf = time.perf_counter()
        self.repository.upsert_step(state.run_id, step.id, agent_id=step.agent_id, status=StepStatus.RUNNING.value, started_at=started_at)
        await self.event_bus.emit("step.started", state.run_id, {"stepId": step.id, "status": StepStatus.RUNNING.value, "agentId": step.agent_id})

        response: LLMResponse | None = None
        parsed_runtime_plan: dict[str, Any] | None = None
        retry_count = 0
        provider_attempts: list[dict[str, Any]] = []
        last_provider_record: dict[str, Any] = {}
        total_input_tokens = 0
        total_output_tokens = 0
        artifact_validation_result: ArtifactValidationResult | None = None
        validation_failure_summary: str | None = None
        try:
            if state.workflow.meta.get("delivery_contract") and step.id in {"database", "backend", "frontend", "tester", "reviewer"}:
                frozen = state.context.snapshot().get("delivery_contract")
                expected_hash = state.context.snapshot().get("delivery_contract_hash")
                if not isinstance(frozen, dict) or contract_hash(frozen) != expected_hash:
                    raise ValueError("交付合同缺失或已变更，停止执行；请重新进行架构确认。")
            agent = self.registry.get(step.agent_id or "")
            runtime = self.runtime_policy.resolve(state, step)
            skill_resolution = self.skill_resolver.resolve(
                step,
                state.requirement_spec,
                runtime,
                agent.system_prompt,
            )
            skill_audit = skill_resolution.audit_payload()
            existing_skill_resolutions = state.context.snapshot().get("__skill_resolutions__", {})
            if not isinstance(existing_skill_resolutions, dict):
                existing_skill_resolutions = {}
            state.context.set(
                "__skill_resolutions__",
                {**existing_skill_resolutions, step.id: skill_audit},
            )
            await self.event_bus.emit(
                "step.skills_resolved",
                state.run_id,
                {"stepId": step.id, "status": StepStatus.RUNNING.value, **skill_audit},
            )
            effective_system_prompt = skill_resolution.system_prompt
            budget_plan = runtime.get("_budget_plan")
            if isinstance(budget_plan, dict):
                await self.event_bus.emit(
                    "step.budget_planned",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.RUNNING.value,
                        "configuredMaxTokens": budget_plan.get("configured_max_tokens"),
                        "recommendedMaxTokens": budget_plan.get("recommended_max_tokens"),
                        "plannedMaxTokens": budget_plan.get("planned_max_tokens"),
                        "difficulty": budget_plan.get("difficulty"),
                        "difficultyScore": budget_plan.get("difficulty_score"),
                        "difficultySignals": budget_plan.get("difficulty_signals", []),
                        "source": budget_plan.get("source"),
                    },
                )
            effective_thinking, thinking_warning = self.provider.resolve_thinking(runtime["thinking"])
            if thinking_warning:
                await self.event_bus.emit("step.runtime_adjusted", state.run_id, {"stepId": step.id, "requestedThinking": runtime["thinking"], "effectiveThinking": effective_thinking, "warning": thinking_warning})
            capabilities = self.provider.capabilities()
            validation_config = dict(step.validation) if isinstance(step.validation, dict) else {}
            validation_config["run_id"] = state.run_id
            if validation_config.get("enabled"):
                async def emit_validation_event(event_type: str, payload: dict[str, Any]) -> None:
                    await self.event_bus.emit(
                        event_type,
                        state.run_id,
                        {"stepId": step.id, "status": StepStatus.RUNNING.value, **payload},
                    )

                artifact_validation_result = await self.verification_engine.validate_artifacts(
                    state.context.snapshot(),
                    validation_config,
                    emit=emit_validation_event,
                )
                repair_attempts = max(0, min(4, int(validation_config.get("repair_attempts", 0) or 0)))
                committed_context = state.context.snapshot()
                committed_validation = artifact_validation_result
                candidate_state = replace(state, context=WorkflowContext(committed_context))
                owner_reexecution_used = False
                candidate_workspace = None
                candidate_snapshots: dict[int, dict[str, Any]] = {}
                candidate_validation_snapshots: dict[int, ArtifactValidationResult] = {}
                repaired_by_attempt: dict[int, list[str]] = {}
                consultation_guidance: dict[str, str] = {}
                latest_candidate_validation: ArtifactValidationResult | None = None

                async def emit_repair_event(event_type: str, payload: dict[str, Any]) -> None:
                    await self.event_bus.emit(
                        event_type,
                        state.run_id,
                        {"stepId": step.id, "status": StepStatus.RUNNING.value, **payload},
                    )

                async def consult_repair(
                    repair_plan: dict[str, Any],
                    validation: ArtifactValidationResult,
                    repair_attempt: int,
                ) -> dict[str, Any]:
                    nonlocal total_input_tokens, total_output_tokens, last_provider_record, consultation_guidance
                    policy = state.workflow.meta.get("collaboration")
                    if not isinstance(policy, dict) or policy.get("mode") != "on_failure":
                        return {}
                    owner_steps = {
                        item.id: item for item in state.workflow.steps
                        if item.type == StepType.AGENT and item.agent_id
                    }
                    owners = [
                        owner for owner in repair_plan.get("owners", [])
                        if owner in owner_steps
                    ][:3]
                    if len(owners) < 2:
                        return {}
                    rounds = max(1, min(2, int(policy.get("max_rounds", 2))))
                    consultation_timeout = max(5.0, min(60.0, float(policy.get("timeout_seconds", 40))))
                    model_timeout = min(20.0, consultation_timeout / rounds)
                    max_tokens = max(1, min(
                        800,
                        max(1, int(policy.get("max_tokens_per_message", 400))),
                        self.token_budget_manager.provider_max_tokens(capabilities),
                    ))
                    compiled = state.context.snapshot().get("compiled_contract")
                    role_contracts = compiled.get("role_contracts", {}) if isinstance(compiled, dict) else {}

                    async def generate_consultation(owner: str, prompt: str) -> LLMResponse:
                        agent_definition = self.registry.get(owner_steps[owner].agent_id or "")
                        return await self.model_invocation.generate(
                            agent_definition.system_prompt + "\n当前只进行跨 Agent 故障协商；不要生成代码或改变冻结合同。",
                            prompt,
                            {
                                "agent_id": agent_definition.id,
                                "generation_phase": "cross_agent_consultation",
                                "max_tokens": max_tokens,
                                "effective_thinking": "off",
                                "thinking_type": "disabled",
                                "reasoning_effort": "off",
                                "connect_timeout": 10,
                                "read_timeout": model_timeout,
                                "write_timeout": 10,
                                "pool_timeout": 10,
                            },
                            model_timeout,
                        )

                    outcome = await self.collaboration_coordinator.consult(
                        run_id=state.run_id,
                        fingerprint=str(repair_plan.get("fingerprint") or ""),
                        owners=owners,
                        evidence=[check.as_dict() for check in validation.checks],
                        role_contracts=role_contracts if isinstance(role_contracts, dict) else {},
                        generate=generate_consultation,
                        emit=emit_repair_event,
                        max_rounds=rounds,
                        timeout_seconds=consultation_timeout,
                    )
                    for consultation_response in outcome.responses:
                        total_input_tokens += consultation_response.input_tokens
                        total_output_tokens += consultation_response.output_tokens
                        last_provider_record = consultation_response.provider_record()
                        provider_attempts.append(last_provider_record)
                    if outcome.responses:
                        self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                    consultation_guidance = dict(outcome.guidance)
                    return {
                        "conversation_id": outcome.conversation_id,
                        "status": outcome.status,
                        "guidance": outcome.guidance,
                    }

                async def prepare_candidate(repair_plan: dict[str, Any], repair_attempt: int) -> None:
                    nonlocal candidate_workspace
                    candidate_snapshots[repair_attempt] = candidate_state.context.snapshot()
                    candidate_validation_snapshots[repair_attempt] = repair_coordinator_validation["value"]
                    files = candidate_state.context.snapshot().get("__artifact_files__", [])
                    if candidate_workspace is None:
                        candidate_workspace = self.code_company_runtime.prepare_candidate(
                            state.workspace or {},
                            f"repair-{step.id}-{uuid.uuid4().hex[:10]}",
                            list(repair_plan.get("owners") or []),
                            files if isinstance(files, list) else [],
                        )
                        await emit_repair_event("repair.candidate_created", {
                            "repairAttempt": repair_attempt,
                            "candidate": candidate_workspace.as_dict(),
                        })
                    else:
                        self.code_company_runtime.update_candidate(
                            candidate_workspace,
                            files if isinstance(files, list) else [],
                        )

                async def reject_candidate(repair_plan: dict[str, Any], repair_attempt: int) -> None:
                    snapshot = candidate_snapshots.get(repair_attempt, committed_context)
                    candidate_state.context = WorkflowContext(snapshot)
                    repair_coordinator_validation["value"] = candidate_validation_snapshots.get(
                        repair_attempt,
                        committed_validation,
                    )
                    files = snapshot.get("__artifact_files__", [])
                    if candidate_workspace is not None:
                        self.code_company_runtime.update_candidate(
                            candidate_workspace,
                            files if isinstance(files, list) else [],
                        )
                    await emit_repair_event("step.validation_candidate_rejected", {
                        "repairAttempt": repair_attempt,
                        "owners": repair_plan.get("owners", []),
                        "reason": "候选整改未推进目标 Gate，已撤回；不以错误文本变化作为修复成功。",
                    })

                async def commit_candidate(repair_plan: dict[str, Any], repair_attempt: int) -> None:
                    nonlocal candidate_workspace
                    self._commit_artifact_revision(candidate_state, committed_context)
                    if candidate_workspace is not None:
                        candidate_workspace = self.code_company_runtime.promote_candidate(candidate_workspace)
                    state.context = candidate_state.context
                    self._persist_state(state)
                    await emit_repair_event("repair.candidate_promoted", {
                        "repairAttempt": repair_attempt,
                        "owners": repair_plan.get("owners", []),
                        "candidate": candidate_workspace.as_dict() if candidate_workspace is not None else {},
                    })

                async def repair_candidate(
                    repair_plan: dict[str, Any],
                    repair_attempt: int,
                    repair_mode: str,
                ) -> tuple[list[str], list[LLMResponse]]:
                    nonlocal owner_reexecution_used, total_input_tokens, total_output_tokens, last_provider_record
                    consultation = repair_plan.get("consultation")
                    guidance = consultation.get("guidance") if isinstance(consultation, dict) else consultation_guidance
                    candidate_state.context.set(
                        "__collaboration_guidance__",
                        guidance if isinstance(guidance, dict) else {},
                    )
                    validation = repair_coordinator_validation["value"]
                    if repair_mode == "grounded_multi_file_patch" and not owner_reexecution_used:
                        repaired_files, repair_responses = await self._reexecute_validation_owners(
                            candidate_state, validation, repair_attempt, step.id,
                        )
                        owner_reexecution_used = True
                    else:
                        repaired_files, repair_responses = await self._repair_failed_artifacts(
                            candidate_state,
                            validation,
                            repair_attempt,
                            step.id,
                            step.timeout_seconds,
                            self.token_budget_manager.provider_max_tokens(capabilities),
                        )
                        if not repaired_files and not owner_reexecution_used:
                            repaired_files, repair_responses = await self._reexecute_validation_owners(
                                candidate_state, validation, repair_attempt, step.id,
                            )
                            owner_reexecution_used = True
                    repaired_by_attempt[repair_attempt] = list(repaired_files)
                    for repair_response in repair_responses:
                        total_input_tokens += repair_response.input_tokens
                        total_output_tokens += repair_response.output_tokens
                        last_provider_record = repair_response.provider_record()
                        provider_attempts.append(last_provider_record)
                    if provider_attempts:
                        self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                    if candidate_workspace is not None:
                        files = candidate_state.context.snapshot().get("__artifact_files__", [])
                        self.code_company_runtime.update_candidate(
                            candidate_workspace,
                            files if isinstance(files, list) else [],
                        )
                    return repaired_files, repair_responses

                async def validate_target(repair_plan: dict[str, Any], repair_attempt: int) -> ArtifactValidationResult:
                    nonlocal latest_candidate_validation
                    owners = list(repair_plan.get("owners") or [])
                    targets = list(dict.fromkeys("backend" if owner == "database" else owner for owner in owners))
                    target_config = {**validation_config, "targets": targets or "auto", "validation_scope": "targeted"}
                    before = repair_coordinator_validation["value"]
                    result = await self.verification_engine.validate_artifacts(
                        candidate_state.context.snapshot(), target_config, emit=emit_validation_event,
                    )
                    latest_candidate_validation = result
                    repair_coordinator_validation["value"] = result
                    await emit_repair_event("step.validation_repaired", {
                        "repairAttempt": repair_attempt,
                        "files": repaired_by_attempt.get(repair_attempt, []),
                        "passed": result.passed,
                        "repairMode": repair_plan.get("strategy"),
                        "failureFingerprint": self._validation_failure_fingerprint(result),
                        "madeProgress": self.repair_engine.made_progress(before, result),
                        "validationScope": "targeted",
                    })
                    return result

                async def validate_full(repair_attempt: int) -> ArtifactValidationResult:
                    nonlocal latest_candidate_validation
                    result = await self.verification_engine.validate_artifacts(
                        candidate_state.context.snapshot(),
                        {**validation_config, "validation_scope": "full_regression"},
                        emit=emit_validation_event,
                    )
                    latest_candidate_validation = result
                    repair_coordinator_validation["value"] = result
                    return result

                repair_coordinator_validation = {"value": artifact_validation_result}
                if not artifact_validation_result.passed and repair_attempts:
                    try:
                        repair_outcome = await self.repair_coordinator.coordinate(
                            artifact_validation_result,
                            max_attempts=repair_attempts,
                            repair=repair_candidate,
                            validate_target=validate_target,
                            validate_full=validate_full,
                            prepare_candidate=prepare_candidate,
                            reject_candidate=reject_candidate,
                            commit_candidate=commit_candidate,
                            emit=emit_repair_event,
                            consult=consult_repair,
                        )
                    except BaseException:
                        # A cancelled repair resumes from the stable outer Run
                        # checkpoint, never from an uncommitted candidate.
                        if candidate_workspace is not None:
                            self.code_company_runtime.discard_candidate(candidate_workspace)
                        raise
                    artifact_validation_result = repair_outcome.validation if repair_outcome.passed else committed_validation
                    state.context.set("repair_history", list(repair_outcome.history))
                    if not repair_outcome.passed and latest_candidate_validation is not None:
                        state.context.set("repair_last_candidate_validation", latest_candidate_validation.as_dict())
                        latest_failed = next(
                            (check for check in latest_candidate_validation.checks if check.status == "failed"),
                            None,
                        )
                        if latest_failed is not None:
                            validation_failure_summary = (
                                f"{committed_validation.summary}；最后候选仍失败："
                                f"{latest_failed.label} · {latest_failed.message}"
                            )
                    if not repair_outcome.passed and candidate_workspace is not None:
                        self.code_company_runtime.discard_candidate(candidate_workspace)
                        await emit_repair_event("repair.candidate_discarded", {
                            "reason": repair_outcome.stop_reason or "候选版本未通过完整回归。",
                            "attempts": repair_outcome.attempts,
                        })
                # This is an explicit Context Packet field so the Tester Agent
                # can explain real command output instead of inventing a test.
                validation_proof = artifact_validation_result.as_dict()
                if state.workflow.meta.get("delivery_contract"):
                    from .delivery_gate import artifact_fingerprint
                    validation_proof["artifactFingerprint"] = artifact_fingerprint(state.context.snapshot().get("__artifact_files__", []))
                    validation_proof["contractHash"] = state.context.snapshot().get("delivery_contract_hash")
                state.context.set("artifact_validation", validation_proof)
                checks = validation_proof.get("checks") if isinstance(validation_proof, dict) else []
                if isinstance(checks, list):
                    normalized_gate = self.verification_engine.normalize_checks(checks, gate="artifact")
                    evidence = normalized_gate["evidence"]
                    routed_failures = self.repair_engine.failure_facts(
                        artifact_validation_result,
                        owner=step.id,
                    )
                    failures = routed_failures or [
                        {**item, "owner": item.get("owner") or step.id}
                        for item in normalized_gate["failureFacts"]
                    ]
                    state.context.set("evidence", evidence)
                    prior_failures = state.context.snapshot().get("failure_facts")
                    historical = [item for item in prior_failures if isinstance(item, dict)] if isinstance(prior_failures, list) else []
                    current_ids = {item.get("failure_id") for item in failures}
                    state.context.set("failure_facts", [item for item in historical if item.get("failure_id") not in current_ids] + failures)
                    self.repository.update_run(
                        state.run_id,
                        evidence_json=json.dumps(evidence, ensure_ascii=False),
                        failure_facts_json=json.dumps(state.context.snapshot()["failure_facts"], ensure_ascii=False),
                    )
                    await self.event_bus.emit("validation.evidence", state.run_id, {"stepId": step.id, "evidence": evidence})
                    if failures:
                        await self.event_bus.emit("repair.failure_fact", state.run_id, {"stepId": step.id, "failureFacts": failures})
                await self.event_bus.emit(
                    "step.validation_succeeded" if artifact_validation_result.passed else "step.validation_failed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.RUNNING.value,
                        "result": artifact_validation_result.as_dict(),
                    },
                )
                if not artifact_validation_result.passed:
                    # The deterministic gate is authoritative. A Tester LLM
                    # summary cannot repair a failed build and only adds cost.
                    raise RuntimeError(validation_failure_summary or artifact_validation_result.summary)
            requested_max_tokens = int(runtime["max_tokens"])
            input_budget = self.token_budget_manager.input_budget_tokens(requested_max_tokens, capabilities)
            context_source = state.context
            compiled_for_role = state.context.snapshot().get("compiled_contract")
            if isinstance(compiled_for_role, dict):
                role_contracts = compiled_for_role.get("role_contracts") or {}
                role_contract = role_contracts.get(step.id) if isinstance(role_contracts, dict) else None
                if isinstance(role_contract, dict):
                    scoped = state.context.snapshot()
                    scoped["role_contract"] = role_contract
                    context_source = WorkflowContext(scoped)
            context_packet = self.context_packer.pack(
                step.task_template,
                context_source,
                input_budget,
                preserve_keys=("delivery_contract", "role_contract"),
            )
            prompt = context_packet.text
            budget = self.token_budget_manager.plan(
                effective_system_prompt,
                prompt,
                requested_max_tokens,
                capabilities,
            )
            runtime["max_tokens"] = budget.effective_max_tokens
            if context_packet.warnings:
                await self.event_bus.emit(
                    "step.context_packed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.RUNNING.value,
                        **context_packet.as_dict(),
                    },
                )
            if budget.warnings:
                await self.event_bus.emit(
                    "step.budget_adjusted",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.RUNNING.value,
                        **budget.as_dict(),
                    },
                )
            # Executor only passes a packed Context Packet to the Agent. Agent
            # code never receives the scheduler or another Agent's mutable state.
            accumulated_output = ""
            accumulated_input_tokens = 0
            accumulated_output_tokens = 0
            accumulated_usage: dict[str, Any] = {}
            attempt_prompt = prompt
            generation_mode = self.runtime_policy.generation_mode(state, step)
            artifact_result: ArtifactGenerationResult | None = None
            automatic_structured_recovery_used = False

            def record_artifact_response(item: LLMResponse) -> None:
                nonlocal last_provider_record
                last_provider_record = item.provider_record()
                provider_attempts.append(last_provider_record)
                self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)

            async def emit_artifact_event(event_type: str, payload: dict[str, Any]) -> None:
                await self.event_bus.emit(
                    event_type,
                    state.run_id,
                    {"stepId": step.id, "status": StepStatus.RUNNING.value, **payload},
                )

            while True:
                try:
                    if generation_mode == "coding_loop":
                        loop = CodingAgentLoop(
                            self.provider,
                            self.code_company_runtime.gateway_for_owner(step.id, state.blueprint),
                        )
                        loop_result = await loop.run(
                            self.workspace_artifacts.workspace_for_step(state, step.id),
                            prompt,
                            system_prompt=effective_system_prompt,
                            config=CodingLoopConfig(
                                max_iterations=max(1, min(32, int(state.runtime.get("max_tool_steps", 8) or 8))),
                                timeout_seconds=step.timeout_seconds,
                                max_tokens=int(runtime["max_tokens"]),
                                thinking=effective_thinking,
                            ),
                            emit=emit_artifact_event,
                        )
                        response = loop_result.responses[-1] if loop_result.responses else LLMResponse(text=loop_result.output)
                        total_input_tokens = sum(item.input_tokens for item in loop_result.responses)
                        total_output_tokens = sum(item.output_tokens for item in loop_result.responses)
                        response = replace(
                            response,
                            text=loop_result.output,
                            message_content=loop_result.output,
                            finish_reason="stop",
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            usage={"prompt_tokens": total_input_tokens, "completion_tokens": total_output_tokens},
                        )
                        last_provider_record = response.provider_record()
                        provider_attempts.extend(item.provider_record() for item in loop_result.responses)
                        self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                        workspace_files = self.workspace_artifacts.collect(state, step.id)
                        if workspace_files:
                            existing_artifacts = state.context.snapshot().get("__artifact_files__", [])
                            if not isinstance(existing_artifacts, list):
                                existing_artifacts = []
                            filtered_artifacts = [
                                item for item in existing_artifacts
                                if isinstance(item, dict) and item.get("step_id") != step.id
                            ]
                            state.context.set("__artifact_files__", [
                                *filtered_artifacts,
                                *workspace_files,
                            ])
                        await emit_artifact_event(
                            "step.coding_loop_completed",
                            {"iterations": loop_result.iterations, "toolCount": len(loop_result.tool_results), "verified": loop_result.verified},
                        )
                        break
                    if generation_mode == "artifacts":
                        artifact_result = await generate_artifacts(
                            system_prompt=effective_system_prompt,
                            original_prompt=prompt,
                            base_config={
                                "agent_id": agent.id,
                                "request_timeout": step.timeout_seconds,
                                "connect_timeout": 10,
                                "read_timeout": 120,
                                "write_timeout": 30,
                                "pool_timeout": 10,
                                "effective_thinking": effective_thinking,
                                "thinking_type": "disabled" if effective_thinking == "off" else "enabled",
                                "reasoning_effort": effective_thinking,
                                "enforce_artifact_contract": step.enforce_artifact_contract,
                                "delivery_contract": state.context.snapshot().get("delivery_contract"),
                                "compiled_contract": state.context.snapshot().get("compiled_contract"),
                            },
                            request_timeout=step.timeout_seconds,
                            max_tokens=int(runtime["max_tokens"]),
                            provider_max_tokens=self.token_budget_manager.provider_max_tokens(capabilities),
                            request=self.model_invocation.generate,
                            record_response=record_artifact_response,
                            emit=emit_artifact_event,
                        )
                        retry_count = artifact_result.repair_count
                        accumulated_input_tokens = artifact_result.input_tokens
                        accumulated_output_tokens = artifact_result.output_tokens
                        total_input_tokens = artifact_result.input_tokens
                        total_output_tokens = artifact_result.output_tokens
                        response = replace(
                            artifact_result.last_response,
                            text=artifact_result.summary,
                            message_content=artifact_result.summary,
                            finish_reason="stop",
                            input_tokens=artifact_result.input_tokens,
                            output_tokens=artifact_result.output_tokens,
                            usage={
                                "prompt_tokens": artifact_result.input_tokens,
                                "completion_tokens": artifact_result.output_tokens,
                            },
                        )
                        existing_artifacts = state.context.snapshot().get("__artifact_files__", [])
                        if not isinstance(existing_artifacts, list):
                            existing_artifacts = []
                        state.context.set(
                            "__artifact_files__",
                            [
                                *existing_artifacts,
                                *[
                                    {"step_id": step.id, **artifact}
                                    for artifact in artifact_result.files
                                ],
                            ],
                        )
                        if step.output:
                            # Keep the compact Agent summary for ordinary
                            # logs, but expose an explicit file-scoped context
                            # key for downstream Agents that must implement
                            # against the generated contract (for example,
                            # Backend and Frontend reading the database schema).
                            state.context.set(
                                f"{step.output}_files",
                                json.dumps(
                                    [
                                        {
                                            "name": item["name"],
                                            "language": item.get("language", "text"),
                                            "purpose": item.get("purpose", ""),
                                            "content": item.get("content", ""),
                                        }
                                        for item in artifact_result.files
                                    ],
                                    ensure_ascii=False,
                                ),
                            )
                        break

                    attempt_thinking = _thinking_for_attempt(effective_thinking, retry_count)
                    attempt_thinking, _ = self.provider.resolve_thinking(attempt_thinking)
                    response = await self.model_invocation.generate(
                        effective_system_prompt,
                        attempt_prompt,
                        {
                            "agent_id": agent.id,
                            "request_timeout": step.timeout_seconds,
                            "connect_timeout": 10,
                            "read_timeout": 120,
                            "write_timeout": 30,
                            "pool_timeout": 10,
                            "max_tokens": runtime["max_tokens"],
                            "thinking_type": "disabled" if attempt_thinking == "off" else "enabled",
                            "effective_thinking": attempt_thinking,
                            "reasoning_effort": attempt_thinking,
                            "continuation": bool(accumulated_output),
                            "continuation_attempt": retry_count,
                        },
                        step.timeout_seconds,
                    )
                    last_provider_record = response.provider_record()
                    provider_attempts.append(last_provider_record)
                    self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                    total_input_tokens += response.input_tokens
                    total_output_tokens += response.output_tokens
                    accumulated_input_tokens += response.input_tokens
                    accumulated_usage = _merge_usage(accumulated_usage, response.usage)

                    # A valid JSON control plan is complete even when a
                    # reasoning-capable provider reports ``length`` because it
                    # used the remaining completion budget for hidden thinking
                    # or trailing explanation.  Parse this small contract before
                    # treating the finish reason as a source-file truncation.
                    if step.runtime_plan:
                        parsed_runtime_plan = parse_runtime_budget_plan(
                            response.text,
                            self.runtime_policy.runtime_plan_target_step_ids(state, step.id),
                            self.token_budget_manager.provider_max_tokens(capabilities),
                        )
                        if parsed_runtime_plan is not None:
                            if response.finish_reason == "length":
                                await self.event_bus.emit(
                                    "step.runtime_adjusted",
                                    state.run_id,
                                    {
                                        "stepId": step.id,
                                        "status": StepStatus.RUNNING.value,
                                        "warning": "Runtime Plan JSON 已完整解析；Provider 仅截断了尾部说明，未影响预算计划。",
                                        "requestedThinking": runtime["thinking"],
                                        "effectiveThinking": effective_thinking,
                                    },
                                )
                            break

                    inspection = inspect_response(response, step.output_format)
                    if inspection.partial and response.text.strip():
                        if retry_count >= runtime["retry"]:
                            raise LLMError(
                                "Agent output was truncated at the provider output limit",
                                retryable=False,
                                response_metadata=last_provider_record,
                            )
                        accumulated_output = _merge_continuation(accumulated_output, response.text, step.output_format)
                        accumulated_output_tokens += response.output_tokens
                        retry_count += 1
                        attempt_prompt = _continuation_prompt(prompt, accumulated_output)
                        await self.event_bus.emit(
                            "step.continuing",
                            state.run_id,
                            {
                                "stepId": step.id,
                                "status": StepStatus.RUNNING.value,
                                "retryCount": retry_count,
                                "reason": "Provider output reached its limit; continuing from the saved partial output",
                                "outputTokens": accumulated_output_tokens,
                            },
                        )
                        await asyncio.sleep(min(0.25 * (2 ** (retry_count - 1)), 2))
                        continue
                    if not response.text.strip():
                        if _reasoning_budget_exhausted(response, int(runtime["max_tokens"])):
                            next_requested_thinking = _thinking_for_attempt(effective_thinking, retry_count + 1)
                            next_effective_thinking, _ = self.provider.resolve_thinking(next_requested_thinking)
                            current_parameters = last_provider_record.get("request_parameters", {})
                            previous_parameters = provider_attempts[-2].get("request_parameters", {}) if len(provider_attempts) > 1 else {}
                            parameters_unchanged = bool(current_parameters) and current_parameters == previous_parameters
                            can_lower_thinking = next_effective_thinking != attempt_thinking and not parameters_unchanged
                            if (
                                can_lower_thinking
                                and not automatic_structured_recovery_used
                                and step.output_format == "json"
                            ):
                                automatic_structured_recovery_used = True
                                retry_count += 1
                                await self.event_bus.emit(
                                    "step.retrying",
                                    state.run_id,
                                    {
                                        "stepId": step.id,
                                        "status": StepStatus.RUNNING.value,
                                        "retryCount": retry_count,
                                        "reason": "结构化输出为空，已自动降低思考强度后恢复",
                                        "reasoningEffort": next_effective_thinking,
                                        "automaticRecovery": True,
                                        "provider": last_provider_record,
                                    },
                                )
                                continue
                            if next_effective_thinking == attempt_thinking or parameters_unchanged:
                                raise LLMError(
                                    "模型已将全部输出 Token 用于推理且未生成正文；Provider 无法进一步降低思考强度，已停止无效重试",
                                    retryable=False,
                                    response_metadata=last_provider_record,
                                )
                        raise LLMError(
                            "Agent returned an empty output",
                            retryable=True,
                            response_metadata=last_provider_record,
                        )
                    if accumulated_output:
                        merged_output = _merge_continuation(accumulated_output, response.text, step.output_format)
                        response = replace(
                            response,
                            text=merged_output,
                            message_content=merged_output,
                            input_tokens=accumulated_input_tokens,
                            output_tokens=accumulated_output_tokens + response.output_tokens,
                            usage=accumulated_usage,
                        )
                        last_provider_record = response.provider_record()
                        provider_attempts[-1] = last_provider_record
                        self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                    inspection = inspect_response(response, step.output_format)
                    if not inspection.complete:
                        raise LLMError(
                            f"Agent output validation failed: {inspection.reason or 'incomplete output'}",
                            retryable=True,
                            response_metadata=last_provider_record,
                        )
                    if step.runtime_plan and parsed_runtime_plan is None:
                        raise LLMError(
                            "Runtime Plan Agent returned an invalid budget contract",
                            retryable=True,
                            response_metadata=last_provider_record,
                        )
                    break
                except (LLMTimeoutError, asyncio.TimeoutError) as exc:
                    if retry_count >= runtime["retry"]:
                        message = str(exc).strip() or f"Agent timed out after {step.timeout_seconds:g}s"
                        raise LLMError(message, retryable=False) from exc
                    retry_count += 1
                    next_thinking = _thinking_for_attempt(effective_thinking, retry_count)
                    await self.event_bus.emit("step.retrying", state.run_id, {"stepId": step.id, "status": StepStatus.RUNNING.value, "retryCount": retry_count, "reason": str(exc) or "Request timed out", "reasoningEffort": next_thinking})
                except LLMError as exc:
                    if exc.response_metadata and (not provider_attempts or provider_attempts[-1] != exc.response_metadata):
                        last_provider_record = exc.response_metadata
                        provider_attempts.append(last_provider_record)
                        self.model_invocation.persist_attempts(state.run_id, step.id, provider_attempts)
                    if not exc.retryable or retry_count >= runtime["retry"]:
                        raise
                    retry_count += 1
                    next_thinking = _thinking_for_attempt(effective_thinking, retry_count)
                    await self.event_bus.emit("step.retrying", state.run_id, {"stepId": step.id, "status": StepStatus.RUNNING.value, "retryCount": retry_count, "reason": str(exc), "reasoningEffort": next_thinking, "provider": last_provider_record})
                if retry_count:
                    await asyncio.sleep(min(0.25 * (2 ** (retry_count - 1)), 2))

            assert response is not None
            if artifact_result is not None:
                runtime["generation_mode"] = "artifacts"
            if parsed_runtime_plan is not None:
                state.runtime_budget_plan = parsed_runtime_plan
                if step.output:
                    state.context.set(step.output, parsed_runtime_plan)
                await self.event_bus.emit(
                    "workflow.budget_planned",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "plan": parsed_runtime_plan,
                        "fallback": False,
                    },
                )
            elif step.output:
                state.context.set(step.output, response.text)
            if step.id == "architecture":
                (
                    response,
                    total_input_tokens,
                    total_output_tokens,
                    last_provider_record,
                ) = await self.architecture_contract.process_step(
                    state,
                    step,
                    response,
                    effective_system_prompt=effective_system_prompt,
                    agent_id=agent.id,
                    runtime=runtime,
                    provider_attempts=provider_attempts,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    last_provider_record=last_provider_record,
                    persist_state=self._persist_state,
                    validate_candidate=self._validate_architecture_candidate,
                )
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            state.results[step.id] = StepStatus.SUCCESS
            self.repository.upsert_step(state.run_id, step.id, status=StepStatus.SUCCESS.value, output=response.text, finished_at=utc_now(), duration_ms=duration_ms, retry_count=retry_count, input_tokens=total_input_tokens, output_tokens=total_output_tokens)
            runtime_detail = {"maxTokens": runtime["max_tokens"], "retry": runtime["retry"], "budgetMode": runtime["budget_mode"], "requestedThinking": runtime["thinking"], "effectiveThinking": effective_thinking, "generationMode": generation_mode, "skill": skill_audit}
            if isinstance(budget_plan, dict):
                runtime_detail.update({
                    "configuredMaxTokens": budget_plan.get("configured_max_tokens"),
                    "recommendedMaxTokens": budget_plan.get("recommended_max_tokens"),
                    "difficulty": budget_plan.get("difficulty"),
                    "difficultyScore": budget_plan.get("difficulty_score"),
                    "budgetSource": budget_plan.get("source"),
                })
            if artifact_result is not None:
                runtime_detail.update({
                    "artifactFiles": [item["name"] for item in artifact_result.files],
                    "artifactRepairCount": artifact_result.repair_count,
                    "artifactContinuationCount": artifact_result.continuation_count,
                    "artifactSplitCount": artifact_result.split_count,
                    "artifactBudgetSource": "file_estimate",
                    "artifactFileMinTokens": ARTIFACT_FILE_MIN_TOKENS,
                })
            if artifact_validation_result is not None:
                runtime_detail["validation"] = artifact_validation_result.as_dict()
            await self.event_bus.emit("step.completed", state.run_id, {"stepId": step.id, "status": StepStatus.SUCCESS.value, "output": response.text, "durationMs": duration_ms, "retryCount": retry_count, "tokens": {"input": total_input_tokens, "output": total_output_tokens}, "runtime": runtime_detail, "provider": last_provider_record})
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            error_message = str(exc).strip() or f"{type(exc).__name__} without a message"

            # Runtime planning is optional.  If its compact LLM response is
            # truncated/invalid, make the deterministic local plan authoritative
            # and finish this control step successfully.  This removes the red
            # false-positive failure and, more importantly, avoids a useless
            # continuation/retry before the real Agents start.
            if step.runtime_plan and step.failure_policy == "continue":
                allowed_step_ids = self.runtime_policy.runtime_plan_target_step_ids(state, step.id)
                fallback_plan = build_local_runtime_budget_plan(
                    self.runtime_policy.confirmed_requirement_for_budget(state),
                    allowed_step_ids,
                    capabilities if "capabilities" in locals() else self.provider.capabilities(),
                )
                state.runtime_budget_plan = fallback_plan
                if step.output:
                    state.context.set(step.output, fallback_plan)
                fallback_output = json.dumps(fallback_plan, ensure_ascii=False)
                state.results[step.id] = StepStatus.SUCCESS
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.SUCCESS.value,
                    output=fallback_output,
                    finished_at=utc_now(),
                    duration_ms=duration_ms,
                    retry_count=retry_count,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
                await self.event_bus.emit(
                    "workflow.budget_planned",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "fallback": True,
                        "error": error_message,
                        "plan": fallback_plan,
                    },
                )
                await self.event_bus.emit(
                    "step.completed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.SUCCESS.value,
                        "output": fallback_output,
                        "durationMs": duration_ms,
                        "retryCount": retry_count,
                        "fallback": True,
                        "fallbackReason": error_message,
                        "tokens": {
                            "input": total_input_tokens,
                            "output": total_output_tokens,
                        },
                        "provider": last_provider_record,
                    },
                )
                return

            if (
                step.failure_policy == "fallback"
                and step.output_format == "json"
                and not isinstance(exc, ArchitectureContractError)
                and _can_use_structured_fallback(response, error_message)
            ):
                repair_route = self.repair_engine.route_node_failure(
                    step.id,
                    error_message,
                    response_present=response is not None,
                )
                await self.event_bus.emit(
                    "step.repairing",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "ownerStep": repair_route["owner_step"],
                        "repairRoute": repair_route,
                        "error": error_message,
                    },
                )
                self.failure_recorder.record_node_failure(state, step.id, error_message, repair_route)
                requirement_spec = state.requirement_spec or self.capability_router.route(
                    str(state.context.snapshot().get("requirement", ""))
                )
                fallback_decision = self.architecture_validator.fallback(requirement_spec)
                fallback_output = json.dumps(fallback_decision.model_dump(mode="json"), ensure_ascii=False)
                state.context.set("architecture_raw", response.text if response is not None else "")
                state.context.set("architecture_decision", fallback_decision.model_dump(mode="json"))
                state.context.set("architecture_doc", fallback_output)
                if state.workflow.meta.get("delivery_contract"):
                    contract = build_delivery_contract(
                        requirement_spec.raw_requirement,
                        fallback_decision.model_dump(mode="json"),
                        requirement_spec=requirement_spec.model_dump(mode="json"),
                    )
                    blueprint = build_project_blueprint(
                        requirement_spec.raw_requirement,
                        requirement_spec.model_dump(mode="json"),
                        fallback_decision.model_dump(mode="json"),
                        contract,
                    )
                    blueprint["contract_hash"] = contract_hash(contract)
                    blueprint = self.contract_compiler.enrich_blueprint(blueprint)
                    state.blueprint = blueprint
                    state.context.set("project_blueprint", blueprint)
                    state.context.set("compiled_contract", self.contract_compiler.compile(blueprint))
                    state.context.set("delivery_contract", contract)
                    state.context.set("delivery_contract_hash", contract_hash(contract))
                    self.repository.update_run(
                        state.run_id,
                        blueprint_json=json.dumps(blueprint, ensure_ascii=False),
                        decision_log_json=json.dumps([{"source": "architecture_fallback", "decision": fallback_decision.model_dump(mode="json")}], ensure_ascii=False),
                    )
                state.adaptive_policy = build_adaptive_policy(fallback_decision.model_dump(mode="json"))
                if state.workflow.meta.get("delivery_contract"):
                    state.adaptive_policy = {
                        **state.adaptive_policy,
                        **self.contract_compiler.execution_policy(state.blueprint),
                    }
                state.policy_skipped_steps = set(state.adaptive_policy.get("skip_steps", []))
                state.results[step.id] = StepStatus.SUCCESS
                self.repository.upsert_step(
                    state.run_id,
                    step.id,
                    status=StepStatus.SUCCESS.value,
                    output=fallback_output,
                    finished_at=utc_now(),
                    duration_ms=duration_ms,
                    retry_count=retry_count,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
                await self.event_bus.emit(
                    "workflow.policy_decided",
                    state.run_id,
                    {
                        "decision": fallback_decision.model_dump(mode="json"),
                        "policy": state.adaptive_policy,
                        "fallback": True,
                        "fallbackReason": error_message,
                    },
                )
                await self.event_bus.emit(
                    "step.completed",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "status": StepStatus.SUCCESS.value,
                        "output": fallback_output,
                        "durationMs": duration_ms,
                        "retryCount": retry_count,
                        "fallback": True,
                        "fallbackReason": error_message,
                        "tokens": {
                            "input": total_input_tokens,
                            "output": total_output_tokens,
                        },
                        "provider": last_provider_record,
                    },
                )
                await self.event_bus.emit(
                    "step.repaired",
                    state.run_id,
                    {
                        "stepId": step.id,
                        "ownerStep": repair_route["owner_step"],
                        "repairRoute": repair_route,
                        "message": "节点已使用保守结构化兜底修复，继续执行下游节点",
                    },
                )
                return

            repair_route = self.repair_engine.route_node_failure(
                step.id,
                error_message,
                response_present=response is not None,
                error_type=type(exc).__name__,
            )
            self.failure_recorder.record_node_failure(state, step.id, error_message, repair_route)
            latest_failure_fact = (state.context.snapshot().get("failure_facts") or [])[-1]
            await self.event_bus.emit(
                "repair.routed",
                state.run_id,
                {
                    "stepId": step.id,
                    "status": StepStatus.FAILED.value,
                    "exhausted": True,
                    "plan": repair_route,
                    "failureFact": latest_failure_fact,
                    "reason": error_message,
                },
            )
            state.results[step.id] = StepStatus.FAILED
            self.repository.upsert_step(state.run_id, step.id, status=StepStatus.FAILED.value, output=response.text if response else None, finished_at=utc_now(), duration_ms=duration_ms, retry_count=retry_count, error_message=error_message, input_tokens=total_input_tokens, output_tokens=total_output_tokens)
            failure_payload = {"stepId": step.id, "status": StepStatus.FAILED.value, "error": error_message, "errorType": type(exc).__name__, "durationMs": duration_ms, "retryCount": retry_count, "tokens": {"input": total_input_tokens, "output": total_output_tokens}, "provider": last_provider_record, "providerAttempts": provider_attempts}
            recorded_facts = state.context.snapshot().get("failure_facts")
            if isinstance(recorded_facts, list):
                matching_facts = [fact for fact in recorded_facts if isinstance(fact, dict) and (fact.get("stage") == step.id or fact.get("stage") == f"{step.id}_contract" or (isinstance(fact.get("evidence"), dict) and fact["evidence"].get("step_id") == step.id))]
                if matching_facts:
                    failure_payload["failureFact"] = next(
                        (fact for fact in reversed(matching_facts) if str(fact.get("code") or "").startswith("ARCH_")),
                        matching_facts[-1],
                    )
            if artifact_validation_result is not None:
                failure_payload["validation"] = artifact_validation_result.as_dict()
            await self.event_bus.emit("step.failed", state.run_id, failure_payload)

    async def _repair_failed_artifacts(
        self,
        state: RunState,
        validation: ArtifactValidationResult,
        repair_attempt: int,
        validation_step_id: str,
        timeout_seconds: float,
        provider_max_tokens: int,
        *,
        bundle: bool = False,
    ) -> tuple[list[str], list[LLMResponse]]:
        """Return deterministic build errors to the Agent that owns each file."""
        raw_files = state.context.snapshot().get("__artifact_files__")
        if not isinstance(raw_files, list):
            return [], []
        repaired: list[str] = []
        responses: list[LLMResponse] = []
        failed_targets = self.repair_engine.targets(validation)
        for target in sorted(failed_targets):
            target_checks = [
                check
                for check in validation.checks
                if check.status == "failed" and check.target == target
            ]
            diagnostics = "\n\n".join(
                f"[{check.label}] {check.message}\n{check.output}".strip()
                for check in target_checks
            )
            owned_files = [
                item
                for item in raw_files
                if isinstance(item, dict)
                and str(item.get("step_id") or "").lower() == target
                and isinstance(item.get("name"), str)
                and isinstance(item.get("content"), str)
            ]
            candidates = self._validation_repair_candidates(
                owned_files,
                diagnostics,
                checks=target_checks,
            )
            existing_names = {str(item.get("name") or "") for item in raw_files if isinstance(item, dict)}
            ownership = (state.blueprint or {}).get("artifact_ownership") or {}
            missing_declarations: list[dict[str, Any]] = []
            missing_modules: list[dict[str, Any]] = []
            if target == "frontend":
                for source_path, imported in re.findall(
                    r"(\S+\.(?:vue|js|ts|tsx|jsx))\s+引用了不存在的模块\s+(\.{1,2}/[^\s；]+)", diagnostics,
                    re.IGNORECASE,
                ):
                    name = posixpath.normpath(posixpath.join(posixpath.dirname(source_path.replace("\\", "/")), imported))
                    if any(posixpath.basename(existing) == posixpath.basename(name) for existing in existing_names):
                        # A likely moved/renamed module exists; repair the
                        # referring import instead of creating a duplicate.
                        continue
                    if (name.startswith("src/") and ".." not in name.split("/")
                            and name not in existing_names
                            and (not ownership or path_allowed(target, name, ownership))):
                        missing_modules.append({"name": name, "content": "", "step_id": target, "create": True})
                if missing_modules:
                    candidates = missing_modules
            if target == "backend":
                missing_declarations = self._missing_java_declaration_candidates(
                    owned_files, raw_files, diagnostics, ownership,
                )
                if missing_declarations:
                    candidates = missing_declarations
                for item in missing_declarations:
                    await self.event_bus.emit("step.validation_missing_declaration", state.run_id, {
                        "stepId": validation_step_id,
                        "target": target,
                        "symbol": item["symbol"],
                        "sourceFile": item["source_file"],
                        "fileName": item["name"],
                        "repairAttempt": repair_attempt,
                        "category": "missing_declaration",
                    })
                contract = state.context.snapshot().get("delivery_contract") or {}
                controllers = [item for item in owned_files if str(item.get("name") or "").endswith("Controller.java")]
                directory = posixpath.dirname(str(controllers[0]["name"])) if controllers else "src/main/java/com/example/app"
                for api in contract.get("api_contract") or []:
                    if not isinstance(api, dict):
                        continue
                    path = str(api.get("collection_path") or api.get("path") or "")
                    entity = str(api.get("entity_id") or "")
                    if not path or not entity or path not in diagnostics:
                        continue
                    if not re.fullmatch(r"[A-Za-z_]\w*", entity):
                        continue
                    name = f"{directory}/{entity}Controller.java"
                    if name not in existing_names and (not ownership or path_allowed(target, name, ownership)):
                        candidates.insert(0, {"name": name, "content": "", "step_id": target, "create": True})
                    else:
                        matched = next((item for item in controllers if str(item.get("name")) == name), None)
                        if matched and matched not in candidates:
                            candidates.insert(0, matched)
            if target == "frontend" and "缺少 src/main/resources/static/index.html" in diagnostics and not any(item.get("name") == "src/main/resources/static/index.html" for item in raw_files if isinstance(item, dict)):
                candidates.insert(0, {"name": "src/main/resources/static/index.html", "content": "", "step_id": target, "create": True})
            if (bundle and candidates and not missing_declarations and not missing_modules
                    and not any(str(item.get("name")) == "pom.xml" for item in candidates)):
                related = [item for item in owned_files if str(item.get("name", "")).lower().endswith(
                    ("controller.java", "service.java", "repository.java", "request.java")
                )]
                candidates = list({str(item["name"]): item for item in [*candidates, *related]}.values())
            if bundle and len(candidates) > 16:
                await self.event_bus.emit("step.validation_repair_skipped", state.run_id, {
                    "stepId": validation_step_id, "target": target, "repairAttempt": repair_attempt,
                    "reason": f"关联文件共 {len(candidates)} 个，超过单轮安全上限 16；已保留原成果物并停止盲目批量改写。",
                })
                continue
            if not candidates:
                await self.event_bus.emit(
                    "step.validation_repair_skipped",
                    state.run_id,
                    {
                        "stepId": validation_step_id,
                        "target": target,
                        "repairAttempt": repair_attempt,
                        "reason": "构建日志没有定位到明确的成果物文件，保留原始失败结果。",
                    },
                )
                continue

            agent = self.registry.get(f"{target}_agent")
            for candidate in candidates if bundle else candidates[:3]:
                name = str(candidate["name"])
                if name == "pom.xml":
                    normalized_content, normalized = normalize_h2_flyway_dependency(str(candidate["content"]))
                    if normalized:
                        raw_files = [
                            {**item, "content": normalized_content}
                            if isinstance(item, dict) and item.get("name") == name and item.get("step_id") == candidate.get("step_id")
                            else item for item in raw_files
                        ]
                        state.context.set("__artifact_files__", raw_files)
                        repaired.append(name)
                        await self.event_bus.emit("step.artifact_dependency_normalized", state.run_id, {
                            "stepId": validation_step_id, "fileName": name,
                            "reason": "已确定性纠正 Flyway H2 模块声明；不消耗模型 Token，候选仍需完整复验。",
                        })
                        continue
                # Include sibling contracts and database-owned configuration.
                current_owned_files = [
                    item
                    for item in raw_files
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("content"), str)
                ]
                repair_context_files = self._select_repair_context_files(
                    current_owned_files,
                    target=target,
                    candidate_name=name,
                    diagnostics=diagnostics,
                )
                project_context_parts: list[str] = []
                context_chars = 0
                for item in repair_context_files:
                    section = f"\n--- {item['name']} ---\n{item['content']}"
                    if context_chars + len(section) > 28000:
                        break
                    project_context_parts.append(section)
                    context_chars += len(section)
                project_context = "".join(project_context_parts)
                await self.event_bus.emit(
                    "step.validation_repairing",
                    state.run_id,
                    {
                        "stepId": validation_step_id,
                        "target": target,
                        "fileName": name,
                        "repairAttempt": repair_attempt,
                        "maxTokens": provider_max_tokens,
                        "agentId": agent.id,
                    },
                )
                await self.event_bus.emit(
                    "step.validation_repair_dispatched",
                    state.run_id,
                    {
                        "stepId": validation_step_id,
                        "target": target,
                        "owner": target,
                        "agentId": agent.id,
                        "fileName": name,
                        "repairAttempt": repair_attempt,
                    },
                )
                prompt = (
                    "你正在执行构建失败后的定点源码修复。请根据真实编译日志和同一项目文件，"
                    f"只输出文件 `{name}` 修复后的完整原文。不要输出 Markdown 代码围栏、解释、标题或省略号；"
                    "不要修改 API 契约来逃避错误，必须与其他文件的类型、方法签名和导入保持一致。\n\n"
                    "保持原文件路径和技术栈；仅修复缺陷，禁止整套重新设计、删除测试或改换 JDBC/JPA。"
                    "下面的当前源码是权威合同，调用方法必须真实存在且参数、返回类型匹配。\n\n"
                    "默认测试数据库为 H2；保留依赖和隔离配置，不得改为外部数据库绕过联调。\n\n"
                    f"原始需求：\n{state.context.snapshot().get('requirement', '')}\n\n"
                    f"冻结交付合同（禁止改写）：\n{json.dumps(state.context.snapshot().get('delivery_contract', {}), ensure_ascii=False)}\n\n"
                    f"真实验证日志：\n{diagnostics[-16000:]}\n\n"
                    f"当前项目文件：{project_context}\n\n"
                    f"待修复文件当前内容：\n{candidate['content']}"
                )
                if candidate.get("create"):
                    if candidate.get("symbol"):
                        prompt += (
                            "\n\n该 Java 文件由编译证据定位为缺失声明，不代表可改变冻结合同。"
                            "请依据引用处、同目录现有源码和真实编译错误生成最小完整文件；"
                            "声明包名必须与文件路径一致，构造器需匹配引用处。"
                            f"\n缺失符号：{candidate['symbol']}；引用文件：{candidate['source_file']}。"
                        )
                    else:
                        prompt += (
                            "\n\n该文件由验证证据定位为缺失文件。"
                            "请依据引用处、同目录现有源码和冻结合同生成最小完整文件，"
                            "不要通过改名或改变路由掩盖缺失。"
                        )
                guidance = state.context.snapshot().get("__collaboration_guidance__")
                if isinstance(guidance, dict) and isinstance(guidance.get(target), str):
                    prompt += (
                        "\n\n跨 Agent 协商建议（仅供定位，不是修改合同的授权；"
                        "若与冻结合同或真实源码冲突，以冻结合同和源码为准）：\n"
                        + guidance[target][:600]
                    )
                try:
                    response = await self.model_invocation.generate(
                        agent.system_prompt,
                        prompt,
                        {
                            "agent_id": agent.id,
                            "generation_phase": "artifact_validation_repair",
                            "artifact_name": name,
                            "artifact_validation_repair_attempt": repair_attempt,
                            "continuation": False,
                            "max_tokens": max(1, int(provider_max_tokens)),
                            "effective_thinking": "off",
                            "thinking_type": "disabled",
                            "reasoning_effort": "off",
                            "connect_timeout": 10,
                            "read_timeout": 120,
                            "write_timeout": 30,
                            "pool_timeout": 10,
                        },
                        timeout_seconds,
                    )
                except Exception as exc:
                    await self.event_bus.emit(
                        "step.validation_repair_failed",
                        state.run_id,
                        {
                            "stepId": validation_step_id,
                            "target": target,
                            "fileName": name,
                            "repairAttempt": repair_attempt,
                            "error": str(exc) or type(exc).__name__,
                        },
                    )
                    continue
                responses.append(response)
                content = self._clean_artifact_repair_output(response.text)
                if name == "pom.xml":
                    content, normalized = normalize_h2_flyway_dependency(content)
                    if normalized:
                        await self.event_bus.emit("step.artifact_dependency_normalized", state.run_id, {
                            "stepId": validation_step_id, "fileName": name,
                            "reason": "已拦截修复输出中的错误 flyway-database-h2，纠正为 flyway-core 后再复验。",
                        })
                if not content or str(response.finish_reason or "").lower() == "length":
                    await self.event_bus.emit(
                        "step.validation_repair_failed",
                        state.run_id,
                        {
                            "stepId": validation_step_id,
                            "target": target,
                            "fileName": name,
                            "repairAttempt": repair_attempt,
                            "error": "定点修复输出为空或达到 Provider 输出上限。",
                        },
                    )
                    continue
                updated_files = []
                for item in raw_files:
                    if (
                        isinstance(item, dict)
                        and item.get("name") == name
                        and str(item.get("step_id") or "").lower() == target
                    ):
                        updated_files.append({**item, "content": content})
                    else:
                        updated_files.append(item)
                if candidate.get("create"):
                    mime_type = "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8"
                    updated_files.append({"name": name, "content": content, "step_id": target, "mime_type": mime_type})
                raw_files = updated_files
                state.context.set("__artifact_files__", raw_files)
                repaired.append(name)
        return repaired, responses

    @staticmethod
    def _select_repair_context_files(
        files: list[dict[str, Any]],
        *,
        target: str,
        candidate_name: str,
        diagnostics: str,
    ) -> list[dict[str, Any]]:
        """Select only source files that can explain the current failure.

        The file being repaired is appended separately to the prompt. Keeping
        unrelated source out of the context cuts latency and reduces the risk
        that an Agent rewrites a correct module while fixing another one.
        """
        diagnostic_text = str(diagnostics or "").lower()
        shared_names = {
            "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
            "src/main/resources/application.yml", "src/main/resources/application.yaml",
            "src/main/resources/schema.sql", "src/main/resources/data.sql",
        }

        def priority(item: dict[str, Any]) -> tuple[int, str]:
            name = str(item.get("name") or "")
            lowered = name.lower()
            owner = str(item.get("step_id") or "").lower()
            if name == candidate_name:
                return (99, lowered)
            if lowered and lowered in diagnostic_text:
                return (0, lowered)
            if lowered in shared_names or lowered.startswith("src/main/resources/db/migration/"):
                return (1, lowered)
            if target == "backend" and (
                lowered.endswith(".java")
                or any(word in lowered for word in ("controller", "service", "repository", "dto", "entity"))
            ):
                return (2, lowered)
            if target == "frontend" and (
                lowered.startswith("src/")
                or any(word in lowered for word in ("api", "router", "store", "app.vue"))
            ):
                return (2, lowered)
            if target == "database" and (
                lowered.endswith(".sql")
                or any(word in lowered for word in ("entity", "repository", "application.yml", "application.yaml"))
            ):
                return (2, lowered)
            if owner == target:
                return (3, lowered)
            return (99, lowered)

        ranked = sorted(files, key=priority)
        return [item for item in ranked if priority(item)[0] < 99]

    @staticmethod
    def _validation_failure_fingerprint(validation: ArtifactValidationResult) -> str:
        """Return a stable identity for no-progress detection across repairs."""
        return RepairEngine.fingerprint(validation)

    async def _reexecute_validation_owners(
        self,
        state: RunState,
        validation: ArtifactValidationResult,
        repair_attempt: int,
        validation_step_id: str,
    ) -> tuple[list[str], list[LLMResponse]]:
        """Patch related owner files against real source, never regenerate a project.

        The caller stages these changes and commits only after deterministic
        validation. Architecture, file layout and passing tests remain frozen.
        """
        validator_step = next(step for step in state.workflow.steps if step.id == validation_step_id)
        await self.event_bus.emit(
            "step.validation_owner_reexecuting",
            state.run_id,
            {
                "stepId": validation_step_id,
                "repairAttempt": repair_attempt,
                "repairMode": "grounded_multi_file_patch",
                "status": StepStatus.RUNNING.value,
                "reason": "基于现有源码整改关联文件，保留目录、技术栈和测试；验证通过前不提交。",
            },
        )
        before = state.context.snapshot()
        before_files = before.get("__artifact_files__", [])
        repaired_files, responses = await self._repair_failed_artifacts(
            state, validation, repair_attempt, validation_step_id,
            validator_step.timeout_seconds,
            self.token_budget_manager.provider_max_tokens(self.provider.capabilities()),
            bundle=True,
        )
        after = state.context.snapshot()
        allowed_owners = {
            str(check.target or "").lower()
            for check in validation.checks
            if check.status == "failed" and str(check.target or "").lower() in {"backend", "frontend", "database"}
        }
        violations = self._owner_reexecution_violations(
            before,
            after,
            allowed_owners=allowed_owners,
        )
        if violations:
            state.context.set("__artifact_files__", before_files if isinstance(before_files, list) else [])
            for protected_key in ("blueprint", "delivery_contract", "delivery_contract_hash", "compiled_contract"):
                if protected_key in before:
                    state.context.set(protected_key, before[protected_key])
            await self.event_bus.emit(
                "step.validation_owner_reexecution_blocked",
                state.run_id,
                {
                    "stepId": validation_step_id,
                    "repairAttempt": repair_attempt,
                    "status": StepStatus.RUNNING.value,
                    "violations": violations,
                    "reason": "责任 Agent 的候选修改越过文件归属或冻结合同边界，已撤回本轮修改。",
                },
            )
            return [], responses
        return repaired_files, responses

    @staticmethod
    def _owner_reexecution_violations(
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        allowed_owners: set[str],
    ) -> list[str]:
        """Detect contract mutation and cross-owner writes during owner replay."""
        violations: list[str] = []
        for key in ("blueprint", "delivery_contract", "delivery_contract_hash", "compiled_contract"):
            if before.get(key) != after.get(key):
                violations.append(f"protected_contract_changed:{key}")

        def file_map(snapshot: dict[str, Any]) -> dict[tuple[str, str], Any]:
            files = snapshot.get("__artifact_files__")
            if not isinstance(files, list):
                return {}
            return {
                (str(item.get("step_id") or "").lower(), str(item.get("name") or "").replace("\\", "/")): item.get("content")
                for item in files
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }

        old_files = file_map(before)
        new_files = file_map(after)
        changed = {
            key
            for key in old_files.keys() | new_files.keys()
            if old_files.get(key) != new_files.get(key)
        }
        ownership = (before.get("blueprint") or {}).get("artifact_ownership") if isinstance(before.get("blueprint"), dict) else {}
        for owner, name in sorted(changed):
            if owner not in allowed_owners:
                violations.append(f"owner_boundary:{owner or 'unowned'}:{name}")
                continue
            if isinstance(ownership, dict) and ownership and not path_allowed(owner, name, ownership):
                violations.append(f"path_boundary:{owner}:{name}")
        return violations

    @staticmethod
    def _validation_quality(validation: ArtifactValidationResult) -> tuple[int, int, int]:
        """A different error is not progress: compilation outranks CRUD checks."""
        return RepairEngine.quality(validation)

    @staticmethod
    def _commit_artifact_revision(state: RunState, before: dict[str, Any]) -> None:
        current = state.context.snapshot()
        files = current.get("__artifact_files__", [])
        old_files = before.get("__artifact_files__", [])
        old_content = {
            (item.get("step_id"), item.get("name")): item.get("content")
            for item in old_files if isinstance(item, dict)
        }
        owners = {
            str(item.get("step_id") or "")
            for item in files if isinstance(item, dict)
            and old_content.get((item.get("step_id"), item.get("name"))) != item.get("content")
        }
        revisions = dict(current.get("__artifact_owner_revisions__", {}) or {})
        for owner in owners:
            revisions[owner] = int(revisions.get(owner, 0)) + 1
        state.context.set("__artifact_owner_revisions__", revisions)
        state.context.set("__artifact_files__", [
            {**item, "owner_revision": revisions[str(item.get("step_id") or "")]}
            if isinstance(item, dict) and str(item.get("step_id") or "") in owners else item
            for item in files
        ])
        for step in state.workflow.steps:
            if step.id in owners and step.output:
                state.context.set(f"{step.output}_files", json.dumps([
                    {"name": item["name"], "content": item["content"]}
                    for item in files if isinstance(item, dict) and item.get("step_id") == step.id
                ], ensure_ascii=False))

    @staticmethod
    def _missing_java_declaration_candidates(
        owned_files: list[dict[str, Any]],
        all_files: list[dict[str, Any]],
        diagnostics: str,
        ownership: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Create candidates only for compiler-proven, locally referenced classes.

        Unknown symbols, third-party imports and ambiguous source locations stay
        with the referencing source Agent; the platform never invents a class
        merely because a name appears in arbitrary validator output.
        """
        normalized = str(diagnostics or "").replace("\\", "/")
        source: dict[str, Any] | None = None
        proposed: dict[str, dict[str, Any]] = {}
        existing_paths = {str(item.get("name") or "") for item in all_files if isinstance(item, dict)}
        declared = "\n".join(str(item.get("content") or "") for item in all_files if isinstance(item, dict))
        for line in normalized.splitlines():
            location = re.search(r"(?i)([A-Za-z0-9_./-]+\.java):\[\d+", line)
            if location:
                path = location.group(1)
                matches = [
                    item for item in owned_files
                    if path.endswith(str(item.get("name") or ""))
                ]
                if not matches:
                    matches = [
                        item for item in owned_files
                        if posixpath.basename(path) == posixpath.basename(str(item.get("name") or ""))
                    ]
                source = matches[0] if len(matches) == 1 else None
                continue
            symbol_match = re.search(r"(?i)^\[ERROR\]\s*(?:symbol\s*:\s*class|符号\s*:\s*类)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if not symbol_match or source is None:
                continue
            symbol = symbol_match.group(1)
            source_path = str(source.get("name") or "")
            content = str(source.get("content") or "")
            # An unqualified constructor call is strong evidence that the
            # project intended to own this class, not just import a library.
            if not re.search(r"\bnew\s+" + re.escape(symbol) + r"\s*\(", content):
                continue
            if re.search(r"\b(?:class|interface|enum|record)\s+" + re.escape(symbol) + r"\b", declared):
                continue
            if (symbol.endswith("NotFoundException") and re.search(
                r"\bclass\s+(?:ResourceNotFoundException|EntityNotFoundException|NotFoundException)\b",
                declared,
            )):
                # The owner Agent should inspect the existing exception's
                # constructor and correct this source reference instead.
                continue
            package_match = re.search(r"(?m)^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;", content)
            source_root = (
                "src/main/java/" if source_path.startswith("src/main/java/") else
                "src/test/java/" if source_path.startswith("src/test/java/") else ""
            )
            if not package_match or not source_root:
                continue
            package_name = package_match.group(1)
            imported = re.search(r"(?m)^\s*import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\." + re.escape(symbol) + r"\s*;", content)
            if imported:
                import_package = imported.group(1)
                root = ".".join(package_name.split(".")[:2])
                if not import_package.startswith(root + "."):
                    continue
                package_name = import_package
            expected_source_dir = source_root + "/".join(package_match.group(1).split("."))
            # A malformed existing path/package is a source-repair issue, not
            # permission to create a class in a second tree.
            if not imported and posixpath.dirname(source_path) != expected_source_dir:
                continue
            target_path = source_root + "/".join(package_name.split(".")) + f"/{symbol}.java"
            if target_path in existing_paths or target_path in proposed:
                continue
            if ownership and not path_allowed("backend", target_path, ownership):
                continue
            proposed[target_path] = {
                "name": target_path, "content": "", "step_id": "backend",
                "create": True, "symbol": symbol, "source_file": source_path,
            }
        return list(proposed.values())[:8]

    @staticmethod
    def _evidence_file_paths(checks: list[Any] | tuple[Any, ...] | None) -> list[str]:
        """Return normalized file paths explicitly named by validator evidence.

        Only file-oriented evidence keys are considered.  Generic evidence such
        as an HTTP ``path`` (``/api/rooms``) must not accidentally become a
        workspace repair target.
        """
        if not checks:
            return []
        file_keys = {
            "file", "files", "filename", "filenames", "filepath", "filepaths",
            "sourcefile", "sourcefiles", "relatedfile", "relatedfiles",
            "artifact", "artifacts", "artifactpath", "artifactpaths",
        }
        found: list[str] = []

        def collect(value: Any, *, file_context: bool = False) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
                    collect(child, file_context=normalized_key in file_keys)
                return
            if isinstance(value, (list, tuple, set)):
                for child in value:
                    collect(child, file_context=file_context)
                return
            if not file_context or not isinstance(value, str):
                return
            path = value.strip().replace("\\", "/").lstrip("./")
            if not path or path.startswith("/") or "/api/" in path.lower():
                return
            if path not in found:
                found.append(path)

        for check in checks:
            collect(getattr(check, "evidence", None))
        return found

    @staticmethod
    def _match_owned_evidence_files(
        owned_files: list[dict[str, Any]],
        evidence_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Resolve evidence paths without guessing between duplicate basenames."""
        if not evidence_paths:
            return []
        normalized_files = {
            str(item.get("name") or "").replace("\\", "/").lstrip("./"): item
            for item in owned_files
            if str(item.get("name") or "").strip()
        }
        selected: dict[str, dict[str, Any]] = {}
        for raw_path in evidence_paths:
            path = raw_path.replace("\\", "/").lstrip("./")
            exact = normalized_files.get(path)
            if exact is not None:
                selected[path] = exact
                continue
            suffix_matches = [
                (name, item)
                for name, item in normalized_files.items()
                if path.endswith("/" + name) or name.endswith("/" + path)
            ]
            if len(suffix_matches) == 1:
                name, item = suffix_matches[0]
                selected[name] = item
                continue
            basename = Path(path).name.lower()
            basename_matches = [
                (name, item)
                for name, item in normalized_files.items()
                if Path(name).name.lower() == basename
            ]
            if len(basename_matches) == 1:
                name, item = basename_matches[0]
                selected[name] = item
        return list(selected.values())

    @staticmethod
    def _validation_repair_candidates(
        owned_files: list[dict[str, Any]],
        diagnostics: str,
        checks: list[Any] | tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate repair files from structured evidence before using heuristics."""
        normalized = diagnostics.replace("\\", "/")
        evidence_candidates = WorkflowExecutor._match_owned_evidence_files(
            owned_files,
            WorkflowExecutor._evidence_file_paths(checks),
        )
        if evidence_candidates:
            return evidence_candidates
        # Maven output may mention dependency names while downloading them.
        # Explicit compiler locations are stronger evidence than those incidental
        # mentions and must be checked before dependency-based routing.
        compiler_paths = [
            match.replace("\\", "/")
            for match in re.findall(
                r"(?m)^\[ERROR\]\s+([^\r\n]+?\.java):\[\d+",
                normalized,
            )
        ]
        if compiler_paths:
            source_files = WorkflowExecutor._match_owned_evidence_files(owned_files, compiler_paths)
            if source_files:
                return source_files
        missing_table = re.search(r"Schema-validation:\s*missing table\s*\[([^\]]+)\]", normalized, re.IGNORECASE)
        if missing_table:
            table_name = missing_table.group(1).split(".")[-1].strip('"`')
            entity_files = [
                item for item in owned_files
                if str(item.get("name") or "").endswith(".java")
                and re.search(
                    r'@(?:[A-Za-z_]\w*\.)*Table\s*\([^)]*\bname\s*=\s*"'
                    + re.escape(table_name) + r'"',
                    str(item.get("content") or ""),
                    re.IGNORECASE,
                )
            ]
            if entity_files:
                return entity_files
        if "后端实体表名合同" in normalized:
            entity_files = [
                item for item in owned_files
                if str(item.get("name") or "").endswith(".java")
                and str(item.get("name") or "").replace("\\", "/") in normalized
            ]
            if entity_files:
                return entity_files
        if "前端 API 合同" in normalized or "前端 Vite 代理合同" in normalized:
            api_sources = [
                item for item in owned_files
                if str(item.get("name") or "").endswith((".vue", ".jsx", ".tsx", ".html", ".js", ".ts"))
                and "vite.config" not in str(item.get("name") or "")
                and str(item.get("name") or "") in normalized
            ]
            if "前端 Vite 代理合同" in normalized:
                api_sources.extend(
                    item for item in owned_files
                    if Path(str(item.get("name") or "")).name.startswith("vite.config.")
                )
            if api_sources:
                return list({str(item["name"]): item for item in api_sources}.values())
        if "真实数据库合同" in normalized:
            return [item for item in owned_files if item.get("name") == "pom.xml"]
        if re.search(r"接口(?:新增|修改|删除)成功，但 H2|可能使用内存 Map", normalized):
            return [item for item in owned_files if str(item.get("name", "")).endswith(("Service.java", "Controller.java", "Repository.java"))]
        if (
            "CRUD 明细路由合同" in normalized
            or re.search(r"H2 CRUD 闭环失败：(?:PUT|PATCH|DELETE)\s+[^\s]+/\{id\}\s+返回 HTTP 404", normalized)
        ):
            controllers = [
                item for item in owned_files
                if str(item.get("name", "")).endswith("Controller.java")
            ]
            if controllers:
                return controllers

        # A startup probe can reach the application while one or more
        # collection entrypoints still return 404 (for example
        # ``/api/products`` and ``/api/cart``). This is a routing failure,
        # not a process-startup failure. Route the repair request to the
        # owner Agent's controller/router instead of leaving it with Tester.
        if re.search(
            r"(?:/api/[^\s=;,]+).*?(?:HTTP\s*404|\b404\b|not found|未找到|未通过)",
            normalized,
            re.IGNORECASE | re.DOTALL,
        ):
            api_paths: list[str] = []
            for raw_path in re.findall(r"/api/[A-Za-z0-9_./{}-]+", normalized):
                path = raw_path.rstrip(".,;:)]}")
                if path and path not in api_paths:
                    api_paths.append(path)
            api_tokens = {
                token.lower()
                for path in api_paths
                for token in path.split("/")
                if token and not token.startswith("{") and token.lower() != "api"
            }
            route_candidates: list[dict[str, Any]] = []
            route_files: list[dict[str, Any]] = []
            for item in owned_files:
                name = str(item.get("name") or "").replace("\\", "/")
                lowered_name = name.lower()
                lowered_content = str(item.get("content") or "").lower()
                route_like = bool(re.search(r"(?:controller|router|routes?|resource|endpoint|api)", lowered_name))
                path_hit = any(path.lower() in lowered_content for path in api_paths)
                token_hit = bool(api_tokens) and any(token in lowered_content for token in api_tokens)
                if route_like and (path_hit or token_hit):
                    route_candidates.append(item)
                if route_like and lowered_name not in {"pom.xml", "package.json"}:
                    route_files.append(item)
            if route_candidates:
                return route_candidates
            if route_files:
                return route_files
            source_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).suffix.lower() in {".java", ".kt", ".py", ".ts", ".js", ".vue"}
            ]
            if source_files:
                return source_files
        if "页面浏览器验证" in normalized or "浏览器 CRUD 操作" in normalized:
            ui_sources = [item for item in owned_files if str(item.get("name", "")).endswith((".html", ".vue", ".jsx", ".tsx", "vite.config.js", "vite.config.ts"))]
            if ui_sources:
                if "VITE_API_PROXY" in normalized or "HTTP 404" in normalized:
                    ui_sources.sort(key=lambda item: 0 if "vite.config" in item["name"] else 1)
                return ui_sources
        if "页面入口 HTTP" in normalized or "页面资源 HTTP" in normalized or "页面打包合同" in normalized:
            html = [item for item in owned_files if str(item.get("name", "")).endswith(".html")]
            if html:
                return html
        if "模板渲染依赖" in normalized or "spring-boot-starter-thymeleaf" in normalized:
            return [item for item in owned_files if item.get("name") == "pom.xml"]
        if "前后端页面渲染合同" in normalized or "模板缺少配套 MVC Controller" in normalized:
            return [item for item in owned_files if str(item.get("name", "")).endswith("Controller.java")]
        export_source = re.search(r"is not exported by\s+[\"'](?P<path>[^\"']+)[\"']", normalized)
        if export_source:
            source_path = export_source.group("path").lstrip("./")
            matched = [
                item
                for item in owned_files
                if str(item.get("name") or "").replace("\\", "/").lstrip("./") == source_path
            ]
            if matched:
                return matched

        # A missing JDBC driver is a dependency/configuration error.  Spring's
        # stack trace usually contains Application.java, but that frame is only
        # where startup was invoked and must never outrank pom.xml.
        if re.search(
            r"Cannot load driver class:\s*org\.h2\.Driver|pom\.xml 缺少 com\.h2database:h2|H2 测试数据库依赖|JPA 源码依赖一致性|spring-boot-starter-data-jpa|package jakarta\.persistence does not exist|flyway-database-h2|dependencies\.dependency\.version|数据库初始化执行器",
            normalized,
            re.IGNORECASE,
        ):
            pom_files = [
                item
                for item in owned_files
                if str(item.get("name") or "").replace("\\", "/").lower() == "pom.xml"
            ]
            if pom_files:
                return pom_files
            config_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"application.yml", "application.yaml", "application.properties"}
            ]
            if config_files:
                return config_files

        if "前后端 api 路由契约" in normalized.lower() or "rest controller" in normalized.lower():
            controllers = [
                item
                for item in owned_files
                if str(item.get("name") or "").lower().endswith("controller.java")
            ]
            if controllers:
                return controllers

        # Spring Boot reports resources using their compiled path
        # (``target/classes/data.sql``), while the Artifact name is usually
        # ``src/main/resources/data.sql``. Map those paths back to the source
        # Artifact. When a data script runs before JPA creates its table, the
        # application configuration is the smallest and safest repair target.
        if re.search(r"table\s+[\"']?[A-Za-z_][\w$]*[\"']?\s+not found", normalized, re.IGNORECASE) and re.search(
            r"(?:^|/)data\.sql\b", normalized, re.IGNORECASE
        ):
            config_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"application.yml", "application.yaml", "application.properties"}
            ]
            seed_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"data.sql", "schema.sql"}
            ]
            if config_files:
                return config_files
            if seed_files:
                return seed_files

        located: list[dict[str, Any]] = []
        for item in owned_files:
            name = str(item.get("name") or "").replace("\\", "/")
            if not name:
                continue
            escaped = re.escape(name)
            if re.search(rf"{escaped}(?::\[?\d|\s*\(\d|:\d)", normalized):
                located.append(item)
        if located:
            return located

        diagnostic_basenames = {
            Path(match.group("path")).name.lower()
            for match in re.finditer(r"(?P<path>(?:[A-Za-z]:)?[^\s'\"()]+\.[A-Za-z0-9]+)", normalized)
        }
        owned_by_basename: dict[str, list[dict[str, Any]]] = {}
        for item in owned_files:
            basename = Path(str(item.get("name") or "")).name.lower()
            if basename:
                owned_by_basename.setdefault(basename, []).append(item)
        basename_matches = [
            items[0]
            for basename, items in owned_by_basename.items()
            if basename in diagnostic_basenames and len(items) == 1
        ]
        if basename_matches:
            return basename_matches

        # Last resort for tools whose logs omit line numbers. Avoid build
        # descriptors unless the diagnostic explicitly identifies them as the
        # failing source; regenerating a healthy pom/package file can introduce
        # unrelated dependency drift.
        return [
            item
            for item in owned_files
            if str(item.get("name") or "").replace("\\", "/") in normalized
            and str(item.get("name") or "").lower() not in {"pom.xml", "package.json"}
        ]

    @staticmethod
    def _clean_artifact_repair_output(value: str) -> str:
        content = str(value or "").strip()
        match = re.fullmatch(r"```[^\r\n]*\r?\n(?P<body>.*?)\r?\n?```", content, flags=re.DOTALL)
        return match.group("body").strip() if match else content

    async def _finish(self, state: RunState, status: RunStatus, error: str | None = None, final_report: str | None = None, started_perf: float | None = None) -> None:
        if state.run_id not in self._active:
            return
        if started_perf is not None:
            # Kept for compatibility with callers that provide an explicit
            # segment start; approval exclusion applies to the run total.
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
        else:
            duration_ms = self._active_duration_ms(state)
        state.execution_status = status.value
        if status == RunStatus.SUCCESS:
            facts = state.context.snapshot().get("failure_facts")
            if isinstance(facts, list):
                resolved_facts = [
                    {**fact, "resolved": True} if isinstance(fact, dict) else fact
                    for fact in facts
                ]
                state.context.set("failure_facts", resolved_facts)
                self.repository.update_run(
                    state.run_id,
                    failure_facts_json=json.dumps(resolved_facts, ensure_ascii=False),
                )
        gate = state.context.snapshot().get("delivery_gate")
        if isinstance(gate, dict):
            state.delivery_status = "PASSED" if gate.get("deliverable") else "FAILED"
        elif state.delivery_status == "NOT_EVALUATED":
            state.delivery_status = "NOT_EVALUATED"
        self._persist_state(state)
        self.repository.update_run(
            state.run_id,
            status=status.value,
            execution_status=state.execution_status,
            delivery_status=state.delivery_status,
            finished_at=utc_now(),
            duration_ms=duration_ms,
            error_message=error,
            final_report=final_report,
        )
        event_type = {RunStatus.SUCCESS: "workflow.completed", RunStatus.STOPPED: "workflow.stopped"}.get(status, "workflow.failed")
        await self.event_bus.emit(event_type, state.run_id, {"workflowId": state.workflow.id, "status": status.value, "error": error, "finalReport": final_report, "durationMs": duration_ms, "approvalDurationMs": self._approval_duration_ms(state), "deliverable": bool((state.context.snapshot().get("delivery_gate") or {}).get("deliverable"))})
        if status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.STOPPED}:
            self._active.pop(state.run_id, None)


def _thinking_for_attempt(initial: str, retry_count: int) -> str:
    """Return a strictly non-increasing thinking level for each attempt."""
    level = str(initial).strip().lower()
    if retry_count <= 0:
        return level
    if retry_count == 1 and level in {"high", "max"}:
        return "low"
    return "off"


def _reasoning_budget_exhausted(response: LLMResponse, max_tokens: int) -> bool:
    """Detect a response that spent essentially all output budget on reasoning."""
    if response.text.strip():
        return False
    usage = response.usage if isinstance(response.usage, dict) else {}
    completion_tokens = int(usage.get("completion_tokens", response.output_tokens or 0) or 0)
    details = usage.get("completion_tokens_details")
    reasoning_tokens = int(details.get("reasoning_tokens", 0) or 0) if isinstance(details, dict) else 0
    reached_limit = response.finish_reason == "length" or completion_tokens >= max(1, int(max_tokens * 0.95))
    reasoning_dominated = reasoning_tokens >= max(1, int(completion_tokens * 0.9)) if completion_tokens else False
    return reached_limit and (reasoning_dominated or bool((response.reasoning_content or "").strip()))


def _can_use_structured_fallback(response: LLMResponse | None, error_message: str) -> bool:
    """Allow local recovery only for response-format failures, not transport errors."""
    if response is None:
        return False
    if str(response.finish_reason or "").strip().lower() == "length":
        return True
    if response.reasoning_content and not response.text.strip():
        return True
    normalized = error_message.lower()
    return any(
        marker in normalized
        for marker in (
            "output validation failed",
            "invalid_json",
            "empty output",
            "truncated",
            "query_parameters",
            "attributeerror",
            "object has no attribute",
            "expected a valid json object",
            "实体合同 fields",
            "字段名到类型的映射",
            "实体字段",
            "类型未明确",
        )
    )


def _continuation_prompt(original_prompt: str, partial_output: str) -> str:
    return (
        f"{original_prompt}\n\n"
        "上一次输出已达到 Provider 的长度上限，以下是已保存的部分输出。"
        "请只从末尾继续生成缺失内容，不要重复已有内容，不要重新开始：\n"
        "<partial_output>\n"
        f"{partial_output}\n"
        "</partial_output>\n"
        "请只输出新增内容。"
    )


def _merge_continuation(previous: str, current: str, output_format: str) -> str:
    if not previous:
        return current
    if not current:
        return previous
    max_overlap = min(len(previous), len(current), 2000)
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == current[:size]:
            return previous + current[size:]
    separator = "\n" if output_format in {"text", "markdown"} else ""
    return previous + separator + current


def _merge_usage(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in current.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        elif key not in merged:
            merged[key] = value
    return merged
