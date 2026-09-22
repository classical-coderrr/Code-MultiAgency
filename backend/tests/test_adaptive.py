import json

from app.workflow.adaptive import (
    bootstrap_budget_for_step,
    build_adaptive_policy,
    estimate_requirement_difficulty,
    infer_requirement_capabilities,
    parse_architecture_decision,
    parse_runtime_budget_plan,
    infer_requirement_routing,
    reconcile_architecture_decision,
)


def test_architecture_decision_accepts_plain_and_fenced_json():
    payload = {
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "low",
        "summary": "A simple advertisement page.",
        "frontend_plan": ["Use semantic HTML", "Add responsive CSS"],
        "backend_reason": "No API, database, login, or payment is required.",
        "risks": ["Check mobile layout"],
    }

    assert parse_architecture_decision(json.dumps(payload)) == payload
    assert parse_architecture_decision(f"```json\n{json.dumps(payload)}\n```") == payload


def test_invalid_architecture_decision_does_not_guess_a_route():
    assert parse_architecture_decision("This is a static HTML project.") is None
    assert parse_architecture_decision('{"project_type":"static_html"}') is None


def test_static_html_policy_skips_backend_and_uses_small_budgets():
    policy = build_adaptive_policy({
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "low",
    })

    assert policy["skip_steps"] == ["backend", "database"]
    assert policy["budgets"] == {"backend": 500, "frontend": 3200, "tester": 1000, "reviewer": 1000}
    assert policy["thinking"]["frontend"] == "low"


def test_crud_management_request_cannot_be_routed_to_static_html():
    raw_decision = {
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "medium",
        "summary": "A student management page.",
        "frontend_plan": ["Student table"],
        "backend_reason": "localStorage is enough.",
        "risks": [],
    }

    routing = infer_requirement_routing("生成一个学生管理系统，要求有 curd")
    corrected = reconcile_architecture_decision(raw_decision, "生成一个学生管理系统，要求有 curd")

    assert routing["backend_required"] is True
    assert routing["crud_management_signal"] is True
    assert corrected is not None
    assert corrected["project_type"] == "web_app"
    assert corrected["backend_required"] is True
    assert corrected["routing_guard"]["correction_applied"] is True
    assert corrected["routing_guard"]["matched_signals"] == ["业务/数据管理", "CRUD 操作"]
    assert build_adaptive_policy(corrected)["skip_steps"] == []


def test_explicit_browser_only_localstorage_demo_can_skip_backend():
    routing = infer_requirement_routing("做一个纯前端学生 CRUD demo，使用 localStorage，不需要后端")
    decision = reconcile_architecture_decision(
        {
            "project_type": "static_html",
            "backend_required": False,
            "complexity": "low",
            "summary": "A browser-only demo.",
            "frontend_plan": [],
            "backend_reason": "",
            "risks": [],
        },
        "做一个纯前端学生 CRUD demo，使用 localStorage，不需要后端",
    )

    assert routing["backend_required"] is False
    assert routing["explicit_backend"] is False
    assert decision is not None
    assert decision["project_type"] == "static_html"
    assert decision["backend_required"] is False
    assert build_adaptive_policy(decision)["skip_steps"] == ["backend", "database"]


def test_inconsistent_web_app_decision_keeps_backend_by_safe_default():
    decision = reconcile_architecture_decision(
        {
            "project_type": "web_app",
            "backend_required": False,
            "complexity": "low",
            "summary": "A web app.",
            "frontend_plan": [],
            "backend_reason": "",
            "risks": [],
        },
        "开发一个业务管理应用",
    )

    assert decision is not None
    assert decision["backend_required"] is True
    assert decision["routing_guard"]["correction_applied"] is True


def test_capability_router_is_not_tied_to_student_domain():
    spec = infer_requirement_capabilities("开发一个 CRM 应用，支持新增、编辑、删除和搜索客户")

    assert spec["backend_required"] is True
    assert "backend" in spec["required_capabilities"]
    assert "persistence" in spec["required_capabilities"]
    assert spec["ambiguous"] is True


def test_ambiguous_application_keeps_branches_and_requests_confirmation():
    decision = reconcile_architecture_decision(
        {
            "project_type": "static_html",
            "backend_required": False,
            "complexity": "low",
            "summary": "An application.",
            "frontend_plan": [],
            "backend_reason": "",
            "risks": [],
        },
        "帮我做一个业务平台",
    )

    assert decision is not None
    assert decision["project_type"] == "web_app"
    assert decision["backend_required"] is True
    assert decision["needs_clarification"] is True
    assert decision["clarification_questions"]
    assert build_adaptive_policy(decision)["skip_steps"] == ["database"]


def test_display_only_request_remains_lightweight():
    spec = infer_requirement_capabilities("制作一个简单的 HTML 广告落地页")

    assert spec["backend_required"] is False
    assert "frontend" in spec["required_capabilities"]
    assert spec["display_only"] is True
    assert spec["ambiguous"] is False


def test_negated_and_incidental_capabilities_do_not_become_delivery_requirements():
    import pathlib

    cases = json.loads((pathlib.Path(__file__).parents[1] / "scripts" / "regression_cases_10_tiered.json").read_text(encoding="utf-8"))
    for case in cases[:3]:
        spec = infer_requirement_capabilities(case["requirement"])
        assert spec["required_capabilities"] == ["frontend"], case["id"]
        decision = reconcile_architecture_decision(
            {"project_type": "web_app", "backend_required": True,
             "required_capabilities": ["backend", "persistence", "rag", "external_api"]},
            case["requirement"],
        )
        assert decision["required_capabilities"] == ["frontend"], case["id"]
    for case in cases[3:7]:
        spec = infer_requirement_capabilities(case["requirement"])
        assert "external_api" not in spec["required_capabilities"], case["id"]
        assert "rag" not in spec["required_capabilities"], case["id"]


def test_required_backend_capability_blocks_an_unsafe_static_policy():
    policy = build_adaptive_policy({
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "low",
        "required_capabilities": ["backend"],
    })

    assert policy["project_type"] == "web_app"
    assert policy["backend_required"] is True
    assert policy["skip_steps"] == ["database"]


def test_persistence_capability_enables_database_agent_before_implementation():
    policy = build_adaptive_policy({
        "project_type": "web_app",
        "backend_required": True,
        "complexity": "medium",
        "required_capabilities": ["backend", "frontend", "persistence"],
    })

    assert "database" not in policy["skip_steps"]
    assert policy["budgets"]["database"] == 4500
    assert policy["thinking"]["database"] == "off"


def test_non_persistent_backend_does_not_pay_for_database_agent():
    policy = build_adaptive_policy({
        "project_type": "backend_service",
        "backend_required": True,
        "complexity": "low",
        "required_capabilities": ["backend"],
    })

    assert policy["skip_steps"] == ["database"]
    assert "database" in policy["skip_reasons"]


def test_startup_budget_scales_with_requirement_difficulty():
    simple = estimate_requirement_difficulty("开发一个简单的静态 HTML 广告页")
    shopping_site = estimate_requirement_difficulty("写一个前后端的简单购物网站")
    complex_requirement = estimate_requirement_difficulty(
        "开发一个企业级多租户平台，包含用户登录、角色权限、数据库、支付、实时消息、API 集成、监控部署和高并发。"
    )

    simple_budget, simple_assessment = bootstrap_budget_for_step("requirement", "开发一个简单的静态 HTML 广告页")
    complex_budget, complex_assessment = bootstrap_budget_for_step(
        "requirement",
        "开发一个企业级多租户平台，包含用户登录、角色权限、数据库、支付、实时消息、API 集成、监控部署和高并发。",
    )

    assert simple["level"] == "low"
    assert shopping_site["level"] == "medium"
    assert complex_requirement["level"] == "high"
    assert simple_assessment["level"] == "low"
    assert complex_assessment["level"] == "high"
    assert simple_budget is not None and complex_budget is not None
    assert complex_budget > simple_budget


def test_runtime_budget_plan_is_validated_and_clamped_to_provider_limit():
    plan = parse_runtime_budget_plan(
        json.dumps({
            "difficulty": "medium",
            "model_strength": "strong",
            "confidence": 0.9,
            "agent_budgets": {"requirement": 3200, "frontend": 9000, "unknown": 1000},
            "thinking": {"requirement": "low", "frontend": "off", "unknown": "max"},
            "rationale": ["需要前后端", "模型支持思考控制"],
        }),
        {"requirement", "frontend"},
        6000,
    )

    assert plan is not None
    assert plan["agent_budgets"] == {"requirement": 3200, "frontend": 6000}
    assert plan["thinking"] == {"requirement": "low", "frontend": "off"}


def test_runtime_budget_plan_has_a_safe_floor_for_normal_agents():
    plan = parse_runtime_budget_plan(
        json.dumps({
            "difficulty": "medium",
            "model_strength": "standard",
            "confidence": 0.4,
            "agent_budgets": {"backend": 675},
            "thinking": {"backend": "off"},
            "rationale": ["低置信度规划"],
        }),
        {"backend"},
        6000,
    )

    assert plan is not None
    assert plan["agent_budgets"] == {"backend": 1024}


def test_malformed_architecture_values_are_rejected_without_attribute_error():
    assert reconcile_architecture_decision('"not-an-object"', "开发一个管理系统") is None
    fallback_policy = build_adaptive_policy("not-an-object")
    assert fallback_policy["backend_required"] is True
    assert fallback_policy["project_type"] == "web_app"
