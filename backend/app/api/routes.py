"""Thin HTTP adapters. Business operations live in services and executor."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dotenv import set_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..repositories.sqlite import SQLiteRepository
from ..llm.base import LLMError
from ..llm.factory import ProviderFactory
from ..llm.local_codex import LocalCodexProvider
from ..llm.mock import MockProvider
from ..schemas.api import ApprovalRequest, ClarificationRequestBody, ProviderConfigRequest, RunCreateRequest
from ..services.workflow_service import WorkflowService
from ..services.artifacts import ArtifactService
from ..workflow.events import WorkflowEventBus
from ..workflow.executor import WorkflowExecutor
from ..workflow.models import RunStatus, StepType, utc_now
from ..runtime.coordination import RunCoordinator


def create_router(
    service: WorkflowService,
    executor: WorkflowExecutor,
    event_bus: WorkflowEventBus,
    repository: SQLiteRepository,
    artifact_service: ArtifactService | None = None,
    coordinator: RunCoordinator | None = None,
    execution_mode: str = "local",
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, object]:
        redis_ok = False
        redis_error = None
        workers: list[dict[str, Any]] = []
        if coordinator is not None:
            try:
                redis_ok = await coordinator.ping()
                workers = await coordinator.active_workers()
            except Exception as exc:
                redis_error = str(exc)
        coordination_required = execution_mode == "redis"
        healthy = not coordination_required or (redis_ok and bool(workers))
        return {
            "status": "ok" if healthy else "degraded",
            "executionMode": execution_mode,
            "redis": {"configured": coordinator is not None, "available": redis_ok, "error": redis_error},
            "workers": {"active": len(workers), "items": workers},
            "eventFanoutError": event_bus.coordination_error,
        }

    @router.get("/provider/config")
    async def get_provider_config() -> dict:
        provider_name = os.getenv("MODEL_PROVIDER", "qwen").strip().lower()
        is_local_codex = provider_name in {"chatgpt_local", "codex_local"}
        key = os.getenv("MODEL_API_KEY", "")
        current_provider = ProviderFactory.create_from_env()
        return {
            "provider": provider_name,
            "baseUrl": "" if is_local_codex else os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "modelName": os.getenv("MODEL_NAME", "codex-default" if is_local_codex else "qwen-plus"),
            "apiKeyConfigured": False if is_local_codex else bool(key) and not key.startswith("REPLACE_WITH_") and not key.startswith("sk-your-"),
            "usingMock": isinstance(current_provider, MockProvider),
            "authMode": "local_codex" if is_local_codex else "api_key",
        }

    @router.get("/provider/capabilities")
    async def get_provider_capabilities() -> dict:
        return ProviderFactory.create_from_env().capabilities()

    @router.get("/skills")
    async def list_skills() -> list[dict]:
        """Expose safe Skill metadata only; prompt bodies stay server-side."""
        return executor.skill_resolver.registry.list_public()

    @router.post("/provider/test")
    async def test_provider(request: ProviderConfigRequest) -> dict:
        current_base_url = os.getenv("MODEL_BASE_URL", "")
        is_local_codex = request.provider.strip().lower() in {"chatgpt_local", "codex_local"}
        api_key = "" if is_local_codex else request.api_key or (os.getenv("MODEL_API_KEY", "") if request.base_url.rstrip("/") == current_base_url.rstrip("/") else "")
        provider = ProviderFactory.create(request.provider, request.base_url, api_key, request.model_name)
        if isinstance(provider, LocalCodexProvider):
            started = time.perf_counter()
            try:
                await provider.check_login()
            except LLMError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {
                "success": True,
                "model": provider.model_name,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": "本地 Codex 登录可用",
                "authMode": "local_codex",
            }
        if isinstance(provider, MockProvider):
            provider_label = {"qwen": "千问（Qwen）", "deepseek": "DeepSeek"}.get(request.provider.strip().lower(), request.provider)
            raise HTTPException(status_code=400, detail=f"请先输入 {provider_label} API Key，再测试连接")
        started = time.perf_counter()
        try:
            response = await provider.generate(
                "You are a connectivity check.",
                "Reply with exactly OK.",
                {
                    "request_timeout": 20,
                    "agent_id": "connection_test",
                    "max_tokens": 32,
                    "effective_thinking": "off",
                    "reasoning_effort": "low",
                    "thinking_type": "disabled",
                },
            )
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"success": True, "model": response.model, "latencyMs": int((time.perf_counter() - started) * 1000), "message": "Provider connection successful"}

    @router.post("/provider/config")
    async def save_provider_config(request: ProviderConfigRequest) -> dict:
        current_base_url = os.getenv("MODEL_BASE_URL", "")
        is_local_codex = request.provider.strip().lower() in {"chatgpt_local", "codex_local"}
        if not is_local_codex and not request.api_key and request.base_url.rstrip("/") != current_base_url.rstrip("/"):
            raise HTTPException(status_code=400, detail="Enter a new API key when changing the provider Base URL")
        api_key = "" if is_local_codex else request.api_key or os.getenv("MODEL_API_KEY", "")
        provider = ProviderFactory.create(request.provider, request.base_url, api_key, request.model_name)
        env_path = Path(__file__).resolve().parents[2] / ".env"
        set_key(str(env_path), "MODEL_PROVIDER", request.provider)
        set_key(str(env_path), "MODEL_BASE_URL", request.base_url)
        set_key(str(env_path), "MODEL_NAME", request.model_name)
        set_key(str(env_path), "MODEL_API_KEY", api_key)
        os.environ.update({"MODEL_PROVIDER": request.provider, "MODEL_BASE_URL": request.base_url, "MODEL_NAME": request.model_name, "MODEL_API_KEY": api_key})
        executor.provider = provider
        executor.model_invocation.set_provider(provider)
        return {
            "success": True,
            "usingMock": isinstance(provider, MockProvider),
            "modelName": request.model_name,
            "apiKeyConfigured": bool(api_key) and not is_local_codex,
            "authMode": "local_codex" if is_local_codex else "api_key",
            "message": "本地 Codex 配置已保存" if is_local_codex else "Provider configuration saved",
        }

    @router.get("/workflows")
    async def list_workflows() -> list[dict]:
        return [{"id": item.id, "name": item.name, "stepCount": len(item.steps), "concurrency": item.concurrency} for item in service.list_workflows()]

    @router.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict:
        try:
            workflow = service.get_workflow(workflow_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        steps = [{"id": step.id, "type": step.type.value, "agentId": step.agent_id, "dependsOn": step.depends_on, "output": step.output, "outputFormat": step.output_format, "failurePolicy": step.failure_policy, "runtimePlan": step.runtime_plan, "generationMode": step.generation_mode, "enforceArtifactContract": step.enforce_artifact_contract, "skills": step.skills, "skillMode": step.skill_mode, "validation": step.validation, "runtime": {"maxTokens": step.max_tokens, "retry": step.retry_count, "thinking": step.thinking}} for step in workflow.steps]
        return {"id": workflow.id, "name": workflow.name, "inputs": workflow.inputs, "concurrency": workflow.concurrency, "runtimeDefaults": workflow.runtime_defaults, "steps": steps, "graph": service.graph_dto(workflow)}

    @router.get("/workflows/{workflow_id}/graph")
    async def get_graph(workflow_id: str) -> dict:
        try:
            return service.graph_dto(service.get_workflow(workflow_id))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/workflows/{workflow_id}/runs", status_code=202)
    async def create_run(workflow_id: str, request: RunCreateRequest) -> dict[str, str]:
        try:
            workflow = service.get_workflow(workflow_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Multiple Runs enter the executor queue; a previous Run no longer
        # blocks creation of a new requirement.
        if False and executor.is_active(workflow_id):
            active_run = executor.active_run_for_workflow(workflow_id)
            detail = {
                "message": "已有未完成的工作流运行，请继续当前运行或先停止它",
                "runId": active_run["runId"] if active_run else None,
                "status": active_run["status"] if active_run else RunStatus.RUNNING.value,
            }
            raise HTTPException(
                status_code=409,
                detail=detail,
            )
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        runtime = request.runtime.model_dump(exclude_none=True)
        repository.create_run(run_id, workflow_id, {"requirement": request.requirement, "runtime": runtime}, RunStatus.PENDING.value, utc_now())
        if execution_mode == "redis":
            if coordinator is None:
                repository.update_run(run_id, status=RunStatus.FAILED.value, execution_status=RunStatus.FAILED.value, error_message="Redis Worker 模式未配置 REDIS_URL")
                raise HTTPException(status_code=503, detail="Redis Worker 模式未配置 REDIS_URL")
            try:
                await coordinator.enqueue(run_id, "start")
            except Exception as exc:
                repository.update_run(run_id, status=RunStatus.FAILED.value, execution_status=RunStatus.FAILED.value, error_message=f"任务入队失败：{exc}")
                raise HTTPException(status_code=503, detail=f"任务入队失败：{exc}") from exc
        else:
            asyncio.create_task(executor.start(run_id, workflow, {"requirement": request.requirement}, runtime))
        return {"runId": run_id, "status": RunStatus.PENDING.value}

    @router.get("/runs")
    async def list_runs(limit: int = 50, offset: int = 0) -> list[dict]:
        return repository.list_run_summaries(limit=limit, offset=offset)

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        result = repository.get_run(run_id)
        if not result:
            raise HTTPException(status_code=404, detail="Run not found")
        # Recovery snapshots and workflow definitions are internal persistence
        # records; keep them out of the normal UI payload because they can
        # contain large Agent outputs. Expose only safe recovery metadata.
        result["recoveryCount"] = int(result.get("recovery_count") or 0)
        result["heartbeatAt"] = result.get("heartbeat_at")
        result["hasRecoverySnapshot"] = bool(result.get("state"))
        snapshot = result.get("state") if isinstance(result.get("state"), dict) else {}
        context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}
        result["deliveryGate"] = context.get("delivery_gate")
        result["deliverable"] = bool((context.get("delivery_gate") or {}).get("deliverable"))
        result["deliveryContract"] = context.get("delivery_contract")
        result["requirementState"] = context.get("requirement_state") or (context.get("requirement_spec") or {}).get("state")
        result["clarificationRequest"] = result.get("clarification") or context.get("clarification_request")
        result["projectBlueprint"] = result.get("blueprint") or context.get("project_blueprint")
        result["compiledContract"] = context.get("compiled_contract") or {}
        result["evidence"] = result.get("evidence") or context.get("evidence") or []
        result["failureFacts"] = result.get("failure_facts") or context.get("failure_facts") or []
        result["decisionLog"] = result.get("decision_log") or context.get("decision_log") or []
        result["assumptionLog"] = result.get("assumption_log") or context.get("assumption_log") or []
        result["workspace"] = context.get("workspace") or {}
        result["executionPlan"] = context.get("execution_plan") or {}
        repo_index = context.get("repo_index") if isinstance(context.get("repo_index"), dict) else {}
        result["repositoryIntelligence"] = {
            "summary": repo_index.get("summary") or {},
            "tree": repo_index.get("tree") or [],
            "dependencies": repo_index.get("dependencies") or [],
        }
        result["artifactVersions"] = repository.list_artifact_versions(run_id)
        result["skillResolutions"] = context.get("__skill_resolutions__", {}) if isinstance(context.get("__skill_resolutions__", {}), dict) else {}
        recent_events = repository.list_events(run_id)[-1000:]
        result["recentEvents"] = recent_events
        result["eventCursor"] = int(recent_events[-1]["id"]) if recent_events else 0
        if coordinator is not None:
            try:
                result["workerLease"] = await coordinator.lease_owner(run_id)
            except Exception:
                result["workerLease"] = None
        result.pop("state", None)
        result.pop("workflow_snapshot", None)
        if artifact_service:
            result["artifacts"] = artifact_service.list_public(run_id)
            for artifact in result["artifacts"]:
                artifact["contentUrl"] = f"/api/runs/{run_id}/artifacts/{artifact['id']}/content"
            result["artifactArchiveUrl"] = f"/api/runs/{run_id}/artifacts/archive"
        return result

    @router.get("/runs/{run_id}/artifacts")
    async def list_run_artifacts(run_id: str) -> list[dict]:
        if not repository.get_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        if not artifact_service:
            return []
        artifacts = artifact_service.list_public(run_id)
        for artifact in artifacts:
            artifact["contentUrl"] = f"/api/runs/{run_id}/artifacts/{artifact['id']}/content"
        return artifacts

    @router.get("/runs/{run_id}/artifacts/archive")
    async def download_artifacts_archive(run_id: str) -> FileResponse:
        run = repository.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if not artifact_service:
            raise HTTPException(status_code=404, detail="Artifact service is not configured")
        try:
            context = (run.get("state") or {}).get("context") or {}
            archive_path = await asyncio.to_thread(artifact_service.create_archive, run_id, strict=bool(context.get("delivery_contract")))
            if context.get("delivery_contract") and run.get("status") == "SUCCESS":
                from ..workflow.delivery_gate import evaluate_delivery
                gate = await asyncio.to_thread(evaluate_delivery, context, context.get("artifact_validation"), archive_path)
                if not gate["deliverable"]:
                    raise HTTPException(status_code=409, detail="交付源码或验证证据已变更，请重新验证后下载：" + "；".join(gate["missing"]))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{run_id}-artifacts.zip",
        )

    @router.get("/runs/{run_id}/artifacts/{artifact_id}/content")
    async def get_artifact_content(run_id: str, artifact_id: str, download: bool = False) -> FileResponse:
        if not repository.get_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        if not artifact_service:
            raise HTTPException(status_code=404, detail="Artifact service is not configured")
        resolved = artifact_service.resolve_path(run_id, artifact_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path, media_type = resolved
        # Omit Content-Disposition so index.html opens as a browser preview
        # instead of being forced into a download. Generated HTML is served
        # in a sandboxed document so it cannot access the platform origin.
        return FileResponse(
            path,
            media_type=media_type,
            filename=path.name if download else None,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline' https:; img-src data: https:; font-src https:; script-src 'unsafe-inline'; connect-src 'none'",
            },
        )

    @router.post("/runs/{run_id}/approval")
    async def resolve_approval(run_id: str, request: ApprovalRequest) -> dict[str, str]:
        try:
            if execution_mode == "redis" and coordinator is not None:
                lease = await coordinator.lease_owner(run_id)
                if not lease:
                    run = repository.get_run(run_id)
                    if not run or str(run.get("status")) != RunStatus.WAITING_APPROVAL.value:
                        raise ValueError("Run is not waiting for approval")
                    await coordinator.enqueue(run_id, "approve", {"decision": request.decision})
                else:
                    await coordinator.send_control(run_id, "approve", {"decision": request.decision, "lease_token": lease.get("token")})
            else:
                await executor.approve(run_id, request.decision)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"runId": run_id, "decision": request.decision}

    @router.post("/runs/{run_id}/clarification")
    async def resolve_clarification(run_id: str, request: ClarificationRequestBody) -> dict[str, str]:
        try:
            if execution_mode == "redis" and coordinator is not None:
                lease = await coordinator.lease_owner(run_id)
                if not lease:
                    run = repository.get_run(run_id)
                    if not run or str(run.get("status")) != RunStatus.WAITING_CLARIFICATION.value:
                        raise ValueError("Run is not waiting for requirement clarification")
                    await coordinator.enqueue(run_id, "clarify", {"answers": request.answers})
                else:
                    await coordinator.send_control(run_id, "clarify", {"answers": request.answers, "lease_token": lease.get("token")})
            else:
                await executor.clarify(run_id, request.answers)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"runId": run_id, "status": RunStatus.RUNNING.value}

    @router.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, str]:
        try:
            if execution_mode == "redis" and coordinator is not None:
                lease = await coordinator.lease_owner(run_id)
                if not lease:
                    run = repository.get_run(run_id)
                    if not run:
                        raise ValueError("Run not found")
                    if str(run.get("status")) in {
                        RunStatus.PENDING.value,
                        RunStatus.QUEUED.value,
                        RunStatus.WAITING_APPROVAL.value,
                        RunStatus.WAITING_CLARIFICATION.value,
                    }:
                        repository.update_run(
                            run_id,
                            status=RunStatus.STOPPED.value,
                            execution_status=RunStatus.STOPPED.value,
                            finished_at=utc_now(),
                            error_message="用户在 Worker 接管前停止了运行",
                        )
                        await event_bus.emit("workflow.stopped", run_id, {
                            "status": RunStatus.STOPPED.value,
                            "reason": "stopped_before_worker_claim",
                        })
                    else:
                        raise ValueError("Run 当前没有活动 Worker，请等待接管后重试")
                else:
                    await coordinator.send_control(run_id, "stop", {"lease_token": lease.get("token")})
            else:
                await executor.stop(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"runId": run_id, "status": RunStatus.STOPPED.value}

    @router.post("/runs/{run_id}/retry", status_code=202)
    async def retry_run(run_id: str) -> dict[str, str | int]:
        run = repository.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            workflow = service.get_workflow_for_run(run)
            snapshot = run.get("state") if isinstance(run.get("state"), dict) else {}
            waiting_step_id = str(snapshot.get("waiting_step_id") or "")
            waiting_row_status = ""
            if not waiting_step_id:
                waiting_row = next(
                    (
                        step for step in run.get("steps", [])
                        if isinstance(step, dict)
                        and str(step.get("status") or "") in {
                            RunStatus.WAITING_APPROVAL.value,
                            RunStatus.WAITING_CLARIFICATION.value,
                        }
                    ),
                    None,
                )
                if waiting_row:
                    waiting_step_id = str(waiting_row.get("step_id") or "")
                    waiting_row_status = str(waiting_row.get("status") or "")
            if str(run.get("status") or "") == RunStatus.STOPPED.value and waiting_step_id:
                waiting_step = next((step for step in workflow.steps if step.id == waiting_step_id), None)
                waiting_status = (
                    RunStatus.WAITING_APPROVAL.value
                    if waiting_row_status == RunStatus.WAITING_APPROVAL.value
                    or (waiting_step is not None and waiting_step.type == StepType.APPROVAL)
                    else RunStatus.WAITING_CLARIFICATION.value
                )
                repository.update_run(
                    run_id,
                    status=waiting_status,
                    execution_status=waiting_status,
                    finished_at=None,
                    error_message=None,
                    heartbeat_at=utc_now(),
                )
                recovery_count = int(run.get("recovery_count") or 0)
                await event_bus.emit("workflow.recovered", run_id, {
                    "workflowId": workflow.id,
                    "previousStatus": RunStatus.STOPPED.value,
                    "recoveryCount": recovery_count,
                    "paused": True,
                    "status": waiting_status,
                    "waitingStepId": waiting_step_id,
                })
                if waiting_status == RunStatus.WAITING_APPROVAL.value:
                    await event_bus.emit("workflow.waiting_approval", run_id, {
                        "stepId": waiting_step_id,
                        "status": waiting_status,
                    })
                else:
                    context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}
                    clarification = run.get("clarification") or context.get("clarification_request") or {}
                    await event_bus.emit("workflow.clarification_required", run_id, {
                        "stepId": waiting_step_id,
                        "status": waiting_status,
                        "request": clarification,
                    })
                return {"runId": run_id, "status": waiting_status, "recoveryCount": recovery_count}

            # Redis dispatch is asynchronous, therefore eligibility must be
            # checked before returning HTTP 202.  Otherwise the UI reports a
            # successful retry while the Worker later rejects the Run.
            executor.validate_retry(run_id, workflow)
            if execution_mode == "redis" and coordinator is not None:
                await coordinator.enqueue(run_id, "retry")
                recovery_count = int(run.get("recovery_count") or 0) + 1
            else:
                recovery_count = await executor.retry(run_id, workflow)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"runId": run_id, "status": RunStatus.RUNNING.value, "recoveryCount": recovery_count}

    @router.get("/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        if not repository.get_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")

        header_cursor = request.headers.get("last-event-id", "").strip()
        try:
            after_event_id = max(int(after or 0), int(header_cursor or 0))
        except ValueError:
            after_event_id = max(0, int(after or 0))

        async def generator() -> AsyncIterator[str]:
            async for event in event_bus.stream(run_id, after_event_id):
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)

        return StreamingResponse(generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    return router
