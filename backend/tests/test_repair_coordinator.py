import asyncio
from pathlib import Path

from app.code_company.failure_classifier import FailureClassifier
from app.code_company.repair_coordinator import RepairCoordinator
from app.platform.workspace import LocalWorkspaceService
from app.workflow.artifact_validator import ArtifactValidationResult, ValidationCheck


def validation(check_id: str, target: str, status: str, message: str) -> ArtifactValidationResult:
    return ArtifactValidationResult(
        status,
        message,
        (target,),
        (ValidationCheck(check_id, target, check_id, status, message),),
    )


def test_failure_classifier_separates_provider_and_source_failures():
    classifier = FailureClassifier()

    provider = classifier.classify_node("frontend", "Provider returned HTTP 401: Incorrect API key")
    transport = classifier.classify_node("frontend", "connection refused while contacting Provider")
    source = classifier.classify_check(
        ValidationCheck(
            "integration-api-contract",
            "artifact",
            "API contract",
            "failed",
            "Vue request path does not match Spring Controller",
        )
    )

    assert provider.owners == ("platform",)
    assert provider.repairable is False
    assert transport.category == "provider_transport"
    assert transport.retryable is True
    assert source.owners == ("backend", "frontend")
    assert source.stage == "integration"


def test_repair_coordinator_uses_backedge_then_target_and_full_gates():
    events: list[tuple[str, dict]] = []
    target_calls: list[int] = []
    full_calls: list[int] = []

    async def emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    async def repair(plan: dict, attempt: int, strategy: str):
        return [f"src/round-{attempt}.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        target_calls.append(attempt)
        if attempt == 1:
            return validation("backend-test", "backend", "failed", "compile progressed to tests")
        return validation("backend-test", "backend", "passed", "target gate passed")

    async def full(attempt: int) -> ArtifactValidationResult:
        full_calls.append(attempt)
        return validation("integration-api-contract", "artifact", "passed", "full regression passed")

    outcome = asyncio.run(
        RepairCoordinator().coordinate(
            validation("backend-build", "backend", "failed", "compile failed"),
            max_attempts=3,
            repair=repair,
            validate_target=target,
            validate_full=full,
            emit=emit,
        )
    )

    assert outcome.passed is True
    assert outcome.attempts == 2
    assert target_calls == [1, 2]
    assert full_calls == [2]
    assert [item["targetGate"] for item in outcome.history] == ["FAILED", "PASSED"]
    assert outcome.history[-1]["fullRegression"] == "PASSED"
    assert any(event_type == "repair.completed" and payload["passed"] for event_type, payload in events)


def test_repair_coordinator_can_follow_four_distinct_failure_stages():
    repaired: list[tuple[int, list[str]]] = []

    async def repair(plan: dict, attempt: int, strategy: str):
        repaired.append((attempt, list(plan["owners"])))
        return [f"src/repair-{attempt}.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        if attempt == 1:
            return validation("backend-test", "backend", "failed", "Java compilation failed")
        if attempt == 2:
            return validation("backend-startup", "backend", "failed", "missing table")
        return validation("backend-startup", "backend", "passed", "startup passed")

    async def full(attempt: int) -> ArtifactValidationResult:
        if attempt == 3:
            return validation("spring-h2-crud", "backend", "failed", "CRUD failed")
        return validation("spring-h2-crud", "backend", "passed", "CRUD passed")

    outcome = asyncio.run(RepairCoordinator().coordinate(
        validation("backend-table-contract", "backend", "failed", "table name mismatch"),
        max_attempts=4,
        repair=repair,
        validate_target=target,
        validate_full=full,
    ))

    assert outcome.passed is True
    assert outcome.attempts == 4
    assert repaired == [(1, ["backend"]), (2, ["backend"]), (3, ["backend"]), (4, ["backend"])]
    assert [row["targetGate"] for row in outcome.history] == ["FAILED", "FAILED", "PASSED", "PASSED"]
    assert [row["fullRegression"] for row in outcome.history] == ["PENDING", "PENDING", "FAILED", "PASSED"]


def test_progressive_repairs_keep_same_candidate_until_full_regression_passes():
    candidate = {"table": False, "class": False}
    discarded: list[int] = []
    transitions: list[str] = []

    async def emit(event_type: str, payload: dict) -> None:
        if event_type == "repair.target_gate_completed" and payload.get("stageTransition"):
            transitions.append(payload["stageTransition"])

    async def repair(plan: dict, attempt: int, strategy: str):
        if attempt == 1:
            candidate["table"] = True
            return ["Product.java"], []
        assert candidate["table"] is True  # first fix was not rolled back
        candidate["class"] = True
        return ["ProductNotFoundException.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        if not candidate["class"]:
            check = ValidationCheck("backend-test", "backend", "后端 Maven 测试", "failed", "缺少类",
                                    output="[ERROR] symbol: class ProductNotFoundException")
            return ArtifactValidationResult("failed", "缺少类", ("backend",), (check,))
        return validation("backend-test", "backend", "passed", "Maven passed")

    async def full(attempt: int) -> ArtifactValidationResult:
        assert candidate == {"table": True, "class": True}
        return validation("backend-startup", "backend", "passed", "full regression passed")

    async def reject(plan: dict, attempt: int) -> None:
        discarded.append(attempt)

    outcome = asyncio.run(RepairCoordinator().coordinate(
        validation("backend-table-contract", "backend", "failed", "table mismatch"),
        max_attempts=2, repair=repair, validate_target=target, validate_full=full,
        reject_candidate=reject, emit=emit,
    ))
    assert outcome.passed and outcome.attempts == 2
    assert discarded == []
    assert any("后端 Maven 测试" in transition for transition in transitions)


def test_full_regression_that_breaks_passing_gate_rejects_only_last_round():
    rejected: list[int] = []

    async def repair(plan: dict, attempt: int, strategy: str):
        return ["src/App.vue"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        return validation("frontend-build", "frontend", "passed", "build passed")

    async def full(attempt: int) -> ArtifactValidationResult:
        checks = (
            ValidationCheck("backend-api-contract", "backend", "后端 API 合同", "failed", "regression"),
            ValidationCheck("frontend-build", "frontend", "前端构建", "passed", "fixed"),
        )
        return ArtifactValidationResult("failed", "regression", ("backend",), checks)

    async def reject(plan: dict, attempt: int) -> None:
        rejected.append(attempt)

    original = ArtifactValidationResult("failed", "front-end failure", ("frontend",), (
        ValidationCheck("backend-api-contract", "backend", "后端 API 合同", "passed", "ok"),
        ValidationCheck("frontend-build", "frontend", "前端构建", "failed", "build failed"),
    ))
    outcome = asyncio.run(RepairCoordinator().coordinate(
        original, max_attempts=2, repair=repair, validate_target=target, validate_full=full,
        reject_candidate=reject,
    ))
    assert not outcome.passed
    assert rejected == [1, 2]
    assert outcome.validation == original


def test_passing_gate_stays_protected_across_target_only_rounds():
    rejected: list[int] = []

    async def repair(plan: dict, attempt: int, strategy: str):
        return [f"src/round-{attempt}.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        return validation("backend-build", "backend", "failed" if attempt == 1 else "passed", "build")

    async def full(attempt: int) -> ArtifactValidationResult:
        return ArtifactValidationResult("failed", "API regression", ("backend",), (
            ValidationCheck("backend-api-contract", "backend", "API", "failed", "regressed"),
            ValidationCheck("backend-build", "backend", "build", "passed", "compiled"),
        ))

    async def reject(plan: dict, attempt: int) -> None:
        rejected.append(attempt)

    initial = ArtifactValidationResult("failed", "table mismatch", ("backend",), (
        ValidationCheck("backend-table-contract", "backend", "table", "failed", "table mismatch"),
        ValidationCheck("backend-api-contract", "backend", "API", "passed", "ok"),
    ))
    outcome = asyncio.run(RepairCoordinator().coordinate(
        initial, max_attempts=2, repair=repair, validate_target=target,
        validate_full=full, reject_candidate=reject,
    ))
    assert not outcome.passed
    assert rejected == [2]
    assert outcome.history[-1]["regressedChecks"] == ["backend-api-contract"]


def test_fail_fast_missing_check_is_not_regression_but_must_pass_before_commit():
    rejected: list[int] = []

    async def repair(plan: dict, attempt: int, strategy: str):
        return [f"src/round-{attempt}.java"], []

    async def target(plan: dict, attempt: int) -> ArtifactValidationResult:
        if attempt == 1:
            return validation("backend-build", "backend", "failed", "compile error")
        return validation("backend-build" if attempt == 2 else "backend-startup", "backend", "passed", "target ok")

    async def full(attempt: int) -> ArtifactValidationResult:
        if attempt == 2:
            return validation("backend-startup", "backend", "failed", "startup failed before API check")
        return ArtifactValidationResult("passed", "all gates passed", ("backend",), (
            ValidationCheck("backend-startup", "backend", "startup", "passed", "ok"),
            ValidationCheck("backend-api-contract", "backend", "API", "passed", "ok"),
        ))

    async def reject(plan: dict, attempt: int) -> None:
        rejected.append(attempt)

    initial = ArtifactValidationResult("failed", "table mismatch", ("backend",), (
        ValidationCheck("backend-table-contract", "backend", "table", "failed", "bad"),
        ValidationCheck("backend-api-contract", "backend", "API", "passed", "ok"),
    ))
    outcome = asyncio.run(RepairCoordinator().coordinate(
        initial, max_attempts=3, repair=repair, validate_target=target,
        validate_full=full, reject_candidate=reject,
    ))
    assert outcome.passed and outcome.attempts == 3
    assert rejected == []
    assert outcome.history[1]["unverifiedChecks"] == ["backend-api-contract"]


def test_candidate_workspace_isolated_promoted_and_discarded(tmp_path: Path):
    service = LocalWorkspaceService(tmp_path / "workspaces")
    stable = service.create_for_run("run-123")
    owner_backend = service.create_for_owner(stable, "backend")
    owner_frontend = service.create_for_owner(stable, "frontend")

    assert owner_backend.worktree_path != owner_frontend.worktree_path
    assert owner_backend.worktree_path != stable.worktree_path

    candidate = service.create_candidate(stable, "repair-1", ["backend"])
    service.stage_candidate(candidate, [{"name": "src/main.txt", "content": "fixed"}])
    assert (Path(candidate.worktree_path) / "src" / "main.txt").read_text(encoding="utf-8") == "fixed"

    promoted = service.promote_candidate(candidate)
    assert promoted.status == "STABLE"
    assert (Path(stable.worktree_path) / "src" / "main.txt").read_text(encoding="utf-8") == "fixed"

    discarded = service.discard_candidate(candidate)
    assert discarded.status == "REJECTED"
    assert not Path(candidate.worktree_path).exists()
