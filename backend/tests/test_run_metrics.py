from __future__ import annotations

from app.repositories.sqlite import SQLiteRepository


def test_repeated_approval_reopens_the_existing_request() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run(
        "run-reapproval",
        "software-development",
        {"requirement": "build"},
        "RUNNING",
        "2026-09-14T00:00:00+00:00",
    )
    repository.create_approval(
        "run-reapproval",
        "architecture_approval",
        "2026-09-14T00:00:01+00:00",
    )
    repository.resolve_approval(
        "run-reapproval",
        "architecture_approval",
        "approve",
        "2026-09-14T00:00:02+00:00",
    )

    repository.create_approval(
        "run-reapproval",
        "architecture_approval",
        "2026-09-14T00:00:03+00:00",
    )

    approval = repository._connection.execute(
        "SELECT status, decision, created_at, resolved_at FROM approval_requests "
        "WHERE run_id = ? AND step_id = ?",
        ("run-reapproval", "architecture_approval"),
    ).fetchone()
    assert dict(approval) == {
        "status": "WAITING",
        "decision": None,
        "created_at": "2026-09-14T00:00:03+00:00",
        "resolved_at": None,
    }


def test_run_metrics_exclude_approval_wait_and_group_tokens_by_agent() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run(
        "run-1",
        "software-development",
        {"requirement": "build a student management system"},
        "RUNNING",
        "2026-09-14T00:00:00+00:00",
    )
    repository.upsert_step(
        "run-1",
        "backend",
        agent_id="backend_agent",
        status="SUCCESS",
        input_tokens=120,
        output_tokens=480,
    )
    repository.upsert_step(
        "run-1",
        "frontend",
        agent_id="frontend_agent",
        status="SUCCESS",
        input_tokens=80,
        output_tokens=320,
    )
    repository.create_approval("run-1", "architecture_approval", "2026-09-14T00:00:02+00:00")
    repository.resolve_approval(
        "run-1",
        "architecture_approval",
        "approve",
        "2026-09-14T00:00:05+00:00",
    )
    repository.update_run(
        "run-1",
        status="SUCCESS",
        finished_at="2026-09-14T00:00:10+00:00",
        duration_ms=10000,
    )

    result = repository.get_run("run-1")

    assert result is not None
    assert result["active_duration_ms"] == 7000
    assert result["approval_duration_ms"] == 3000
    assert result["token_usage"] == {
        "input_tokens": 200,
        "output_tokens": 800,
        "total_tokens": 1000,
        "by_agent": [
            {
                "step_id": "backend",
                "agent_id": "backend_agent",
                "status": "SUCCESS",
                "input_tokens": 120,
                "output_tokens": 480,
                "total_tokens": 600,
                "retry_count": 0,
            },
            {
                "step_id": "frontend",
                "agent_id": "frontend_agent",
                "status": "SUCCESS",
                "input_tokens": 80,
                "output_tokens": 320,
                "total_tokens": 400,
                "retry_count": 0,
            },
        ],
    }


def test_run_metrics_accumulate_failed_and_recovered_step_attempts() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run(
        "run-recovered",
        "software-development",
        {"requirement": "build"},
        "RUNNING",
        "2026-09-14T00:00:00+00:00",
    )
    repository.upsert_step(
        "run-recovered",
        "tester",
        agent_id="tester_agent",
        status="FAILED",
        input_tokens=100,
        output_tokens=25,
    )
    repository.append_event(
        "run-recovered",
        "step.failed",
        "2026-09-14T00:00:05+00:00",
        {"stepId": "tester", "tokens": {"input": 100, "output": 25}},
    )
    repository.upsert_step(
        "run-recovered",
        "tester",
        agent_id="tester_agent",
        status="SUCCESS",
        input_tokens=40,
        output_tokens=10,
    )
    repository.append_event(
        "run-recovered",
        "step.completed",
        "2026-09-14T00:00:08+00:00",
        {"stepId": "tester", "tokens": {"input": 40, "output": 10}},
    )

    result = repository.get_run("run-recovered")

    assert result is not None
    assert result["token_usage"]["input_tokens"] == 140
    assert result["token_usage"]["output_tokens"] == 35
    assert result["token_usage"]["total_tokens"] == 175
    assert result["token_usage"]["by_agent"][0]["status"] == "SUCCESS"


def test_run_metrics_report_repair_quality_and_validation_stages() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run(
        "run-repair-metrics",
        "software-development",
        {"requirement": "build"},
        "RUNNING",
        "2026-09-14T00:00:00+00:00",
    )
    repository.upsert_step(
        "run-repair-metrics",
        "tester",
        agent_id="tester_agent",
        status="SUCCESS",
        duration_ms=1200,
        input_tokens=20,
        output_tokens=10,
    )
    repository.append_event(
        "run-repair-metrics",
        "repair.round_started",
        "2026-09-14T00:00:01+00:00",
        {"stepId": "tester", "repairAttempt": 1},
    )
    repository.append_event(
        "run-repair-metrics",
        "repair.completed",
        "2026-09-14T00:00:02+00:00",
        {"stepId": "tester", "passed": True, "attempts": 1},
    )
    repository.append_event(
        "run-repair-metrics",
        "step.validation_stage_completed",
        "2026-09-14T00:00:03+00:00",
        {"stepId": "tester", "stage": "preflight", "durationMs": 125},
    )
    repository.update_run(
        "run-repair-metrics",
        status="SUCCESS",
        finished_at="2026-09-14T00:00:04+00:00",
    )

    result = repository.get_run("run-repair-metrics")

    assert result is not None
    assert result["repair_metrics"] == {
        "first_pass": False,
        "repair_rounds": 1,
        "successful_repairs": 1,
        "automatic_repair_rate": 1.0,
        "average_repair_rounds": 1.0,
        "circuit_breaks": 0,
    }
    assert any(
        stage["stage"] == "validation_preflight" and stage["duration_ms"] == 125
        for stage in result["stage_metrics"]
    )


def test_architecture_backedge_contributes_to_repair_metrics() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run("architecture-metrics", "software-development", {"requirement": "build"},
                          "RUNNING", "2026-09-14T00:00:00+00:00")
    repository.append_event("architecture-metrics", "architecture.contract_failed", "2026-09-14T00:00:01+00:00",
                            {"stepId": "architecture", "repairAttempt": 0})
    repository.append_event("architecture-metrics", "architecture.repair_started", "2026-09-14T00:00:02+00:00",
                            {"stepId": "architecture", "repairAttempt": 1})
    repository.append_event("architecture-metrics", "architecture.target_gate_completed", "2026-09-14T00:00:03+00:00",
                            {"stepId": "architecture", "repairAttempt": 1, "passed": True})
    metrics = repository.get_run("architecture-metrics")["repair_metrics"]
    assert metrics["repair_rounds"] == 1
    assert metrics["successful_repairs"] == 1
    assert metrics["automatic_repair_rate"] == 1.0
    assert repository.get_run("architecture-metrics")["repair_trace"][-1]["status"] == "目标 Gate 通过"
    repository.append_event("architecture-metrics", "architecture.repair_circuit_open", "2026-09-14T00:00:04+00:00",
                            {"stepId": "architecture", "repairAttempt": 2})
    assert repository.get_run("architecture-metrics")["repair_metrics"]["circuit_breaks"] == 1


def test_delivery_repairs_and_archive_rebuilds_are_visible_in_run_metrics() -> None:
    repository = SQLiteRepository(":memory:")
    repository.create_run("delivery-metrics", "software-development", {"requirement": "build"},
                          "RUNNING", "2026-09-14T00:00:00+00:00")
    repository.append_event("delivery-metrics", "delivery.archive_rebuild_started", "2026-09-14T00:00:01+00:00",
                            {"owner": "platform", "missing": ["delivery-archive"]})
    repository.append_event("delivery-metrics", "delivery.archive_rebuild_completed", "2026-09-14T00:00:02+00:00",
                            {"owner": "platform", "passed": True, "missing": []})
    repository.append_event("delivery-metrics", "delivery.repair_started", "2026-09-14T00:00:03+00:00",
                            {"stepId": "tester", "repairAttempt": 1, "missing": ["browser-evidence"]})
    repository.append_event("delivery-metrics", "delivery.repair_completed", "2026-09-14T00:00:04+00:00",
                            {"stepId": "tester", "repairAttempt": 1, "passed": True, "missing": []})

    result = repository.get_run("delivery-metrics")

    assert result["repair_metrics"]["repair_rounds"] == 1
    assert result["repair_metrics"]["successful_repairs"] == 2
    assert result["repair_metrics"]["automatic_repair_rate"] == 1.0
    traces = {(row["step_id"], row["status"]) for row in result["repair_trace"]}
    assert ("platform", "压缩包重建通过") in traces
    assert ("tester", "交付门禁通过") in traces
