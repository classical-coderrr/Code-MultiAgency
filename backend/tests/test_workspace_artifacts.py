from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.platform.tool_gateway import ToolPolicy
from app.workflow.workspace_artifacts import WorkspaceArtifactCollector


def _collector() -> WorkspaceArtifactCollector:
    collector = object.__new__(WorkspaceArtifactCollector)
    collector.runtime = SimpleNamespace(
        tool_gateway=SimpleNamespace(policy=ToolPolicy.coding_loop(mutable_path_globs=("**", "*")))
    )
    return collector


def test_file_plan_owner_overrides_overlapping_workspace_globs(tmp_path: Path) -> None:
    files = {
        "src/main/java/com/example/app/Application.java": "class Application {}",
        "src/main/resources/schema.sql": "create table items(id bigint);",
        "src/App.vue": "<template />",
        "src/components/ItemManager.vue": "<template />",
        "src/style.css": ".app {}",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    plan = [
        {"path": name, "owner": owner}
        for name, owner in [
            ("src/main/java/com/example/app/Application.java", "backend"),
            ("src/main/resources/schema.sql", "database"),
            ("src/App.vue", "frontend"),
            ("src/components/ItemManager.vue", "frontend"),
            ("src/style.css", "frontend"),
        ]
    ]
    ownership = {
        "backend": ["src/main/java/**"],
        "database": ["src/main/resources/schema.sql"],
        "frontend": ["src/*.vue", "src/*.css"],
    }

    collected = _collector().collect_workspace(
        {"worktree_path": str(tmp_path)}, "frontend", ownership=ownership, file_plan=plan,
    )

    assert {item["name"] for item in collected} == {
        "src/App.vue", "src/components/ItemManager.vue", "src/style.css",
    }


def test_owner_merge_replaces_duplicate_path_and_preserves_foreign_artifacts() -> None:
    original = [
        {"step_id": "backend", "name": "src/main/java/App.java", "content": "backend"},
        {"step_id": "database", "name": "src/main/resources/schema.sql", "content": "create"},
        {"step_id": "frontend", "name": "src/App.vue", "content": "old"},
        {"step_id": "frontend", "name": "src/App.vue", "content": "stale duplicate"},
    ]
    file_plan = [
        {"owner": "backend", "path": "src/main/java/App.java"},
        {"owner": "database", "path": "src/main/resources/schema.sql"},
        {"owner": "frontend", "path": "src/App.vue"},
        {"owner": "frontend", "path": "src/components/List.vue"},
    ]

    merged = WorkspaceArtifactCollector.merge_owner_artifacts(
        original,
        [
            {"step_id": "frontend", "name": "src/App.vue", "content": "new"},
            {"step_id": "frontend", "name": "src/components/List.vue", "content": "component"},
            {"step_id": "frontend", "name": "src/main/java/App.java", "content": "foreign"},
        ],
        "frontend",
        file_plan=file_plan,
    )

    assert len({item["name"].casefold() for item in merged}) == len(merged)
    assert {item["name"]: item["content"] for item in merged} == {
        "src/main/java/App.java": "backend",
        "src/main/resources/schema.sql": "create",
        "src/App.vue": "new",
        "src/components/List.vue": "component",
    }


def test_owner_merge_rejects_same_path_with_unplanned_different_owners() -> None:
    with pytest.raises(ValueError, match="conflicting owners"):
        WorkspaceArtifactCollector.merge_owner_artifacts(
            [
                {"step_id": "backend", "name": "src/App.java", "content": "a"},
                {"step_id": "frontend", "name": "src/App.java", "content": "b"},
            ],
            [],
            "frontend",
        )


def test_planned_component_path_is_writable_even_if_not_matched_by_owner_glob() -> None:
    policy = ToolPolicy.coding_loop(
        mutable_path_globs=("src/*.vue",),
        exact_write_paths=frozenset({"src/components/RoomManager.vue"}),
    )

    assert policy.permits_write_path("src/components/RoomManager.vue")
    assert not policy.permits_write_path("src/components/Unplanned.vue")
    assert not policy.permits_write_path(".env")
