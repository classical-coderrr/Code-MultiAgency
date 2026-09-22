from app.workflow.artifact_generation import ArtifactFileSpec, _complete_agent_manifest, _fallback_artifact_manifest


def test_conditional_vue_instruction_cannot_change_html_stack():
    plan = [ArtifactFileSpec("index.html", "html", "页面", 1200), ArtifactFileSpec("app.js", "javascript", "交互", 1000)]
    contract = {"backend_stack": "springboot", "frontend_stack": "html", "page_mode": "static_rest", "crud_required": True}
    names = [item.name for item in _complete_agent_manifest(plan, "需求是 Spring Boot HTML；如果用户选 Vue 则使用 Vite", "frontend_agent", contract=contract)]
    assert names == ["src/main/resources/static/index.html", "src/main/resources/static/app.js"]


def test_wrong_vue_and_platform_test_manifest_filtered_before_generation():
    plan = [ArtifactFileSpec(name, "text", "文件", 1000) for name in ["src/App.vue", "vite.config.js", "package.json", "tests/test.js", "index.html", "app.js"]]
    contract = {"backend_stack": "none", "frontend_stack": "html", "page_mode": "static", "crud_required": False}
    assert [item.name for item in _complete_agent_manifest(plan, "Vue 是可选项", "frontend_agent", contract=contract)] == ["index.html", "app.js"]


def test_vue_selected_keeps_bootstrap():
    contract = {"backend_stack": "springboot", "frontend_stack": "vue", "page_mode": "spa"}
    names = {item.name for item in _complete_agent_manifest([], "HTML只是可选项", "frontend_agent", contract=contract)}
    assert {"package.json", "index.html", "src/main.js", "src/App.vue"} <= names


def test_fallback_entity_is_from_contract_not_industry_keyword():
    plan = _fallback_artifact_manifest("springboot CRUD", "backend_agent", contract={"entities": [{"name": "Book"}]})
    names = {item.name for item in plan}
    assert any(name.endswith("BookController.java") for name in names)
    assert not any("Student" in name for name in names)
