from __future__ import annotations

import aiosqlite
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agents.registry import AgentRegistry
from .api.routes import create_router
from .llm.factory import ProviderFactory
from .repositories.sqlite import SQLiteRepository
from .services.workflow_service import WorkflowService
from .services.artifacts import ArtifactService
from .skills.registry import SkillRegistry
from .workflow.events import WorkflowEventBus
from .workflow.executor import WorkflowExecutor
from .runtime.coordination import create_run_coordinator_from_env


BASE_DIR = Path(__file__).resolve().parent
# 从 backend/.env 读取云端模型配置；Key 只在后端进程中使用，不下发到前端。
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR.parent / ".env.runtime", override=False)
repository = SQLiteRepository(BASE_DIR.parent / "data" / "agent_team.db")
execution_mode = os.getenv("AGENT_TEAM_EXECUTION_MODE", "local").strip().lower()
if execution_mode not in {"local", "redis"}:
    execution_mode = "local"
run_coordinator = create_run_coordinator_from_env() if execution_mode == "redis" else None
event_bus = WorkflowEventBus(repository, run_coordinator)
checkpoint_connection: aiosqlite.Connection | None = None
checkpoint_store: AsyncSqliteSaver | None = None
checkpoint_db_path = BASE_DIR.parent / "data" / "langgraph_checkpoints.db"
workflow_service = WorkflowService(BASE_DIR / "workflows")
artifact_service = ArtifactService(repository, BASE_DIR.parent / "data" / "workspaces")
agent_registry = AgentRegistry.from_directory(BASE_DIR.parent / "agents")
skill_registry = SkillRegistry.from_directories([
    # A local override is useful while developing a company template, while
    # the repository Skills remain the safe default for every installation.
    os.getenv("AGENT_TEAM_SKILLS_DIR"),
    BASE_DIR.parent / "skills",
])
provider = ProviderFactory.create_from_env()
executor = WorkflowExecutor(
    agent_registry,
    provider,
    event_bus,
    repository,
    # The durable async checkpointer is attached during FastAPI startup,
    # inside the running event loop. This in-memory value is only a safe
    # construction-time fallback before startup hooks execute.
    checkpointer=InMemorySaver(),
    artifact_service=artifact_service,
    skill_registry=skill_registry,
)

app = FastAPI(title="Agent Team Orchestrator", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:2199", "http://127.0.0.1:2199"], allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Type"])
app.include_router(create_router(
    workflow_service,
    executor,
    event_bus,
    repository,
    artifact_service,
    run_coordinator,
    execution_mode,
))


@app.on_event("startup")
async def restore_waiting_runs() -> None:
    global checkpoint_connection, checkpoint_store
    if execution_mode == "redis":
        if run_coordinator is None:
            raise RuntimeError("AGENT_TEAM_EXECUTION_MODE=redis requires REDIS_URL")
        await run_coordinator.ping()
        return
    # Keep LangGraph checkpoint writes separate from business run history. Both
    # layers are active during parallel execution and SQLite has one writer at
    # a time, so sharing the file creates avoidable lock contention.
    checkpoint_connection = await aiosqlite.connect(str(checkpoint_db_path), timeout=30.0)
    await checkpoint_connection.execute("PRAGMA busy_timeout = 30000")
    await checkpoint_connection.execute("PRAGMA synchronous = NORMAL")
    await checkpoint_connection.commit()
    checkpoint_store = AsyncSqliteSaver(checkpoint_connection)
    await checkpoint_store.setup()
    executor.checkpointer = checkpoint_store
    executor.restore_runs(workflow_service)


@app.on_event("shutdown")
async def close_checkpoint_store() -> None:
    if checkpoint_connection is not None:
        await checkpoint_connection.close()
    if run_coordinator is not None:
        await run_coordinator.close()
