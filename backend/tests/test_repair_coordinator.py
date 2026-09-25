import asyncio
import hashlib
import json
from pathlib import Path

from app.code_company.coding_loop import CodingAgentLoop, CodingLoopConfig
from app.code_company.failure_classifier import FailureClassifier
from app.code_company.repair_coordinator import RepairCoordinator
from app.llm.base import LLMProvider, LLMResponse
from app.platform.tool_gateway import LocalToolGateway, ToolPolicy
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
    assert outcome.history[1]["resolvedFailureKeys"] == ["missing-class:ProductNotFoundException"]


def test_full_regression_new_backend_failure_reassigns_after_frontend_gate_passes():
    candidate = {"frontend": False, "backend": False}
    rejected: list[int] = []
    routes: list[list[str]] = []

    async def repair(plan: dict, attempt: int, _strategy: str):
        routes.append(list(plan["owners"]))
        if attempt == 1:
            assert plan["owners"] == ["frontend"]
            candidate["frontend"] = True
            return ["src/components/StudentManager.vue"], []
        assert plan["owners"] == ["backend"]
        assert candidate["frontend"] is True  # passed frontend work was retained
        candidate["backend"] = True
        return ["src/main/java/com/example/app/StudentService.java"], []

    async def target(plan: dict, _attempt: int) -> ArtifactValidationResult:
        owner = plan["owners"][0]
        gate = "frontend-ui-hooks" if owner == "frontend" else "backend-test"
        return validation(gate, owner, "passed", "target fixed")

    async def full(_attempt: int) -> ArtifactValidationResult:
        checks = [
            ValidationCheck("backend-api-contract", "backend", "API contract", "passed", "still valid"),
            ValidationCheck("frontend-ui-hooks", "frontend", "UI hooks", "passed", "fixed"),
            ValidationCheck("backend-test", "backend", "Maven", "passed" if candidate["backend"] else "failed",
                            "compiled" if candidate["backend"] else "setId method missing"),
        ]
        status = "passed" if candidate["backend"] else "failed"
        return ArtifactValidationResult(status, status, ("backend", "frontend"), tuple(checks))

    async def reject(_plan: dict, attempt: int) -> None:
        rejected.append(attempt)

    initial = ArtifactValidationResult("failed", "UI hooks missing", ("frontend",), (
        ValidationCheck("backend-api-contract", "backend", "API contract", "passed", "valid"),
        ValidationCheck("frontend-ui-hooks", "frontend", "UI hooks", "failed", "missing panels"),
    ))
    outcome = asyncio.run(RepairCoordinator().coordinate(
        initial, max_attempts=2, repair=repair, validate_target=target,
        validate_full=full, reject_candidate=reject,
    ))
    assert outcome.passed
    assert routes == [["frontend"], ["backend"]]
    assert rejected == []
    assert outcome.history[0]["fullRegression"] == "FAILED"
    assert outcome.history[0]["madeProgress"] is True
    assert outcome.history[1]["fullRegression"] == "PASSED"


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


def test_coding_tool_repair_runs_inside_candidate_then_target_and_full_gates(tmp_path: Path):
    service = LocalWorkspaceService(tmp_path / "workspaces")
    stable = service.create_for_run("run-tool-repair")
    original = 'class App { String value = "broken"; }'
    stable_file = Path(stable.worktree_path) / "src" / "App.java"
    stable_file.parent.mkdir(parents=True)
    stable_file.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    events: list[str] = []
    candidate_refs = []

    class RepairProvider(LLMProvider):
        def __init__(self):
            self.outputs = [
                {"type": "tool_call", "tool": "repo.read", "arguments": {"path": "src/App.java"}},
                {"type": "tool_call", "tool": "fs.patch", "arguments": {
                    "path": "src/App.java", "old_text": "broken", "new_text": "fixed",
                    "expected_sha256": digest,
                }},
                {"type": "final", "content": "修复了编译器指出的字段值。"},
            ]

        async def generate(self, system_prompt, user_prompt, config=None):
            return LLMResponse(
                text=json.dumps(self.outputs.pop(0), ensure_ascii=False),
                input_tokens=5,
                output_tokens=10,
                finish_reason="stop",
            )

    async def emit(event_type: str, payload: dict):
        events.append(event_type)

    async def prepare_candidate(plan: dict, attempt: int):
        candidate = service.create_candidate(stable, f"repair-{attempt}", plan["owners"])
        service.stage_candidate(candidate, [{"name": "src/App.java", "content": original}])
        candidate_refs.append(candidate)

    async def repair(plan: dict, attempt: int, strategy: str):
        candidate = candidate_refs[-1]
        gateway = LocalToolGateway(
            service,
            ToolPolicy.coding_loop(mutable_path_globs=("src/**",)),
        )
        result = await CodingAgentLoop(RepairProvider(), gateway).run(
            candidate.as_dict(),
            "依据编译器证据修复 src/App.java 中的 broken 字段值。",
            config=CodingLoopConfig(
                max_iterations=4,
                max_tool_actions=2,
                max_tokens=100,
                required_artifacts=("src/App.java",),
            ),
            emit=emit,
        )
        assert result.verified is False  # Coding Agent cannot self-approve its repair.
        assert [tool.tool for tool in result.tool_results] == ["repo.read", "fs.patch"]
        assert stable_file.read_text(encoding="utf-8") == original
        return ["src/App.java"], result.responses

    async def validate_target(plan: dict, attempt: int) -> ArtifactValidationResult:
        candidate_file = Path(candidate_refs[-1].worktree_path) / "src" / "App.java"
        assert "fixed" in candidate_file.read_text(encoding="utf-8")
        return validation("backend-compile", "backend", "passed", "目标编译 Gate 通过")

    async def validate_full(attempt: int) -> ArtifactValidationResult:
        candidate_file = Path(candidate_refs[-1].worktree_path) / "src" / "App.java"
        assert "fixed" in candidate_file.read_text(encoding="utf-8")
        assert stable_file.read_text(encoding="utf-8") == original
        return validation("integration-api-contract", "backend", "passed", "完整回归通过")

    async def commit_candidate(plan: dict, attempt: int):
        service.promote_candidate(candidate_refs[-1])

    outcome = asyncio.run(RepairCoordinator().coordinate(
        validation("backend-compile", "backend", "failed", "编译失败：App.java 字段值错误"),
        max_attempts=2,
        repair=repair,
        validate_target=validate_target,
        validate_full=validate_full,
        prepare_candidate=prepare_candidate,
        commit_candidate=commit_candidate,
        emit=emit,
    ))

    assert outcome.passed is True
    assert outcome.attempts == 1
    assert stable_file.read_text(encoding="utf-8") == 'class App { String value = "fixed"; }'
    assert events.index("coding_loop.action_completed") < events.index("repair.target_gate_completed")
    assert events.index("repair.target_gate_completed") < events.index("repair.full_regression_completed")
