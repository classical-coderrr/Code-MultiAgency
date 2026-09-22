import pytest

from app.workflow.context import UndefinedWorkflowVariable, WorkflowContext


def test_context_template_variable():
    context = WorkflowContext({"requirement": "菜谱应用", "count": 3})
    assert context.render("需求：{{ requirement }} / 数量：{{count}}") == "需求：菜谱应用 / 数量：3"


def test_context_rejects_undefined_variable():
    with pytest.raises(UndefinedWorkflowVariable, match="Undefined workflow variable: missing"):
        WorkflowContext().render("{{missing}}")

