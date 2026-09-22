import asyncio
import json

from app.agents.registry import AgentRegistry
from app.llm.base import LLMProvider, LLMResponse, LLMTimeoutError
from app.repositories.sqlite import SQLiteRepository
from app.services.artifacts import ArtifactService
from app.workflow.events import WorkflowEventBus
from app.workflow.artifact_generation import (
    ArtifactFileSpec, _complete_spring_web_annotation_imports,
    _ensure_vue_vite_config, _file_budget, _repair_budget,
    _validated_file_content, generate_artifacts,
)
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, WorkflowDefinition


def test_source_file_discards_orphan_markdown_fence():
    source = "package com.example;\npublic interface StudentRepository {}\n```"
    response = LLMResponse(text=source, finish_reason="stop", message_content=source)

    content, reason = _validated_file_content(
        response, ArtifactFileSpec("src/main/java/com/example/StudentRepository.java", "java", "", 400),
    )

    assert reason is None
    assert content.endswith("StudentRepository {}")
    assert "```" not in content


def test_artifact_transport_retry_keeps_plan_and_retries_only_current_file():
    phases = []
    events = []

    async def request(system, user, config, timeout):
        phase = config["generation_phase"]
        phases.append(phase)
        if phase == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "purpose": "页面", "estimated_tokens": 500}]})
        elif phases.count("artifact_file") == 1:
            raise LLMTimeoutError("Provider connect timeout after 10s")
        else:
            text = "<!doctype html><html><body>ok</body></html>"
        return LLMResponse(text=text, input_tokens=10, output_tokens=20, finish_reason="stop", message_content=text)

    async def emit(name, payload):
        events.append(name)

    result = asyncio.run(generate_artifacts(
        system_prompt="test", original_prompt="创建一个 HTML 页面", base_config={"agent_id": "frontend_agent"},
        request_timeout=30, max_tokens=1000, provider_max_tokens=2000,
        request=request, record_response=lambda _: None, emit=emit,
    ))
    assert phases == ["artifact_plan", "artifact_file", "artifact_file"]
    assert result.files[0]["name"] == "index.html"
    assert "step.artifact_request_retrying" in events


def test_hidden_reasoning_raises_next_file_ceiling_without_forcing_more_output():
    file_budgets = []

    async def request(system, user, config, timeout):
        phase = config["generation_phase"]
        if phase == "artifact_plan":
            text = json.dumps({"files": [
                {"name": "index.html", "language": "html", "purpose": "页面", "estimated_tokens": 500},
                {"name": "style.css", "language": "css", "purpose": "样式", "estimated_tokens": 500},
            ]})
            return LLMResponse(text=text, finish_reason="stop")
        file_budgets.append((config["artifact_name"], config["max_tokens"]))
        text = "<!doctype html><html><body>ok</body></html>" if config["artifact_name"] == "index.html" else "body { color: red; }"
        reasoning = "internal" if config["artifact_name"] == "index.html" else None
        return LLMResponse(text=text, finish_reason="stop", reasoning_content=reasoning, message_content=text)

    async def emit(name, payload):
        pass

    result = asyncio.run(generate_artifacts(
        system_prompt="test", original_prompt="创建网页", base_config={"agent_id": "frontend_agent"},
        request_timeout=30, max_tokens=1000, provider_max_tokens=6000,
        request=request, record_response=lambda _: None, emit=emit,
    ))
    assert [item["name"] for item in result.files] == ["index.html", "style.css"]
    assert file_budgets == [("index.html", 2048), ("style.css", 6000)]


def test_vue_vite_bootstrap_adds_missing_plugin_config_without_replacing_package():
    files = [
        {"name": "src/App.vue", "content": "<template><h1>Hi</h1></template>"},
        {"name": "package.json", "content": json.dumps({
            "name": "demo", "dependencies": {"vue": "^3.4.0"},
            "devDependencies": {"vite": "^5.2.0", "@vitejs/plugin-vue": "^5.0.4"},
        })},
    ]

    assert _ensure_vue_vite_config(files) == "vite.config.js"
    assert _ensure_vue_vite_config(files) is None
    assert len([item for item in files if item["name"] == "vite.config.js"]) == 1
    assert "plugins: [vue()]" in files[-1]["content"]
    assert "process.env.VITE_API_PROXY" in files[-1]["content"]


def test_vue_vite_bootstrap_uses_frozen_api_prefixes():
    files = [
        {"name": "src/App.vue", "content": "<template />"},
        {"name": "package.json", "content": json.dumps({
            "dependencies": {"vue": "^3.4.0"},
            "devDependencies": {"vite": "^5.2.0", "@vitejs/plugin-vue": "^5.0.4"},
        })},
    ]
    assert _ensure_vue_vite_config(files, api_paths=["/products", "/products/{id}"]) == "vite.config.js"
    assert "'/products': process.env.VITE_API_PROXY" in files[-1]["content"]
    assert "'/api':" not in files[-1]["content"]


def test_vue_vite_bootstrap_adds_missing_compatible_plugin_dependency():
    files = [
        {"name": "src/App.vue", "content": "<template />"},
        {"name": "package.json", "content": json.dumps({
            "dependencies": {"vue": "^3.4.0"}, "devDependencies": {"vite": "^5.2.0"},
        })},
    ]

    assert _ensure_vue_vite_config(files) == "vite.config.js"
    assert json.loads(files[1]["content"])["devDependencies"]["@vitejs/plugin-vue"] == "^5.0.4"


def test_spring_web_annotation_imports_are_completed_without_duplicates():
    source = (
        "package com.example;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "@RestController\nclass StudentController {\n"
        "  @GetMapping public void get() {}\n"
        "  @PostMapping public void post(@RequestBody String value) {}\n"
        "}\n"
    )

    completed = _complete_spring_web_annotation_imports(source)

    assert completed.count("import org.springframework.web.bind.annotation.GetMapping;") == 1
    assert "import org.springframework.web.bind.annotation.RestController;" in completed
    assert "import org.springframework.web.bind.annotation.PostMapping;" in completed
    assert "import org.springframework.web.bind.annotation.RequestBody;" in completed
    assert _complete_spring_web_annotation_imports(completed) == completed


class FilePlanProvider(LLMProvider):
    def __init__(self) -> None:
        self.configs: list[dict] = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        if settings.get("generation_phase") == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "purpose": "页面入口", "estimated_tokens": 600}]})
        else:
            text = "<!doctype html><html><body><h1>完整成果物</h1></body></html>"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason="stop", message_content=text)


class PartPlanProvider(LLMProvider):
    def __init__(self) -> None:
        self.configs: list[dict] = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        if settings.get("generation_phase") == "artifact_plan":
            text = json.dumps({
                "files": [{
                    "name": "index.html",
                    "language": "html",
                    "purpose": "学生管理页面",
                    "estimated_tokens": 2200,
                    "parts": [
                        {"id": "shell", "purpose": "页面声明与开头结构", "estimated_tokens": 700},
                        {"id": "body", "purpose": "页面主体与结尾结构", "estimated_tokens": 700},
                    ],
                }],
            })
        elif settings.get("generation_phase") == "artifact_part":
            text = "<!doctype html><html><body>" if settings.get("artifact_part_id") == "shell" else "<h1>学生管理系统</h1></body></html>"
        else:
            text = ""
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason="stop", message_content=text)


class SplitBeforeContinuationProvider(PartPlanProvider):
    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        phase = settings.get("generation_phase")
        if phase == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "purpose": "页面", "estimated_tokens": 600}]})
            reason = "stop"
        elif phase == "artifact_file":
            text = "<html><body><h1>"
            reason = "length"
        elif phase == "artifact_split_plan":
            text = json.dumps({"parts": [
                {"id": "head", "purpose": "页面开头", "estimated_tokens": 600},
                {"id": "tail", "purpose": "页面结尾", "estimated_tokens": 600},
            ]})
            reason = "stop"
        elif phase == "artifact_part":
            text = "<html><body><h1>" if settings.get("artifact_part_id") == "head" else "学生管理系统</h1></body></html>"
            reason = "stop"
        else:
            text = ""
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


class ContinuationFallbackProvider(PartPlanProvider):
    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        phase = settings.get("generation_phase")
        if phase == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "purpose": "页面", "estimated_tokens": 600}]})
            reason = "stop"
        elif phase == "artifact_file":
            text = "<html><body><h1>"
            reason = "length"
        elif phase == "artifact_split_plan":
            text = json.dumps({"parts": [{"id": "only", "purpose": "不能拆分", "estimated_tokens": 600}]})
            reason = "stop"
        elif phase == "artifact_suffix_repair":
            text = "学生管理"
            reason = "length"
        elif phase == "artifact_continuation":
            text = "系统</h1></body></html>"
            reason = "stop"
        else:
            text = ""
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


class SuffixRepairProvider(PartPlanProvider):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        self.prompts.append(user_prompt)
        phase = settings.get("generation_phase")
        if phase == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "purpose": "页面", "estimated_tokens": 600}]})
            reason = "stop"
        elif phase == "artifact_file":
            text = "<html><body><h1>学生管理"
            reason = "length"
        elif phase == "artifact_split_plan":
            text = json.dumps({"parts": [{"id": "only", "purpose": "不能拆分", "estimated_tokens": 600}]})
            reason = "stop"
        elif phase == "artifact_suffix_repair":
            text = "系统</h1></body></html>"
            reason = "stop"
        else:
            text = ""
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


class TruncatedPlanRecoveryProvider(FilePlanProvider):
    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []

    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        self.system_prompts.append(system_prompt)
        phase = settings.get("generation_phase")
        if phase == "artifact_plan":
            text = '{"files":[{"name":"App.vue","language":"vue"'
            reason = "length"
        elif phase == "artifact_plan_retry":
            text = json.dumps({"files": [{"name": "App.vue", "language": "vue", "estimated_tokens": 900}]})
            reason = "stop"
        else:
            text = "<template><main>学生管理 CRUD</main></template>"
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


class CompleteLengthPlanProvider(FilePlanProvider):
    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        if settings.get("generation_phase") == "artifact_plan":
            text = json.dumps({"files": [{"name": "index.html", "language": "html", "estimated_tokens": 600}]})
            reason = "length"
        else:
            text = "<!doctype html><html><body>学生管理</body></html>"
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


class InvalidPlanFallbackProvider(FilePlanProvider):
    async def generate(self, system_prompt, user_prompt, config=None):
        settings = config or {}
        self.configs.append(settings)
        phase = settings.get("generation_phase")
        name = settings.get("artifact_name")
        if phase in {"artifact_plan", "artifact_plan_retry"}:
            text = '{"files":['
            reason = "length"
        elif phase == "artifact_split_plan":
            text = "{}"
            reason = "stop"
        elif name == "package.json":
            text = '{"scripts":{"dev":"vite"},"dependencies":{"vue":"latest"}}'
            reason = "stop"
        elif name == "index.html":
            text = "<!doctype html><html><body><div id=\"app\"></div></body></html>"
            reason = "stop"
        elif name == "App.vue":
            text = "<template><main>学生管理 CRUD</main></template>"
            reason = "stop"
        elif name == "style.css":
            text = "body { margin: 0; }"
            reason = "stop"
        else:
            text = "import { createApp } from 'vue'"
            reason = "stop"
        return LLMResponse(text=text, input_tokens=20, output_tokens=40, finish_reason=reason, message_content=text)


def test_artifact_mode_generates_and_materializes_independent_file(tmp_path):
    provider = FilePlanProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = WorkflowDefinition(
        id="artifact-mode",
        name="artifact-mode",
        steps=[
            StepDefinition(
                "frontend",
                agent_id="frontend_agent",
                task_template="{{requirement}}",
                output="frontend_result",
                generation_mode="artifacts",
            )
        ],
    )
    repository.create_run("run_artifact_mode", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        WorkflowEventBus(),
        repository,
        artifact_service=artifacts,
    )

    asyncio.run(executor.start("run_artifact_mode", workflow, {"requirement": "开发一个完整网页"}))

    run = repository.get_run("run_artifact_mode")
    assert run["status"] == RunStatus.SUCCESS.value
    assert "按文件独立生成" in run["steps"][0]["output"]
    assert [config["generation_phase"] for config in provider.configs] == ["artifact_plan", "artifact_file"]
    assert all(config.get("continuation") is False for config in provider.configs)
    assert not any(event["type"] == "step.continuing" for event in executor.event_bus._history["run_artifact_mode"])
    assert [item["name"] for item in repository.list_artifacts("run_artifact_mode")] == ["index.html"]


def test_artifact_file_budget_is_not_capped_by_tiny_agent_plan():
    spec = ArtifactFileSpec("style.css", "css", "页面样式", 600)

    assert _file_budget(spec, agent_max_tokens=675, provider_max_tokens=6000) == 2048
    assert _repair_budget(2048, provider_max_tokens=6000) == 2765


def test_file_generation_receives_actual_completed_sibling_source():
    from app.workflow.artifact_generation import generate_artifacts
    prompts = []
    async def request(system, prompt, config, timeout):
        if config["generation_phase"] == "artifact_plan":
            text = json.dumps({"files": [
                {"name": "service.js", "language": "javascript", "estimated_tokens": 300},
                {"name": "controller.js", "language": "javascript", "estimated_tokens": 300},
            ]})
        else:
            prompts.append(prompt)
            text = "export function getAllStudents() { return []; }" if config["artifact_name"] == "service.js" else "import { getAllStudents } from './service.js';"
        return LLMResponse(text=text, input_tokens=5, output_tokens=15, finish_reason="stop", message_content=text)
    async def emit(kind, payload):
        pass
    result = asyncio.run(generate_artifacts(
        system_prompt="generate", original_prompt="build app", base_config={}, request_timeout=10,
        max_tokens=6000, provider_max_tokens=6000, request=request, record_response=lambda response: None, emit=emit,
    ))
    assert len(result.files) == 2
    assert "export function getAllStudents() { return []; }" in prompts[1]


def test_generated_pom_normalizes_wrong_h2_module_before_publication():
    from app.workflow.artifact_generation import generate_artifacts
    events = []
    async def request(system, prompt, config, timeout):
        text = json.dumps({"files": [{"name": "pom.xml", "language": "xml", "estimated_tokens": 300}]}) if config["generation_phase"] == "artifact_plan" else "<project><dependencies><dependency><groupId>org.flywaydb</groupId><artifactId>flyway-database-h2</artifactId></dependency></dependencies></project>"
        return LLMResponse(text=text, input_tokens=5, output_tokens=15, finish_reason="stop", message_content=text)
    async def emit(kind, payload):
        events.append(kind)
    result = asyncio.run(generate_artifacts(
        system_prompt="generate", original_prompt="build backend", base_config={}, request_timeout=10,
        max_tokens=6000, provider_max_tokens=6000, request=request, record_response=lambda response: None, emit=emit,
    ))
    assert "flyway-database-h2" not in result.files[0]["content"]
    assert "flyway-core" in result.files[0]["content"]
    assert "step.artifact_dependency_normalized" in events


def _artifact_workflow(max_tokens: int = 675) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="artifact-mode",
        name="artifact-mode",
        steps=[
            StepDefinition(
                "frontend",
                agent_id="frontend_agent",
                task_template="{{requirement}}",
                output="frontend_result",
                max_tokens=max_tokens,
                generation_mode="artifacts",
            )
        ],
    )


def test_truncated_manifest_retries_with_compact_prompt_and_larger_budget(tmp_path):
    provider = TruncatedPlanRecoveryProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow(max_tokens=6000)
    repository.create_run("run_plan_recovery", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_plan_recovery", workflow, {"requirement": "使用 SpringBoot 和 Vue 开发带 CRUD 的学生管理系统"}))

    assert repository.get_run("run_plan_recovery")["status"] == RunStatus.SUCCESS.value
    assert [config["generation_phase"] for config in provider.configs] == ["artifact_plan", "artifact_plan_retry", "artifact_file"]
    assert [config["max_tokens"] for config in provider.configs[:2]] == [1600, 2000]
    assert all("唯一职责" in prompt for prompt in provider.system_prompts[:2])
    assert any(event["type"] == "step.artifact_plan_recovering" for event in executor.event_bus._history["run_plan_recovery"])


def test_complete_manifest_is_accepted_even_when_provider_reports_length(tmp_path):
    provider = CompleteLengthPlanProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow(max_tokens=6000)
    repository.create_run("run_complete_length_plan", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_complete_length_plan", workflow, {"requirement": "开发一个网页"}))

    assert repository.get_run("run_complete_length_plan")["status"] == RunStatus.SUCCESS.value
    assert [config["generation_phase"] for config in provider.configs] == ["artifact_plan", "artifact_file"]


def test_artifact_files_are_published_as_explicit_downstream_context(tmp_path):
    provider = FilePlanProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow(max_tokens=6000)
    repository.create_run("run_artifact_context", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_artifact_context", workflow, {"requirement": "开发一个网页"}))

    snapshot = repository.get_run("run_artifact_context")["state"]
    files = json.loads(snapshot["context"]["frontend_result_files"])
    assert files[0]["name"] == "index.html"
    assert "<!doctype html>" in files[0]["content"]


def test_invalid_manifest_uses_deterministic_frontend_fallback(tmp_path):
    provider = InvalidPlanFallbackProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow(max_tokens=6000)
    repository.create_run("run_plan_fallback", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_plan_fallback", workflow, {"requirement": "使用 SpringBoot 和 Vue 开发带 CRUD 的学生管理系统"}))

    assert repository.get_run("run_plan_fallback")["status"] == RunStatus.SUCCESS.value
    assert {item["name"] for item in repository.list_artifacts("run_plan_fallback")} == {"package.json", "index.html", "src/main.js", "src/App.vue", "src/style.css"}
    assert any(event["type"] == "step.artifact_plan_fallback" for event in executor.event_bus._history["run_plan_fallback"])


def test_artifact_manifest_can_split_one_file_into_fine_grained_tasks(tmp_path):
    provider = PartPlanProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow()
    repository.create_run("run_artifact_parts", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_artifact_parts", workflow, {"requirement": "开发一个学生管理系统"}))

    assert repository.get_run("run_artifact_parts")["status"] == RunStatus.SUCCESS.value
    assert [config["generation_phase"] for config in provider.configs] == ["artifact_plan", "artifact_part", "artifact_part"]
    assert [config["max_tokens"] for config in provider.configs[1:]] == [2048, 2048]
    assert [item["name"] for item in repository.list_artifacts("run_artifact_parts")] == ["index.html"]


def test_truncated_file_is_split_before_continuation(tmp_path):
    provider = SplitBeforeContinuationProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow()
    repository.create_run("run_artifact_split", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_artifact_split", workflow, {"requirement": "开发一个学生管理页面"}))

    assert repository.get_run("run_artifact_split")["status"] == RunStatus.SUCCESS.value
    phases = [config["generation_phase"] for config in provider.configs]
    assert phases == ["artifact_plan", "artifact_file", "artifact_split_plan", "artifact_part", "artifact_part"]
    assert "artifact_continuation" not in phases


def test_continuation_is_only_the_last_artifact_fallback(tmp_path):
    provider = ContinuationFallbackProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow()
    repository.create_run("run_artifact_continuation", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_artifact_continuation", workflow, {"requirement": "开发一个学生管理页面"}))

    assert repository.get_run("run_artifact_continuation")["status"] == RunStatus.SUCCESS.value
    phases = [config["generation_phase"] for config in provider.configs]
    assert phases == ["artifact_plan", "artifact_file", "artifact_split_plan", "artifact_suffix_repair", "artifact_continuation"]


def test_truncated_file_is_completed_by_suffix_merge_without_regenerating_file(tmp_path):
    provider = SuffixRepairProvider()
    repository = SQLiteRepository(":memory:")
    artifacts = ArtifactService(repository, tmp_path / "workspaces")
    workflow = _artifact_workflow()
    repository.create_run("run_artifact_suffix", workflow.id, {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository, artifact_service=artifacts)

    asyncio.run(executor.start("run_artifact_suffix", workflow, {"requirement": "开发一个学生管理页面"}))

    assert repository.get_run("run_artifact_suffix")["status"] == RunStatus.SUCCESS.value
    phases = [config["generation_phase"] for config in provider.configs]
    assert phases == ["artifact_plan", "artifact_file", "artifact_split_plan", "artifact_suffix_repair"]
    assert "绝对不要重新输出整个文件" in provider.prompts[-1]
    assert [item["name"] for item in repository.list_artifacts("run_artifact_suffix")] == ["index.html"]
