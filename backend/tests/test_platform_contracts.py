from __future__ import annotations

from app.workflow.capability_router import CapabilityRouter
from app.workflow.clarification import RequirementClarificationService
from app.workflow.platform_contracts import build_project_blueprint, evidence_from_check, failure_fact_from_check, normalize_failure_fact
from app.workflow.delivery_contract import build_delivery_contract
from app.workflow.integration_gate import IntegrationGate
from app.code_company.contract_compiler import ContractCompiler


def test_ambiguous_business_request_gets_structured_clarification():
    spec = CapabilityRouter().route("开发一个学生管理系统，需要增删改查")
    state, request = RequirementClarificationService().assess(spec)
    assert state == "USER_CLARIFICATION_REQUIRED"
    assert request is not None
    assert "backend_required" in request.unresolved_fields
    assert request.recommended_default["database_mode"] == "h2"


def test_clarification_answer_becomes_requirement_state_and_blueprint():
    router = CapabilityRouter()
    spec = router.route("开发一个学生管理系统，需要增删改查")
    service = RequirementClarificationService()
    state, request = service.assess(spec)
    assert request is not None
    updated, answered, context = service.apply_answer(spec, request, {"option": "full_stack_h2"})
    assert answered.status == "ANSWERED"
    assert updated.state == "ASSUMPTION_ALLOWED"
    assert updated.backend_required is True
    assert updated.safe_defaults["database_mode"] == "h2"
    assert context["requirement_state"] == "ASSUMPTION_ALLOWED"
    decision = {"project_type": "web_app", "backend_required": True, "required_capabilities": ["backend", "frontend", "persistence"]}
    contract = build_delivery_contract(updated.raw_requirement, decision)
    blueprint = build_project_blueprint(updated.raw_requirement, updated.model_dump(mode="json"), decision, contract)
    assert blueprint["database"]["mode"] == "h2"
    assert blueprint["status"] == "DRAFT"


def test_clarification_exposes_recommendation_reference_and_custom_fields():
    spec = CapabilityRouter().route("开发一个学生管理系统，需要增删改查")
    service = RequirementClarificationService()
    _, request = service.assess(spec)

    assert request is not None
    assert request.prompt_reference
    assert any(option["value"] == "custom" for option in request.options)
    prompts = {item["field"]: item for item in request.field_prompts}
    assert prompts["backend_required"]["type"] == "select"
    assert prompts["frontend_stack"]["recommended"] == "html"
    assert prompts["database_mode"]["recommended"] == "h2"

    updated, answered, _ = service.apply_answer(spec, request, {
        "option": "custom",
        "backend_required": "true",
        "frontend_stack": "vue",
        "database_mode": "h2",
    })

    assert answered.answers["backend_required"] is True
    assert updated.backend_required is True
    assert updated.safe_defaults["frontend_stack"] == "vue"


def test_custom_clarification_requires_every_unresolved_field():
    spec = CapabilityRouter().route("开发一个学生管理系统，需要增删改查")
    service = RequirementClarificationService()
    _, request = service.assess(spec)
    assert request is not None

    try:
        service.apply_answer(spec, request, {"option": "custom", "frontend_stack": "vue"})
    except ValueError as exc:
        assert "backend_required" in str(exc)
        assert "database_mode" in str(exc)
    else:
        raise AssertionError("incomplete custom clarification must fail")


def test_clarified_database_boundary_overrides_model_database_suggestion():
    spec = CapabilityRouter().route("开发一个学生信息展示系统")
    service = RequirementClarificationService()
    _, request = service.assess(spec)
    assert request is not None
    updated, _, _ = service.apply_answer(spec, request, {
        "backend_required": True, "frontend_stack": "html", "database_mode": "h2",
    })
    contract = build_delivery_contract(
        updated.raw_requirement,
        {"project_type": "web_app", "backend_required": True,
         "delivery_contract": {"database_mode": "mysql", "database_required": True}},
        requirement_spec=updated.model_dump(mode="json"),
    )
    assert contract["database_mode"] == "h2"


def test_evidence_and_failure_fact_are_normalized_from_deterministic_check():
    check = {"id": "api-crud", "status": "failed", "category": "crud", "message": "POST returned 500", "files": ["StudentController.java"]}
    evidence = evidence_from_check(check, gate="api", index=0)
    failure = failure_fact_from_check(check, gate="api", owner="backend", index=0)
    assert evidence["evidence_id"] == "api-crud"
    assert evidence["status"] == "failed"
    assert failure["owner"] == "backend"
    assert failure["file"] == "StudentController.java"


def test_legacy_failure_fact_is_upgraded_without_losing_evidence():
    normalized = normalize_failure_fact({
        "id": "backend-build",
        "type": "build",
        "target": "backend",
        "phase": "build",
        "error": "cannot find symbol RoomService",
        "files": ["src/main/java/app/RoomController.java"],
        "action": "dispatch_owner_repair",
        "retry": True,
        "retry_fingerprint": "legacy-fingerprint",
    })

    assert normalized is not None
    assert normalized["code"] == "backend-build"
    assert normalized["category"] == "build"
    assert normalized["owner"] == "backend"
    assert normalized["file"] == "src/main/java/app/RoomController.java"
    assert normalized["retryable"] is True
    assert normalized["repair_action"] == "dispatch_owner_repair"
    assert normalized["evidence"]["legacy"] is True


def test_artifact_failure_routes_to_check_target_owner():
    result = IntegrationGate().normalize(
        [{
            "id": "backend-startup",
            "target": "backend",
            "status": "failed",
            "message": "服务已有 HTTP 响应，但 /api/products 返回 HTTP 404",
        }],
        gate="artifact",
    )

    assert result["failureFacts"][0]["owner"] == "backend"


def test_descriptive_spring_stack_builds_java_and_embedded_frontend_ownership():
    requirement = "开发糖果广告网站，前端展示糖果，后端管理员增删改查糖果"
    decision = {
        "project_type": "web_app",
        "backend_required": True,
        "delivery_contract": {
            "backend_stack": "Spring Boot + Spring Data JPA + H2",
            "frontend_stack": "HTML/CSS/JavaScript，static/index.html，同源 REST",
            "crud_required": True,
            "entities": [{"name": "Candy", "fields": {"id": "Long", "name": "String"}}],
            "api_contract": [{
                "entity_id": "Candy",
                "path": "/api/candies",
                "methods": ["GET", "POST", "PUT", "DELETE"],
                "fields": ["id", "name"],
                "payload": {"name": "草莓软糖"},
            }],
        },
    }
    spec = CapabilityRouter().route(requirement).model_dump(mode="json")
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec)
    blueprint = build_project_blueprint(requirement, spec, decision, contract, frozen=True)

    assert contract["backend_stack"] == "springboot"
    assert "pom.xml" in blueprint["artifact_ownership"]["backend"]
    assert "src/main/java/**" in blueprint["artifact_ownership"]["backend"]
    assert blueprint["artifact_ownership"]["frontend"] == [
        "src/main/resources/static/**",
        "src/main/resources/templates/**",
    ]
    enriched = ContractCompiler().enrich_blueprint(blueprint)
    backend_plan = enriched["role_contracts"]["backend"]["file_plan"]
    frontend_plan = enriched["role_contracts"]["frontend"]["file_plan"]
    assert any(item["path"] == "pom.xml" for item in backend_plan)
    assert any(item["path"].endswith("CandyController.java") for item in backend_plan)
    assert any(item["path"] == "src/main/resources/static/index.html" for item in frontend_plan)
