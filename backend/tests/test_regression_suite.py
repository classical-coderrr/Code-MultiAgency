"""Regression harness tests: never construct or run a live model."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import regression_suite as suite


def event(kind, payload, ident):
    return {"type": kind, "payload": payload, "id": ident}


def html_run():
    return {"id": "test-run", "status": "SUCCESS", "steps": [], "state": {"context": {
        "__artifact_files__": [{"name": "index.html", "content": "<!doctype html><html></html>"}],
        "artifact_validation": {"status": "passed", "checks": [{"id": "static-html", "status": "passed"}]},
    }}}


def test_default_cases_cover_requested_stacks_and_fresh_requirements():
    cases = suite.load_cases()
    assert {case.expected_stack for case in cases} == suite.STACKS
    assert len({case.id for case in cases}) == 3
    assert all("源码" in case.requirement for case in cases)
    spring = [case for case in cases if case.expected_stack.startswith("springboot")]
    assert all("增删改查" in case.requirement and "H2" in case.requirement for case in spring)
    assert "localStorage" in cases[2].requirement and "无需后端" in cases[2].requirement


def test_custom_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"id": "custom", "requirement": "生成静态 HTML 源码", "expected_stack": "static_html"}]), encoding="utf-8")
    assert suite.load_cases(path) == (suite.Case("custom", "生成静态 HTML 源码", "static_html"),)


def test_case_id_limits_targeted_regression_without_model_call(tmp_path, capsys):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([
        {"id": "l2", "requirement": "后端 API 与 HTML 页面", "expected_stack": "springboot_html"},
        {"id": "l3", "requirement": "Vue + Spring Boot + H2 CRUD", "expected_stack": "springboot_vue"},
    ]), encoding="utf-8")

    assert suite.main(["--cases", str(path), "--case-id", "l3"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [case["id"] for case in plan["cases"]] == ["l3"]


@pytest.mark.parametrize("raw", [[], {}, [None], [{"id": "../escape"}],
    [{"id": "ok", "requirement": " ", "expected_stack": "static_html"}],
    [{"id": "ok", "requirement": "build", "expected_stack": "other"}],
    [{"id": "ok", "requirement": "build", "expected_stack": []}],
    [{"id": "same", "requirement": "build", "expected_stack": "static_html"}] * 2])
def test_invalid_cases_fail_before_work(raw, tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        suite.load_cases(path)


def test_tokens_include_failed_attempts_without_double_counting_rows_or_replayed_events():
    failed = event("step.failed", {"stepId": "tester", "tokens": {"input": 100, "output": 25}}, 1)
    completed = event("step.completed", {"stepId": "tester", "tokens": {"input": 40, "output": 10}}, 2)
    run = {"steps": [{"step_id": "tester", "input_tokens": 40, "output_tokens": 10},
                     {"step_id": "frontend", "input_tokens": 20, "output_tokens": 30}]}
    result = suite.token_metrics(run, [failed, completed, failed])
    assert result["input_tokens"] == 160
    assert result["output_tokens"] == 65
    assert result["total_tokens"] == 225


def test_repository_token_totals_take_precedence_and_unknown_is_unavailable():
    usage = suite.token_metrics({"token_usage": {"input_tokens": 140, "output_tokens": 35}},
                               [event("step.completed", {"stepId": "tester", "tokens": {"input": 40}}, 1)])
    assert usage["total_tokens"] == 175
    assert usage["source"] == "repository_attempt_totals"
    assert suite.token_metrics({}, [])["available"] is False
    assert suite.token_metrics({"token_usage": {"input_tokens": -1, "output_tokens": "bad"}}, [])["total_tokens"] == 0


def test_repairs_count_rounds_once_and_generation_repairs_separately():
    events = [event("step.validation_repairing", {"stepId": "tester", "repairAttempt": 1, "fileName": "a"}, 1),
              event("step.validation_repairing", {"stepId": "tester", "repairAttempt": 1, "fileName": "b"}, 2),
              event("step.validation_repaired", {"stepId": "tester", "repairAttempt": 1}, 3),
              event("step.validation_repair_failed", {"stepId": "tester", "repairAttempt": 2}, 4),
              event("step.completed", {"runtime": {"artifactRepairCount": 2}}, 5)]
    assert suite.repair_metrics(events + events) == 4
    events += [event("workflow.retrying", {}, 6),
               event("step.validation_repairing", {"stepId": "tester", "repairAttempt": 1}, 7)]
    assert suite.repair_metrics(events) == 5


def test_failed_generation_repairs_survive_recovery_without_summary_double_count():
    events = [event("step.started", {"stepId": "frontend"}, 1),
              event("step.artifact_repairing", {"stepId": "frontend", "fileName": "index.html", "repairAttempt": 1}, 2),
              event("step.failed", {"stepId": "frontend", "retryCount": 0}, 3),
              event("workflow.retrying", {"recoveryCount": 1}, 4),
              event("workflow.recovered", {"recoveryCount": 1}, 5),
              event("step.started", {"stepId": "frontend"}, 6),
              event("step.artifact_repairing", {"stepId": "frontend", "fileName": "App.vue", "partId": "script", "repairAttempt": 1}, 7),
              event("step.artifact_repairing", {"stepId": "frontend", "fileName": "App.vue", "partId": "template", "repairAttempt": 2}, 8),
              event("step.completed", {"stepId": "frontend", "retryCount": 2,
                                        "runtime": {"generationMode": "artifacts", "artifactRepairCount": 2}}, 9)]
    run = {"recovery_count": 1, "steps": [{"step_id": "frontend", "agent_id": "frontend_agent", "retry_count": 2}]}
    result = suite.attempt_metrics(run, events + events)
    assert result["generation_repair_count"] == 3 and result["repair_count"] == 3
    assert result["retry_count"] == 0 and result["recovery_count"] == 1
    assert result["attempt_metrics_available"] is True


def test_generation_repair_start_counts_even_without_terminal_event_on_timeout():
    events = [event("step.started", {"stepId": "frontend"}, 1),
              event("step.artifact_repairing", {"stepId": "frontend", "fileName": "index.html", "repairAttempt": 1}, 2)]
    result = suite.attempt_metrics({"steps": [{"step_id": "frontend", "agent_id": "frontend_agent"}]}, events)
    assert result["repair_count"] == 1
    assert result["attempt_metrics_available"] is False


def test_validation_skips_are_not_repairs_and_actual_rounds_count_once():
    events = [event("step.validation_repair_skipped", {"stepId": "tester", "repairAttempt": 1}, 1),
              event("step.validation_owner_reexecuting", {"stepId": "tester", "repairAttempt": 2}, 2),
              event("step.validation_repairing", {"stepId": "tester", "repairAttempt": 2, "fileName": "a.java"}, 3),
              event("step.validation_repairing", {"stepId": "tester", "repairAttempt": 2, "fileName": "b.java"}, 4),
              event("step.validation_repair_failed", {"stepId": "tester", "repairAttempt": 2}, 5),
              event("step.validation_repaired", {"stepId": "tester", "repairAttempt": 2}, 6)]
    result = suite.attempt_metrics({}, events)
    assert result["validation_repair_count"] == 1 and result["generation_repair_count"] == 0
    assert suite.repair_metrics(events[:1]) == 0


def test_retry_ledger_accumulates_old_attempts_and_separates_continuation():
    events = [event("step.started", {"stepId": "architecture"}, 1),
              event("step.retrying", {"stepId": "architecture", "retryCount": 1, "automaticRecovery": True}, 2),
              event("step.continuing", {"stepId": "architecture", "retryCount": 2}, 3),
              event("step.failed", {"stepId": "architecture", "retryCount": 2}, 4),
              event("workflow.recovered", {"recoveryCount": 1}, 5),
              event("step.started", {"stepId": "architecture"}, 6),
              event("step.retrying", {"stepId": "architecture", "retryCount": 1}, 7),
              event("step.completed", {"stepId": "architecture", "retryCount": 1}, 8)]
    run = {"steps": [{"step_id": "architecture", "agent_id": "architect_agent", "retry_count": 1}]}
    result = suite.attempt_metrics(run, events + events)
    assert result["retry_count"] == 2  # Final row alone would report only one.
    assert result["continuation_count"] == 1 and result["recovery_count"] == 1
    assert result["repair_count"] == 0


def test_terminal_retry_counters_accumulate_when_start_events_are_missing():
    events = [event("step.failed", {"stepId": "architecture", "retryCount": 2}, 1),
              event("step.completed", {"stepId": "architecture", "retryCount": 1}, 2)]
    run = {"steps": [{"step_id": "architecture", "retry_count": 1}]}
    assert suite.attempt_metrics(run, events)["retry_count"] == 3


def test_legacy_row_only_counts_remain_uncertain_instead_of_claiming_first_pass():
    result = suite.attempt_metrics({"steps": [{"step_id": "frontend", "retry_count": 2}]}, [])
    assert result["retry_count"] == 2 and result["retry_count_ambiguous"] is True
    assert result["attempt_metrics_available"] is False
    assert suite.measure(suite.DEFAULT_CASES[2], html_run(), [], 0)["first_pass"] is None


@pytest.mark.parametrize("correction", ["generation", "validation", "retry", "continuation", "fallback", "recovery"])
def test_first_pass_excludes_every_corrective_path_even_if_deliverable(correction):
    payload = {"stepId": "frontend"}
    kind = {"generation": "step.artifact_repairing", "validation": "step.validation_repairing",
            "retry": "step.retrying", "continuation": "step.artifact_continuing",
            "fallback": "step.completed", "recovery": "workflow.recovered"}[correction]
    payload.update(repairAttempt=1, continuationAttempt=1, retryCount=0)
    if correction == "fallback":
        payload["fallback"] = True
    events = [event(kind, payload, 1), event("step.completed", {"stepId": "frontend", "retryCount": 0}, 2)]
    run = html_run()
    run["steps"] = [{"step_id": "frontend", "agent_id": "frontend_agent", "retry_count": 0}]
    measured = suite.measure(suite.DEFAULT_CASES[2], run, events, 0)
    assert measured["deliverable"] is True and measured["first_pass"] is False


def test_first_pass_rates_use_all_runs_and_do_not_trust_stale_flags():
    run = html_run()
    run["steps"] = [{"step_id": "frontend", "agent_id": "frontend_agent", "retry_count": 0}]
    events = [event("step.completed", {"stepId": "frontend", "retryCount": 0}, 1)]
    clean = suite.measure(suite.DEFAULT_CASES[2], run, events, 0)
    assert clean["first_pass"] is True
    repaired = suite.measure(suite.DEFAULT_CASES[2], run,
                             [event("step.artifact_repairing", {"stepId": "frontend"}, 2)] + events, 0)
    unknown = suite.measure(suite.DEFAULT_CASES[2], html_run(), [], 0)
    failed = suite.measure(suite.DEFAULT_CASES[2], run, events, 0, outcome="TIMEOUT")
    repaired["first_pass"] = True  # Aggregate must also check actual counters.
    failed["first_pass"] = True  # A stale worker field cannot override cancellation.
    totals = suite.aggregate([clean, repaired, unknown, failed])
    assert totals["first_passes"] == 1 and totals["first_pass_rate"] == 0.25
    assert totals["deliverable_rate"] == 0.75 and totals["success_rate"] == 0.75
    assert totals["first_pass_unknown_runs"] == 1
    assert totals["by_case"]["static-html"]["first_pass_rate"] == 0.25
    assert totals["repair_count"] == 1
    assert suite.aggregate([])["first_pass_rate"] == 0


def test_markdown_report_preserves_fractional_rates_and_durations():
    markdown = suite.render_markdown_report({
        "mode": "live",
        "workflow_sha256": "abc",
        "summary": {
            "runs": 2, "successes": 1, "deliverables": 1,
            "deliverable_rate": 0.5, "first_passes": 1, "first_pass_rate": 0.5,
            "duration_seconds": 12.345, "total_tokens": 900,
        },
        "results": [{
            "case_id": "sample", "difficulty": "L1", "status": "SUCCESS",
            "deliverable": True, "first_pass": True, "duration_seconds": 12.345,
            "tokens": {"total_tokens": 900},
        }],
    })

    assert "50.0%" in markdown
    assert "12.3 秒" in markdown
    assert "12.3s" in markdown


def test_aggregate_sums_corrective_counts_for_failed_and_successful_runs():
    rows = [{"case_id": "html", "success": True, "deliverable": True, "attempt_metrics_available": True,
             "repair_count": 3, "generation_repair_count": 2, "validation_repair_count": 1,
             "retry_count": 2, "continuation_count": 1, "fallback_count": 1, "recovery_count": 1},
            {"case_id": "vue", "success": False, "deliverable": False, "repair_count": 2,
             "generation_repair_count": 1, "validation_repair_count": 1,
             "retry_count": 3, "continuation_count": 2, "fallback_count": 0, "recovery_count": 2}]
    totals = suite.aggregate(rows)
    assert totals["repair_count"] == 5 and totals["retry_count"] == 5
    assert totals["generation_repair_count"] == 3 and totals["validation_repair_count"] == 2
    assert totals["continuation_count"] == 3 and totals["fallback_count"] == 1 and totals["recovery_count"] == 3
    assert totals["first_pass_rate"] == 0


def test_adaptive_skipped_agents_are_complete_evidence_for_first_pass():
    run = html_run()
    run["steps"] = [{"step_id": "database", "agent_id": "database_agent", "status": "SKIPPED", "retry_count": 0},
                    {"step_id": "frontend", "agent_id": "frontend_agent", "status": "SUCCESS", "retry_count": 0}]
    events = [event("step.skipped", {"stepId": "database"}, 1),
              event("step.completed", {"stepId": "frontend", "retryCount": 0}, 2)]
    result = suite.measure(suite.DEFAULT_CASES[2], run, events, 0)
    assert result["attempt_metrics_available"] is True and result["first_pass"] is True


@pytest.mark.parametrize("gate", [None, {}, {"status": "passed", "checks": []},
    {"status": "passed", "checks": [{"status": "failed"}]},
    {"status": "passed", "checks": [{"status": "blocked"}]},
    {"status": "passed", "checks": [{"status": "passed"}], "deliverable": False}])
def test_success_is_not_delivery_without_real_gate_evidence(gate):
    run = html_run()
    run["state"]["context"]["artifact_validation"] = gate
    result = suite.measure(suite.DEFAULT_CASES[2], run, [], 1.2349)
    assert result["success"] is True
    assert result["deliverable"] is False
    assert result["failure_summary"]
    assert result["duration_seconds"] == 1.235


def test_delivery_contract_requires_its_own_gate_and_blocks_legacy_fallback():
    run = html_run()
    contract = {"schema_version": "1.0"}
    run["state"]["context"]["delivery_contract"] = contract
    result = suite.measure(suite.DEFAULT_CASES[2], run, [], 0)
    assert result["delivery_contract"] == contract
    assert result["deliverable"] is False
    run["delivery_gate"] = {"status": "blocked", "checks": [{"status": "blocked"}], "missing": ["browser-page"]}
    result = suite.measure(suite.DEFAULT_CASES[2], run, [], 0)
    assert "browser-page" in result["failure_summary"]
    run["delivery_gate"] = {"status": "passed", "deliverable": True, "checks": [{"status": "passed"}]}
    assert suite.measure(suite.DEFAULT_CASES[2], run, [], 0)["deliverable"] is True


def test_stack_evidence_and_timeout_override_executor_success():
    run = html_run()
    assert suite.measure(suite.DEFAULT_CASES[2], run, [], 1)["deliverable"] is True
    assert suite.measure(suite.DEFAULT_CASES[0], run, [], 1)["deliverable"] is False
    run["artifacts"] = [{"name": "backend/pom.xml"}, {"name": "backend/src/main/java/App.java"}]
    assert suite.stack_matches(suite.DEFAULT_CASES[0], run)
    assert not suite.stack_matches(suite.DEFAULT_CASES[2], run)
    run["artifacts"] += [{"name": "frontend/package.json"}, {"name": "frontend/src/App.vue"}]
    assert suite.stack_matches(suite.DEFAULT_CASES[1], run)
    assert not suite.stack_matches(suite.DEFAULT_CASES[0], run)
    result = suite.measure(suite.DEFAULT_CASES[1], run, [], 2, outcome="TIMEOUT", error="deadline")
    assert result["success"] is False and result["deliverable"] is False
    assert result["executor_status"] == "SUCCESS" and result["failure_summary"] == "deadline"


def test_aggregate_rates_include_every_failed_run_and_separate_delivery():
    results = [{"case_id": "html", "success": True, "deliverable": True, "duration_seconds": 2,
                "tokens": {"total_tokens": 100}, "repair_count": 1},
               {"case_id": "html", "success": True, "deliverable": False},
               {"case_id": "vue", "success": False, "deliverable": False}]
    totals = suite.aggregate(results)
    assert totals["runs"] == 3 and totals["success_rate"] == 2 / 3
    assert totals["deliverable_rate"] == 1 / 3
    assert totals["by_case"]["html"]["success_rate"] == 1
    assert totals["by_case"]["vue"]["deliverable_rate"] == 0
    assert totals["total_tokens"] == 100 and totals["repair_count"] == 1
    assert suite.aggregate([])["success_rate"] == 0


class FakeRepository:
    def __init__(self):
        self.run = {"status": "PENDING", "state": {}}

    def get_run(self, _):
        return self.run


class FakeExecutor:
    def __init__(self, repository, *, waiting=False, failing=False, retryable=False, hanging=False):
        self.repository = repository
        self.waiting, self.failing, self.retryable, self.hanging = waiting, failing, retryable, hanging
        self.approvals = self.retries = self.stops = 0
        self.cancelled = False

    async def start(self, *_):
        if self.hanging:
            self.repository.run["status"] = "RUNNING"
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        else:
            self.repository.run.update(status="WAITING_APPROVAL" if self.waiting else "FAILED" if self.failing else "SUCCESS",
                                       retryable=self.retryable, state={"waiting_step_id": "architecture_approval"})

    async def approve(self, *_):
        self.approvals += 1
        await asyncio.sleep(0)
        self.repository.run["status"] = "SUCCESS"

    async def retry(self, *_):
        self.retries += 1
        self.repository.run["status"] = "FAILED"

    async def stop(self, *_):
        self.stops += 1


@pytest.mark.asyncio
async def test_total_timeout_includes_manual_approval_wait_and_cancels_task():
    repository = FakeRepository()
    executor = FakeExecutor(repository, waiting=True)
    start = time.monotonic()
    result = await suite.drive_run(executor, repository, "r", None, {}, timeout=0.03, poll=0.002)
    assert result == "TIMEOUT" and time.monotonic() - start < 1
    assert executor.approvals == 0 and executor.stops == 1
    executor = FakeExecutor(repository, hanging=True)
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=0.03, poll=0.002) == "TIMEOUT"
    assert executor.cancelled


@pytest.mark.asyncio
async def test_auto_approval_is_explicit_and_retry_is_bounded():
    repository = FakeRepository()
    executor = FakeExecutor(repository, waiting=True)
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=1, auto_approve=True, poll=0.002) == "SUCCESS"
    assert executor.approvals == 1
    repository = FakeRepository()
    executor = FakeExecutor(repository, failing=True, retryable=True)
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=1, max_retries=2, poll=0.002) == "FAILED"
    assert executor.retries == 2
    repository = FakeRepository()
    executor = FakeExecutor(repository, failing=True, retryable=False)
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=1, max_retries=2, poll=0.002) == "FAILED"
    assert executor.retries == 0


@pytest.mark.asyncio
async def test_synthetic_case_clarification_uses_explicit_answers_and_request_id():
    class ClarifyingExecutor(FakeExecutor):
        def __init__(self, repository):
            super().__init__(repository)
            self.answers = []

        async def start(self, *_):
            self.repository.run.update(
                status="WAITING_CLARIFICATION",
                clarification={"request_id": "request-1", "unresolved_fields": ["primary_entity"]},
            )

        async def clarify(self, _run_id, answers):
            self.answers.append(answers)
            self.repository.run["status"] = "SUCCESS"

    repository = FakeRepository()
    executor = ClarifyingExecutor(repository)
    assert await suite.drive_run(
        executor, repository, "r", None, {}, timeout=1,
        clarification_answers={"primary_entity": "Task"}, poll=0.002,
    ) == "SUCCESS"
    assert executor.answers == [{"primary_entity": "Task", "request_id": "request-1"}]

    repository = FakeRepository()
    executor = ClarifyingExecutor(repository)
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=1, poll=0.002) == "NEEDS_INPUT"
    assert executor.answers == []


@pytest.mark.asyncio
async def test_requested_cancel_stops_run_and_checkpoints():
    repository = FakeRepository()
    executor = FakeExecutor(repository, hanging=True)
    checkpoints = []
    assert await suite.drive_run(executor, repository, "r", None, {}, timeout=1,
                                cancel_requested=lambda: True, checkpoint=lambda: checkpoints.append(True)) == "CANCELLED"
    assert executor.stops == 1 and checkpoints


def test_default_cli_is_model_free_and_does_not_create_output(monkeypatch, tmp_path, capsys):
    def forbidden():
        raise AssertionError("default mode must not construct Provider or import app startup")
    monkeypatch.setattr(suite, "real_provider", forbidden)
    monkeypatch.setattr(suite, "_imports", forbidden)
    assert suite.main(["--repeat", "2", "--output", str(tmp_path / "output")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["live"] is False and plan["repeat"] == 2
    assert not (tmp_path / "output").exists()


def test_live_configuration_cannot_silently_fall_back_to_mock(monkeypatch):
    from app.llm.factory import ProviderFactory
    from app.llm.mock import MockProvider

    monkeypatch.setattr(ProviderFactory, "create_from_env", staticmethod(lambda: MockProvider()))
    with pytest.raises(ValueError, match="MockProvider"):
        suite.real_provider()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_timeout_values_must_be_finite_and_positive(value):
    with pytest.raises(Exception):
        suite._positive(value)


def test_json_reports_redact_credentials_but_keep_token_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", "secret-example-value")
    path = tmp_path / "report.json"
    suite.write_json(path, {"api_key": "secret-example-value", "error": "Bearer abc.def secret-example-value",
                            "tokens": {"total_tokens": 123}})
    content = path.read_text(encoding="utf-8")
    assert "secret-example-value" not in content and "abc.def" not in content
    assert json.loads(content)["tokens"]["total_tokens"] == 123
    assert not path.with_suffix(".json.tmp").exists()


def test_json_snapshot_retries_transient_windows_reader_lock(monkeypatch, tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"old": true}', encoding="utf-8")
    original_replace = Path.replace
    attempts = []

    def replace_with_reader_lock(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            error = PermissionError("transient sharing violation")
            error.winerror = 5
            raise error
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_reader_lock)
    suite.write_json(path, {"new": True})
    assert len(attempts) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert not list(tmp_path.glob("evidence.json.*.tmp"))


def test_supervisor_hard_timeout_cleans_a_real_local_process(tmp_path):
    # Local process only: exercises the actual Windows Job/POSIX group code.
    rtk = shutil.which("rtk")
    assert rtk, "project requires rtk"
    command = [rtk, "proxy", sys.executable, "-c", "import time; time.sleep(30)"]
    result = suite.supervise(command, tmp_path, timeout=0.15, grace=0.05)
    assert result["outcome"] == "TIMEOUT"
    assert result["returncode"] is not None and result["duration"] < 6
    assert (tmp_path / "cancel").exists() and (tmp_path / "worker.log").exists()


def test_supervisor_always_closes_tree_on_success_and_cancel(tmp_path):
    class Process:
        returncode = 0
        pid = 123
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            return self.returncode
    closed = []
    class Tree:
        def __init__(self, process):
            pass
        def close(self):
            closed.append(True)
    result = suite.supervise(["rtk", "proxy", "test"], tmp_path, timeout=1,
                             process_factory=lambda *a, **k: Process(), tree_factory=Tree)
    assert result["outcome"] is None and closed == [True]
    assert (tmp_path / "release").exists()
    process = Process()
    process.returncode = None
    def pending_wait(timeout):
        raise subprocess.TimeoutExpired("local-test", timeout)
    process.wait = pending_wait
    result = suite.supervise(["rtk", "proxy", "test"], tmp_path, timeout=1, cancel_requested=lambda: True,
                             process_factory=lambda *a, **k: process, tree_factory=Tree)
    assert result["outcome"] == "CANCELLED" and closed == [True, True]
    assert (tmp_path / "cancel").exists()


def test_parent_salvage_retains_failed_sources_without_constructing_provider(tmp_path, monkeypatch):
    from app.repositories.sqlite import SQLiteRepository
    suite._imports()
    repository = SQLiteRepository(tmp_path / "run.sqlite3")
    repository.create_run("r", "software-development", {"requirement": "new"}, "FAILED", "now")
    repository.save_run_snapshot("r", {"context": {"__artifact_files__": [
        {"name": "index.html", "content": "<html><body>failed-site</body></html>", "step_id": "frontend"}]}})
    repository._connection.close()
    monkeypatch.setattr(suite, "real_provider", lambda: pytest.fail("salvage must not call models"))
    evidence = suite.salvage(tmp_path, {"run_id": "r", "workflow_id": "software-development"})
    assert evidence["run"]["status"] == "FAILED"
    assert evidence["run"]["artifacts"]
    assert list((tmp_path / "workspace").rglob("*.html"))
    assert list((tmp_path / "workspace").rglob("artifacts.zip"))


def test_repeats_keep_independent_databases_and_one_frozen_workflow(tmp_path, monkeypatch):
    source = tmp_path / "current.yaml"
    original = "name: software-development\nsteps:\n  - id: requirement\n    agent: requirement_agent\n    task: '{{requirement}}'\n"
    source.write_text(original, encoding="utf-8")
    specs = []
    def local_worker(command, directory, **kwargs):
        from app.repositories.sqlite import SQLiteRepository

        spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
        specs.append(spec)
        assert (directory / "workflow.yaml").read_text(encoding="utf-8") == original
        assert spec["auto_approve"] is True and spec["live"] is True
        source.write_text(original.replace("requirement_agent", "other_agent"), encoding="utf-8")
        run = html_run()
        run["id"] = spec["run_id"]
        result = suite.measure(suite.Case(**spec["case"]), run, [], 1,
                               outcome="FAILED" if len(specs) == 2 else None,
                               error="specific failed test evidence" if len(specs) == 2 else None)
        repository = SQLiteRepository(directory / "run.sqlite3")
        repository.create_run(spec["run_id"], spec["workflow_id"], {"requirement": spec["case"]["requirement"]}, result["status"], "now")
        repository.save_run_snapshot(spec["run_id"], run["state"])
        repository.save_workflow_snapshot(spec["run_id"], spec["workflow_snapshot"])
        repository._connection.close()
        suite.write_json(directory / "result.json", result)
        return {"outcome": None, "returncode": 0, "duration": 1}
    monkeypatch.setattr(suite, "supervise", local_worker)
    monkeypatch.setattr(suite, "real_provider", lambda: pytest.fail("test must not call live Provider"))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "html", "requirement": "生成纯前端 HTML 源码", "expected_stack": "static_html"}]), encoding="utf-8")
    assert suite.main(["--live", "--auto-approve", "--repeat", "3", "--workflow", str(source),
                       "--cases", str(cases), "--output", str(tmp_path / "reports")]) == 1
    report_path = next((tmp_path / "reports").rglob("report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["success_rate"] == 2 / 3
    assert report["summary"]["deliverable_rate"] == 2 / 3
    assert report["results"][1]["failure_summary"] == "specific failed test evidence"
    assert len({spec["run_id"] for spec in specs}) == 3
    assert len({row["directory"] for row in report["results"]}) == 3
    assert len({row["workflow_sha256"] for row in report["results"]}) == 1
    assert all(spec["workflow_snapshot"] == report["workflow_snapshot"] for spec in specs)
    assert not (tmp_path / "run.sqlite3").exists()
    for row in report["results"]:
        from app.repositories.sqlite import SQLiteRepository

        repository = SQLiteRepository(Path(row["directory"]) / "run.sqlite3")
        assert len(repository.list_runs()) == 1
        assert repository.get_run(row["run_id"])["workflow_snapshot"] == report["workflow_snapshot"]
        repository._connection.close()


@pytest.mark.asyncio
async def test_terminal_status_waits_for_graph_cleanup_without_stopping_success():
    repository = FakeRepository()
    class CleaningExecutor(FakeExecutor):
        active = True
        async def start(self, *_):
            self.repository.run["status"] = "SUCCESS"
            await asyncio.sleep(0.02)
            self.active = False
        def is_active(self, _):
            return self.active
    executor = CleaningExecutor(repository)
    start = time.monotonic()
    result = await suite.drive_run(executor, repository, "r", SimpleNamespace(id="workflow"), {},
                                  timeout=1, poll=0.001)
    assert result == "SUCCESS" and time.monotonic() - start >= 0.02
    assert executor.stops == 0


def test_contract_gate_interface_is_consumed_after_archive_materialization(tmp_path, monkeypatch):
    from app.repositories.sqlite import SQLiteRepository
    from app.services.artifacts import ArtifactService

    repository = SQLiteRepository(tmp_path / "run.sqlite3")
    run = html_run()
    run["state"]["context"]["delivery_contract"] = {"schema_version": "test"}
    repository.create_run("r", "w", {}, "SUCCESS", "now")
    repository.save_run_snapshot("r", run["state"])
    calls = []
    def evaluate(context, validation, archive):
        assert archive.is_file()
        assert context["delivery_contract"] == {"schema_version": "test"}
        assert validation["status"] == "passed"
        calls.append(True)
        return {"status": "blocked", "deliverable": False, "checks": [{"status": "blocked"}], "missing": ["page-proof"]}
    monkeypatch.setattr(suite.importlib, "import_module", lambda name: SimpleNamespace(evaluate_delivery=evaluate))
    evidence = suite.collect_site(tmp_path, repository, ArtifactService(repository, tmp_path / "workspace"), "r", "w")
    result = suite.measure(suite.DEFAULT_CASES[2], evidence["run"], evidence["events"], 0)
    assert calls == [True] and result["deliverable"] is False
    assert "page-proof" in result["failure_summary"]
    repository._connection.close()


def test_persisted_final_gate_preserves_human_approval_and_uses_strict_archive(tmp_path, monkeypatch):
    from app.repositories.sqlite import SQLiteRepository
    from app.services.artifacts import ArtifactService

    repository = SQLiteRepository(tmp_path / "run.sqlite3")
    run = html_run()
    gate = {"status": "passed", "deliverable": True, "checks": [
        {"id": "human-approval", "status": "passed"}, {"id": "delivery-archive", "status": "passed"}]}
    run["state"]["context"].update(delivery_contract={"schema_version": "1.0"}, delivery_gate=gate)
    repository.create_run("r", "w", {}, "SUCCESS", "now")
    repository.save_run_snapshot("r", run["state"])
    modes = []
    class RecordingArtifacts(ArtifactService):
        def create_archive(self, run_id, *, strict=False):
            modes.append(strict)
            return super().create_archive(run_id, strict=strict)
    monkeypatch.setattr(suite.importlib, "import_module", lambda name: pytest.fail("must not reevaluate final gate"))
    evidence = suite.collect_site(tmp_path, repository, RecordingArtifacts(repository, tmp_path / "workspace"), "r", "w")
    assert evidence["run"]["delivery_gate"] == gate
    assert modes == [True]
    assert suite.measure(suite.DEFAULT_CASES[2], evidence["run"], [], 0)["deliverable"] is True
    repository._connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_approve", [False, True])
async def test_worker_persists_real_langgraph_checkpoints_with_offline_provider(tmp_path, monkeypatch, auto_approve):
    from app.llm.base import LLMResponse
    from app.llm.mock import MockProvider
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    class OfflineProvider(MockProvider):
        async def generate(self, system_prompt, user_prompt, config=None):
            return LLMResponse(text="Confirmed original requirement.", input_tokens=10, output_tokens=20,
                               finish_reason="stop", message_content="Confirmed original requirement.")
    monkeypatch.setattr(suite, "real_provider", lambda: OfflineProvider())
    yaml_text = ("name: durable-test\nsteps:\n  - id: requirement\n    agent: requirement_agent\n"
                 "    task: '{{requirement}}'\n    output: requirement_doc\n"
                 "  - id: approval\n    type: approval\n    depends_on: [requirement]\n")
    (tmp_path / "workflow.yaml").write_text(yaml_text, encoding="utf-8")
    (tmp_path / "workspace").mkdir()
    case = suite.DEFAULT_CASES[2]
    spec = {"live": True, "workflow_id": "durable-test", "workflow_snapshot": {}, "run_id": "durable-run",
            "case": {"id": case.id, "requirement": case.requirement, "expected_stack": case.expected_stack},
            "timeout": 2 if auto_approve else 0.18, "auto_approve": auto_approve, "max_retries": 0}
    await suite.execute_worker(tmp_path, spec)
    assert (tmp_path / "checkpoint.sqlite3").is_file()
    # Reopen the saver after the worker's AsyncExitStack has closed its connection.
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite3")) as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": "durable-run"}})
        assert saved is not None
        assert saved.checkpoint["channel_values"]["context"]["requirement"] == case.requirement
        assert saved.checkpoint["channel_values"]["context"]["requirement_doc"] == "Confirmed original requirement."
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == ("SUCCESS" if auto_approve else "TIMEOUT")
    assert result["tokens"]["total_tokens"] == 30
    assert result["deliverable"] is False  # A document-only fixture proves no code delivery.
