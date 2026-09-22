import asyncio
import json

from app.code_company.repair_coordinator import RepairCoordinator
from app.llm.base import LLMResponse
from app.repositories.sqlite import SQLiteRepository
from app.workflow.artifact_validator import ArtifactValidationResult, ValidationCheck
from app.workflow.collaboration import CollaborationCoordinator


def _failed_validation() -> ArtifactValidationResult:
    return ArtifactValidationResult(
        "failed", "route mismatch", ("backend", "frontend"),
        (ValidationCheck(
            "integration-api-contract", "artifact", "API contract", "failed",
            "Vue path differs from Spring Controller",
        ),),
    )


def test_two_round_consultation_is_durable_and_idempotent():
    repository = SQLiteRepository(":memory:")
    coordinator = CollaborationCoordinator(repository)
    calls: list[tuple[str, str]] = []
    events: list[str] = []

    async def generate(owner: str, prompt: str) -> LLMResponse:
        calls.append((owner, prompt))
        if "其他 Agent 的提议" in prompt:
            return LLMResponse(json.dumps({
                "agree": True, "action": f"Review {owner} endpoint", "reason": "contract aligned",
                "needs_contract_change": False,
            }), input_tokens=30, output_tokens=20)
        return LLMResponse(json.dumps({
            "diagnosis": "route mismatch", "action": f"Fix {owner} route",
            "needs_contract_change": False,
        }), input_tokens=30, output_tokens=20)

    async def emit(event_type: str, payload: dict) -> None:
        events.append(event_type)

    kwargs = dict(
        run_id="run-a", fingerprint="fingerprint-1", owners=["backend", "frontend"],
        evidence=[{"id": "api", "status": "failed", "message": "route mismatch"}],
        role_contracts={"backend": {"api": "/rooms"}, "frontend": {"api": "/rooms"}},
        generate=generate, emit=emit,
    )
    first = asyncio.run(coordinator.consult(**kwargs))
    assert first.status == "consensus"
    assert len(first.responses) == 4
    assert "Fix backend route" in first.guidance["backend"]
    assert "Fix frontend route" in first.guidance["frontend"]
    assert len(repository.list_collaboration_messages("run-a")) == 9
    assert "collaboration.completed" in events

    second = asyncio.run(coordinator.consult(**kwargs))
    assert second.status == "consensus"
    assert second.guidance == first.guidance
    assert len(calls) == 4
    assert len(repository.list_collaboration_messages("run-a")) == 9


def test_contract_change_proposal_never_becomes_repair_guidance():
    repository = SQLiteRepository(":memory:")
    coordinator = CollaborationCoordinator(repository)

    async def generate(owner: str, prompt: str) -> LLMResponse:
        return LLMResponse(json.dumps({
            "diagnosis": "contract conflict", "action": "change the frozen API",
            "needs_contract_change": True,
        }))

    async def emit(event_type: str, payload: dict) -> None:
        pass

    result = asyncio.run(coordinator.consult(
        run_id="run-b", fingerprint="fingerprint-2", owners=["backend", "frontend"],
        evidence=[{"id": "api", "status": "failed", "message": "contract conflict"}],
        role_contracts={}, generate=generate, emit=emit,
    ))
    assert result.status == "contract_change_requested"
    assert result.guidance == {}
    decisions = [message for message in repository.list_collaboration_messages("run-b") if message["act"] == "decision"]
    assert decisions[0]["payload"]["contract_change_requested"] is True


def test_mailbox_survives_repository_restart_and_is_run_scoped(tmp_path):
    database_path = tmp_path / "runs.db"
    first = SQLiteRepository(database_path)
    first.append_collaboration_message(
        message_id="message-a", run_id="run-a", conversation_id="thread-a",
        sender="backend", recipient="frontend", act="propose", round_number=1,
        payload={"action": "align path"},
    )
    first.append_collaboration_message(
        message_id="message-a", run_id="run-a", conversation_id="thread-a",
        sender="backend", recipient="frontend", act="propose", round_number=1,
        payload={"action": "duplicate must be ignored"},
    )
    second = SQLiteRepository(database_path)
    assert len(second.list_collaboration_messages("run-a")) == 1
    assert second.list_collaboration_messages("run-a")[0]["payload"]["action"] == "align path"
    assert second.list_collaboration_messages("run-b") == []


def test_consultation_timeout_cancels_provider_and_falls_back():
    repository = SQLiteRepository(":memory:")
    cancelled: list[str] = []

    async def generate(owner: str, prompt: str) -> LLMResponse:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(owner)
            raise
        raise AssertionError("provider should have been cancelled")

    async def emit(event_type: str, payload: dict) -> None:
        pass

    result = asyncio.run(CollaborationCoordinator(repository).consult(
        run_id="run-timeout", fingerprint="fp", owners=["backend", "frontend"],
        evidence=[{"id": "api", "status": "failed", "message": "route mismatch"}],
        role_contracts={}, generate=generate, emit=emit, timeout_seconds=1,
    ))
    assert result.status == "incomplete"
    assert result.guidance == {}
    assert set(cancelled) == {"backend", "frontend"}


def test_empty_model_reply_cannot_be_counted_as_consensus():
    repository = SQLiteRepository(":memory:")

    async def generate(owner: str, prompt: str) -> LLMResponse:
        return LLMResponse("{}")

    async def emit(event_type: str, payload: dict) -> None:
        pass

    result = asyncio.run(CollaborationCoordinator(repository).consult(
        run_id="run-empty", fingerprint="fp", owners=["backend", "frontend"],
        evidence=[{"id": "api", "status": "failed", "message": "route mismatch"}],
        role_contracts={}, generate=generate, emit=emit,
    ))
    assert result.status == "incomplete"
    assert result.guidance == {}
    assert not any(message["act"] == "propose" for message in repository.list_collaboration_messages("run-empty"))


def test_repair_coordinator_consults_once_and_still_requires_gate():
    consulted: list[int] = []
    target_calls: list[int] = []

    async def consult(plan: dict, validation: ArtifactValidationResult, attempt: int) -> dict:
        consulted.append(attempt)
        assert set(plan["owners"]) == {"backend", "frontend"}
        return {"status": "consensus", "guidance": {"backend": "align route"}}

    async def repair(plan: dict, attempt: int, strategy: str):
        assert plan["consultation"]["status"] == "consensus"
        return ["src/main/java/RoomController.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        target_calls.append(attempt)
        return _failed_validation()

    async def full(attempt: int) -> ArtifactValidationResult:
        raise AssertionError("full Gate must not run when the target Gate fails")

    outcome = asyncio.run(RepairCoordinator().coordinate(
        _failed_validation(), max_attempts=1, repair=repair,
        validate_target=target, validate_full=full, consult=consult,
    ))
    assert consulted == [1]
    assert target_calls == [1]
    assert outcome.passed is False
