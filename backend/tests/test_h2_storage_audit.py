import asyncio
import copy
import json

import httpx
import pytest

from app.workflow.artifact_validator import ArtifactValidator, _SpringCrudSpec


@pytest.mark.parametrize("defect", [None, "create", "update", "update-secondary", "delete", "wrong-database"])
def test_api_green_cannot_hide_missing_database_writes(defect):
    async def run():
        api_rows = []
        stored = []
        def handler(request):
            if request.url.path == "/probe":
                return httpx.Response(200, json={"databaseProduct": "SQLite" if defect == "wrong-database" else "H2", "tables": {"books": stored}})
            if request.method == "POST":
                row = {**json.loads(request.content), "bookId": 9}
                api_rows.append(row)
                if defect != "create":
                    stored.append({"book_id": 9, "title": row["title"], "category": row["category"]})
                return httpx.Response(201, json=row)
            if request.method == "GET":
                return httpx.Response(200, json=api_rows)
            if request.method == "PUT":
                api_rows[0].update(json.loads(request.content))
                if defect != "update":
                    stored[0]["title"] = api_rows[0]["title"]
                if defect != "update-secondary":
                    stored[0]["category"] = api_rows[0]["category"]
                return httpx.Response(200, json=api_rows[0])
            api_rows.clear()
            if defect != "delete":
                stored.clear()
            return httpx.Response(204)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await ArtifactValidator._probe_spring_crud(client, "http://localhost", _SpringCrudSpec("/api/books", {"title": "Book", "category": "Course"}, "title", frozenset({"GET", "POST", "PUT", "DELETE"}), "bookId", False, "/probe"))
        assert result.status == ("failed" if defect else "passed")
        if defect:
            assert result.evidence is None
        else:
            assert result.evidence["storageVerified"]
            assert result.evidence["databaseProduct"] == "H2"
    asyncio.run(run())


def test_probe_only_in_temporary_copy_and_only_reads_known_tables(tmp_path):
    files = {
        "src/main/java/demo/Main.java": "package demo; @SpringBootApplication class Main {}",
        "src/main/resources/schema.sql": 'CREATE TABLE IF NOT EXISTS "books"(id BIGINT); CREATE TABLE second_table(id BIGINT);',
    }
    initial = copy.deepcopy(files)
    path = ArtifactValidator._install_h2_probe(tmp_path, files)
    sources = list(tmp_path.rglob("*.java"))
    assert files == initial
    assert len(sources) == 1
    content = sources[0].read_text(encoding="utf-8")
    assert path.startswith("/__agent_team_validation/database/")
    assert '@Profile("agent-team-validation")' in content
    assert '"books","second_table"' in content
    assert "SELECT * FROM " in content
    assert "executeUpdate" not in content


def test_unknown_sql_and_missing_startup_entry_do_not_get_fake_probe(tmp_path):
    assert ArtifactValidator._install_h2_probe(tmp_path, {"schema.sql": "-- no tables"}) == ""
    assert not list(tmp_path.rglob("*.java"))
