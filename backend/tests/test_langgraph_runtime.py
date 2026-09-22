import pytest

from app.workflow.langgraph_runtime import LangGraphWorkflow
from app.workflow.models import StepDefinition, WorkflowDefinition


@pytest.mark.asyncio
async def test_multiple_dependencies_form_an_all_parent_barrier():
    completed: list[str] = []

    async def handler(step, state):
        if step.id == "join":
            assert {"architecture_approval", "database"}.issubset(completed)
        completed.append(step.id)
        return {
            "context": {step.id: "done"},
            "results": {step.id: "SUCCESS"},
        }

    workflow = WorkflowDefinition(
        id="fanin",
        name="fanin",
        steps=[
            StepDefinition("architecture_approval"),
            StepDefinition("database", depends_on=["architecture_approval"]),
            StepDefinition("join", depends_on=["architecture_approval", "database"]),
        ],
    )

    graph = LangGraphWorkflow(workflow, handler)
    result = await graph.ainvoke({"run_id": "fanin-test", "context": {}, "results": {}}, run_id="fanin-test")

    assert completed == ["architecture_approval", "database", "join"]
    assert result["results"]["join"] == "SUCCESS"


def test_owner_revision_removes_stale_layout_without_losing_sibling():
    from app.workflow.langgraph_runtime import _merge_dicts

    old = {"__artifact_files__": [
        {"step_id": "backend", "name": "old/Student.java", "content": "old"},
        {"step_id": "frontend", "name": "src/App.vue", "content": "vue"},
    ]}
    new = {"__artifact_owner_revisions__": {"backend": 1}, "__artifact_files__": [
        {"step_id": "backend", "name": "new/Student.java", "content": "new", "owner_revision": 1},
    ]}
    merged = _merge_dicts(old, new)
    assert {item["name"] for item in merged["__artifact_files__"]} == {"new/Student.java", "src/App.vue"}
    assert _merge_dicts(merged, old)["__artifact_files__"] == merged["__artifact_files__"]
