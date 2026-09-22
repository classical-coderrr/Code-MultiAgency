import asyncio

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agents.registry import AgentRegistry
from app.llm.mock import MockProvider
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, StepType, WorkflowDefinition


@pytest.mark.asyncio
async def test_executor_uses_async_sqlite_checkpoint_for_approval(tmp_path):
    checkpoint_path = tmp_path / "checkpoints.db"
    connection = await aiosqlite.connect(str(checkpoint_path))
    saver = AsyncSqliteSaver(connection)
    await saver.setup()
    workflow = WorkflowDefinition(
        id="async-checkpoint",
        name="async-checkpoint",
        steps=[
            StepDefinition("before", agent_id="requirement_agent", task_template="hi", output="before"),
            StepDefinition("approval", type=StepType.APPROVAL, depends_on=["before"]),
            StepDefinition("after", agent_id="frontend_agent", depends_on=["approval"], task_template="{{before}}", output="after"),
        ],
    )
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_async_checkpoint", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        MockProvider(),
        WorkflowEventBus(),
        repository,
        checkpointer=saver,
    )

    try:
        await executor.start("run_async_checkpoint", workflow, {})
        assert repository.get_run("run_async_checkpoint")["status"] == RunStatus.WAITING_APPROVAL.value
        await executor.approve("run_async_checkpoint", "approve")
        await asyncio.sleep(0.8)
        assert repository.get_run("run_async_checkpoint")["status"] == RunStatus.SUCCESS.value
    finally:
        await connection.close()
