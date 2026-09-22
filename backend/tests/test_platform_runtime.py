from __future__ import annotations

from pathlib import Path

from app.code_company.runtime import CodeCompanyRuntime
from app.code_company.planner import DynamicPlanner
from app.code_company.repair import RepairEngine
from app.code_company.repository_intelligence import RepositoryIndexer
from app.platform.tool_gateway import ToolCall, ToolPolicy
from app.platform.workspace import LocalWorkspaceService, WorkspacePathError


def test_workspace_ref_is_run_scoped_and_recoverable(tmp_path: Path) -> None:
    runtime = CodeCompanyRuntime.from_root(tmp_path / "workspaces")
    workspace = runtime.prepare_run(
        "run_abc123",
        {
            "project_id": "student-admin",
            "repository_id": "repo-main",
            "base_branch": "main",
            "base_commit_sha": "abc123",
        },
    )

    assert workspace.workspace_id == "ws_run_abc123"
    assert workspace.project_id == "student-admin"
    assert Path(workspace.worktree_path).name == "run_abc123"
    assert runtime.capabilities()["write_tools_enabled"] is True


def test_workspace_rejects_escape_and_sensitive_paths(tmp_path: Path) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_safe")

    try:
        service.resolve_relative(workspace, "../outside.txt")
    except WorkspacePathError:
        pass
    else:
        raise AssertionError("path traversal must be rejected")

    assert service.can_read(workspace, ".env") is False
    assert service.can_read(workspace, "nested/app.py") is True


def test_tool_gateway_reads_searches_and_denies_unapproved_tools(tmp_path: Path) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_tools")
    root = Path(workspace.worktree_path)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def hello():\n    return 'ok'\n", encoding="utf-8")
    (root / ".env").write_text("MODEL_API_KEY=secret", encoding="utf-8")
    gateway = CodeCompanyRuntime(service).tool_gateway

    tree = gateway.execute(workspace, ToolCall("repo.tree", {"max_depth": 3}))
    assert tree.success is True
    assert "src/app.py" in tree.output
    assert ".env" not in tree.output

    read = gateway.execute(workspace, ToolCall("repo.read", {"path": "src/app.py"}))
    assert read.success is True
    assert "hello" in read.output

    search = gateway.execute(workspace, ToolCall("repo.search", {"query": "hello"}))
    assert search.success is True
    assert "src/app.py:1" in search.output

    secret = gateway.execute(workspace, ToolCall("repo.read", {"path": ".env"}))
    assert secret.success is False

    shell = gateway.execute(workspace, ToolCall("shell.execute", {"command": ["echo", "unsafe"]}))
    assert shell.success is False
    assert "not allowed" in (shell.error or "")

    write = gateway.execute(workspace, ToolCall("fs.write", {"path": "src/generated.py", "content": "# generated\n"}))
    assert write.success is True
    assert (root / "src" / "generated.py").read_text(encoding="utf-8") == "# generated\n"

    patch = gateway.execute(workspace, ToolCall("fs.patch", {"path": "src/generated.py", "old_text": "generated", "new_text": "patched"}))
    assert patch.success is True
    assert "patched" in (root / "src" / "generated.py").read_text(encoding="utf-8")


def test_tool_gateway_output_limit_is_enforced(tmp_path: Path) -> None:
    service = LocalWorkspaceService(tmp_path / "workspaces")
    workspace = service.create_for_run("run_limit")
    path = Path(workspace.worktree_path) / "large.txt"
    path.write_text("x" * 100, encoding="utf-8")
    gateway = CodeCompanyRuntime(service).tool_gateway
    gateway.policy = ToolPolicy(max_output_bytes=10)

    result = gateway.execute(workspace, ToolCall("repo.read", {"path": "large.txt"}))
    assert result.success is True
    assert result.truncated is True
    assert len(result.output.encode("utf-8")) <= 10


def test_existing_repo_is_copied_and_indexed_without_sensitive_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "StudentService.java").write_text(
        "public class StudentService {\n public void listStudents() {}\n}\n", encoding="utf-8"
    )
    (source / ".env").write_text("MODEL_API_KEY=secret", encoding="utf-8")
    runtime = CodeCompanyRuntime.from_root(tmp_path / "workspaces")

    workspace = runtime.prepare_run("run_existing", {"repo_path": str(source)})
    assert workspace.mode == "existing_repo"
    assert (Path(workspace.worktree_path) / "src" / "StudentService.java").exists()
    assert not (Path(workspace.worktree_path) / ".env").exists()

    index = RepositoryIndexer(runtime.workspace_service).index(workspace)
    assert "src/StudentService.java" in index.tree
    assert any(item.name == "StudentService" for item in index.symbols)
    assert index.relevant_files("StudentService") == ["src/StudentService.java"]


def test_dynamic_planner_keeps_crud_backend_and_frontend() -> None:
    plan = DynamicPlanner().plan("开发一个 Spring Boot + Vue 学生管理系统，支持增删改查")
    owners = {task.owner for task in plan.tasks}
    assert {"backend", "frontend", "tester", "reviewer"}.issubset(owners)
    assert plan.tasks[-1].owner == "reviewer"


def test_node_failure_is_routed_back_to_the_producing_agent() -> None:
    route = RepairEngine().route_node_failure(
        "architecture",
        "Architecture output validation failed: expected a valid JSON object",
        response_present=True,
    )
    assert route["owner_step"] == "architecture"
    assert route["action"] == "retry_same_node"
    assert route["category"] == "structured_output"
