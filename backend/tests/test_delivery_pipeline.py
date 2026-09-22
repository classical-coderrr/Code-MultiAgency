"""Real finalization -> materialization -> ZIP -> delivery -> persisted status.

Only build/CRUD/browser evidence is synthetic in finalization fixtures. The
executor, ArtifactService, gate, event bus and SQLite repository are real;
SQLite is explicitly in-memory and files live under tmp_path. Static tests
also execute ArtifactValidator's real isolated local HTTP server and browser. No model,
npm/Maven/Gradle command, external HTTP service or historical DB is used.
"""

import asyncio
import copy
import hashlib
import json
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.agents.registry import AgentRegistry
from app.repositories.sqlite import SQLiteRepository
from app.services.artifacts import ArtifactService
from app.workflow.artifact_validator import ArtifactValidationResult, ArtifactValidator, ValidationCheck
from app.workflow.context import WorkflowContext
from app.workflow.dag import build_dag
from app.workflow.delivery_contract import build_delivery_contract, contract_hash
from app.workflow.delivery_gate import artifact_fingerprint
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import RunState, WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, StepStatus, StepType, WorkflowDefinition, utc_now


class _NoModel:
    async def generate(self, *args, **kwargs):
        raise AssertionError("Delivery finalization must never call a model")


def _file(name, content, owner="frontend", language="text"):
    return {"name": name, "content": content, "step_id": owner, "language": language}


def _passed(identifier, target="frontend", evidence=None, command=""):
    return ValidationCheck(identifier, target, identifier, "passed", "本地测试证据",
                           command=command, evidence=evidence).as_dict()


def _project(mode):
    backend = mode in {"static_rest", "template"}
    requirement = ("Spring Boot + HTML CRUD" + (" Thymeleaf" if mode == "template" else "")
                   if backend else "Vue frontend" if mode == "vue" else "纯 HTML 页面")
    frozen = build_delivery_contract(requirement, {
        "backend_required": backend,
        "required_capabilities": ["frontend", "backend", "persistence"] if backend else ["frontend"],
        "delivery_contract": {"api_contract": [{"path": "/api/books", "methods": ["GET", "POST", "PUT", "DELETE"],
                                                "fields": ["title"], "payload": {"title": "Book"}}]} if backend else {},
    })
    page = "\n<!doctype html>\n<html><head><meta charset='utf-8'></head><body><main>交付页面：这是用于真实浏览器验收的静态页面，包含脚本与可见正文。</main><script src='/app.js'></script></body></html>\n\n"
    script = "\nconst message = '交付';\nconsole.log(message);\n\n"
    if backend:
        root = "src/main/resources/templates/" if mode == "template" else "src/main/resources/static/"
        files = [_file("pom.xml", "\n<project />\n\n", "backend", "xml"),
                 _file("src/main/java/demo/Application.java", "\npackage demo; class Application {}\n", "backend", "java"),
                 _file("src/main/java/demo/Book.java", "package demo; @Entity class Book {}\n", "backend", "java"),
                 _file("src/main/resources/schema.sql", "\ncreate table book(id int, title varchar(80));\n\n", "database", "sql"),
                 _file(root + "index.html", page, language="html"),
                 _file("src/main/resources/static/app.js", script, language="javascript")]
        checks = [_passed("backend-test", "backend", command="mvn -B test"),
                  _passed("backend-startup", "backend"),
                  _passed("spring-page-entry", "backend", {"path": "/", "httpStatus": 200}),
                  _passed("spring-page-assets", "backend", {"path": "/"}),
                  _passed("spring-h2-crud", "backend", {"path": "/api/books", "methods": ["GET", "POST", "PUT", "DELETE"],
                                                        "fields": ["title"], "storageVerified": True, "databaseProduct": "H2"})]
    else:
        files = [_file("index.html", page, language="html"), _file("app.js", script, language="javascript")]
        checks = [_passed("frontend-page-entry", evidence={"path": "/", "httpStatus": 200}),
                  _passed("frontend-page-assets", evidence={"path": "/"})]
        if mode == "vue":
            files.extend([_file("package.json", '{"scripts":{"build":"vite build"},"dependencies":{"vue":"^3"}}\n', language="json"),
                          _file("src/main.ts", "\nimport App from './App.vue';\n", language="typescript"),
                          _file("src/App.vue", "\n<template><main>交付</main></template>\n", language="vue")])
            checks.append(_passed("frontend-build", command="npm run build"))
        else:
            checks.append(_passed("static-html"))
    if frozen.get("browser_required"):
        checks.append(_passed("browser-render", evidence={"path": "/", "uiCrud": backend, "visibleTextChars": 40}))
        if backend:
            checks.append(_passed("browser-crud", evidence={"path": "/"}))
    proof = ArtifactValidationResult("passed", "构建及浏览器测试证据", ("frontend",), ()).as_dict()
    proof.update(checks=checks, artifactFingerprint=artifact_fingerprint(files), contractHash=contract_hash(frozen))
    return {"requirement": requirement, "delivery_contract": frozen, "delivery_contract_hash": contract_hash(frozen),
            "__artifact_files__": files, "artifact_validation": proof,
            "final_report": "# Delivery\n\n交付验收通过\nSecond line"}


@dataclass
class _Pipeline:
    executor: WorkflowExecutor
    state: RunState
    repository: SQLiteRepository
    service: ArtifactService

    @property
    def context(self):
        return self.state.context.snapshot()

    @property
    def archive(self):
        return self.service.workspace_root / self.state.run_id / "artifacts.zip"

    def finalize(self, result=None):
        asyncio.run(self.executor._finalize_graph(self.state, result))
        return self.repository.get_run(self.state.run_id)


@pytest.fixture
def pipeline(tmp_path):
    repositories = []

    def make(mode="static", approval=None):
        repository = SQLiteRepository(":memory:")
        repositories.append(repository)
        context = _project(mode)
        owners = sorted({item["step_id"] for item in context["__artifact_files__"]})
        steps = [StepDefinition(owner, agent_id=owner + "_agent", generation_mode="artifacts") for owner in owners]
        if approval is not None:
            steps.append(StepDefinition("approval", type=StepType.APPROVAL, depends_on=owners))
            context["delivery_contract"]["required_capabilities"].append("human_approval")
            context["delivery_contract_hash"] = contract_hash(context["delivery_contract"])
            context["artifact_validation"]["contractHash"] = context["delivery_contract_hash"]
        workflow = WorkflowDefinition("pipeline", "Delivery pipeline", steps=steps, meta={"delivery_contract": True})
        run_id = "run_pipeline_" + str(len(repositories))
        repository.create_run(run_id, workflow.id, {}, RunStatus.RUNNING.value, utc_now())
        service = ArtifactService(repository, tmp_path / run_id)
        executor = WorkflowExecutor(AgentRegistry(), _NoModel(), WorkflowEventBus(repository), repository,
                                    artifact_service=service)
        results = {step.id: StepStatus.SUCCESS for step in steps}
        if approval is not None:
            results["approval"] = approval
        for step in steps:
            repository.upsert_step(run_id, step.id, status=results[step.id].value)
        state = RunState(run_id, workflow, build_dag(workflow), WorkflowContext(context), results=results)
        executor._active[run_id] = state
        return _Pipeline(executor, state, repository, service)

    yield make
    for repository in repositories:
        repository._connection.close()


def _assert_final(pipeline, run, status, gate_status=None):
    assert run["status"] == status.value
    assert run["state"]["results"] == {key: value.value for key, value in pipeline.state.results.items()}
    assert pipeline.state.run_id not in pipeline.executor._active
    events = pipeline.repository.list_events(pipeline.state.run_id)
    terminal = [event for event in events if event["type"] in {"workflow.completed", "workflow.failed"}]
    assert len(terminal) == 1
    assert terminal[0]["type"] == ("workflow.completed" if status == RunStatus.SUCCESS else "workflow.failed")
    assert terminal[0]["payload"]["deliverable"] is (status == RunStatus.SUCCESS)
    if gate_status is not None:
        gate = run["state"]["context"]["delivery_gate"]
        assert gate["status"] == gate_status
        assert gate["deliverable"] is (status == RunStatus.SUCCESS)
        assert pipeline.context["delivery_gate"] == gate
        checked = [event for event in events if event["type"] == "workflow.delivery_checked"]
        assert len(checked) == 1
        assert checked[0]["payload"] == gate
        assert events.index(checked[0]) < events.index(terminal[0])
        assert any(event["type"] == "artifact.created" for event in events[:events.index(checked[0])])
    return run["state"]["context"].get("delivery_gate")


@pytest.mark.parametrize("mode", ["static", "vue", "static_rest", "template"])
def test_real_finalize_success_requires_complete_zip_and_preserves_source_and_metadata(pipeline, mode):
    delivery = pipeline(mode)
    original_proof = copy.deepcopy(delivery.context["artifact_validation"])
    metadata = json.dumps({"contractHash": original_proof["contractHash"],
                           "artifactFingerprint": original_proof["artifactFingerprint"],
                           "notes": "第一行\n第二行"}, ensure_ascii=False, indent=2) + "\n"
    delivery.state.context.set("__artifact_files__", [*delivery.context["__artifact_files__"],
                                                     _file("handoff.md", metadata, "", "text")])
    # Reviewer/Tester prose cannot introduce extra unverified code artifacts.
    delivery.state.context.set("frontend_result", "```html file=old.html\n<html><body>old</body></html>\n```")
    delivery.state.context.set("review_result", "Later Reviewer report\nMore explanation")
    run = delivery.finalize()
    _assert_final(delivery, run, RunStatus.SUCCESS, "passed")
    assert run["final_report"] == delivery.context["final_report"]
    assert delivery.context["artifact_validation"] == original_proof
    expected = {"final-report.md": delivery.context["final_report"].encode("utf-8")}
    expected_metadata = {"schema_version": "1.0", "run_id": delivery.state.run_id,
                         "workflow_id": delivery.state.workflow.id, "contract": delivery.context["delivery_contract"],
                         "contractHash": delivery.context["delivery_contract_hash"], "delivery_gate": delivery.context["delivery_gate"],
                         "validation": delivery.context["artifact_validation"]}
    expected["delivery-report.json"] = json.dumps(expected_metadata, ensure_ascii=False, indent=2).encode("utf-8")
    for item in delivery.context["__artifact_files__"]:
        name = item["name"]
        if mode in {"static_rest", "template"} and item["step_id"]:
            name = ("backend/" if name.startswith("src/main/resources/") else item["step_id"] + "/") + name
        expected[name] = item["content"].encode("utf-8")
    with zipfile.ZipFile(delivery.archive) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == expected
        assert json.loads(archive.read("delivery-report.json")) == expected_metadata
        assert archive.read("handoff.md").endswith(b"\n")
    for record in delivery.repository.list_artifacts(delivery.state.run_id):
        data = (delivery.service.workspace_root / record["relative_path"]).read_bytes()
        assert record["sha256"] == hashlib.sha256(data).hexdigest()
    assert artifact_fingerprint(delivery.context["__artifact_files__"]) == original_proof["artifactFingerprint"]


@pytest.mark.parametrize("mode", ["static", "vue", "static_rest", "template"])
@pytest.mark.parametrize("missing", ["entry", "assets"])
def test_all_agents_success_cannot_hide_missing_page_or_asset_gate(pipeline, mode, missing):
    delivery = pipeline(mode)
    proof = delivery.context["artifact_validation"]
    proof["checks"] = [item for item in proof["checks"] if "-page-" + missing not in item["id"]]
    assert proof["status"] == "passed"
    assert all(status == StepStatus.SUCCESS for status in delivery.state.results.values())
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.FAILED, "blocked")
    assert ("delivery-page:/" if missing == "entry" else "delivery-page-assets:/") in gate["missing"]
    assert delivery.archive.is_file()


def test_late_delivery_gate_reenters_tester_before_failing(pipeline, monkeypatch):
    delivery = pipeline("static")
    valid_proof = copy.deepcopy(delivery.context["artifact_validation"])
    stale_proof = copy.deepcopy(valid_proof)
    stale_proof["checks"] = [
        item for item in stale_proof["checks"] if item["id"] != "frontend-page-assets"
    ]
    delivery.state.context.set("artifact_validation", stale_proof)
    tester = StepDefinition(
        "tester",
        agent_id="tester_agent",
        validation={"enabled": True, "repair_attempts": 1},
    )
    delivery.state.workflow.steps.append(tester)
    delivery.state.results[tester.id] = StepStatus.SUCCESS
    delivery.repository.upsert_step(
        delivery.state.run_id,
        tester.id,
        agent_id=tester.agent_id,
        status=StepStatus.SUCCESS.value,
    )
    calls = []

    async def replay_tester(state, step):
        calls.append(step.id)
        state.context.set("artifact_validation", copy.deepcopy(valid_proof))
        state.results[step.id] = StepStatus.SUCCESS
        delivery.repository.upsert_step(
            state.run_id,
            step.id,
            agent_id=step.agent_id,
            status=StepStatus.SUCCESS.value,
        )

    monkeypatch.setattr(delivery.executor, "_execute_step", replay_tester)

    run = delivery.finalize()

    _assert_final(delivery, run, RunStatus.SUCCESS, "passed")
    assert calls == ["tester"]
    events = delivery.repository.list_events(delivery.state.run_id)
    assert [event["type"] for event in events if event["type"].startswith("delivery.repair_")] == [
        "delivery.repair_started",
        "delivery.repair_completed",
    ]


@pytest.mark.parametrize("mutation", ["content", "name", "addition"])
def test_final_graph_result_cannot_modify_validated_source_and_still_succeed(pipeline, mutation):
    delivery = pipeline("static")
    files = copy.deepcopy(delivery.context["__artifact_files__"])
    if mutation == "content":
        files[0]["content"] += "\n<!-- post-validation edit -->\n"
    elif mutation == "name":
        files[1]["name"] = "other.js"
    else:
        files.append(_file("extra.js", "console.log('new source');\n"))
    gate = _assert_final(delivery, delivery.finalize({"context": {"__artifact_files__": files}}), RunStatus.FAILED, "blocked")
    assert "delivery-fingerprint" in gate["missing"]
    assert gate["fingerprint"] != delivery.context["artifact_validation"]["artifactFingerprint"]


@pytest.mark.parametrize("mutation", ["contract", "context-hash", "proof-hash", "missing-proof-hash", "restamped-context-hash"])
def test_changed_frozen_contract_or_hash_fails_final_workflow(pipeline, mutation):
    delivery = pipeline()
    context = delivery.context
    if mutation in {"contract", "restamped-context-hash"}:
        frozen = copy.deepcopy(context["delivery_contract"])
        frozen["assumptions"].append("Changed after validation")
        delivery.state.context.set("delivery_contract", frozen)
        if mutation == "restamped-context-hash":
            delivery.state.context.set("delivery_contract_hash", contract_hash(frozen))
    elif mutation == "context-hash":
        delivery.state.context.set("delivery_contract_hash", "changed")
    elif mutation == "proof-hash":
        context["artifact_validation"]["contractHash"] = "changed"
    else:
        del context["artifact_validation"]["contractHash"]
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.FAILED, "blocked")
    assert "delivery-contract-hash" in gate["missing"]


@pytest.mark.parametrize("damage", ["missing-source", "report-only", "changed-source", "unregistered-extra", "crc"])
def test_actual_zip_content_is_decisive_even_when_all_checks_and_agents_pass(pipeline, monkeypatch, damage):
    delivery = pipeline("static_rest")
    create_archive = delivery.service.create_archive

    def damage_archive(run_id, *, strict=False):
        assert strict is True
        path = create_archive(run_id, strict=strict)
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
            source = next(name for name in members if name.endswith("pom.xml"))
            report = archive.getinfo("final-report.md")
        if damage == "crc":
            data = bytearray(path.read_bytes())
            offset = report.header_offset
            # Break the CRC for even a permitted metadata member by changing
            # its central-directory CRC, leaving a readable archive structure.
            central = data.find(b"PK\x01\x02")
            while central != -1:
                name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, central + 28)
                name = bytes(data[central + 46:central + 46 + name_len]).decode("utf-8")
                if name == "final-report.md":
                    data[central + 16] ^= 1
                    break
                central = data.find(b"PK\x01\x02", central + 46 + name_len + extra_len + comment_len)
            assert central != -1 and offset >= 0
            path.write_bytes(data)
        else:
            if damage == "missing-source":
                del members[source]
            elif damage == "report-only":
                members = {"final-report.md": members["final-report.md"]}
            elif damage == "changed-source":
                members[source] += b"changed"
            else:
                members["backend/old.java"] = b"stale unregistered source"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
        return path

    monkeypatch.setattr(delivery.service, "create_archive", damage_archive)
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.FAILED, "failed")
    assert "delivery-archive" in gate["missing"]


def test_strict_executor_archive_does_not_package_unregistered_workspace_history(pipeline):
    delivery = pipeline()
    run_dir = delivery.archive.parent
    run_dir.mkdir(parents=True)
    (run_dir / "old.java").write_bytes(b"unregistered history")
    (run_dir / "old.zip").write_bytes(b"old download")
    _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS, "passed")
    with zipfile.ZipFile(delivery.archive) as archive:
        assert "old.java" not in archive.namelist()
        assert "old.zip" not in archive.namelist()


@pytest.mark.parametrize("capability", ["authentication", "external_api", "async"])
def test_runtime_capabilities_are_not_silently_dropped_by_finalization(pipeline, capability):
    delivery = pipeline()
    frozen = delivery.context["delivery_contract"]
    frozen["required_capabilities"].append(capability)
    digest = contract_hash(frozen)
    delivery.state.context.set("delivery_contract_hash", digest)
    delivery.context["artifact_validation"]["contractHash"] = digest
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.FAILED, "blocked")
    assert f"delivery-capability:{capability}" in gate["missing"]


@pytest.mark.parametrize("approval", [StepStatus.SUCCESS, StepStatus.SKIPPED])
def test_real_approval_state_supplies_only_its_own_capability_evidence(pipeline, approval):
    delivery = pipeline(approval=approval)
    success = approval == StepStatus.SUCCESS
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS if success else RunStatus.FAILED,
                         "passed" if success else "blocked")
    evidence = [item for item in gate["checks"] if item["id"] == "capability-human_approval"]
    assert len(evidence) == 1
    assert evidence[0]["status"] == ("passed" if success else "blocked")


@pytest.mark.parametrize("render", [None, "wrong-page", "passed", "failed"])
def test_browser_required_contract_reaches_final_status_only_with_matching_render(pipeline, render):
    delivery = pipeline()
    frozen = delivery.context["delivery_contract"]
    frozen["browser_required"] = True
    digest = contract_hash(frozen)
    delivery.state.context.set("delivery_contract_hash", digest)
    proof = delivery.context["artifact_validation"]
    proof["contractHash"] = digest
    proof["checks"] = [item for item in proof["checks"] if item["id"] != "browser-render"]
    if render is not None:
        evidence = _passed("browser-render", evidence={"path": "/other" if render == "wrong-page" else "/", "domNonempty": True})
        if render == "failed":
            evidence["status"] = "failed"
        proof["checks"].append(evidence)
    status = RunStatus.SUCCESS if render == "passed" else RunStatus.FAILED
    gate_status = "passed" if render == "passed" else "failed" if render == "failed" else "blocked"
    gate = _assert_final(delivery, delivery.finalize(), status, gate_status)
    if render in {None, "wrong-page"}:
        assert "delivery-browser-render:/" in gate["missing"]


@pytest.mark.parametrize("scenario", ["passed", "missing-page", "missing-asset"])
def test_real_static_validator_local_http_evidence_drives_real_finalization(pipeline, scenario):
    delivery = pipeline()
    if scenario == "missing-page":
        delivery.context["delivery_contract"]["entrypoints"] = ["/missing.html"]
    elif scenario == "missing-asset":
        delivery.context["__artifact_files__"][0]["content"] = delivery.context["__artifact_files__"][0]["content"].replace("/app.js", "/missing.js")
    digest = contract_hash(delivery.context["delivery_contract"])
    delivery.state.context.set("delivery_contract_hash", digest)
    emitted = []

    async def validate():
        async def emit(event_type, payload):
            emitted.append((event_type, payload))
        return await ArtifactValidator().validate(delivery.context, emit=emit)

    # This invokes sys.executable -m http.server on a random loopback port in
    # the validator's isolated temporary workspace; no mock HTTP transport.
    result = asyncio.run(validate())
    proof = result.as_dict()
    proof.update(artifactFingerprint=artifact_fingerprint(delivery.context["__artifact_files__"]), contractHash=digest)
    delivery.state.context.set("artifact_validation", proof)
    checks = {item["id"]: item for item in proof["checks"]}
    assert checks["static-html"]["status"] == "passed"
    assert checks["frontend-startup"]["status"] == "passed"
    assert checks["frontend-page-entry"]["evidence"]["path"] == ("/missing.html" if scenario == "missing-page" else "/")
    if scenario != "missing-page":
        assert checks["frontend-page-assets"]["status"] == ("passed" if scenario == "passed" else "failed")
        assert checks["frontend-page-assets"]["evidence"]["path"] == "/"
    emitted_checks = [payload["check"] for event_type, payload in emitted if event_type == "step.validation_check"]
    assert emitted_checks == proof["checks"]
    success = scenario == "passed"
    _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS if success else RunStatus.FAILED,
                  "passed" if success else "failed")


@pytest.mark.parametrize("crud", [None, "passed", "wrong-page", "failed"])
def test_backend_api_crud_and_render_cannot_replace_separate_browser_ui_crud(pipeline, crud):
    delivery = pipeline("static_rest")
    frozen = delivery.context["delivery_contract"]
    frozen["browser_required"] = True
    digest = contract_hash(frozen)
    delivery.state.context.set("delivery_contract_hash", digest)
    proof = delivery.context["artifact_validation"]
    proof["contractHash"] = digest
    proof["checks"] = [item for item in proof["checks"] if item["id"] not in {"browser-render", "browser-crud"}]
    proof["checks"].append(_passed("browser-render", evidence={"path": "/", "uiCrud": True}))
    if crud is not None:
        evidence = _passed("browser-crud", evidence={"path": "/other" if crud == "wrong-page" else "/"})
        if crud == "failed":
            evidence["status"] = "failed"
        proof["checks"].append(evidence)
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS if crud == "passed" else RunStatus.FAILED,
                         "passed" if crud == "passed" else "failed" if crud == "failed" else "blocked")
    if crud in {None, "wrong-page"}:
        assert "delivery-browser-crud" in gate["missing"]


@pytest.mark.parametrize("scenario", ["passed", "javascript-error", "missing-resource"])
def test_real_static_browser_render_and_same_origin_errors_drive_final_status(pipeline, scenario):
    delivery = pipeline()
    frozen = delivery.context["delivery_contract"]
    frozen["browser_required"] = True
    files = delivery.context["__artifact_files__"]
    if scenario == "javascript-error":
        files[1]["content"] = "throw new Error('交付流水线脚本异常');\n"
    elif scenario == "missing-resource":
        files[0]["content"] = files[0]["content"].replace("/app.js", "/missing-resource.js")
    digest = contract_hash(frozen)
    delivery.state.context.set("delivery_contract_hash", digest)
    result = asyncio.run(ArtifactValidator().validate(delivery.context))
    proof = result.as_dict()
    proof.update(artifactFingerprint=artifact_fingerprint(files), contractHash=digest)
    delivery.state.context.set("artifact_validation", proof)
    checks = {item["id"]: item for item in proof["checks"]}
    assert checks["frontend-page-entry"]["status"] == "passed"
    assert checks["browser-render"]["evidence"]["path"] == "/"
    success = scenario == "passed"
    assert checks["browser-render"]["status"] == ("passed" if success else "failed")
    if scenario == "javascript-error":
        assert checks["frontend-page-assets"]["status"] == "passed"
        assert "交付流水线脚本异常" in checks["browser-render"]["message"]
    elif scenario == "missing-resource":
        assert checks["frontend-page-assets"]["status"] == "failed"
    _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS if success else RunStatus.FAILED,
                  "passed" if success else "failed")


@pytest.mark.parametrize("storage", ["passed", "missing", "false", "wrong-product", "wrong-path"])
def test_real_finalization_requires_contract_scoped_h2_storage_proof(pipeline, storage):
    delivery = pipeline("static_rest")
    frozen = delivery.context["delivery_contract"]
    frozen["schema_version"] = "1.1"
    frozen["database_audit_required"] = True
    digest = contract_hash(frozen)
    delivery.state.context.set("delivery_contract_hash", digest)
    proof = delivery.context["artifact_validation"]
    proof["contractHash"] = digest
    crud = next(item for item in proof["checks"] if item["id"] == "spring-h2-crud")
    if storage == "missing":
        del crud["evidence"]["storageVerified"]
    elif storage == "false":
        crud["evidence"]["storageVerified"] = False
    elif storage == "wrong-product":
        crud["evidence"]["databaseProduct"] = "PostgreSQL"
    elif storage == "wrong-path":
        # Retain valid API/method evidence independently; this failure must
        # specifically come from the H2 audit for the frozen CRUD path.
        proof["checks"].append(_passed("api-contract", "backend", copy.deepcopy(crud["evidence"])))
        crud["evidence"]["path"] = "/api/other"
    success = storage == "passed"
    gate = _assert_final(delivery, delivery.finalize(), RunStatus.SUCCESS if success else RunStatus.FAILED,
                         "passed" if success else "blocked")
    if not success:
        assert gate["missing"] == ["delivery-database-audit:/api/books"]
    with zipfile.ZipFile(delivery.archive) as archive:
        metadata = json.loads(archive.read("delivery-report.json"))
        assert metadata["delivery_gate"] == gate
        assert metadata["validation"] == proof
        assert not any("StorageProbe" in name for name in archive.namelist())
