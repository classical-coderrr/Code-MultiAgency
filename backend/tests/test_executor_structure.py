"""Architecture guards for the workflow execution facade."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


EXECUTOR = Path(__file__).resolve().parents[1] / "app" / "workflow" / "executor.py"


def _executor_class() -> ast.ClassDef:
    module = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "WorkflowExecutor"
    )


def test_workflow_executor_has_no_shadowed_methods() -> None:
    methods = [
        node.name
        for node in _executor_class().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    duplicates = {name: count for name, count in Counter(methods).items() if count > 1}
    assert duplicates == {}


def test_workflow_executor_does_not_own_run_state_model() -> None:
    module = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    classes = {
        node.name for node in module.body if isinstance(node, ast.ClassDef)
    }
    assert "RunState" not in classes


def test_workflow_executor_does_not_call_provider_generate_directly() -> None:
    executor = _executor_class()
    direct_calls = []
    for node in ast.walk(executor):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            node.func.attr == "generate"
            and isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "provider"
        ):
            direct_calls.append(node.lineno)
    assert direct_calls == []


def test_executor_file_cannot_silently_grow_back_to_previous_size() -> None:
    # This is a ratchet, not the final target. The facade should continue to
    # shrink as the step and repair pipelines move behind dedicated services.
    assert len(EXECUTOR.read_text(encoding="utf-8").splitlines()) <= 3200
