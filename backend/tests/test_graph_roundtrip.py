from app.services.workflow_service import WorkflowService
from app.agents.registry import AgentRegistry


def test_graph_to_workflow_preserves_layout_and_dependencies():
    service = WorkflowService("app/workflows")
    workflow = service.get_workflow("software-development")
    graph = service.graph_dto(workflow)
    requirement_node = next(node for node in graph["nodes"] if node["id"] == "requirement")
    requirement_node["position"] = {"x": 123, "y": 456}
    roundtrip = service.graph_to_workflow(workflow, graph)
    assert roundtrip.meta["layout"]["requirement"] == {"x": 123, "y": 456}
    assert next(step for step in roundtrip.steps if step.id == "tester").depends_on == ["database", "backend", "frontend"]
    assert next(step for step in roundtrip.steps if step.id == "database").agent_id == "database_agent"
    # Frozen contracts remove the need for implementation Agents to wait for
    # Database prose/files; all three owners can work in parallel.
    assert next(step for step in roundtrip.steps if step.id == "backend").depends_on == ["architecture_approval"]
    assert next(step for step in roundtrip.steps if step.id == "frontend").depends_on == ["architecture_approval"]
    original_modes = {step.id: step.generation_mode for step in workflow.steps}
    assert {step.id: step.generation_mode for step in roundtrip.steps} == original_modes
    assert all(original_modes[step_id] == "artifacts" for step_id in ("database", "backend", "frontend"))


def test_independent_workflow_yaml_files_are_discoverable():
    service = WorkflowService("app/workflows")
    workflows = {workflow.id: workflow for workflow in service.list_workflows()}

    assert {"requirement-analysis", "backend-implementation", "frontend-implementation", "software-development"}.issubset(workflows)
    assert workflows["requirement-analysis"].steps[0].agent_id == "requirement_agent"
    assert workflows["backend-implementation"].steps[0].agent_id == "backend_agent"
    assert workflows["frontend-implementation"].steps[0].agent_id == "frontend_agent"


def test_software_workflow_preserves_original_scope_for_downstream_agents():
    service = WorkflowService("app/workflows")
    workflow = service.get_workflow("software-development")
    steps = {step.id: step for step in workflow.steps}

    assert steps["token_estimator"].runtime_plan is True
    assert steps["token_estimator"].max_tokens == 800
    assert steps["token_estimator"].retry_count == 0
    assert steps["token_estimator"].thinking == "off"
    assert steps["requirement"].depends_on == []
    assert steps["token_estimator"].depends_on == ["requirement"]
    assert steps["architecture"].depends_on == ["token_estimator"]
    assert "{{requirement_doc}}" in steps["token_estimator"].task_template
    assert "{{requirement_spec}}" in steps["token_estimator"].task_template
    for step_id in ("architecture", "backend", "frontend"):
        assert "{{requirement}}" in steps[step_id].task_template

    requirement_agent = AgentRegistry.from_directory("agents").get("requirement_agent")
    assert "不得把用户未明确要求" in requirement_agent.system_prompt
    assert "可选建议" in requirement_agent.system_prompt
