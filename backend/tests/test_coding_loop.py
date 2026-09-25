from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.code_company.coding_loop import CodingAgentLoop, CodingLoopConfig, CodingLoopError
from app.code_company.runtime import CodeCompanyRuntime
from app.llm.base import LLMProvider, LLMResponse
from app.platform.tool_gateway import LocalToolGateway, ToolPolicy
from app.platform.tool_gateway import ToolCall
from app.platform.sandbox import SandboxResult
from app.platform.workspace import LocalWorkspaceService
from app.repositories.sqlite import SQLiteRepository
from app.workflow.executor import WorkflowExecutor


class ScriptedProvider(LLMProvider):
    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = list(outputs)
        self.configs: list[dict[str, Any]] = []

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "test", "model": "scripted", "supportsThinking": False,
            "supportedLevels": ["off"], "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt: str, user_prompt: str, config: dict[str, Any] | None = None) -> LLMResponse:
        self.configs.append(dict(config or {}))
        if not self.outputs:
            raise AssertionError("unexpected model turn")
        return self.outputs.pop(0)


def test_auto_coding_budget_scales_with_frozen_files_but_manual_remains_a_hard_cap() -> None:
    file_plan = [
        {"owner": "backend", "path": "pom.xml"},
        {"owner": "backend", "path": "src/main/java/app/Student.java"},
        {"owner": "backend", "path": "src/main/java/app/StudentController.java"},
        {"owner": "frontend", "path": "src/App.vue"},
    ]

    automatic = WorkflowExecutor._coding_loop_budget(file_plan, "backend", 6000, 6000, "auto")
    manual = WorkflowExecutor._coding_loop_budget(file_plan, "backend", 6000, 6000, "manual")

    assert automatic > 6000
    assert automatic <= 128000
    assert manual == 6000


def _response(value: dict[str, Any], *, output_tokens: int = 10) -> LLMResponse:
    return LLMResponse(
        text=json.dumps(value, ensure_ascii=False),
        input_tokens=5,
        output_tokens=output_tokens,
        finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": output_tokens},
    )


def _runtime(tmp_path: Path, *, repository: SQLiteRepository | None = None):
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_coding_loop")
    gateway = LocalToolGateway(
        service,
        ToolPolicy.coding_loop(mutable_path_globs=("*", "src/**")),
    )
    return workspace, gateway


def test_coding_loop_creates_files_then_submits_unverified_candidate(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    provider = ScriptedProvider([
        _response({"type": "final", "content": "I did not modify any files."}),
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {
            "path": "index.html", "content": "<main>Ready</main>",
        }}),
        _response({"type": "final", "content": "Created index.html."}),
    ])
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    result = asyncio.run(CodingAgentLoop(provider, gateway).run(
        workspace,
        "Create a small HTML page.",
        config=CodingLoopConfig(max_iterations=4, max_tool_actions=2, max_tokens=100),
        emit=emit,
    ))

    assert Path(workspace.worktree_path, "index.html").read_text(encoding="utf-8") == "<main>Ready</main>"
    assert result.verified is False
    assert result.iterations == 3
    assert "coding_loop.protocol_error" in events
    assert "coding_loop.final_pending_validation" in events


def test_coding_loop_can_repair_after_fixed_owner_verification(tmp_path: Path, monkeypatch) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_verify")
    initial = "<project>broken</project>"
    digest = hashlib.sha256(initial.encode()).hexdigest()
    calls: list[tuple[str, ...]] = []

    class FakeSandbox:
        async def arun(self, _workspace, command, *, timeout_seconds):
            calls.append(tuple(command))
            return SandboxResult(tuple(command), 1 if len(calls) == 1 else 0,
                                 stderr="compile error" if len(calls) == 1 else "")

    monkeypatch.setattr("app.platform.tool_gateway.shutil.which", lambda name: name)
    gateway = LocalToolGateway(
        service,
        ToolPolicy.coding_loop(mutable_path_globs=("pom.xml",), verification_gates=frozenset({"backend-compile"})),
        sandbox=FakeSandbox(),
    )
    assert gateway.coding_action_intent(workspace, ToolCall("verify.run", {"gate": "backend-compile"})) == {
        "operation": "verify", "gate": "backend-compile",
    }
    assert gateway.execute(workspace, ToolCall("verify.run", {"gate": "frontend-build"})).success is False
    assert gateway.execute(workspace, ToolCall("verify.run", {"gate": "backend-compile", "command": ["echo", "bad"]})).success is False
    assert gateway.execute(workspace, ToolCall("shell.execute", {"command": ["mvn", "test"]})).success is False

    provider = ScriptedProvider([
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {"path": "pom.xml", "content": initial}}),
        _response({"type": "tool_call", "tool": "verify.run", "arguments": {"gate": "backend-compile"}}),
        _response({"type": "tool_call", "tool": "fs.patch", "arguments": {
            "path": "pom.xml", "old_text": "broken", "new_text": "fixed", "expected_sha256": digest,
        }}),
        _response({"type": "tool_call", "tool": "verify.run", "arguments": {"gate": "backend-compile"}}),
        _response({"type": "final", "content": "fixed after compile failure"}),
    ])
    result = asyncio.run(CodingAgentLoop(provider, gateway).run(
        workspace, "fix backend", config=CodingLoopConfig(max_iterations=6, max_tool_actions=4, max_tokens=200),
    ))
    assert [item.success for item in result.tool_results] == [True, False, True, True]
    assert len(calls) == 2
    assert Path(workspace.worktree_path, "pom.xml").read_text(encoding="utf-8") == "<project>fixed</project>"
    assert result.verified is False  # the outer Tester still owns final acceptance


def test_frontend_verification_restores_only_frozen_dependencies(tmp_path: Path, monkeypatch) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_frontend_verify")
    root = Path(workspace.worktree_path)
    (root / "package.json").write_text(json.dumps({
        "scripts": {"build": "vite build", "prebuild": "echo should-not-run", "postbuild": "echo should-not-run"},
        "dependencies": {"vue": "^3"},
        "devDependencies": {"vite": "^6"},
    }), encoding="utf-8")
    monkeypatch.setattr("app.platform.tool_gateway.shutil.which", lambda name: name)
    commands: list[tuple[str, ...]] = []

    class FakeSandbox:
        async def arun(self, _workspace, command, *, timeout_seconds):
            commands.append(tuple(command))
            return SandboxResult(tuple(command), 0, stdout="ok")

    gateway = LocalToolGateway(
        service,
        ToolPolicy.coding_loop(
            mutable_path_globs=("package.json", "src/**"),
            verification_gates=frozenset({"frontend-build"}),
            verification_dependencies=frozenset({"vue", "vite"}),
        ),
        sandbox=FakeSandbox(),
    )
    result = asyncio.run(gateway.aexecute(workspace, ToolCall("verify.run", {"gate": "frontend-build"})))
    assert result.success
    assert commands[0][1] == "install"
    assert commands[1][-1] == "build"
    assert commands[1][-2].replace("\\", "/").endswith("node_modules/vite/bin/vite.js")
    assert "npm" not in Path(commands[1][0]).name.lower()
    assert "--ignore-scripts" in commands[0]

    (root / "package.json").write_text(json.dumps({
        "scripts": {"build": "rm -rf src"},
        "dependencies": {"vue": "^3"},
    }), encoding="utf-8")
    denied = asyncio.run(gateway.aexecute(workspace, ToolCall("verify.run", {"gate": "frontend-build"})))
    assert not denied.success
    assert len(commands) == 2  # no untrusted project script was launched

    (root / "package.json").write_text(json.dumps({
        "scripts": {"build": "vite build"},
        "dependencies": {"vue": "file:../../private"},
    }), encoding="utf-8")
    denied_dependency = asyncio.run(gateway.aexecute(workspace, ToolCall("verify.run", {"gate": "frontend-build"})))
    assert not denied_dependency.success
    assert len(commands) == 2


def test_owner_verification_gates_follow_frozen_database_boundary(tmp_path: Path) -> None:
    runtime = CodeCompanyRuntime.from_root(tmp_path / "workspaces")
    blueprint = {
        "database": {"mode": "h2"},
        "artifact_ownership": {"backend": ["pom.xml", "src/main/java/**"], "frontend": ["package.json", "src/**"]},
        "dependency_manifest": {"frontend": {
            "dependencies": {"vue": "^3.5.0"},
            "dev_dependencies": {"vite": "^6.0.0"},
        }},
    }
    backend = runtime.gateway_for_owner("backend", blueprint, coding_loop=True)
    frontend = runtime.gateway_for_owner("frontend", blueprint, coding_loop=True)
    assert backend.policy.verification_gates == frozenset({"backend-compile"})
    assert frontend.policy.verification_dependencies == frozenset({"vue", "vite"})
    assert "shell.execute" not in backend.policy.allowed_tools


def test_unplanned_owned_helper_requires_audited_plan_amendment(tmp_path: Path) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_plan_amendment")
    gateway = LocalToolGateway(service, ToolPolicy.coding_loop(
        mutable_path_globs=("src/**",),
        exact_write_paths=frozenset({"src/App.vue"}),
    ))
    frozen = gateway.execute(workspace, ToolCall("fs.create", {
        "path": "src/App.vue", "content": "<template><main>OK</main></template>",
    }))
    assert frozen.success

    unplanned = ToolCall("fs.create", {
        "path": "src/helpers/format.ts", "content": "export const format = String;",
    })
    with pytest.raises(ValueError, match="requires reason"):
        gateway.coding_action_intent(workspace, unplanned)
    assert not gateway.execute(workspace, unplanned).success
    assert not Path(workspace.worktree_path, "src/helpers/format.ts").exists()

    unplanned.arguments["reason"] = "Share display formatting between two owned components"
    intent = gateway.coding_action_intent(workspace, unplanned)
    assert intent["planAmendmentReason"] == unplanned.arguments["reason"]
    created = gateway.execute(workspace, unplanned)
    assert created.success
    assert created.metadata["planAmendmentReason"] == unplanned.arguments["reason"]
    assert gateway.audit_log[-1]["arguments"]["reason"] == unplanned.arguments["reason"]

    denied = gateway.execute(workspace, ToolCall("fs.create", {
        "path": "pom.xml", "content": "<project/>", "reason": "A separate backend project is useful",
    }))
    assert not denied.success
    assert not Path(workspace.worktree_path, "pom.xml").exists()


def test_coding_loop_obeys_per_call_cap_and_requires_all_frozen_files(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    provider = ScriptedProvider([
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {
            "path": "index.html", "content": "<main>Ready</main>",
        }}),
        _response({"type": "final", "content": "All done."}),
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {
            "path": "style.css", "content": "main { color: teal; }",
        }}),
        _response({"type": "final", "content": "Both frozen files are present."}),
    ])
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    result = asyncio.run(CodingAgentLoop(provider, gateway).run(
        workspace,
        "Create the frozen deliverables.",
        config=CodingLoopConfig(
            max_iterations=6,
            max_tool_actions=3,
            max_tokens=100,
            response_max_tokens=25,
            required_artifacts=("index.html", "style.css"),
        ),
        emit=emit,
    ))

    assert result.verified is False
    assert Path(workspace.worktree_path, "index.html").is_file()
    assert Path(workspace.worktree_path, "style.css").is_file()
    assert all(config["max_tokens"] <= 25 for config in provider.configs)
    assert any(kind == "coding_loop.required_files_missing" for kind, _ in events)


def test_coding_loop_persists_turns_actions_and_does_not_replay_completed_write(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    repository = SQLiteRepository(tmp_path / "runs.sqlite3")
    first_provider = ScriptedProvider([
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {
            "path": "src/app.py", "content": "print('ok')\n",
        }}),
        _response({"type": "final", "content": "Created app."}),
    ])
    first = asyncio.run(CodingAgentLoop(first_provider, gateway, repository).run(
        workspace, "Create a Python entry point.",
        config=CodingLoopConfig(max_iterations=4, max_tool_actions=3, max_tokens=100),
        run_id="run_coding_loop", step_id="backend",
    ))

    action_rows = repository.list_coding_actions("run_coding_loop", "backend")
    turn_rows = repository.list_coding_turns("run_coding_loop", "backend")
    assert first.verified is False
    assert len(action_rows) == 1
    assert [row["status"] for row in turn_rows] == ["COMPLETED", "COMPLETED"]

    second_provider = ScriptedProvider([_response({"type": "final", "content": "No further changes."})])
    second = asyncio.run(CodingAgentLoop(second_provider, gateway, repository).run(
        workspace, "Continue the same task.",
        config=CodingLoopConfig(max_iterations=4, max_tool_actions=3, max_tokens=100),
        run_id="run_coding_loop", step_id="backend",
    ))
    assert second.iterations == 3
    assert len(repository.list_coding_actions("run_coding_loop", "backend")) == 1
    assert len(repository.list_coding_turns("run_coding_loop", "backend")) == 3
    assert Path(workspace.worktree_path, "src", "app.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_interrupted_model_turn_is_charged_before_resume(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    repository = SQLiteRepository(tmp_path / "runs.sqlite3")
    repository.start_coding_turn(
        run_id="run_coding_loop", step_id="frontend", turn_no=1, requested_max_tokens=60,
    )
    provider = ScriptedProvider([_response({"type": "tool_call", "tool": "fs.create", "arguments": {
        "path": "index.html", "content": "<main>resumed</main>",
    }}, output_tokens=10)])

    result = asyncio.run(CodingAgentLoop(provider, gateway, repository).run(
        workspace, "Finish the page.",
        config=CodingLoopConfig(max_iterations=2, max_tool_actions=1, max_tokens=100),
        run_id="run_coding_loop", step_id="frontend",
    ))

    turns = repository.list_coding_turns("run_coding_loop", "frontend")
    assert turns[0]["status"] == "INTERRUPTED"
    assert turns[0]["output_tokens"] == 60
    assert provider.configs[0]["max_tokens"] == 40
    assert result.iterations == 2


def test_coding_loop_rejects_writes_outside_frozen_ownership(tmp_path: Path) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_coding_loop")
    gateway = LocalToolGateway(
        service,
        ToolPolicy.coding_loop(mutable_path_globs=("src/**",)),
    )
    provider = ScriptedProvider([
        _response({"type": "tool_call", "tool": "fs.create", "arguments": {
            "path": "README.md", "content": "outside owner",
        }}),
        _response({"type": "final", "content": "No permitted changes were required."}),
    ])
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    with pytest.raises(CodingLoopError, match="轮数上限"):
        asyncio.run(CodingAgentLoop(provider, gateway).run(
            workspace, "Try to create a file outside ownership.",
            config=CodingLoopConfig(max_iterations=2, max_tool_actions=2, max_tokens=100),
            emit=emit,
        ))

    assert not Path(workspace.worktree_path, "README.md").exists()
    assert "coding_loop.action_rejected" in events


def test_coding_loop_breaks_on_identical_successful_tree_reads(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    provider = ScriptedProvider([
        _response({"type": "tool_call", "tool": "repo.tree", "arguments": {}}),
        _response({"type": "tool_call", "tool": "repo.tree", "arguments": {}}),
    ])
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    with pytest.raises(CodingLoopError, match="相同目录或文件读取"):
        asyncio.run(CodingAgentLoop(provider, gateway).run(
            workspace, "Create the required source files.",
            config=CodingLoopConfig(max_iterations=5, max_tool_actions=4, max_tokens=100),
            emit=emit,
        ))

    assert len(provider.configs) == 2
    assert "coding_loop.no_progress" in events


def test_coding_loop_breaks_on_repeated_invalid_protocol_responses(tmp_path: Path) -> None:
    workspace, gateway = _runtime(tmp_path)
    provider = ScriptedProvider([
        LLMResponse(text="I am unable to write files in this environment."),
        LLMResponse(text="The requested files were not modified."),
    ])
    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    with pytest.raises(CodingLoopError, match="连续两轮未遵守编码协议"):
        asyncio.run(CodingAgentLoop(provider, gateway).run(
            workspace, "Create the required source files.",
            config=CodingLoopConfig(max_iterations=8, max_tool_actions=4, max_tokens=100),
            emit=emit,
        ))

    assert len(provider.configs) == 2
    assert "coding_loop.no_progress" in events
