"""LangGraph adapter for the platform workflow contract.

The platform keeps its own domain models and event bus. This module owns the
translation to LangGraph StateGraph so the UI/API do not need to know about
LangGraph internals.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .models import StepDefinition, StepType, WorkflowDefinition


def _merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Merge parallel node updates without losing sibling outputs.

    LangGraph invokes sibling nodes with the same state snapshot.  Most
    context values are single-writer values and a last-write-wins merge is
    correct for them, but artifact generation is intentionally multi-writer:
    Backend and Frontend each append files to ``__artifact_files__``.  Treating
    that list like a scalar caused the second parallel branch to erase the
    first branch's files at the checkpoint merge boundary.
    """
    merged = {**(left or {}), **(right or {})}
    left_files = (left or {}).get("__artifact_files__")
    right_files = (right or {}).get("__artifact_files__")
    revisions: dict[str, int] = {}
    for context in (left or {}, right or {}):
        for owner, revision in (context.get("__artifact_owner_revisions__") or {}).items():
            revisions[owner] = max(revisions.get(owner, 0), int(revision))
    if revisions:
        merged["__artifact_owner_revisions__"] = revisions
    if isinstance(left_files, list) or isinstance(right_files, list):
        merged["__artifact_files__"] = _merge_artifact_files(left_files, right_files, revisions)
    return merged


def _merge_artifact_files(left: Any, right: Any, revisions: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Merge same-owner revisions but preserve cross-owner conflicts for the Gate.

    Raising inside a LangGraph reducer would bypass the repair coordinator.
    Keeping conflicting rows lets the deterministic Artifact Gate report the
    collision as structured evidence and route it to the platform/owner.
    """
    result: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for value in [*(left if isinstance(left, list) else []), *(right if isinstance(right, list) else [])]:
        if not isinstance(value, dict):
            continue
        item = dict(value)
        owner = str(item.get("step_id") or item.get("owner_step") or "").strip().casefold()
        path = str(item.get("name") or item.get("path") or "").replace("\\", "/").strip("/")
        if not path:
            continue
        identity = (path.casefold(), owner)
        if int(item.get("owner_revision", 0)) < (revisions or {}).get(owner, 0):
            continue
        if identity in positions:
            position = positions[identity]
            previous = result[position]
            if int(item.get("owner_revision", 0)) >= int(previous.get("owner_revision", 0)):
                result[position] = item
        else:
            positions[identity] = len(result)
            result.append(item)
    return result


class LangGraphState(TypedDict, total=False):
    run_id: str
    # Used only by the repository-backed restore fallback when the original
    # checkpointer is not available in the new process.
    resume_decision: str
    resume_payload: Any
    resume_step_id: str
    context: Annotated[dict[str, Any], _merge_dicts]
    results: Annotated[dict[str, str], _merge_dicts]


StepHandler = Callable[[StepDefinition, LangGraphState], Awaitable[dict[str, Any]]]


class LangGraphWorkflow:
    """Compile a platform WorkflowDefinition into a checkpointed StateGraph."""

    def __init__(
        self,
        workflow: WorkflowDefinition,
        step_handler: StepHandler,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.workflow = workflow
        self.step_handler = step_handler
        self.checkpointer = checkpointer
        self.graph = self._compile()

    def _compile(self):
        builder = StateGraph(LangGraphState)
        for step in self.workflow.steps:
            builder.add_node(step.id, self._node_for(step))

        for step in self.workflow.steps:
            if not step.depends_on:
                builder.add_edge(START, step.id)
            elif len(step.depends_on) == 1:
                builder.add_edge(step.depends_on[0], step.id)
            else:
                # LangGraph treats separate ``add_edge(parent, child)`` calls
                # as alternative triggers.  A workflow dependency list is an
                # AND condition, so use the list form to create a true fan-in
                # barrier.  Without this, a node such as Backend with
                # ``[approval, database]`` could start after approval alone,
                # observe database as PENDING, and be incorrectly skipped.
                builder.add_edge(list(step.depends_on), step.id)

        targets = {dependency for step in self.workflow.steps for dependency in step.depends_on}
        for step in self.workflow.steps:
            if step.id not in targets:
                builder.add_edge(step.id, END)

        return builder.compile(checkpointer=self.checkpointer)

    def _node_for(self, step: StepDefinition):
        async def node(state: LangGraphState) -> dict[str, Any]:
            return await self.step_handler(step, state)

        node.__name__ = f"agent_node_{step.id}"
        return node

    async def ainvoke(self, input_state: LangGraphState | Any, *, run_id: str) -> dict[str, Any]:
        """Invoke the graph with a stable thread id for checkpoint recovery."""
        return await self.graph.ainvoke(input_state, config={"configurable": {"thread_id": run_id}})

    async def resume(self, decision: Any, *, run_id: str) -> dict[str, Any]:
        """Resume an interrupted approval/clarification with the same checkpoint."""
        from langgraph.types import Command

        return await self.graph.ainvoke(Command(resume=decision), config={"configurable": {"thread_id": run_id}})

    @staticmethod
    def is_interrupted(result: dict[str, Any] | None) -> bool:
        return bool(result and result.get("__interrupt__"))
