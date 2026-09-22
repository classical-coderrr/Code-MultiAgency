import asyncio
import json
import sys

import httpx

from app.agents.registry import AgentRegistry
from app.repositories.sqlite import SQLiteRepository
from app.services.artifacts import ArtifactService
from app.workflow.artifact_validator import ArtifactValidationResult, ArtifactValidator, ValidationCheck
from app.workflow.context import WorkflowContext
from app.workflow.dag import build_dag
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import RunState, WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, StepStatus, WorkflowDefinition


def _result(status: str) -> ArtifactValidationResult:
    check_status = "passed" if status == "passed" else "failed"
    check = ValidationCheck("stub", "artifact", "Stub validation", check_status, status)
    return ArtifactValidationResult(status, status, ("frontend",), (check,))


def test_frontend_validation_requires_standard_source_tree():
    validator = ArtifactValidator()
    files = [
        {"name": "package.json", "content": json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vue": "^3.0.0"}})},
        {"name": "index.html", "content": "<!doctype html><html><body><div id='app'></div></body></html>"},
        {"name": "main.js", "content": "import { createApp } from 'vue'"},
        {"name": "App.vue", "content": "<template><main /></template>"},
    ]

    result = asyncio.run(validator.validate({"__artifact_files__": files}, {"build": False, "startup": False}))

    assert result.status == "failed"
    assert any(check.id == "frontend-entry" and check.status == "failed" for check in result.checks)


def test_nested_frontend_and_spring_files_pass_structural_validation():
    validator = ArtifactValidator()
    files = [
        {"name": "package.json", "content": json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"vue": "^3.0.0"}})},
        {"name": "index.html", "content": "<!doctype html><html><body><div id='app'></div></body></html>"},
        {"name": "src/main.js", "content": "import { createApp } from 'vue'"},
        {"name": "src/App.vue", "content": "<template><main /></template>"},
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/studentmanagement/Application.java", "content": "package com.example.studentmanagement; class Application {}"},
        {"name": "src/main/resources/application.yml", "content": "server:\n  port: 8080"},
    ]

    result = asyncio.run(validator.validate({"__artifact_files__": files}, {"build": False, "startup": False}))

    assert result.status == "passed"
    assert {check.target for check in result.checks} == {"frontend", "backend"}


def test_maven_test_sources_are_valid_standard_java_layout():
    validator = ArtifactValidator()
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {
            "name": "src/main/java/com/example/Application.java",
            "content": "package com.example; public class Application {}",
        },
        {
            "name": "src/test/java/com/example/ApplicationTest.java",
            "content": "package com.example; public class ApplicationTest {}",
        },
    ]

    result = asyncio.run(validator.validate({"__artifact_files__": files}, {"build": False, "startup": False}))

    assert result.status == "passed"
    assert any(check.id == "backend-test-java-layout" and check.status == "passed" for check in result.checks)
    assert not any(check.id == "backend-root-java" for check in result.checks)


def test_artifact_path_traversal_is_rejected_before_commands():
    validator = ArtifactValidator()

    result = asyncio.run(validator.validate({"__artifact_files__": [{"name": "../package.json", "content": "{}"}]}))

    assert result.status == "failed"
    assert result.checks[0].id == "artifact-files"


def test_command_and_startup_validation_work_on_current_event_loop(tmp_path):
    validator = ArtifactValidator()

    async def exercise():
        command = await validator._run_command(sys.executable, ["-c", "print('validator-ok')"], tmp_path, 5)
        checks = []

        async def record(check):
            checks.append(check)

        await validator._validate_startup(
            tmp_path,
            sys.executable,
            ["-m", "http.server", "{port}", "--bind", "127.0.0.1"],
            "frontend",
            "测试 HTTP 启动",
            ("/",),
            5,
            record,
        )
        return command, checks

    outcome, checks = asyncio.run(exercise())

    assert outcome.returncode == 0
    assert "validator-ok" in outcome.output
    assert checks[0].status == "passed"


def test_startup_validation_does_not_accept_404_as_success(tmp_path):
    validator = ArtifactValidator()

    async def exercise():
        checks = []

        async def record(check):
            checks.append(check)

        await validator._validate_startup(
            tmp_path,
            sys.executable,
            ["-m", "http.server", "{port}", "--bind", "127.0.0.1"],
            "backend",
            "HTTP entry point",
            ("/missing-entry",),
            1,
            record,
        )
        return checks

    checks = asyncio.run(exercise())

    assert checks[0].status == "failed"


def test_frontend_backend_query_parameter_contract_detects_mismatch():
    files = {
        "src/main/resources/static/index.html": """
            <script>
              const params = new URLSearchParams();
              params.append('name', name);
              params.append('className', className);
            </script>
        """,
        "src/main/java/com/example/StudentController.java": """
            @GetMapping
            public List<Student> list(
                @RequestParam(value = "keyword", required = false) String keyword
            ) { return service.search(keyword); }
        """,
    }

    checks = ArtifactValidator._integration_contract(files)

    assert len(checks) == 1
    assert checks[0].status == "failed"
    assert "className" in checks[0].message
    assert "name" in checks[0].message
    assert "keyword" in checks[0].message


def test_spring_persistence_requires_h2_for_isolated_integration_tests():
    files = {
        "pom.xml": """
            <project><dependencies><dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-data-jpa</artifactId>
            </dependency></dependencies></project>
        """,
        "src/main/resources/application.properties": "spring.datasource.url=jdbc:mysql://localhost/demo",
    }

    missing = ArtifactValidator._spring_h2_contract(files)
    assert missing is not None
    assert missing.status == "failed"
    assert "pom.xml" in missing.message

    files["pom.xml"] = files["pom.xml"].replace(
        "</dependencies>",
        "<dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency></dependencies>",
    )
    present = ArtifactValidator._spring_h2_contract(files)
    assert present is not None
    assert present.status == "passed"

    args = ArtifactValidator._spring_h2_test_arguments(
        {
            **files,
            "src/main/resources/schema.sql": "create table student(id bigint primary key);",
        }
    )
    assert any("jdbc:h2:mem:agent_team_test" in item for item in args)
    assert "--spring.sql.init.mode=always" in args


def test_database_startup_failures_are_routed_back_to_database_agent():
    assert ArtifactValidator._is_database_startup_failure(
        "Flyway migration failed in db/migration/V1__schema.sql: syntax error in SQL statement"
    )
    assert not ArtifactValidator._is_database_startup_failure(
        "Cannot load driver class: org.h2.Driver"
    )


def test_frontend_backend_route_contract_requires_matching_rest_crud_api():
    files = {
        "src/main/resources/static/app.js": """
            const API_BASE = "/api/students";
            fetch(API_BASE);
            fetch(API_BASE, {method: "POST"});
            fetch(`${API_BASE}/1`, {method: "PUT"});
            fetch(`${API_BASE}/1`, {method: "DELETE"});
        """,
        "src/main/java/com/example/StudentController.java": """
            @Controller
            @RequestMapping("/students")
            public class StudentController {
              @GetMapping public String list() { return "students/list"; }
              @PostMapping public String create() { return "redirect:/students"; }
            }
        """,
    }

    failed = ArtifactValidator._integration_contract(files)
    route_failure = next(check for check in failed if check.id == "frontend-backend-route-contract")
    assert route_failure.status == "failed"
    assert "/api/students" in route_failure.message
    assert "@RestController" in route_failure.message
    assert "PUT" in route_failure.message
    assert "DELETE" in route_failure.message

    files["src/main/java/com/example/StudentController.java"] = """
        @RestController
        @RequestMapping("/api/students")
        public class StudentController {
          @GetMapping public Object list() { return null; }
          @PostMapping public Object create() { return null; }
          @PutMapping("/{id}") public Object update() { return null; }
          @DeleteMapping("/{id}") public void delete() {}
        }
    """
    passed = ArtifactValidator._integration_contract(files)
    route_success = next(check for check in passed if check.id == "frontend-backend-route-contract")
    assert route_success.status == "passed"


def test_frontend_backend_route_contract_checks_non_api_prefix():
    files = {
        "src/App.vue": "fetch('/products')",
        "src/main/java/ProductController.java": '''
            @RestController
            @RequestMapping("/products")
            public class ProductController {
                @GetMapping public Object list() { return null; }
            }
        ''',
    }
    result = ArtifactValidator._integration_contract(files)
    route = next(check for check in result if check.id == "frontend-backend-route-contract")
    assert route.status == "passed"
    files["src/App.vue"] = "fetch('/api/products')"
    result = ArtifactValidator._integration_contract(files)
    route = next(check for check in result if check.id == "frontend-backend-route-contract")
    assert route.status == "failed"


def test_static_html_frontend_repair_never_requires_npm_manifest():
    files = {"index.html": "<!doctype html><html><body>hello</body></html>", "script.js": ""}
    assert ArtifactValidator._targets(files, {"targets": ["frontend"]}) == ()
    files["pom.xml"] = "<project></project>"
    assert ArtifactValidator._targets(files, {"targets": ["frontend"]}) == ("backend",)


def test_crud_probe_preserves_decimal_string_type_for_bigdecimal():
    assert ArtifactValidator._mutated_contract_value("9.90") == "9.91"
    assert ArtifactValidator._mutated_contract_value("0.01") == "0.02"


def test_delivery_contract_requires_item_routes_for_update_and_delete():
    contract = {
        "backend_stack": "springboot",
        "crud_required": True,
        "api_contract": [{
            "path": "/api/students",
            "methods": ["GET", "POST", "PUT", "DELETE"],
            "payload": {"name": "张三"},
        }],
    }
    files = {
        "src/main/java/com/example/StudentController.java": """
            @RestController
            @RequestMapping("/api/students")
            public class StudentController {
              @GetMapping public Object list() { return null; }
              @PostMapping public Object create() { return null; }
              @PutMapping public Object update() { return null; }
              @DeleteMapping public void delete() {}
            }
        """,
    }

    failed = ArtifactValidator._delivery_contract_structure(files, contract)
    item_routes = next(check for check in failed if check.id == "backend-item-route-contract")
    assert item_routes.status == "failed"
    assert "PUT /api/students/{id}" in item_routes.message
    assert "StudentController.java" in item_routes.output

    files["src/main/java/com/example/StudentController.java"] = """
        @RestController
        @RequestMapping("/api/students")
        public class StudentController {
          @GetMapping public Object list() { return null; }
          @PostMapping public Object create() { return null; }
          @PutMapping("/{studentId}") public Object update() { return null; }
          @DeleteMapping("/{studentId}") public void delete() {}
        }
    """
    passed = ArtifactValidator._delivery_contract_structure(files, contract)
    assert next(check for check in passed if check.id == "backend-item-route-contract").status == "passed"


def test_h2_crud_probe_executes_complete_http_cycle():
    files = {
        "src/app.js": """
            const API_BASE = "/api/students";
            fetch(API_BASE);
            fetch(API_BASE, {method: "POST"});
            fetch(`${API_BASE}/1`, {method: "PUT"});
            fetch(`${API_BASE}/1`, {method: "DELETE"});
        """,
        "src/main/java/com/example/Student.java": """
            @Entity class Student {
              private Long id;
              private String studentNo;
              private String name;
            }
        """,
    }
    spec = ArtifactValidator._spring_crud_spec(files)
    assert spec is not None

    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payload = json.loads(request.content)
            payload["id"] = 1
            records[:] = [payload]
            return httpx.Response(201, json=payload)
        if request.method == "PUT":
            payload = json.loads(request.content)
            payload["id"] = 1
            records[:] = [payload]
            return httpx.Response(200, json=payload)
        if request.method == "DELETE":
            records.clear()
            return httpx.Response(204)
        return httpx.Response(200, json=records)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ArtifactValidator._probe_spring_crud(client, "http://test", spec)

    check = asyncio.run(exercise())
    assert check.status == "passed"
    assert "POST、GET、PUT、DELETE" in check.message


def test_crud_probe_email_is_unique_without_mutating_frozen_contract():
    from app.workflow.artifact_validator import _SpringCrudSpec

    payload = {"name": "张三", "email": "zhangsan@example.com"}
    spec = _SpringCrudSpec("/api/students", payload, "name", frozenset({"GET", "POST", "PUT", "DELETE"}))
    records = []
    submitted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            item = json.loads(request.content)
            submitted.append(item)
            if item["email"] == "zhangsan@example.com":
                return httpx.Response(500, json={"error": "seed email collision"})
            item["id"] = 1
            records[:] = [item]
            return httpx.Response(201, json=item)
        if request.method == "PUT":
            item = json.loads(request.content)
            item["id"] = 1
            records[:] = [item]
            return httpx.Response(200, json=item)
        if request.method == "DELETE":
            records.clear()
            return httpx.Response(204)
        return httpx.Response(200, json=records)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ArtifactValidator._probe_spring_crud(client, "http://test", spec)

    assert asyncio.run(exercise()).status == "passed"
    assert submitted[0]["email"].startswith("zhangsan+probe-")
    assert submitted[0]["email"].endswith("@example.com")
    assert payload["email"] == "zhangsan@example.com"


class _StubValidator:
    def __init__(self, result: ArtifactValidationResult):
        self.result = result

    async def validate(self, context, config=None, *, emit=None):
        return self.result


class _TestProvider:
    def __init__(self):
        self.configs = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    def resolve_thinking(self, requested):
        return "off", None

    async def generate(self, system_prompt, user_prompt, config=None):
        from app.llm.base import LLMResponse

        settings = dict(config or {})
        self.configs.append(settings)
        text = "<html><body>fixed</body></html>" if settings.get("generation_phase") == "artifact_validation_repair" else "测试报告"
        return LLMResponse(text, input_tokens=1, output_tokens=2, finish_reason="stop", message_content=text)


def test_tester_validation_failure_blocks_final_success_and_keeps_artifacts(tmp_path):
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_validation_gate", "validation-gate", {}, RunStatus.PENDING.value, "now")
    workflow = WorkflowDefinition(
        id="validation-gate",
        name="validation-gate",
        steps=[
            StepDefinition(
                "tester",
                agent_id="tester_agent",
                task_template="验证结果：{{artifact_validation}}",
                output="test_report",
                validation={"enabled": True, "build": False, "startup": False},
            )
        ],
    )
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        _TestProvider(),
        WorkflowEventBus(repository),
        repository,
        artifact_service=ArtifactService(repository, tmp_path / "workspaces"),
        artifact_validator=_StubValidator(_result("failed")),
    )

    asyncio.run(executor.start("run_validation_gate", workflow, {"__artifact_files__": [{"name": "index.html", "content": "<html></html>"}]}))

    run = repository.get_run("run_validation_gate")
    assert run["status"] == RunStatus.FAILED.value
    assert run["steps"][0]["status"] == "FAILED"
    assert any(event["type"] == "step.validation_failed" for event in executor.event_bus._history["run_validation_gate"])
    assert [item["name"] for item in executor.artifact_service.list_public("run_validation_gate")] == ["index.html"]


class _RepairingValidator:
    async def validate(self, context, config=None, *, emit=None):
        files = context.get("__artifact_files__", [])
        repaired = any(item.get("name") == "index.html" and "fixed" in item.get("content", "") for item in files)
        if repaired:
            return _result("passed")
        check = ValidationCheck(
            "frontend-build",
            "frontend",
            "前端构建",
            "failed",
            "前端构建失败。",
            output="index.html: invalid source",
        )
        return ArtifactValidationResult("failed", "前端构建失败。", ("frontend",), (check,))


def test_tester_can_repair_a_located_file_and_revalidate(tmp_path):
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_validation_repair", "validation-repair", {}, RunStatus.PENDING.value, "now")
    workflow = WorkflowDefinition(
        id="validation-repair",
        name="validation-repair",
        steps=[
            StepDefinition(
                "tester",
                agent_id="tester_agent",
                task_template="验证结果：{{artifact_validation}}",
                output="test_report",
                validation={"enabled": True, "build": False, "startup": False, "repair_attempts": 1},
            )
        ],
    )
    provider = _TestProvider()
    artifact_service = ArtifactService(repository, tmp_path / "workspaces")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        WorkflowEventBus(repository),
        repository,
        artifact_service=artifact_service,
        artifact_validator=_RepairingValidator(),
    )

    asyncio.run(
        executor.start(
            "run_validation_repair",
            workflow,
            {"__artifact_files__": [{"step_id": "frontend", "name": "index.html", "content": "<html>broken</html>"}]},
        )
    )

    run = repository.get_run("run_validation_repair")
    resolved = artifact_service.resolve_path("run_validation_repair", "run_validation_repair_index_html")
    assert run["status"] == RunStatus.SUCCESS.value
    assert resolved is not None and "fixed" in resolved[0].read_text(encoding="utf-8")
    assert [item.get("generation_phase") for item in provider.configs] == ["artifact_validation_repair", None]
    assert any(event["type"] == "step.validation_repaired" for event in executor.event_bus._history["run_validation_repair"])


def test_validation_repair_candidate_prefers_export_owner_and_compiler_file():
    frontend_files = [
        {"name": "src/App.vue", "content": "app"},
        {"name": "src/api/itemApi.js", "content": "api"},
        {"name": "package.json", "content": "{}"},
    ]
    backend_files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/ItemController.java", "content": "class ItemController {}"},
    ]

    frontend = WorkflowExecutor._validation_repair_candidates(
        frontend_files,
        '"fetchItems" is not exported by "src/api/itemApi.js", imported by "src/App.vue".',
    )
    backend = WorkflowExecutor._validation_repair_candidates(
        backend_files,
        "pom.xml loaded\n/src/main/java/com/example/ItemController.java:[35,41] compilation error",
    )

    assert [item["name"] for item in frontend] == ["src/api/itemApi.js"]
    assert [item["name"] for item in backend] == ["src/main/java/com/example/ItemController.java"]

    spring_files = [
        {"name": "src/main/resources/application.yml", "content": "spring:\n  jpa:\n    hibernate:\n      ddl-auto: update"},
        {"name": "src/main/resources/data.sql", "content": "INSERT INTO student ..."},
        {"name": "src/main/java/com/example/Student.java", "content": "class Student {}"},
    ]
    spring = WorkflowExecutor._validation_repair_candidates(
        spring_files,
        'Failed to execute SQL script [target/classes/data.sql]: Table "STUDENT" not found',
    )

    assert [item["name"] for item in spring] == [
        "src/main/resources/application.yml",
    ]


def test_validation_repair_candidate_routes_h2_driver_errors_to_pom():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/resources/application.properties", "content": "spring.datasource.driver-class-name=org.h2.Driver"},
        {"name": "src/main/java/com/example/Application.java", "content": "class Application {}"},
    ]

    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "Application.java:10\nCannot load driver class: org.h2.Driver",
    )

    assert [item["name"] for item in selected] == ["pom.xml"]


def test_validation_repair_prefers_explicit_compiler_source_over_downloaded_dependency():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/StudentRepository.java", "content": "bad source"},
    ]
    diagnostics = (
        "[INFO] Downloading spring-boot-starter-data-jpa\n"
        "[ERROR] /C:/Temp/build/src/main/java/com/example/StudentRepository.java:[9,1] illegal character"
    )

    selected = WorkflowExecutor._validation_repair_candidates(files, diagnostics)

    assert [item["name"] for item in selected] == [
        "src/main/java/com/example/StudentRepository.java",
    ]


def test_validation_repair_routes_entity_table_mismatch_to_entity_not_pom():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/app/Product.java", "content": '@Table(name = "products") class Product {}'},
        {"name": "src/main/java/com/example/app/ProductService.java", "content": "class ProductService {}"},
    ]
    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        '[后端实体表名合同] src/main/java/com/example/app/Product.java @Table(name="products") 与冻结表 product 不一致\n'
        'Maven build reads pom.xml',
    )
    assert [item["name"] for item in selected] == ["src/main/java/com/example/app/Product.java"]


def test_validation_repair_routes_hibernate_missing_table_to_entity_not_pom():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/app/Product.java", "content": '@Entity @Table(name = "products") class Product {}'},
        {"name": "src/main/java/com/example/app/ProductService.java", "content": "class ProductService {}"},
    ]
    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "Maven used pom.xml; Schema-validation: missing table [products]",
    )
    assert [item["name"] for item in selected] == ["src/main/java/com/example/app/Product.java"]


def test_validation_repair_routes_missing_item_endpoint_to_controller_not_pom():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/StudentController.java", "content": "class StudentController {}"},
        {"name": "src/main/java/com/example/StudentService.java", "content": "class StudentService {}"},
    ]

    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "[CRUD 明细路由合同] H2 CRUD 闭环失败：PUT /api/students/{id} 返回 HTTP 404\npom.xml loaded",
    )

    assert [item["name"] for item in selected] == ["src/main/java/com/example/StudentController.java"]


def test_validation_repair_routes_missing_collection_endpoints_to_owner_controllers():
    files = [
        {"name": "pom.xml", "content": "<project />"},
        {"name": "src/main/java/com/example/ProductController.java", "content": '@RequestMapping("/api/products") class ProductController {}'},
        {"name": "src/main/java/com/example/CartController.java", "content": '@RequestMapping("/api/cart") class CartController {}'},
        {"name": "src/main/java/com/example/Application.java", "content": "class Application {}"},
    ]

    selected = WorkflowExecutor._validation_repair_candidates(
        files,
        "服务已有 HTTP 响应，但探测端点未通过：/api/products=HTTP 404、/api/cart=HTTP 404",
    )

    assert [item["name"] for item in selected] == [
        "src/main/java/com/example/ProductController.java",
        "src/main/java/com/example/CartController.java",
    ]


def test_missing_collection_endpoint_dispatches_repair_to_backend_agent(tmp_path):
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_collection_route_repair", "collection-route-repair", {}, RunStatus.PENDING.value, "now")
    event_bus = WorkflowEventBus(repository)
    provider = _TestProvider()
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        event_bus,
        repository,
        artifact_service=ArtifactService(repository, tmp_path / "workspaces"),
    )
    workflow = WorkflowDefinition(
        id="collection-route-repair",
        name="collection-route-repair",
        steps=[StepDefinition("tester", agent_id="tester_agent")],
    )
    state = RunState(
        run_id="run_collection_route_repair",
        workflow=workflow,
        dag=build_dag(workflow),
        context=WorkflowContext({
            "requirement": "实现商品和购物车 API",
            "__artifact_files__": [
                {
                    "step_id": "backend",
                    "name": "src/main/java/com/example/ProductController.java",
                    "content": '@RequestMapping("/api/products") class ProductController {}',
                },
            ],
        }),
    )
    validation = ArtifactValidationResult(
        "failed",
        "服务已有 HTTP 响应，但探测端点未通过：/api/products=HTTP 404、/api/cart=HTTP 404",
        ("backend",),
        (
            ValidationCheck(
                "backend-startup",
                "backend",
                "后端启动检查",
                "failed",
                "服务已有 HTTP 响应，但探测端点未通过",
                output="/api/products=HTTP 404; /api/cart=HTTP 404",
            ),
        ),
    )

    repaired, _ = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000))

    assert repaired == ["src/main/java/com/example/ProductController.java"]
    assert provider.configs[0]["agent_id"] == "backend_agent"
    assert any(
        event["type"] == "step.validation_repair_dispatched"
        and event["payload"]["agentId"] == "backend_agent"
        for event in event_bus._history["run_collection_route_repair"]
    )


def test_validation_failure_fingerprint_is_stable_across_temp_workspaces():
    first = ArtifactValidationResult(
        "failed",
        "failed",
        ("backend",),
        (
            ValidationCheck(
                "backend-test",
                "backend",
                "Maven test",
                "failed",
                "Maven failed",
                output="C:/Temp/agent-team-validation-abc123/src/Test.java failed after 120 seconds",
            ),
        ),
    )
    second = ArtifactValidationResult(
        "failed",
        "failed",
        ("backend",),
        (
            ValidationCheck(
                "backend-test",
                "backend",
                "Maven test",
                "failed",
                "Maven failed",
                output="C:/Temp/agent-team-validation-xyz999/src/Test.java failed after 120 seconds",
            ),
        ),
    )

    assert WorkflowExecutor._validation_failure_fingerprint(first) == WorkflowExecutor._validation_failure_fingerprint(second)


def test_repeated_validation_failure_patches_existing_owner_without_regeneration(tmp_path):
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_owner_repair", "owner-repair", {}, RunStatus.PENDING.value, "now")
    workflow = WorkflowDefinition(
        id="owner-repair",
        name="owner-repair",
        steps=[
            StepDefinition("backend", agent_id="backend_agent", task_template="build", output="backend_result", generation_mode="artifacts"),
            StepDefinition("tester", agent_id="tester_agent", task_template="test", depends_on=["backend"]),
        ],
    )
    repository.upsert_step("run_owner_repair", "backend", agent_id="backend_agent", status=StepStatus.SUCCESS.value, output="old")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        _TestProvider(),
        WorkflowEventBus(repository),
        repository,
        artifact_service=ArtifactService(repository, tmp_path / "workspaces"),
    )
    state = RunState(
        run_id="run_owner_repair",
        workflow=workflow,
        dag=build_dag(workflow),
        context=WorkflowContext(
            {
                "requirement": "build app",
                "backend_result": "old",
                "backend_result_files": "old-files",
                "__artifact_files__": [{"step_id": "backend", "name": "old.java", "content": "old"}],
            }
        ),
    )
    state.results = {"backend": StepStatus.SUCCESS, "tester": StepStatus.RUNNING}

    async def fake_repair(current_state, validation, attempt, tester, timeout, maximum, *, bundle=False):
        assert bundle is True
        assert current_state.context.get("backend_result") == "old"
        files = current_state.context.get("__artifact_files__")
        assert [item["name"] for item in files] == ["old.java"]
        current_state.context.set(
            "__artifact_files__",
            [{**files[0], "content": "new"}],
        )
        return ["old.java"], []

    executor._repair_failed_artifacts = fake_repair
    validation = ArtifactValidationResult(
        "failed",
        "failed",
        ("backend",),
        (ValidationCheck("backend-test", "backend", "Maven test", "failed", "compile failed"),),
    )

    files, responses = asyncio.run(executor._reexecute_validation_owners(state, validation, 2, "tester"))

    assert files == ["old.java"]
    assert responses == []
    assert state.context.get("backend_result") == "old"
    assert [item["name"] for item in state.context.get("__artifact_files__")] == ["old.java"]
    assert "validation_feedback" not in state.context.snapshot()


def test_jpa_source_requires_jpa_starter_not_just_jdbc_and_h2():
    files = [
        {"name": "pom.xml", "content": "<project><artifactId>spring-boot-starter-jdbc</artifactId><groupId>com.h2database</groupId><artifactId>h2</artifactId></project>"},
        {"name": "src/main/java/com/example/Student.java", "content": "package com.example; import jakarta.persistence.Entity; @Entity class Student {}"},
    ]
    result = asyncio.run(ArtifactValidator().validate({"__artifact_files__": files}, {"build": False, "startup": False}))
    assert any(check.id == "backend-jpa-dependencies" and check.status == "failed" for check in result.checks)
    files[0]["content"] += "<artifactId>spring-boot-starter-data-jpa</artifactId>"
    result = asyncio.run(ArtifactValidator().validate({"__artifact_files__": files}, {"build": False, "startup": False}))
    assert not any(check.id == "backend-jpa-dependencies" and check.status == "failed" for check in result.checks)


def test_log_clipping_preserves_middle_sql_exception_root():
    output = "startup\n" + "noise\n" * 4000 + "Caused by: org.h2.jdbc.JdbcSQLIntegrityConstraintViolationException: NULL not allowed\n" + "tail\n" * 4000
    clipped = ArtifactValidator._clip_output(output)
    assert "NULL not allowed" in clipped
    assert len(clipped) < 12000


def test_migration_files_without_flyway_use_existing_schema_before_jpa_validation():
    files = {
        "pom.xml": "<project><artifactId>h2</artifactId><artifactId>spring-boot-starter-data-jpa</artifactId></project>",
        "src/main/resources/schema.sql": "CREATE TABLE students(id BIGINT);",
        "src/main/resources/db/migration/V1__students.sql": "CREATE TABLE students(id BIGINT);",
    }
    arguments = ArtifactValidator._spring_h2_test_arguments(files)
    assert "--spring.sql.init.mode=always" in arguments
    assert "--spring.flyway.enabled=false" in arguments
    assert "--spring.jpa.defer-datasource-initialization=false" in arguments
    files["pom.xml"] += "<artifactId>flyway-core</artifactId>"
    arguments = ArtifactValidator._spring_h2_test_arguments(files)
    assert "--spring.flyway.enabled=true" in arguments
    assert "--spring.sql.init.mode=never" in arguments


def test_migration_only_without_executor_is_blocked_before_maven():
    files = [
        {"name": "pom.xml", "content": "<project><artifactId>h2</artifactId></project>"},
        {"name": "src/main/java/com/example/Application.java", "content": "package com.example; class Application {}"},
        {"name": "src/main/resources/db/migration/V1__students.sql", "content": "CREATE TABLE students(id BIGINT);"},
    ]
    result = asyncio.run(ArtifactValidator().validate({"__artifact_files__": files}, {"build": False, "startup": False}))
    assert any(check.id == "backend-migration-engine" and check.status == "failed" for check in result.checks)
    assert WorkflowExecutor._validation_repair_candidates([
        {"name": "pom.xml", "content": "pom"}, {"name": "Application.java", "content": "java"},
    ], "数据库初始化执行器") == [{"name": "pom.xml", "content": "pom"}]


def test_crud_probe_detects_conditional_post_put_and_uses_short_payload():
    files = {
        "index.html": "const API_URL='/api/students'; fetch(API_URL, {method: id ? \"PUT\" : \"POST\"}); fetch(API_URL, {method: 'DELETE'});",
        "Student.java": "@Entity class Student { private Long id; private String studentNo; @Size(max=16) private String gender; }",
    }
    spec = ArtifactValidator._spring_crud_spec(files)
    assert spec is not None
    assert {"GET", "POST", "PUT", "DELETE"}.issubset(spec.expected_methods)
    assert len(spec.payload["gender"]) <= 16


def test_known_wrong_flyway_dependency_repair_needs_no_llm(tmp_path):
    repository = SQLiteRepository(":memory:")
    provider = _TestProvider()
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository)
    workflow = WorkflowDefinition(id="repair", name="repair", steps=[StepDefinition("tester", agent_id="tester_agent")])
    state = RunState(run_id="repair", workflow=workflow, dag=build_dag(workflow), context=WorkflowContext({
        "__artifact_files__": [{"step_id": "backend", "name": "pom.xml", "content": "<project><dependency><groupId>org.flywaydb</groupId><artifactId>flyway-database-h2</artifactId></dependency></project>"}],
    }))
    validation = ArtifactValidationResult("failed", "bad dependency", ("backend",), (
        ValidationCheck("backend-flyway-dependencies", "backend", "Flyway", "failed", "flyway-database-h2"),
    ))
    files, responses = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000))
    assert files == ["pom.xml"] and responses == [] and provider.configs == []
    assert "flyway-core" in state.context.get("__artifact_files__")[0]["content"]


def test_missing_vue_component_is_created_only_in_candidate_state():
    repository = SQLiteRepository(":memory:")
    provider = _TestProvider()
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository)
    workflow = WorkflowDefinition(id="missing-component", name="missing-component", steps=[StepDefinition("tester", agent_id="tester_agent")])
    original = {"step_id": "frontend", "name": "src/App.vue", "content": "<script setup>import Form from './components/RoomForm.vue'</script>"}
    state = RunState(run_id="missing-component", workflow=workflow, dag=build_dag(workflow),
                     context=WorkflowContext({"__artifact_files__": [original]}))
    validation = ArtifactValidationResult("failed", "missing component", ("frontend",), (
        ValidationCheck("frontend-build", "frontend", "Vue build", "failed",
                        "src/App.vue 引用了不存在的模块 ./components/RoomForm.vue"),
    ))
    repaired, _ = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000))
    assert "src/components/RoomForm.vue" in repaired
    assert original["content"] == "<script setup>import Form from './components/RoomForm.vue'</script>"
    assert any(item["name"] == "src/components/RoomForm.vue" and item["step_id"] == "frontend"
               for item in state.context.get("__artifact_files__"))


def test_compiler_missing_java_class_is_created_without_rewriting_reference():
    from app.llm.base import LLMResponse

    class MissingClassProvider(_TestProvider):
        async def generate(self, system_prompt, user_prompt, config=None):
            self.configs.append(dict(config or {}))
            assert "缺失符号：ProductNotFoundException" in user_prompt
            content = ("package com.example.app;\n"
                       "public class ProductNotFoundException extends RuntimeException {\n"
                       "  public ProductNotFoundException(Long id) { super(\"Product \" + id); }\n}")
            return LLMResponse(content, input_tokens=1, output_tokens=2, finish_reason="stop", message_content=content)

    repository = SQLiteRepository(":memory:")
    provider = MissingClassProvider()
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository)
    workflow = WorkflowDefinition(id="missing-java-class", name="missing-java-class",
                                  steps=[StepDefinition("tester", agent_id="tester_agent")])
    original = {"step_id": "backend", "name": "src/main/java/com/example/app/ProductService.java",
                "content": ("package com.example.app;\nclass ProductService { void get() { "
                            "throw new ProductNotFoundException(1L); } }")}
    state = RunState(run_id="missing-java-class", workflow=workflow, dag=build_dag(workflow),
                     context=WorkflowContext({"__artifact_files__": [original]}))
    output = ("[ERROR] /tmp/src/main/java/com/example/app/ProductService.java:[2,43] cannot find symbol\n"
              "[ERROR] symbol: class ProductNotFoundException")
    validation = ArtifactValidationResult("failed", "Maven compile failed", ("backend",), (
        ValidationCheck("backend-test", "backend", "Maven", "failed", "compile failed", output=output),
    ))
    repaired, _ = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000))
    assert repaired == ["src/main/java/com/example/app/ProductNotFoundException.java"]
    files = state.context.get("__artifact_files__")
    assert len(files) == 2
    assert files[0] == original
    assert "extends RuntimeException" in files[1]["content"]
    assert len(provider.configs) == 1


def test_bundle_repair_includes_every_related_file_or_escalates_safely():
    repository = SQLiteRepository(":memory:")
    provider = _TestProvider()
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository)
    workflow = WorkflowDefinition(id="related", name="related", steps=[StepDefinition("tester", agent_id="tester_agent")])
    validation = ArtifactValidationResult("failed", "backend compile", ("backend",), (
        ValidationCheck("backend-build", "backend", "Maven", "failed", "X0Controller.java compile failed"),
    ))

    def state_with(count):
        return RunState(run_id="related", workflow=workflow, dag=build_dag(workflow), context=WorkflowContext({
            "__artifact_files__": [{"step_id": "backend", "name": f"src/main/java/com/example/X{i}Controller.java",
                                    "content": f"class X{i}Controller {{}}"} for i in range(count)],
        }))

    state = state_with(9)
    repaired, _ = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000, bundle=True))
    assert len(repaired) == 9
    assert len(provider.configs) == 9
    state = state_with(17)
    repaired, _ = asyncio.run(executor._repair_failed_artifacts(state, validation, 1, "tester", 10, 6000, bundle=True))
    assert repaired == []
    assert len(provider.configs) == 9


def test_compile_regression_is_not_progress_after_crud_failure():
    crud = ArtifactValidationResult("failed", "CRUD failed", ("backend",), (
        ValidationCheck("spring-h2-crud", "backend", "CRUD", "failed", "HTTP 500"),
    ))
    compile_error = ArtifactValidationResult("failed", "compile failed", ("backend",), (
        ValidationCheck("backend-test", "backend", "Maven", "failed", "compile failed", output="28 errors"),
    ))
    assert WorkflowExecutor._validation_quality(compile_error) < WorkflowExecutor._validation_quality(crud)


def test_failed_candidate_is_rolled_back_and_not_persisted(tmp_path):
    class RegressingValidator:
        async def validate(self, context, config=None, *, emit=None):
            changed = "fixed" in context["__artifact_files__"][0]["content"]
            check = ValidationCheck(
                "frontend-build" if changed else "spring-h2-crud", "frontend", "check", "failed",
                "compile regression" if changed else "HTTP 500", output="index.html: error",
            )
            return ArtifactValidationResult("failed", check.message, ("frontend",), (check,))

    repository = SQLiteRepository(":memory:")
    repository.create_run("run_rollback", "rollback", {}, RunStatus.PENDING.value, "now")
    workflow = WorkflowDefinition(id="rollback", name="rollback", steps=[
        StepDefinition("tester", agent_id="tester_agent", task_template="{{artifact_validation}}", output="test_report",
                       validation={"enabled": True, "repair_attempts": 1}),
    ])
    service = ArtifactService(repository, tmp_path / "workspaces")
    provider = _TestProvider()
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository,
                                artifact_service=service, artifact_validator=RegressingValidator())
    asyncio.run(executor.start("run_rollback", workflow, {
        "__artifact_files__": [{"step_id": "frontend", "name": "index.html", "content": "<html>original</html>"}],
    }))
    assert repository.get_run("run_rollback")["status"] == RunStatus.FAILED.value
    resolved = service.resolve_path("run_rollback", "run_rollback_index_html")
    assert resolved is not None and "original" in resolved[0].read_text(encoding="utf-8")
    assert any(event["type"] == "step.validation_candidate_rejected" for event in executor.event_bus._history["run_rollback"])
    assert all(config.get("generation_phase") == "artifact_validation_repair" for config in provider.configs)


def test_cancelled_repair_discards_candidate_without_overwriting_stable_files(tmp_path):
    class AlwaysFailValidator:
        async def validate(self, context, config=None, *, emit=None):
            check = ValidationCheck("frontend-build", "frontend", "Vue build", "failed", "src/App.vue compile failed")
            return ArtifactValidationResult("failed", check.message, ("frontend",), (check,))

    class CancelAfterCandidate:
        async def coordinate(self, initial_validation, *, prepare_candidate, **kwargs):
            await prepare_candidate({"owners": ["frontend"]}, 1)
            raise asyncio.CancelledError()

    repository = SQLiteRepository(":memory:")
    repository.create_run("cancel-candidate", "cancel-candidate", {}, RunStatus.PENDING.value, "now")
    service = ArtifactService(repository, tmp_path / "workspaces")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), _TestProvider(), WorkflowEventBus(repository),
                                repository, artifact_service=service, artifact_validator=AlwaysFailValidator())
    executor.repair_coordinator = CancelAfterCandidate()
    workflow = WorkflowDefinition(id="cancel-candidate", name="cancel-candidate", steps=[
        StepDefinition("tester", agent_id="tester_agent", task_template="check", output="test_report",
                       validation={"enabled": True, "repair_attempts": 1}),
    ])
    source = {"step_id": "frontend", "name": "src/App.vue", "content": "<template>original</template>"}
    try:
        asyncio.run(executor.start("cancel-candidate", workflow, {"__artifact_files__": [source]}))
    except asyncio.CancelledError:
        pass
    candidate_root = tmp_path / "workspaces" / ".candidates" / "cancel-candidate"
    assert not candidate_root.exists() or not list(candidate_root.iterdir())
    assert source["content"] == "<template>original</template>"


def test_h2_crud_rejects_update_that_does_not_change_database():
    from app.workflow.artifact_validator import _SpringCrudSpec
    records = []
    spec = _SpringCrudSpec("/api/students", {"name": "probe"}, "name", frozenset({"POST", "GET", "PUT", "DELETE"}))
    def handler(request):
        if request.method == "POST":
            records[:] = [{"id": 1, "name": "probe"}]
            return httpx.Response(201, json=records[0])
        if request.method == "PUT":
            return httpx.Response(200, json={"id": 1, "name": "probe-updated"})
        return httpx.Response(200, json=records)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ArtifactValidator._probe_spring_crud(client, "http://test", spec)
    result = asyncio.run(exercise())
    assert result.status == "failed"
    assert "未反映修改" in result.message


def test_crud_http_500_check_contains_backend_exception_log(tmp_path):
    from app.workflow.artifact_validator import _SpringCrudSpec
    script = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'[]')
    def do_POST(self):
        print('Caused by: H2 NULL constraint exception', flush=True)
        self.send_response(500); self.end_headers(); self.wfile.write(b'failed')
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""
    checks = []
    async def exercise():
        async def record(check):
            checks.append(check)
        await ArtifactValidator()._validate_startup(
            tmp_path, sys.executable, ["-u", "-c", script, "{port}"], "backend", "startup", ("/",), 5, record,
            crud_spec=_SpringCrudSpec("/api/students", {"name": "probe"}, "name", frozenset({"POST", "GET", "PUT", "DELETE"})),
        )
    asyncio.run(exercise())
    crud = next(check for check in checks if check.id == "spring-h2-crud")
    assert crud.status == "failed"
    assert "HTTP 500" in crud.message
    assert "H2 NULL constraint exception" in crud.output


def test_probe_discovers_real_controller_route_without_browser_api_literals():
    files = {"StudentController.java": "@RestController @RequestMapping(\"/api/students\") public class StudentController { @GetMapping public Object list() {} @GetMapping(\"/{id}\") public Object get() {} @PostMapping void create() {} @PutMapping(\"/{id}\") void update() {} @DeleteMapping(\"/{id}\") void delete() {} }",
             "Student.java": "@Entity class Student { private Long id; private String name; }"}
    assert ArtifactValidator._spring_probe_paths(files)[0] == "/api/students"
    assert "/api/students/{id}" not in ArtifactValidator._spring_probe_paths(files)
    assert ArtifactValidator._spring_crud_spec(files) is not None
    files["PageController.java"] = '@Controller class PageController { @GetMapping("/") String home() { return "redirect:/students"; } }'
    assert ArtifactValidator._spring_crud_spec(files).collection_path == "/api/students"


def test_crud_probe_uses_json_property_wire_names():
    files = {"index.html": "const API_URL='/api/students'; fetch(API_URL, {method: 'POST'}); fetch(API_URL, {method: 'PUT'}); fetch(API_URL, {method: 'DELETE'});",
             "Student.java": '@Entity class Student { private Long id; @JsonProperty("student_number") @Column(name="student_number") private String studentNumber; @JsonProperty("class_name") private String className; }'}
    spec = ArtifactValidator._spring_crud_spec(files)
    assert spec is not None and spec.identity_field == "student_number"
    assert "student_number" in spec.payload and "class_name" in spec.payload
    assert "studentNumber" not in spec.payload


def test_template_contract_requires_renderer_and_mvc_not_just_rest():
    files = {"pom.xml": "<project/>", "src/main/resources/templates/students/list.html": '<html><p th:text="${student.name}"></p></html>',
             "StudentController.java": '@RestController @RequestMapping("/api/students") class StudentController { @GetMapping Object list() {} }'}
    checks = ArtifactValidator._spring_template_contract(files)
    assert {check.id for check in checks} == {"backend-template-dependencies", "backend-template-views"}
    files["pom.xml"] = "<project><artifactId>spring-boot-starter-thymeleaf</artifactId></project>"
    files["PageController.java"] = '@Controller class PageController { @GetMapping("/students") String list() { return "students/list"; } }'
    assert ArtifactValidator._spring_template_contract(files) == []


def test_startup_http_404_is_reported_as_route_failure_not_no_response(tmp_path):
    script = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404); self.end_headers()
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""
    checks = []
    async def exercise():
        async def record(check): checks.append(check)
        await ArtifactValidator()._validate_startup(tmp_path, sys.executable, ["-u", "-c", script, "{port}"], "backend", "startup", ("/",), 2, record)
    asyncio.run(exercise())
    assert checks[0].status == "failed"
    assert "HTTP 404" in checks[0].message
    assert "不是无响应启动超时" in checks[0].message
