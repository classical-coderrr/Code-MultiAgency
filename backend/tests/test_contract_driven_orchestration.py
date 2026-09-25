import json

import pytest

from app.code_company.artifact_contracts import enforce_artifact_plan, path_allowed
from app.code_company.contract_compiler import ContractCompiler
from app.code_company.dependency_manifest import render_managed_artifact
from app.workflow.artifact_generation import ArtifactFileSpec
from app.workflow.capability_router import CapabilityRouter
from app.workflow.clarification import RequirementClarificationService
from app.workflow.delivery_contract import build_delivery_contract
from app.workflow.integration_gate import IntegrationGate
from app.workflow.platform_contracts import build_project_blueprint, request_blueprint_change


def entity(name="Product"):
    return {
        "name": name,
        "table": f"{name.lower()}s",
        "identity_field": "id",
        "fields": {
            "id": {"java_type": "Long", "sql_type": "BIGINT", "json_type": "integer", "nullable": False, "generated": True},
            "name": {"java_type": "String", "sql_type": "VARCHAR(255)", "json_type": "string", "nullable": False, "generated": False},
        },
    }


def blueprint(*, frontend="vue", database="h2", entities=None, api=None):
    values = entities or [entity()]
    rows = api or [{
        "entity_id": values[0]["name"], "path": f"/api/{values[0]['name'].lower()}s",
        "methods": ["GET", "POST", "PUT", "DELETE"], "fields": ["id", "name"],
        "payload": {"name": "demo"}, "identity_field": "id",
    }]
    return {
        "schema_version": "2.0", "blueprint_id": "bp_test", "version": 1, "status": "FROZEN",
        "project_type": "web_app", "backend": {"required": True, "stack": "springboot"},
        "frontend": {"required": frontend != "none", "stack": frontend},
        "database": {"mode": database, "production_engine": database, "validation_engine": "h2"},
        "entities": values, "dto": [], "api_contract": rows, "entrypoints": ["/"],
        "delivery_requirements": {"crud_required": True, "page_mode": "spa" if frontend == "vue" else "static_rest"},
        "artifact_ownership": {
            "database": ["src/main/resources/schema.sql", "src/main/resources/data.sql"],
            "backend": ["pom.xml", "src/main/java/**", "src/main/resources/application.yml"],
            "frontend": ["package.json", "index.html", "src/**", "src/main/resources/static/**"],
        },
        "constraints": {"agents_may_change_contract": False}, "assumptions": [], "decisions": [],
    }


def test_ambiguous_shopping_crud_requires_entity_clarification():
    spec = CapabilityRouter().route("开发一个购物网站，后端使用 Spring Boot，前端使用 HTML，支持增删改查")
    assert spec.support_status == "CLARIFICATION_REQUIRED"
    assert "primary_entity" in spec.impactful_gaps
    state, request = RequirementClarificationService().assess(spec)
    assert state == "USER_CLARIFICATION_REQUIRED"
    assert request is not None and request.recommended_default["primary_entity"] == "Product"


def test_hotel_crud_shows_room_as_explicit_suggestion_and_accepts_custom_entity():
    spec = CapabilityRouter().route("开发一个酒店管理系统，前端 Vue、后端 Spring Boot，支持增删改查")
    service = RequirementClarificationService()
    state, request = service.assess(spec)
    assert state == "USER_CLARIFICATION_REQUIRED"
    assert request is not None
    assert request.recommended_default["primary_entity"] == "Room"
    assert "你想管理的对象是什么" in request.prompt_reference
    assert "Room" in request.options[0]["label"]

    custom, answered, _ = service.apply_answer(spec, request, {
        "option": "custom", "primary_entity": "预订（Booking）",
    })
    assert answered.answers["primary_entity"] == "Booking"
    assert custom.primary_entities == ["Booking"]
    assert "Product" not in custom.safe_defaults.get("entity_fields", {})
    assert custom.safe_defaults["entity_fields"]["Booking"]["id"] == "long"
    recommended, _, _ = service.apply_answer(spec, request, {"option": "use_recommended"})
    assert recommended.primary_entities == ["Room"]
    chinese, _, _ = service.apply_answer(spec, request, {"option": "custom", "primary_entity": "客房"})
    assert chinese.primary_entities == ["Room"]

    with pytest.raises(ValueError, match="请填写要管理的对象"):
        service.apply_answer(spec, request, {"option": "custom", "primary_entity": ""})


def test_unknown_crud_domain_requires_custom_entity_instead_of_product_default():
    spec = CapabilityRouter().route("开发一个航班管理系统，前端 Vue、后端 Spring Boot，支持增删改查")
    _, request = RequirementClarificationService().assess(spec)
    assert request is not None
    assert "primary_entity" not in request.recommended_default
    assert [option["value"] for option in request.options] == ["custom"]


def test_explicit_student_crud_uses_safe_h2_default_without_entity_question():
    spec = CapabilityRouter().route("使用 Spring Boot 和 Vue 开发学生管理系统，实现增删改查")
    assert spec.primary_entities == ["Student"]
    assert "primary_entity" not in spec.impactful_gaps
    assert spec.safe_defaults["validation_database"] == "h2"


def test_confirmed_entity_safe_defaults_compile_minimum_crud_contract():
    spec = CapabilityRouter().route("Build a Student management CRUD system with Spring Boot and Vue")
    contract = build_delivery_contract(
        spec.raw_requirement,
        {"project_type": "web_app", "backend_required": True, "required_capabilities": ["frontend", "backend", "persistence"], "delivery_contract": {}},
        requirement_spec=spec.model_dump(mode="json"),
    )
    assert contract["entities"][0]["name"] == "Student"
    assert contract["api_contract"][0]["entity_id"] == "Student"
    assert contract["api_contract"][0]["detail_path"] == "/api/students/{id}"


def test_api_contract_uses_explicit_entity_and_separate_detail_path():
    compiled = ContractCompiler().compile(blueprint())
    paths = compiled["openapi"]["paths"]
    assert "/api/products" in paths
    assert "/api/products/{id}" in paths
    assert paths["/api/products"]["get"]["x-entity-id"] == "Product"
    assert "Api" not in compiled["json_schema"]


def test_stateless_api_uses_request_response_schemas_without_fake_entity_or_database():
    value = blueprint(
        frontend="html",
        database="none",
        api=[{
            "path": "/api/convert",
            "methods": ["POST"],
            "fields": ["amount", "currency"],
            "payload": {"amount": 100, "currency": "USD"},
            "request_schema": {
                "type": "object",
                "properties": {"amount": {"type": "number"}, "currency": {"type": "string"}},
                "required": ["amount", "currency"],
            },
            "response_schema": {
                "type": "object",
                "properties": {"convertedAmount": {"type": "number"}},
            },
        }],
    )
    value["entities"] = []
    value["delivery_requirements"]["crud_required"] = False

    compiled = ContractCompiler().compile(value)
    operation = compiled["openapi"]["paths"]["/api/convert"]["post"]

    assert operation["x-entity-id"] is None
    assert operation["requestBody"]["content"]["application/json"]["schema"]["required"] == ["amount", "currency"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["convertedAmount"]["type"] == "number"
    assert compiled["database_schema"]["tables"] == {}


def test_ambiguous_api_entity_is_rejected_instead_of_guessed_from_api_prefix():
    rows = [{"path": "/api/items", "methods": ["POST"], "fields": ["name"], "payload": {"name": "x"}}]
    value = blueprint(entities=[entity("Product"), entity("Order")], api=rows)
    with pytest.raises(ValueError, match="entity_id"):
        ContractCompiler().compile(value)


def test_user_named_routes_bind_multi_entity_api_without_model_entity_id():
    requirement = (
        "使用 Spring Boot 和 Vue 开发课程与学生管理系统，Student 和 Course 两个实体，"
        "分别使用 /api/students、/api/courses 及各自 /{id} 的 CRUD。"
    )
    spec = CapabilityRouter().route(requirement)
    proposal = {
        "entities": [entity("Student"), entity("Course")],
        "api_contract": [
            {"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
             "fields": ["id", "name"], "payload": {"name": "A"}},
            {"path": "/api/courses", "methods": ["GET", "POST", "PUT", "DELETE"],
             "fields": ["id", "name"], "payload": {"name": "B"}},
        ],
    }
    decision = {"project_type": "web_app", "backend_required": True,
                "required_capabilities": ["frontend", "backend", "persistence"],
                "delivery_contract": proposal}
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    assert {row["entity_id"] for row in contract["api_contract"]} == {"Student", "Course"}
    compiled = ContractCompiler().compile(blueprint(entities=contract["entities"], api=contract["api_contract"]))
    assert compiled["openapi"]["paths"]["/api/courses"]["get"]["x-entity-id"] == "Course"


def test_multi_entity_route_is_not_bound_without_exact_user_path():
    requirement = "使用 Spring Boot 和 Vue 开发课程与学生管理系统，Student 和 Course 两个实体，支持 CRUD。"
    spec = CapabilityRouter().route(requirement)
    decision = {"project_type": "web_app", "backend_required": True,
                "delivery_contract": {"entities": [entity("Student"), entity("Course")],
                                      "api_contract": [{"path": "/api/items", "methods": ["POST"],
                                                        "fields": ["name"], "payload": {"name": "A"}}]}}
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    with pytest.raises(ValueError, match="entity_id"):
        ContractCompiler().compile(blueprint(entities=contract["entities"], api=contract["api_contract"]))


def test_single_entity_model_route_without_user_named_path_also_needs_entity_id():
    requirement = "使用 Spring Boot 和 Vue 开发 Student 管理系统，支持 CRUD。"
    spec = CapabilityRouter().route(requirement)
    decision = {"project_type": "web_app", "backend_required": True,
                "delivery_contract": {"entities": [entity("Student")],
                                      "api_contract": [{"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
                                                        "fields": ["id", "name"], "payload": {"name": "A"}}]}}
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    assert contract["api_contract"][0]["entity_id"] is None
    with pytest.raises(ValueError, match="entity_id"):
        ContractCompiler().compile(blueprint(entities=contract["entities"], api=contract["api_contract"]))


def test_blueprint_compiler_emits_manifest_ownership_role_contracts_and_file_dag():
    enriched = ContractCompiler().enrich_blueprint(blueprint(database="mysql"))
    assert enriched["dependency_manifest"]["backend"]["manager"] == "maven"
    assert enriched["dependency_manifest"]["frontend"]["manager"] == "npm"
    assert enriched["role_contracts"]["backend"]["apis"]
    assert enriched["role_contracts"]["frontend"]["file_plan"]
    assert any(item["provides"] == ["ProductService"] for item in enriched["file_dependencies"])


def test_multi_entity_vue_file_plan_splits_managers_and_keeps_root_small():
    compiled = ContractCompiler().compile(blueprint(entities=[entity("Student"), entity("Course")], api=[
        {"entity_id": "Student", "path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
         "fields": ["id", "name"], "payload": {"name": "A"}, "identity_field": "id"},
        {"entity_id": "Course", "path": "/api/courses", "methods": ["GET", "POST", "PUT", "DELETE"],
         "fields": ["id", "name"], "payload": {"name": "B"}, "identity_field": "id"},
    ]))
    by_path = {row["path"]: row for row in compiled["file_plan"]}
    managers = {"src/components/StudentManager.vue", "src/components/CourseManager.vue"}
    assert managers.issubset(by_path)
    assert set(by_path["src/App.vue"]["depends_on_files"]) == managers
    assert set(by_path["src/App.vue"]["requires"]) == {"StudentManagerComponent", "CourseManagerComponent"}
    planned, denied = enforce_artifact_plan(
        [ArtifactFileSpec("src/App.vue", "vue", "Root", 3500)], owner="frontend", compiled_contract=compiled,
    )
    assert not denied
    selected = {item.name: item for item in planned}
    assert managers.issubset(selected)
    assert selected["src/App.vue"].estimated_tokens <= 1000
    assert all(selected[path].estimated_tokens >= 2400 for path in managers)
    assert [item.name for item in planned].index("src/App.vue") > max(
        [item.name for item in planned].index(path) for path in managers
    )
    assert set(by_path["src/main/java/com/example/app/StudentService.java"]["depends_on_files"]) == {
        "src/main/java/com/example/app/Student.java",
        "src/main/java/com/example/app/StudentRepository.java",
    }
    assert set(by_path["src/main/java/com/example/app/CourseController.java"]["depends_on_files"]) == {
        "src/main/java/com/example/app/Course.java",
        "src/main/java/com/example/app/CourseService.java",
    }


def test_single_entity_vue_plan_keeps_existing_app_component():
    by_path = {row["path"]: row for row in ContractCompiler().compile(blueprint())["file_plan"]}
    assert not any(path.startswith("src/components/") for path in by_path)
    assert by_path["src/App.vue"]["requires"] == ["ApiContract"]


def test_dependency_files_are_deterministic_and_have_no_duplicate_h2():
    compiled = ContractCompiler().compile(blueprint())
    manifest = compiled["dependency_manifest"]
    pom = render_managed_artifact("pom.xml", manifest)
    package = render_managed_artifact("package.json", manifest)
    assert pom and pom.count("<artifactId>h2</artifactId>") == 1
    assert "flyway-database-h2" not in pom
    assert json.loads(package or "{}")["dependencies"]["vue"]


def test_frontend_cannot_claim_backend_manifest():
    compiled = ContractCompiler().compile(blueprint())
    plan = [
        ArtifactFileSpec("pom.xml", "xml", "invalid", 500),
        ArtifactFileSpec("src/App.vue", "vue", "page", 1000),
    ]
    accepted, denied = enforce_artifact_plan(plan, owner="frontend", compiled_contract=compiled)
    assert "pom.xml" in denied
    assert all(item.name != "pom.xml" for item in accepted)
    assert path_allowed("backend", "pom.xml", compiled["artifact_ownership"])


def test_contract_gate_detects_cross_owner_duplicate_before_build():
    compiler = ContractCompiler()
    frozen = compiler.enrich_blueprint(blueprint())
    compiled = compiler.compile(frozen)
    context = {
        "project_blueprint": frozen,
        "compiled_contract": compiled,
        "__artifact_files__": [
            {"step_id": "backend", "name": "pom.xml", "content": "<project/>"},
            {"step_id": "frontend", "name": "pom.xml", "content": "<project/>"},
        ],
    }
    checks = {item["id"]: item for item in IntegrationGate.contract_checks(context)}
    assert checks["artifact-ownership-contract"]["status"] == "failed"


def test_mysql_production_blueprint_keeps_h2_validation_engine():
    requirement = CapabilityRouter().route("Spring Boot + Vue + MySQL 商品 CRUD")
    contract = {
        "backend_stack": "springboot", "frontend_stack": "vue", "page_mode": "spa",
        "database_mode": "mysql", "entities": [entity()], "api_contract": [],
        "required_capabilities": ["backend", "frontend", "persistence"], "crud_required": True,
    }
    value = build_project_blueprint(
        requirement.raw_requirement,
        requirement.model_dump(mode="json"),
        {"project_type": "web_app", "backend_required": True},
        contract,
    )
    assert value["database"]["production_engine"] == "mysql"
    assert value["database"]["validation_engine"] == "h2"
    assert value["database"]["compatibility_mode"] == "mysql"


def test_frozen_blueprint_change_is_recorded_not_silently_applied():
    frozen = ContractCompiler().enrich_blueprint(blueprint())
    updated = request_blueprint_change(
        frozen,
        source_agent="backend",
        reason="需要增加审计字段",
        affected_sections=["entities"],
        proposed_changes={"entities.Product.fields.updatedAt": "LocalDateTime"},
    )
    assert updated["status"] == "CHANGE_REQUESTED"
    assert "updatedAt" not in updated["entities"][0]["fields"]
    assert updated["change_requests"][0]["status"] == "OPEN"
