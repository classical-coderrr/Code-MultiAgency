"""Bounded retry, contract reopen, and stale-output invalidation services."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .models import RunStatus, StepStatus, StepType, WorkflowDefinition
from .platform_contracts import normalize_failure_fact, request_blueprint_change
from .run_state import RunState


class RunRecoveryMixin:
    """Recovery behavior shared by WorkflowExecutor without owning scheduling."""
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

