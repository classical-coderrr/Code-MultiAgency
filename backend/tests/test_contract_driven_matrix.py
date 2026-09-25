"""L1-L3 contract routing and deliberate cross-agent conflict regressions."""

import pytest

from app.code_company.contract_compiler import ContractCompiler
from app.code_company.contract_conformance import check_contract_conformance
from app.code_company.artifact_contracts import enforce_artifact_plan
from app.workflow.artifact_generation import ArtifactFileSpec
from app.workflow.capability_router import CapabilityRouter
from app.workflow.clarification import RequirementClarificationService
from app.workflow.delivery_contract import build_delivery_contract
from app.workflow.integration_gate import IntegrationGate
from app.workflow.platform_contracts import build_project_blueprint


@pytest.mark.parametrize(
    ("requirement", "backend", "frontend", "database"),
    [
        ("只做纯前端 HTML/CSS/JS 学生信息增删改查，使用 localStorage，无需后端和数据库", False, "html", "none"),
        ("使用 Spring Boot + HTML + H2 开发学生管理系统，支持增删改查", True, "html", "h2"),
        ("使用 Spring Boot + Vue + H2 开发学生管理系统，支持增删改查", True, "vue", "h2"),
        ("使用 Spring Boot + Vue + MySQL 开发学生管理系统，支持增删改查", True, "vue", "mysql"),
    ],
)
def test_l1_l2_l3_blueprint_has_one_authoritative_route(requirement, backend, frontend, database):
    spec = CapabilityRouter().route(requirement)
    decision = {
        "project_type": "web_app" if backend else "static_html",
        "backend_required": backend,
        "required_capabilities": ["frontend", *(["backend", "persistence"] if backend else [])],
        "delivery_contract": {},
    }
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    blueprint = build_project_blueprint(requirement, spec.model_dump(mode="json"), decision, contract)
    compiler = ContractCompiler()
    frozen = compiler.enrich_blueprint(blueprint)
    compiled = compiler.compile(frozen)
    assert frozen["backend"]["required"] is backend
    assert frozen["frontend"]["stack"] == frontend
    assert frozen["database"]["mode"] == database
    assert ("backend" in compiled["execution_policy"]["skip_steps"]) is not backend
    assert ("database" in compiled["execution_policy"]["skip_steps"]) is (database == "none")
    if backend:
        assert contract["api_contract"] and contract["entities"]
        assert "/api/students/{id}" in compiled["openapi"]["paths"]
    if database == "mysql":
        assert frozen["database"]["validation_engine"] == "h2"


def _compiled():
    requirement = "使用 Spring Boot + Vue + H2 开发学生管理系统，支持增删改查"
    spec = CapabilityRouter().route(requirement)
    decision = {"project_type": "web_app", "backend_required": True, "required_capabilities": ["frontend", "backend", "persistence"], "delivery_contract": {}}
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    frozen = ContractCompiler().enrich_blueprint(build_project_blueprint(requirement, spec.model_dump(mode="json"), decision, contract))
    return frozen, ContractCompiler().compile(frozen)


def test_frontend_unknown_api_is_rejected_with_owner():
    _, compiled = _compiled()
    checks = check_contract_conformance(compiled, [{"step_id": "frontend", "name": "src/App.vue", "content": "fetch('/api/student/list')"}])
    failure = next(item for item in checks if item["id"] == "frontend-api-contract")
    assert failure["status"] == "failed"
    assert failure["target"] == "frontend"
    assert "/api/student/list" in failure["message"]


def test_encoded_id_template_literal_matches_frozen_detail_route():
    _, compiled = _compiled()
    source = (
        "const API_BASE = '/api/students'; "
        "fetch(`${API_BASE}/${encodeURIComponent(id)}`, { method: 'PUT' }); "
        "fetch(`/api/students/${encodeURIComponent(id)}`, { method: 'DELETE' });"
    )
    checks = check_contract_conformance(compiled, [
        {"step_id": "frontend", "name": "src/App.vue", "content": source},
    ])
    api = next(item for item in checks if item["id"] == "frontend-api-contract")
    assert api["status"] == "passed"
    assert "${encodeURIComponent" not in api["message"]


def test_explicit_non_api_route_checks_frontend_backend_and_vite_proxy():
    compiled = {
        "openapi": {"paths": {
            "/products": {"get": {}, "post": {}},
            "/products/{id}": {"get": {}, "put": {}, "delete": {}},
        }},
        "role_contracts": {"frontend": {"frontend": {"stack": "vue"}}},
        "file_plan": [],
    }
    controller = {
        "step_id": "backend", "name": "src/main/java/ProductController.java",
        "content": '''@RestController
        @RequestMapping("/products") class ProductController {
          @GetMapping Object list() { return null; }
          @PostMapping Object create() { return null; }
          @GetMapping("/{id}") Object detail() { return null; }
          @PutMapping("/{id}") Object update() { return null; }
          @DeleteMapping("/{id}") Object delete() { return null; }
        }''',
    }
    frontend = {"step_id": "frontend", "name": "src/App.vue", "content": "fetch('/api/products')"}
    vite = {"step_id": "frontend", "name": "vite.config.js", "content": "proxy: { '/api': { target: process.env.VITE_API_PROXY } }"}
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [controller, frontend, vite])}
    assert checks["backend-api-contract"]["status"] == "passed"
    assert checks["frontend-api-contract"]["status"] == "failed"
    assert checks["frontend-proxy-contract"]["status"] == "failed"

    frontend["content"] = "fetch('/products')"
    vite["content"] = "proxy: { '/products': { target: process.env.VITE_API_PROXY } }"
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [controller, frontend, vite])}
    assert checks["backend-api-contract"]["status"] == "passed"
    assert checks["frontend-api-contract"]["status"] == "passed"
    assert checks["frontend-proxy-contract"]["status"] == "passed"


def test_compiled_frontend_role_lists_contract_proxy_prefixes():
    frozen, compiled = _compiled()
    assert frozen["role_contracts"]["frontend"]["proxy_paths"] == ["/api"]
    assert compiled["role_contracts"]["frontend"]["proxy_paths"] == ["/api"]


def test_missing_backend_java_class_is_caught_before_build():
    _, compiled = _compiled()
    checks = check_contract_conformance(compiled, [{"step_id": "backend", "name": "pom.xml", "content": "<project/>"}])
    failure = next(item for item in checks if item["id"] == "frozen-file-plan")
    assert failure["status"] == "failed"
    assert "StudentService.java" in failure["message"]


def test_backend_and_frontend_cannot_both_register_package_json():
    frozen, compiled = _compiled()
    checks = IntegrationGate.contract_checks({
        "project_blueprint": frozen, "compiled_contract": compiled,
        "__artifact_files__": [
            {"step_id": "frontend", "name": "package.json", "content": "{}"},
            {"step_id": "backend", "name": "package.json", "content": "{}"},
        ],
    })
    failure = next(item for item in checks if item["id"] == "artifact-ownership-contract")
    assert failure["status"] == "failed"
    assert "重复登记" in failure["message"]


def test_database_schema_missing_entity_field_is_caught():
    _, compiled = _compiled()
    checks = check_contract_conformance(compiled, [{
        "step_id": "database", "name": "src/main/resources/schema.sql",
        "content": "CREATE TABLE students (id BIGINT PRIMARY KEY);",
    }])
    failure = next(item for item in checks if item["id"] == "database-entity-contract")
    assert failure["status"] == "failed"
    assert "name" in failure["message"]


def test_explicit_room_and_booking_entities_keep_both_user_named_crud_routes():
    requirement = (
        "开发酒店客房与预订管理系统，Spring Boot 3 + Vue 3/Vite + JPA/H2。"
        "Room 实体字段 id(Long)、number(String)；Booking 实体字段 id(Long)、roomId(Long)、guestName(String)。"
        "两实体分别提供 /api/rooms、/api/bookings 及各自 /{id} 的 CRUD。"
    )
    spec = CapabilityRouter().route(requirement)
    assert set(spec.primary_entities) == {"Room", "Booking"}
    decision = {
        "project_type": "web_app", "backend_required": True,
        "required_capabilities": ["frontend", "backend", "persistence"],
        "delivery_contract": {
            "entities": [
                {"name": "Room", "fields": {"id": "Long", "number": "String"}},
                {"name": "Booking", "fields": {"id": "Long", "roomId": "Long", "guestName": "String"}},
            ],
            "api_contract": [
                {"entity_id": "Room", "path": "/api/rooms", "methods": ["GET", "POST", "PUT", "DELETE"]},
                {"entity_id": "Booking", "path": "/api/bookings", "methods": ["GET", "POST", "PUT", "DELETE"]},
            ],
        },
    }
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    assert {row["path"] for row in contract["api_contract"]} == {"/api/rooms", "/api/bookings"}


def test_default_spring_physical_naming_rejects_camelcase_sql_column():
    compiled = {
        "database_schema": {
            "tables": {"order": {"entity_id": "Order", "columns": {"id": "Long", "productId": "Long"}}},
        },
    }
    entity = {
        "step_id": "backend", "name": "src/main/java/com/example/app/Order.java",
        "content": '@Entity @Table(name="order") class Order { private Long id; private Long productId; }',
    }
    sql = {
        "step_id": "database", "name": "src/main/resources/schema.sql",
        "content": 'CREATE TABLE "order" (id BIGINT PRIMARY KEY, productId BIGINT NOT NULL);',
    }

    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity, sql])}
    assert checks["database-entity-contract"]["status"] == "passed"
    assert checks["database-physical-column-contract"]["status"] == "failed"
    assert checks["database-physical-column-contract"]["target"] == "database"
    assert checks["database-physical-column-contract"]["evidence"]["relatedFiles"] == [sql["name"]]

    sql["content"] = sql["content"].replace("productId BIGINT", "product_id BIGINT")
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity, sql])}
    assert checks["database-physical-column-contract"]["status"] == "passed"


def test_order_entity_table_must_quote_frozen_sql_keyword_without_renaming_it():
    compiled = {
        "database_schema": {"tables": {"order": {"entity_id": "Order", "columns": {"id": "Long"}}}},
        "backend_dto": {"Order": "Order(Long id)"},
    }
    entity = {
        "step_id": "backend", "name": "src/main/java/com/example/app/Order.java",
        "content": '@Entity @Table(name="order") class Order { private Long id; }',
    }
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity])}
    assert checks["backend-table-contract"]["status"] == "passed"
    assert checks["backend-reserved-table-contract"]["status"] == "failed"
    assert checks["backend-reserved-table-contract"]["evidence"]["relatedFiles"] == [entity["name"]]

    entity["content"] = r'@Entity @Table(name="\"order\"") class Order { private Long id; }'
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity])}
    assert checks["backend-table-contract"]["status"] == "passed"
    assert checks["backend-reserved-table-contract"]["status"] == "passed"

    entity["content"] = '@Entity class Order { private Long id; }'
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity])}
    assert checks["backend-reserved-table-contract"]["status"] == "failed"


def test_backend_entity_field_type_must_match_database_contract():
    _, compiled = _compiled()
    checks = check_contract_conformance(compiled, [{
        "step_id": "backend", "name": "src/main/java/com/example/app/Student.java",
        "content": "@Entity class Student { private Long id; private Integer name; }",
    }])
    failure = next(item for item in checks if item["id"] == "backend-entity-contract")
    assert failure["status"] == "failed"
    assert "name" in failure["message"]


def test_backend_explicit_table_name_must_match_frozen_database_contract():
    _, compiled = _compiled()
    expected_table = next(iter(compiled["database_schema"]["tables"]))
    entity = {
        "step_id": "backend", "name": "src/main/java/com/example/app/Student.java",
        "content": '@Entity @Table(name = "wrong_students") class Student { private Long id; private String name; }',
    }
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity])}
    assert checks["backend-table-contract"]["status"] == "failed"
    assert checks["backend-table-contract"]["target"] == "backend"
    assert "wrong_students" in checks["backend-table-contract"]["message"]
    assert expected_table in checks["backend-table-contract"]["message"]

    entity["content"] = entity["content"].replace("wrong_students", expected_table)
    checks = {item["id"]: item for item in check_contract_conformance(compiled, [entity])}
    assert checks["backend-table-contract"]["status"] == "passed"


def test_backend_controller_missing_contract_method_is_caught():
    _, compiled = _compiled()
    source = '''
        @RestController
        @RequestMapping("/api/students")
        class StudentController {
            @GetMapping public Object list() { return null; }
            @PostMapping public Object create() { return null; }
        }
    '''
    checks = check_contract_conformance(compiled, [{
        "step_id": "backend", "name": "src/main/java/com/example/app/StudentController.java", "content": source,
    }])
    failure = next(item for item in checks if item["id"] == "backend-api-contract")
    assert failure["status"] == "failed"
    assert "DELETE" in failure["message"]


def test_frontend_form_and_import_must_match_frozen_contract_and_files():
    _, compiled = _compiled()
    checks = check_contract_conformance(compiled, [{
        "step_id": "frontend", "name": "src/App.vue",
        "content": '''<template><input v-model="form.fakeField"></template>
        <script setup>import Client from './missing-client';</script>''',
    }])
    by_id = {item["id"]: item for item in checks}
    assert by_id["frontend-field-contract"]["status"] == "failed"
    assert by_id["frontend-import-contract"]["status"] == "failed"


def test_file_plan_adds_missing_classes_and_orders_dependencies_before_consumers():
    _, compiled = _compiled()
    planned, denied = enforce_artifact_plan(
        [ArtifactFileSpec("src/main/java/com/example/student/StudentController.java", "java", "控制器", 900)],
        owner="backend", compiled_contract=compiled,
    )
    assert not denied
    names = [item.name for item in planned]
    assert "src/main/java/com/example/student/StudentService.java" in names
    assert "src/main/java/com/example/student/StudentRepository.java" in names
    assert names.index("src/main/java/com/example/student/StudentRepository.java") < names.index("src/main/java/com/example/student/StudentService.java") < names.index("src/main/java/com/example/student/StudentController.java")


def test_file_plan_rejects_missing_provider_and_dependency_cycle():
    _, compiled = _compiled()
    broken = dict(compiled)
    broken["file_plan"] = [{"path": "src/App.vue", "owner": "frontend", "provides": ["AppComponent"], "requires": ["MissingSymbol"]}]
    with pytest.raises(ValueError, match="MissingSymbol"):
        enforce_artifact_plan([ArtifactFileSpec("src/App.vue", "vue", "page", 500)], owner="frontend", compiled_contract=broken)
    broken["file_plan"] = [
        {"path": "src/App.vue", "owner": "frontend", "provides": ["A"], "depends_on_files": ["src/main.js"]},
        {"path": "src/main.js", "owner": "frontend", "provides": ["B"], "depends_on_files": ["src/App.vue"]},
    ]
    with pytest.raises(ValueError, match="存在环"):
        enforce_artifact_plan([ArtifactFileSpec("src/App.vue", "vue", "page", 500)], owner="frontend", compiled_contract=broken)


def test_compiler_rejects_duplicate_paths_before_freezing_parallel_owner_plan():
    blueprint = {
        "backend": {"stack": "springboot"},
        "frontend": {"stack": "none"},
        "database": {"mode": "none"},
        "entities": [{"id": "Application"}, {"id": "application"}],
    }
    with pytest.raises(ValueError, match="duplicate path"):
        ContractCompiler()._file_plan(blueprint)


def test_explicit_student_requirement_overrides_wrong_model_product_entity():
    requirement = "使用 Spring Boot 和 Vue 开发学生管理系统，完成增删改查"
    spec = CapabilityRouter().route(requirement)
    decision = {
        "project_type": "web_app", "backend_required": True,
        "required_capabilities": ["frontend", "backend", "persistence"],
        "delivery_contract": {
            "entities": [{"name": "Product", "table": "products", "fields": {"id": "long", "name": "string"}}],
            "api_contract": [{"entity_id": "Product", "path": "/api/products", "methods": ["GET", "POST"]}],
        },
    }
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    assert [item["name"] for item in contract["entities"]] == ["Student"]
    assert contract["api_contract"][0]["path"] == "/api/students"
    assert any("Product" in item for item in contract["assumptions"])


def test_crud_collection_model_row_always_freezes_detail_get_route():
    requirement = "Spring Boot + HTML 学生信息增删改查"
    spec = CapabilityRouter().route(requirement)
    decision = {
        "project_type": "web_app", "backend_required": True,
        "required_capabilities": ["frontend", "backend", "persistence"],
        "delivery_contract": {
            "entities": [{"name": "Student", "table": "students", "fields": {"id": "long", "name": "string"}}],
            "api_contract": [{"entity_id": "Student", "path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"], "detail_required": False}],
        },
    }
    contract = build_delivery_contract(requirement, decision, requirement_spec=spec.model_dump(mode="json"))
    assert contract["api_contract"][0]["detail_required"] is True
    blueprint = build_project_blueprint(requirement, spec.model_dump(mode="json"), decision, contract)
    compiled = ContractCompiler().compile(blueprint)
    assert "get" in compiled["openapi"]["paths"]["/api/students/{id}"]


def test_english_username_does_not_create_unrequested_user_entity():
    spec = CapabilityRouter().route("Build a Student CRUD app with a username field using Spring Boot and Vue")
    assert spec.primary_entities == ["Student"]
    plural = CapabilityRouter().route("Build a students CRUD app using Spring Boot and Vue")
    assert plural.primary_entities == ["Student"]


def test_explicit_springboot_html_crud_uses_h2_default_without_second_question():
    spec = CapabilityRouter().route("开发一个学生管理系统，用 Spring Boot 和 HTML 作为技术栈，要体现增删改查功能。")
    state, request = RequirementClarificationService().assess(spec)
    assert state == "ASSUMPTION_ALLOWED"
    assert request is None
    assert spec.needs_clarification is False
    assert spec.safe_defaults["production_database"] == "h2"
