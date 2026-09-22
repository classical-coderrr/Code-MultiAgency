import pytest

from app.workflow.dag import build_dag
from app.workflow.models import StepDefinition, WorkflowDefinition
from app.workflow.validator import WorkflowValidationError


def workflow(*steps: StepDefinition) -> WorkflowDefinition:
    return WorkflowDefinition(id="test", name="test", steps=list(steps))


def test_dag_topology_and_parallel_level():
    dag = build_dag(workflow(
        StepDefinition("a", agent_id="a"),
        StepDefinition("b", agent_id="b", depends_on=["a"]),
        StepDefinition("c", agent_id="c", depends_on=["a"]),
        StepDefinition("d", agent_id="d", depends_on=["b", "c"]),
    ))
    assert dag.levels == [["a"], ["b", "c"], ["d"]]
    assert dag.topological_order == ["a", "b", "c", "d"]


def test_cycle_detection():
    with pytest.raises(WorkflowValidationError, match="cycle"):
        build_dag(workflow(
            StepDefinition("a", agent_id="a", depends_on=["b"]),
            StepDefinition("b", agent_id="b", depends_on=["a"]),
        ))


def test_missing_dependency_and_duplicate_step_id():
    with pytest.raises(WorkflowValidationError, match="missing"):
        build_dag(workflow(StepDefinition("a", agent_id="a", depends_on=["missing"])))
    with pytest.raises(WorkflowValidationError, match="Duplicate"):
        build_dag(workflow(StepDefinition("a", agent_id="a"), StepDefinition("a", agent_id="a")))

