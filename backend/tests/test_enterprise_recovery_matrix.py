"""Representative delivery and failure-routing matrix for bounded recovery.

These tests deliberately keep deterministic Gates enabled.  A scenario may
only pass by producing a coherent contract or by assigning a defect to the
correct owner; lowering a Gate to ``skipped`` is not accepted as success.
"""

import pytest

from app.code_company.contract_compiler import ContractCompiler
from app.code_company.contract_conformance import check_contract_conformance
from app.code_company.failure_classifier import FailureClassifier
from app.workflow.architecture_repair import architecture_patch_issue, contract_failure_fact
from app.workflow.artifact_validator import ValidationCheck
from app.workflow.capability_router import CapabilityRouter
from app.workflow.delivery_contract import build_delivery_contract
from app.workflow.platform_contracts import build_project_blueprint


def _compile(requirement: str, *, delivery_contract: dict | None = None):
    spec = CapabilityRouter().route(requirement)
    decision = {
        "project_type": "web_app" if spec.backend_required else "static_html",
        "backend_required": spec.backend_required,
        "required_capabilities": list(spec.required_capabilities),
        "delivery_contract": delivery_contract or {},
    }
    contract = build_delivery_contract(
        requirement,
        decision,
        requirement_spec=spec.model_dump(mode="json"),
    )
    blueprint = ContractCompiler().enrich_blueprint(
        build_project_blueprint(
            requirement,
            spec.model_dump(mode="json"),
            decision,
            contract,
        )
    )
    return spec, blueprint, ContractCompiler().compile(blueprint)


@pytest.mark.parametrize(
    ("requirement", "frontend", "backend", "database"),
    [
        ("纯 HTML/CSS/JS 待办网页，不需要后端和数据库", "html", False, "none"),
        ("纯前端 HTML 学生管理页面，使用 localStorage 完成增删改查，无需后端和数据库", "html", False, "none"),
        ("Spring Boot + HTML + H2 学生管理系统，支持增删改查", "html", True, "h2"),
        ("Spring Boot + Vue + H2 学生管理系统，支持增删改查", "vue", True, "h2"),
    ],
)
def test_representative_delivery_scenarios_keep_required_boundaries(
    requirement, frontend, backend, database
):
    spec, blueprint, compiled = _compile(requirement)

    assert blueprint["frontend"]["stack"] == frontend
    assert blueprint["backend"]["required"] is backend
    assert blueprint["database"]["mode"] == database
    assert "rag" not in spec.required_capabilities
    assert "external_api" not in spec.required_capabilities
    assert ("backend" in compiled["execution_policy"]["skip_steps"]) is not backend


def test_dual_entity_crud_freezes_explicit_entity_routes_without_guessing():
    requirement = "前端用 Vue、后端用 Spring Boot、数据库用 H2，管理学生和课程，两个实体都要完整增删改查"
    delivery_contract = {
        "entities": [
            {"name": "Student", "table": "students", "fields": {"id": "long", "name": "string"}},
            {"name": "Course", "table": "courses", "fields": {"id": "long", "name": "string"}},
        ],
        "api_contract": [
            {"entity_id": "Student", "path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"]},
            {"entity_id": "Course", "path": "/api/courses", "methods": ["GET", "POST", "PUT", "DELETE"]},
        ],
    }

    spec, _, compiled = _compile(requirement, delivery_contract=delivery_contract)

    assert set(spec.primary_entities) == {"Student", "Course"}
    assert {"/api/students", "/api/students/{id}", "/api/courses", "/api/courses/{id}"} <= set(compiled["openapi"]["paths"])


def test_missing_entity_id_cannot_be_faked_by_a_url_only_patch():
    candidate = {
        "delivery_contract": {
            "entities": [
                {"name": "Student", "fields": {"id": "long", "name": "string"}},
                {"name": "Course", "fields": {"id": "long", "name": "string"}},
            ],
            "api_contract": [],
        }
    }
    fact = contract_failure_fact(
        ValueError("多实体 API 缺少明确的 entity_id"), attempt=1
    )

    issue = architecture_patch_issue(
        candidate,
        {"api_contract": [{"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"]}]},
        fact,
    )

    assert fact["owner"] == "architecture"
    assert fact["repairable"] is True
    assert "仍缺少 entity_id" in issue


@pytest.mark.parametrize(
    ("check", "owners", "repairable"),
    [
        (
            ValidationCheck("frontend-import", "frontend", "Vue 构建", "failed", "Cannot find module './RoomForm.vue'"),
            ("frontend",),
            True,
        ),
        (
            ValidationCheck(
                "backend-maven-test", "backend", "Maven 测试", "failed", "cannot find symbol",
                output="[ERROR] symbol: class RoomService",
            ),
            ("backend",),
            True,
        ),
        (
            ValidationCheck("frontend-backend-route-contract", "frontend", "API 合同", "failed", "路由不一致"),
            ("backend", "frontend"),
            True,
        ),
        (
            ValidationCheck("backend-database-contract", "backend", "数据库合同", "failed", "missing table rooms"),
            ("database", "backend"),
            True,
        ),
        (
            ValidationCheck("validator-error", "tester", "验证器", "failed", "validator internal error"),
            ("platform",),
            False,
        ),
    ],
)
def test_injected_failures_are_routed_without_weakening_the_gate(check, owners, repairable):
    classification = FailureClassifier().classify_check(check)

    assert check.status == "failed"
    assert classification.owners == owners
    assert classification.repairable is repairable


def test_missing_required_files_and_wrong_routes_remain_failed_gates():
    _, _, compiled = _compile("Spring Boot + Vue + H2 学生管理系统，支持增删改查")
    checks = check_contract_conformance(
        compiled,
        [
            {"step_id": "backend", "name": "pom.xml", "content": "<project/>"},
            {"step_id": "frontend", "name": "src/App.vue", "content": "fetch('/api/products')"},
        ],
    )
    by_id = {item["id"]: item for item in checks}

    assert by_id["frozen-file-plan"]["status"] == "failed"
    assert by_id["frontend-api-contract"]["status"] == "failed"
    assert all(item["status"] != "skipped" for item in by_id.values())
