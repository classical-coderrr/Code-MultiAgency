import asyncio

from app.code_company.failure_classifier import FailureClassifier
from app.code_company.repair import RepairEngine
from app.workflow.artifact_validator import ArtifactValidationResult, ArtifactValidator, ValidationCheck
from app.workflow.executor import WorkflowExecutor


def failed(check_id: str, target: str, message: str, output: str = "") -> ArtifactValidationResult:
    return ArtifactValidationResult(
        "failed",
        message,
        (target,),
        (ValidationCheck(check_id, target, check_id, "failed", message, output=output),),
    )


def test_provider_configuration_failure_is_not_sent_to_a_business_agent():
    route = RepairEngine().route_node_failure(
        "frontend",
        "Provider returned HTTP 401: Incorrect API key provided",
    )

    assert route["owner_step"] == "platform"
    assert route["action"] == "pause_for_configuration"
    assert route["repairable"] is False
    assert route["retryable"] is False


def test_validation_failure_routes_to_source_owner_instead_of_tester():
    engine = RepairEngine()
    validation = failed(
        "artifact-files",
        "artifact",
        "pom.xml declares an invalid H2 dependency",
        "Maven cannot resolve flyway-database-h2",
    )

    plan = engine.plan_validation_repair(validation, attempt=1)

    assert plan["owners"] == ["backend"]
    assert plan["strategy"] == "targeted_file"
    assert plan["repairable"] is True


def test_integration_contract_failure_can_route_to_both_owners():
    engine = RepairEngine()
    validation = failed(
        "integration-api-contract",
        "artifact",
        "API contract mismatch between browser and server",
    )

    assert engine.targets(validation) == ["backend", "frontend"]


def test_browser_api_404_routes_to_backend_and_frontend():
    validation = failed(
        "browser-render",
        "frontend",
        "locator.waitFor timed out",
        "/api/candies=HTTP 404",
    )

    assert RepairEngine().targets(validation) == ["backend", "frontend"]


def test_frontend_route_mismatch_does_not_repair_passing_backend():
    validation = ArtifactValidationResult(
        "failed", "API 路径不一致", ("frontend", "backend"), (
            ValidationCheck("frontend-api-contract", "frontend", "前端 API 合同", "failed", "src/App.vue 调用了 /api/products；允许 /products"),
            ValidationCheck("frontend-proxy-contract", "frontend", "前端 Vite 代理合同", "failed", "vite.config.js 缺少 /products"),
            ValidationCheck("backend-api-contract", "backend", "后端 API 合同", "passed", "Controller 合同一致"),
            ValidationCheck("frontend-backend-route-contract", "backend", "前后端 API 路由契约", "failed", "前端 /api/products 与后端 /products 不一致"),
        ),
    )
    engine = RepairEngine()
    assert engine.targets(validation) == ["frontend"]
    assert engine.plan_validation_repair(validation, attempt=1)["owners"] == ["frontend"]


def test_candidate_that_breaks_passing_backend_gate_is_not_progress():
    before = ArtifactValidationResult("failed", "前端路径错误", ("frontend",), (
        ValidationCheck("frontend-api-contract", "frontend", "前端 API", "failed", "路径错误"),
        ValidationCheck("backend-api-contract", "backend", "后端 API", "passed", "正确"),
    ))
    after = ArtifactValidationResult("failed", "后端路径错误", ("backend",), (
        ValidationCheck("frontend-api-contract", "frontend", "前端 API", "passed", "正确"),
        ValidationCheck("backend-api-contract", "backend", "后端 API", "failed", "路径错误"),
    ))
    assert RepairEngine.made_progress(before, after) is False


def test_frontend_route_repair_selects_app_and_vite_config_only():
    files = [
        {"name": "src/App.vue", "content": "fetch('/api/products')", "step_id": "frontend"},
        {"name": "vite.config.js", "content": "proxy: { '/api': {} }", "step_id": "frontend"},
        {"name": "src/style.css", "content": "body {}", "step_id": "frontend"},
    ]
    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "[前端 API 合同] src/App.vue 调用了合同外 API /api/products；允许 /products\n"
        "[前端 Vite 代理合同] vite.config.js 缺少冻结 API 前缀 /products",
    )
    assert [item["name"] for item in selected] == ["src/App.vue", "vite.config.js"]


def test_repair_candidates_prefer_structured_related_file_evidence():
    files = [
        {"name": "src/views/admin/StudentList.vue", "content": "admin", "step_id": "frontend"},
        {"name": "src/views/mobile/StudentList.vue", "content": "mobile", "step_id": "frontend"},
        {"name": "src/App.vue", "content": "app", "step_id": "frontend"},
    ]
    check = ValidationCheck(
        "browser-crud",
        "frontend",
        "浏览器 CRUD 操作",
        "failed",
        "StudentList.vue 中的保存按钮没有更新列表。",
        evidence={"relatedFiles": ["src/views/mobile/StudentList.vue"], "path": "/rooms"},
    )

    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        check.message,
        checks=[check],
    )

    assert [item["name"] for item in selected] == ["src/views/mobile/StudentList.vue"]


def test_repair_candidates_do_not_guess_between_duplicate_basenames():
    files = [
        {"name": "src/views/admin/StudentList.vue", "content": "admin", "step_id": "frontend"},
        {"name": "src/views/mobile/StudentList.vue", "content": "mobile", "step_id": "frontend"},
    ]

    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "StudentList.vue:12 保存失败",
    )

    assert selected == []


def test_compiler_location_selects_exact_path_when_basenames_repeat():
    files = [
        {"name": "src/main/java/com/example/a/Product.java", "content": "a", "step_id": "backend"},
        {"name": "src/main/java/com/example/b/Product.java", "content": "b", "step_id": "backend"},
    ]
    log = "[ERROR] C:/tmp/build/src/main/java/com/example/b/Product.java:[17,9] cannot find symbol"

    selected = WorkflowExecutor._validation_repair_candidates(files, log)

    assert [item["name"] for item in selected] == ["src/main/java/com/example/b/Product.java"]


def test_no_progress_opens_repair_circuit_after_two_rounds():
    engine = RepairEngine()
    validation = failed("backend-build", "backend", "compile failed")
    fingerprint = engine.fingerprint(validation)

    plan = engine.plan_validation_repair(
        validation,
        attempt=3,
        previous_fingerprint=fingerprint,
        no_progress_count=2,
    )

    assert plan["circuit_open"] is True
    assert plan["repairable"] is False
    assert plan["action"] == "stop_no_progress"


def test_compiler_missing_class_is_classified_and_candidate_uses_actual_package():
    source = {"name": "src/main/java/com/example/app/ProductService.java", "step_id": "backend",
              "content": "package com.example.app;\nclass ProductService { void get() { throw new ProductNotFoundException(1L); } }"}
    log = ("[ERROR] /tmp/build/src/main/java/com/example/app/ProductService.java:[2,47] cannot find symbol\n"
           "[ERROR]   symbol: class ProductNotFoundException\n"
           "[ERROR]   location: class com.example.app.ProductService")
    candidates = WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source], log, {"backend": ["src/main/java/**"]},
    )
    assert len(candidates) == 1
    assert candidates[0]["name"] == "src/main/java/com/example/app/ProductNotFoundException.java"
    assert candidates[0]["source_file"] == source["name"]
    check = ValidationCheck("backend-test", "backend", "后端 Maven 测试", "failed", "编译失败", output=log)
    assert FailureClassifier().classify_check(check).category == "missing_declaration"
    localized = log.replace("cannot find symbol", "找不到符号").replace("symbol: class", "符号: 类")
    assert WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source], localized, {"backend": ["src/main/java/**"]},
    ) == candidates
    assert FailureClassifier().classify_check(
        ValidationCheck("backend-test", "backend", "后端 Maven 测试", "failed", "编译失败", output=localized),
    ).category == "missing_declaration"
    assert "ProductNotFoundException" in RepairEngine().failure_facts(
        ArtifactValidationResult("failed", "编译失败", ("backend",), (check,)),
    )[0]["summary"]

    blocked = WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source], log, {"backend": ["src/main/resources/**"]},
    )
    assert blocked == []
    assert WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source, {"name": candidates[0]["name"], "content": "class ProductNotFoundException {}"}],
        log, {"backend": ["src/main/java/**"]},
    ) == []
    equivalent = {"name": "src/main/java/com/example/app/ResourceNotFoundException.java",
                  "content": "package com.example.app; class ResourceNotFoundException extends RuntimeException {}"}
    assert WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source, equivalent], log, {"backend": ["src/main/java/**"]},
    ) == []
    assert WorkflowExecutor._validation_repair_candidates([source], log) == [source]
    unrelated = {"name": "src/main/java/com/example/app/RoomNotFoundException.java",
                 "content": "package com.example.app; class RoomNotFoundException extends RuntimeException {}"}
    assert WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source, unrelated], log, {"backend": ["src/main/java/**"]},
    ) == candidates


def test_missing_class_requires_compiler_location_and_local_constructor_reference():
    source = {"name": "src/main/java/com/example/app/ProductService.java", "step_id": "backend",
              "content": "package com.example.app;\nclass ProductService { List<String> names; }"}
    log = ("[ERROR] /tmp/src/main/java/com/example/app/ProductService.java:[2,12] cannot find symbol\n"
           "[ERROR] symbol: class List")
    assert WorkflowExecutor._missing_java_declaration_candidates([source], [source], log, {}) == []
    assert WorkflowExecutor._missing_java_declaration_candidates(
        [source], [source], "[ERROR] symbol: class ProductNotFoundException", {},
    ) == []


def test_missing_relative_frontend_module_is_classified_as_missing_file():
    check = ValidationCheck("frontend-build", "frontend", "前端构建", "failed",
                            "src/App.vue 引用了不存在的模块 ./components/RoomForm.vue")
    assert FailureClassifier().classify_check(check).category == "missing_declaration"
    fact = RepairEngine().failure_facts(
        ArtifactValidationResult("failed", "missing module", ("frontend",), (check,)),
    )[0]
    assert "RoomForm.vue" in fact["summary"]
    validator_error = ValidationCheck("validator-error", "frontend", "验证器内部错误", "failed",
                                      "src/App.vue 引用了不存在的模块 ./components/RoomForm.vue")
    assert FailureClassifier().classify_check(validator_error).owners == ("platform",)


def test_resolving_one_missing_symbol_can_progress_to_next_at_same_build_gate():
    def failed_symbol(symbol: str) -> ArtifactValidationResult:
        check = ValidationCheck("backend-test", "backend", "后端 Maven 测试", "failed", "编译失败",
                                output=f"[ERROR] symbol: class {symbol}")
        return ArtifactValidationResult("failed", "编译失败", ("backend",), (check,))
    assert RepairEngine.made_progress(failed_symbol("FirstException"), failed_symbol("SecondException"))
    assert not RepairEngine.made_progress(failed_symbol("FirstException"), failed_symbol("FirstException"))


def test_repair_context_excludes_unrelated_large_files():
    files = [
        {"name": "pom.xml", "content": "pom", "step_id": "backend"},
        {"name": "src/main/java/com/example/StudentController.java", "content": "controller", "step_id": "backend"},
        {"name": "src/main/java/com/example/StudentService.java", "content": "service", "step_id": "backend"},
        {"name": "src/views/Unrelated.vue", "content": "unrelated", "step_id": "frontend"},
        {"name": "notes/large.txt", "content": "x" * 50000, "step_id": "reviewer"},
    ]

    selected = WorkflowExecutor._select_repair_context_files(
        files,
        target="backend",
        candidate_name="src/main/java/com/example/StudentController.java",
        diagnostics="StudentController.java does not compile",
    )
    names = [item["name"] for item in selected]

    assert "pom.xml" in names
    assert "src/main/java/com/example/StudentService.java" in names
    assert "src/views/Unrelated.vue" not in names
    assert "notes/large.txt" not in names
    assert "src/main/java/com/example/StudentController.java" not in names


def test_validator_fast_fails_before_build_commands():
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    result = asyncio.run(
        ArtifactValidator().validate(
            {
                "__artifact_files__": [
                    {"name": "package.json", "content": '{"scripts": {}}'},
                    {"name": "index.html", "content": "<html><body></body></html>"},
                    {"name": "src/main.js", "content": "console.log('ok')"},
                ]
            },
            {"build": True, "startup": True, "fail_fast": True},
            emit=emit,
        )
    )

    assert result.status == "failed"
    assert any(event_type == "step.validation_stage_started" and payload["stage"] == "preflight" for event_type, payload in events)
    assert any(event_type == "step.validation_stage_completed" and payload["stage"] == "preflight" for event_type, payload in events)
    assert any(event_type == "step.validation_short_circuited" for event_type, _ in events)
    assert not any(check.id in {"frontend-install", "frontend-build", "frontend-startup"} for check in result.checks)
