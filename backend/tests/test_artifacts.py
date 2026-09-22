from pathlib import Path
import hashlib
import zipfile
import pytest

from app.repositories.sqlite import SQLiteRepository
from app.services.artifacts import ArtifactService


def test_materialize_rejects_conflicting_versions_of_same_owned_file(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")
    with pytest.raises(ValueError, match="相互冲突"):
        service.materialize("run_conflict", "software-development", {
            "delivery_contract": {"schema_version": "2.0"},
            "__artifact_files__": [
                {"step_id": "frontend", "name": "index.html", "language": "html", "content": "<html>old</html>"},
                {"step_id": "frontend", "name": "index.html", "language": "html", "content": "<html>new</html>"},
            ],
        }, None)
    assert repository.list_artifacts("run_conflict") == []


def test_materialize_report_and_html_into_run_workspace(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    artifacts = service.materialize(
        "run_artifact",
        "software-development",
        {
            "frontend_result": "```html\n<!doctype html><html><body><h1>广告页</h1></body></html>\n```",
        },
        "# 实现方案\n\n已生成静态页面。",
    )

    assert {item["name"] for item in artifacts} == {"final-report.md", "index.html"}
    records = repository.list_artifacts("run_artifact")
    assert len(records) == 2
    assert (tmp_path / "workspaces" / "run_artifact" / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert service.resolve_path("run_artifact", "run_artifact_index_html")
    assert records[0]["sha256"]


def test_artifact_workspace_rejects_path_escape(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    try:
        service.materialize("../outside", "software-development", {}, "report")
    except ValueError as exc:
        assert "run id" in str(exc).lower()
    else:
        raise AssertionError("path traversal run id was accepted")


def test_materialize_extracts_named_frontend_and_backend_source_files(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    artifacts = service.materialize(
        "run_source",
        "software-development",
        {
            "frontend_result": "```html index.html\n<!doctype html><html><body>demo</body></html>\n```\n```css style.css\nbody { color: red; }\n```",
            "backend_result": "```python filename=main.py\nprint('ok')\n```",
        },
        "",
    )

    assert {item["name"] for item in artifacts} == {"index.html", "style.css", "main.py"}
    assert (tmp_path / "workspaces" / "run_source" / "style.css").read_text(encoding="utf-8") == "body { color: red; }"
    assert (tmp_path / "workspaces" / "run_source" / "main.py").read_text(encoding="utf-8") == "print('ok')"


def test_materialize_reads_structured_artifact_files_from_run_context(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    artifacts = service.materialize(
        "run_structured",
        "software-development",
        {
            "__artifact_files__": [
                {"step_id": "frontend", "name": "index.html", "language": "html", "content": "<html><body>ok</body></html>"},
                {"step_id": "frontend", "name": "style.css", "language": "css", "content": "body { color: red; }"},
            ],
            "frontend_result": "成果物已按文件独立生成",
        },
        "最终报告",
    )

    assert {item["name"] for item in artifacts} == {"final-report.md", "index.html", "style.css"}
    assert (tmp_path / "workspaces" / "run_structured" / "index.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"
    index_record = next(item for item in artifacts if item["name"] == "index.html")
    assert index_record["ownerStep"] == "frontend"
    assert index_record["sha256"] == hashlib.sha256(b"<html><body>ok</body></html>").hexdigest()


def test_corrupted_artifact_is_not_downloadable(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_integrity",
        "software-development",
        {"__artifact_files__": [{"name": "index.html", "language": "html", "content": "<html>ok</html>"}]},
        "",
    )
    path = tmp_path / "workspaces" / "run_integrity" / "index.html"
    path.write_text("<html>tampered</html>", encoding="utf-8")

    assert service.resolve_path("run_integrity", "run_integrity_index_html") is None
    assert service.list_public("run_integrity") == []


def test_create_archive_contains_all_registered_artifacts(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_archive",
        "software-development",
        {
            "__artifact_files__": [
                {"name": "index.html", "language": "html", "content": "<html>ok</html>"},
                {"name": "style.css", "language": "css", "content": "body { color: red; }"},
            ],
        },
        "最终报告",
    )
    (tmp_path / "workspaces" / "run_archive" / "unregistered.txt").write_text("workspace file", encoding="utf-8")

    archive_path = service.create_archive("run_archive")

    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"final-report.md", "index.html", "style.css", "unregistered.txt"}
        assert archive.read("index.html") == b"<html>ok</html>"
        assert archive.read("unregistered.txt") == b"workspace file"


def test_archive_groups_multi_agent_projects_by_owner(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_fullstack",
        "software-development",
        {
            "__artifact_files__": [
                {"step_id": "frontend", "name": "package.json", "language": "json", "content": "{}"},
                {"step_id": "frontend", "name": "src/App.vue", "language": "vue", "content": "<template />"},
                {"step_id": "backend", "name": "pom.xml", "language": "xml", "content": "<project />"},
                {"step_id": "backend", "name": "backend/README.md", "language": "markdown", "content": "backend"},
            ],
        },
        "report",
    )
    (tmp_path / "workspaces" / "run_fullstack" / "stale-download.zip").write_bytes(b"old archive")

    archive_path = service.create_archive("run_fullstack")

    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "final-report.md",
            "frontend/package.json",
            "frontend/src/App.vue",
            "backend/pom.xml",
            "backend/README.md",
        }


def test_materialize_and_archive_preserve_nested_project_paths(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_nested",
        "software-development",
        {
            "__artifact_files__": [
                {"name": "src/main.js", "language": "javascript", "content": "console.log('ok')"},
                {"name": "src/main/java/com/example/App.java", "language": "java", "content": "package com.example; class App {}"},
            ],
        },
        "",
    )

    assert (tmp_path / "workspaces" / "run_nested" / "src" / "main.js").read_text(encoding="utf-8") == "console.log('ok')"
    archive_path = service.create_archive("run_nested")
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"src/main.js", "src/main/java/com/example/App.java"}


def test_list_public_hides_stale_artifact_rows(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_stale",
        "software-development",
        {"__artifact_files__": [{"name": "archive.json", "language": "json", "content": "{}"}]},
        "",
    )
    (tmp_path / "workspaces" / "run_stale" / "archive.json").unlink()

    assert service.list_public("run_stale") == []
def test_archive_embeds_spring_static_and_database_resources_in_backend(tmp_path: Path):
    repository = SQLiteRepository(":memory:")
    service = ArtifactService(repository, tmp_path / "workspaces")

    service.materialize(
        "run_spring_html",
        "software-development",
        {
            "__artifact_files__": [
                {
                    "step_id": "backend",
                    "name": "pom.xml",
                    "language": "xml",
                    "content": "<project />",
                },
                {
                    "step_id": "frontend",
                    "name": "src/main/resources/static/index.html",
                    "language": "html",
                    "content": "<html><body>ok</body></html>",
                },
                {
                    "step_id": "database",
                    "name": "src/main/resources/schema.sql",
                    "language": "sql",
                    "content": "create table student(id bigint);",
                },
            ],
        },
        "report",
    )

    archive_path = service.create_archive("run_spring_html")

    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "final-report.md",
            "backend/pom.xml",
            "backend/src/main/resources/static/index.html",
            "backend/src/main/resources/schema.sql",
        }
