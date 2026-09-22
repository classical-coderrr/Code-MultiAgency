from app.workflow.artifact_generation import ArtifactFileSpec, _complete_agent_manifest
from app.workflow.langgraph_runtime import _merge_dicts


def test_parallel_artifact_merge_keeps_both_agent_file_lists():
    backend = {
        "__artifact_files__": [
            {"step_id": "backend", "name": "Student.java", "content": "class Student {}"},
        ],
        "backend_result": "backend",
    }
    frontend = {
        "__artifact_files__": [
            {"step_id": "frontend", "name": "App.vue", "content": "<template />"},
        ],
        "frontend_result": "frontend",
    }

    merged = _merge_dicts(backend, frontend)

    assert {item["name"] for item in merged["__artifact_files__"]} == {"Student.java", "App.vue"}
    assert merged["backend_result"] == "backend"
    assert merged["frontend_result"] == "frontend"
    assert len(_merge_dicts(merged, frontend)["__artifact_files__"]) == 2


def test_artifact_merge_replaces_repaired_content_for_same_owner_and_path():
    original = {"__artifact_files__": [{"step_id": "frontend", "name": "src/main.js", "content": "broken"}]}
    repaired = {"__artifact_files__": [{"step_id": "frontend", "name": "src/main.js", "content": "fixed"}]}

    merged = _merge_dicts(original, repaired)

    assert merged["__artifact_files__"] == [{"step_id": "frontend", "name": "src/main.js", "content": "fixed"}]


def test_contract_manifest_filters_cross_agent_files_and_adds_bootstrap_files():
    backend_plan = [
        ArtifactFileSpec("Student.java", "java", "实体", 500),
        ArtifactFileSpec("App.vue", "vue", "错误归属的前端文件", 500),
        ArtifactFileSpec("application.properties", "properties", "配置", 300),
    ]
    completed_backend = _complete_agent_manifest(
        backend_plan,
        "使用 Spring Boot 和 Vue 开发学生管理系统，要求 CRUD",
        "backend_agent",
    )
    backend_names = {item.name for item in completed_backend}

    assert "App.vue" not in backend_names
    assert {
        "pom.xml",
        "src/main/java/com/example/studentmanagement/Application.java",
        "src/main/java/com/example/studentmanagement/Student.java",
        "src/main/java/com/example/studentmanagement/StudentRepository.java",
        "src/main/java/com/example/studentmanagement/StudentService.java",
        "src/main/java/com/example/studentmanagement/StudentController.java",
        "src/main/resources/application.properties",
    } <= backend_names

    completed_frontend = _complete_agent_manifest(
        [ArtifactFileSpec("App.vue", "vue", "主页面", 900)],
        "使用 Spring Boot 和 Vue 开发学生管理系统，要求 CRUD",
        "frontend_agent",
    )
    assert {"package.json", "index.html", "src/main.js", "src/App.vue", "src/style.css"} <= {
        item.name for item in completed_frontend
    }


def test_contract_manifest_normalizes_agent_roots_and_avoids_cross_agent_duplicates():
    frontend_plan = [
        ArtifactFileSpec("README.md", "markdown", "前端说明", 300),
        ArtifactFileSpec("backend/pom.xml", "xml", "错误归属的后端文件", 500),
        ArtifactFileSpec("frontend/package.json", "json", "前端依赖", 500),
        ArtifactFileSpec("package.json", "json", "重复前端依赖", 500),
        ArtifactFileSpec("frontend/src/main.js", "javascript", "前端入口", 500),
    ]
    completed_frontend = _complete_agent_manifest(
        frontend_plan,
        "使用 Spring Boot 和 Vue 开发 CRUD 管理系统",
        "frontend_agent",
    )
    frontend_names = [item.name for item in completed_frontend]

    assert "backend/pom.xml" not in frontend_names
    assert frontend_names.count("package.json") == 1
    assert frontend_names.count("src/main.js") == 1
    assert "frontend/README.md" in frontend_names

    backend_plan = [
        ArtifactFileSpec("README.md", "markdown", "后端说明", 300),
        ArtifactFileSpec("pom.xml", "xml", "构建配置", 700),
        ArtifactFileSpec("package.json", "json", "错误归属的 Vue 构建配置", 500),
        ArtifactFileSpec("src/main/java/com/example/demo/DemoApplication.java", "java", "启动入口", 400),
        ArtifactFileSpec("src/main/java/com/example/demo/entity/Item.java", "java", "实体", 700),
        ArtifactFileSpec("src/main/java/com/example/demo/repository/ItemRepository.java", "java", "仓储", 500),
        ArtifactFileSpec("src/main/java/com/example/demo/controller/ItemController.java", "java", "接口", 900),
    ]
    completed_backend = _complete_agent_manifest(
        backend_plan,
        "使用 Spring Boot 和 Vue 开发 CRUD 管理系统",
        "backend_agent",
    )
    backend_names = {item.name for item in completed_backend}

    assert "backend/README.md" in backend_names
    assert "package.json" not in backend_names
    assert "src/main/java/com/example/studentmanagement/Item.java" not in backend_names
    assert {
        "src/main/java/com/example/demo/entity/Item.java",
        "src/main/java/com/example/demo/repository/ItemRepository.java",
        "src/main/java/com/example/demo/service/ItemService.java",
        "src/main/java/com/example/demo/controller/ItemController.java",
    } <= backend_names


def test_database_manifest_is_framework_aware_and_does_not_generate_app_code():
    database_plan = [
        ArtifactFileSpec("schema.sql", "sql", "表结构", 800),
        ArtifactFileSpec("Student.java", "java", "不属于数据库 Agent", 500),
    ]

    completed = _complete_agent_manifest(
        database_plan,
        "使用 Spring Boot 和 Vue 开发学生管理系统，要求 CRUD",
        "database_agent",
    )
    names = {item.name for item in completed}

    assert "Student.java" not in names
    assert "src/main/resources/schema.sql" in names


def test_database_manifest_rejects_backend_runtime_configuration():
    completed = _complete_agent_manifest(
        [
            ArtifactFileSpec("schema.sql", "sql", "database schema", 800),
            ArtifactFileSpec(
                "src/main/resources/application.yml",
                "yaml",
                "Spring Boot runtime configuration",
                400,
            ),
            ArtifactFileSpec(
                "application.properties",
                "properties",
                "Spring Boot runtime configuration",
                400,
            ),
        ],
        "Build a Spring Boot CRUD application with database contract files",
        "database_agent",
    )

    names = {item.name for item in completed}

    assert "src/main/resources/schema.sql" in names
    assert "src/main/resources/application.yml" not in names
    assert "application.properties" not in names
