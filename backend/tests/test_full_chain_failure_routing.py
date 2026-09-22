import asyncio

from app.code_company.contract_conformance import _controller_routes, _entity_fields_with_mapped_superclass, _frontend_paths
from app.code_company.repair import RepairEngine
from app.code_company.verification import VerificationEngine
from app.workflow.artifact_validator import ArtifactValidationResult, ArtifactValidator, ValidationCheck


def test_requirement_routing_error_is_not_dispatched_to_code_agent():
    route = RepairEngine().route_node_failure(
        "requirement", "invalid capability boundary", error_type="ValueError",
    )
    assert route["category"] == "requirement_routing"
    assert route["owner_step"] == "requirement"
    assert route["repairable"] is False


def test_unexpected_validator_exception_is_owned_by_platform_and_keeps_evidence():
    class BrokenValidator:
        async def validate(self, context, config=None, *, emit=None):
            raise TypeError("invalid typed test fixture")

    engine = VerificationEngine(artifact_validator=BrokenValidator())
    result = asyncio.run(engine.validate_artifacts({"__artifact_files__": []}))
    assert result.status == "failed"
    assert result.checks[0].id == "validator-error"
    assert result.checks[0].evidence["exceptionType"] == "TypeError"
    plan = RepairEngine().plan_validation_repair(result, attempt=1)
    assert plan["owners"] == []
    assert plan["repairable"] is False
    assert plan["action"] == "escalate_contract_or_platform"
    facts = RepairEngine().failure_facts(result)
    assert facts[0]["owner"] == "platform"
    assert facts[0]["code"] == "validator-error"


def test_mapped_superclass_id_is_not_falsely_reported_as_missing():
    files = {
        "src/main/java/com/example/DomainEntity.java": {
            "content": "@MappedSuperclass public abstract class DomainEntity { protected Long id; }",
        },
        "src/main/java/com/example/Product.java": {
            "content": "@Entity public class Product extends DomainEntity { private String name; }",
        },
    }
    actual = _entity_fields_with_mapped_superclass(
        "src/main/java/com/example/Product.java", files["src/main/java/com/example/Product.java"]["content"], files,
    )
    assert actual == {"name": "String", "id": "Long"}


def test_abstract_controller_crud_methods_are_checked_under_concrete_route():
    files = {
        "src/main/java/com/example/DomainEntityController.java": {
            "step_id": "backend",
            "content": "public abstract class DomainEntityController<T> { @GetMapping public T all() {} @GetMapping(\"/{id}\") public T one() {} @PostMapping public T create() {} @PutMapping(\"/{id}\") public T update() {} @DeleteMapping(\"/{id}\") public T delete() {} }",
        },
        "src/main/java/com/example/StudentController.java": {
            "step_id": "backend",
            "content": "@RestController @RequestMapping(\"/api/students\") public class StudentController extends DomainEntityController<Student> {}",
        },
    }
    expected = {"/api/students", "/api/students/{id}"}
    routes = _controller_routes(files, expected)
    assert {method for method, path in routes if path == "/api/students/{id}"} == {"GET", "PUT", "DELETE"}
    assert {method for method, path in routes if path == "/api/students"} == {"GET", "POST"}


def test_delivery_gate_accepts_inherited_item_routes_without_weakening_crud():
    files = {
        "src/main/java/com/example/DomainEntityController.java":
            "public abstract class DomainEntityController<T> { @GetMapping(\"/{id}\") public T one() {} @PutMapping(\"/{id}\") public T update() {} @DeleteMapping(\"/{id}\") public T delete() {} }",
        "src/main/java/com/example/StudentController.java":
            "@RestController @RequestMapping(\"/api/students\") public class StudentController extends DomainEntityController<Student> {}",
    }
    contract = {"backend_stack": "springboot", "validation_database": "none", "crud_required": True,
                "api_contract": [{"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
                                  "detail_required": True, "payload": {"name": "A"}}]}
    checks = ArtifactValidator._delivery_contract_structure(files, contract)
    item_gate = next(check for check in checks if check.id == "backend-item-route-contract")
    assert item_gate.status == "passed"
    files["src/main/java/com/example/DomainEntityController.java"] = files["src/main/java/com/example/DomainEntityController.java"].replace('@DeleteMapping("/{id}")', '@DeleteMapping')
    item_gate = next(check for check in ArtifactValidator._delivery_contract_structure(files, contract)
                     if check.id == "backend-item-route-contract")
    assert item_gate.status == "failed"


def test_api_base_prefix_is_not_mistaken_for_collection_endpoint():
    files = {"src/App.vue": {"step_id": "frontend", "content": "const baseUrl = '/api'; fetch(`${baseUrl}/students`); fetch('/api/students')"}}
    found = _frontend_paths(files, {"/api/students", "/api/students/{id}"})
    assert "/api" not in found
    assert "/api/students" in found
    files["src/App.vue"]["content"] = "fetch('/api')"
    assert "/api" in _frontend_paths(files, {"/api/students"})


def test_frozen_contract_defect_never_rewrites_frontend_or_backend():
    result = ArtifactValidationResult("failed", "合同无效", ("frontend", "backend"), (
        ValidationCheck("delivery-api-contract", "backend", "冻结合同", "failed", "冻结合同缺少 entity_id"),
        ValidationCheck("frontend-api-contract", "frontend", "前端接口", "failed", "路径与错误合同不一致"),
    ))
    plan = RepairEngine().plan_validation_repair(result, attempt=1)
    assert plan["owners"] == []
    assert plan["repairable"] is False
    assert RepairEngine().targets(result) == ["frontend"]
