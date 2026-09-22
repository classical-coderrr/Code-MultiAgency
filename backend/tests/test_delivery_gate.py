"""Delivery gates use local fixtures only: no live models or historical DB."""

import copy
import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path

import pytest

from app.workflow.delivery_gate import artifact_fingerprint, evaluate_delivery


def file(name, content="source", owner=""):
    return {"name": name, "content": content, "step_id": owner}


def check(identifier, status="passed", **evidence):
    return {"id": identifier, "status": status, "message": "本地测试证据", **evidence}


def contract(**overrides):
    return {"schema_version": "1.0", "backend_stack": "none", "frontend_stack": "static_html",
            "page_mode": "static", "database_mode": "none", "entrypoints": ["/"],
            "api_contract": [], "required_capabilities": ["frontend", "artifact"],
            "crud_required": False, **overrides}


def static_context(**overrides):
    return {"delivery_contract": contract(**overrides),
            "__artifact_files__": [file("index.html", "<!doctype html><html><body>Hi</body></html>", "frontend")]}


def spring_context(gradle=False, vue=False):
    context = {"delivery_contract": contract(
        backend_stack="spring_boot", frontend_stack="vue" if vue else "html",
        page_mode="spa" if vue else "thymeleaf", database_mode="h2",
        entrypoints=["/students"], required_capabilities=["backend", "frontend", "persistence"],
        crud_required=True, api_contract=[{"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
                                           "fields": ["id", "studentName"]}]),
        "__artifact_files__": [file("build.gradle" if gradle else "pom.xml", "build manifest", "backend"),
                               file("src/main/java/demo/Application.java", "package demo; class Application {}", "backend"),
                               file("src/main/java/demo/Student.java", "package demo; @Entity class Student {}", "backend"),
                               file("src/main/resources/schema.sql", "create table student(id int);", "database")]}
    context["__artifact_files__"].extend(
        [file("package.json", '{"scripts":{"build":"vite build"}}', "frontend"),
         file("src/main.ts", "import './App.vue'", "frontend"),
         file("src/App.vue", "<template><main>Students</main></template>", "frontend"),
         file("index.html", "<html><body><div id='app'></div></body></html>", "frontend")]
        if vue else [file("src/main/resources/templates/students.html", "<html><body th:text='${students}'></body></html>", "frontend")])
    return context


def validation(context, checks=None, **overrides):
    if checks is None:
        if context["delivery_contract"]["backend_stack"] == "none":
            checks = [check("static-html"), check("browser-page", path="/")]
        else:
            checks = [check("backend-test", command="mvn -B test"), check("backend-startup"),
                      check("spring-page-entry", entrypoints=["/students"]), check("spring-h2-crud"),
                      check("api-contract", api_contract=copy.deepcopy(context["delivery_contract"]["api_contract"]))]
            if any(item["name"] == "build.gradle" for item in context["__artifact_files__"]):
                checks[0] = check("gradle-test", command="gradle test")
                checks.append(check("gradle-build", command="gradle build"))
            if context["delivery_contract"]["frontend_stack"] == "vue":
                checks.append(check("frontend-build"))
    checks = list(checks)
    checks.insert(-1 if context["delivery_contract"]["backend_stack"] != "none" else len(checks),
                  check("frontend-page-assets", evidence={"paths": context["delivery_contract"]["entrypoints"]}))
    return {"status": "passed", "checks": checks,
            "artifactFingerprint": artifact_fingerprint(context["__artifact_files__"]), **overrides}


def archive(tmp_path, context, layout="flat", replacements=None, omit=()):
    path = tmp_path / "delivery.zip"
    with zipfile.ZipFile(path, "w") as output:
        for item in context["__artifact_files__"]:
            name = item["name"]
            if name in omit:
                continue
            owner = item.get("step_id", "")
            if layout != "flat" and owner and not name.startswith(owner + "/"):
                if layout == "spring" and name.startswith("src/main/resources/"):
                    owner = "backend"
                name = owner + "/" + name
            output.writestr(name, (replacements or {}).get(item["name"], item["content"]))
        if not any(item["name"] == "final-report.md" for item in context["__artifact_files__"]):
            output.writestr("final-report.md", "Delivery report")
    return path


def assert_status(result, status):
    assert result["status"] == status
    assert result["deliverable"] is (status == "passed")
    assert set(result) == {"status", "deliverable", "checks", "missing", "fingerprint"}


@pytest.mark.parametrize("context", [{}, {"required": True}, {"required": False},
                                     {"delivery_contract": None, "required": True}])
def test_legacy_analysis_runs_are_not_delivery_runs(context):
    assert_status(evaluate_delivery(context, {"status": "failed"}), "not_required")


@pytest.mark.parametrize("layout", ["flat", "owner", "spring"])
@pytest.mark.parametrize("project", ["static", "maven", "gradle", "vue"])
def test_complete_delivery_passes_in_supported_archive_layouts(tmp_path, layout, project):
    context = static_context() if project == "static" else spring_context(gradle=project == "gradle", vue=project == "vue")
    result = evaluate_delivery(context, validation(context), archive(tmp_path, context, layout))
    assert_status(result, "passed")
    assert result["missing"] == []
    assert result["fingerprint"] == artifact_fingerprint(context["__artifact_files__"])


def test_required_false_cannot_disable_a_present_contract(tmp_path):
    context = static_context()
    context["required"] = False
    result = evaluate_delivery(context, validation(context, [check("static-html")]), archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-page:/" in result["missing"]


@pytest.mark.parametrize("summary", [None, {}, {"status": "passed"}, {"status": "disabled"}, {"status": {}}])
def test_missing_disabled_or_malformed_validation_cannot_turn_green(tmp_path, summary):
    context = static_context()
    assert_status(evaluate_delivery(context, summary, archive(tmp_path, context)), "blocked")


@pytest.mark.parametrize("status", ["disabled", "skipped", "not_required", "blocked", None, {}])
def test_disabled_individual_gate_is_blocked_even_if_aggregate_passes(tmp_path, status):
    context = static_context()
    result = evaluate_delivery(context, validation(context, [check("static-html", status), check("browser-page", path="/")]),
                               archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-static-html" in result["missing"]


@pytest.mark.parametrize("disabled", ["validation", "check"])
def test_enabled_false_is_not_passed_evidence(tmp_path, disabled):
    context = static_context()
    report = validation(context)
    (report if disabled == "validation" else report["checks"][1])["enabled"] = False
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


@pytest.mark.parametrize("page", [check("backend-startup", path="/"), check("static-html", path="/"),
                                  check("browser-page", target="/api/health"),
                                  check("spring-page-entry", message="passed /"),
                                  check("browser-page", path="/*"), check("browser-page", path="https://example.test/")])
def test_health_structure_prose_and_wrong_paths_do_not_cover_page_contract(tmp_path, page):
    context = static_context()
    result = evaluate_delivery(context, validation(context, [check("static-html"), page]), archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-page:/" in result["missing"]


@pytest.mark.parametrize("identifier", ["spring-page-entry", "spring-page-render", "browser-navigation",
                                        "frontend-page-entry", "frontend-page-render"])
@pytest.mark.parametrize("coverage", [{"path": "/"}, {"target": "/"}, {"entrypoint": "/"},
                                      {"entrypoints": ["/"]}, {"paths": ["/"]},
                                      {"coveredEntrypoints": ["/"]}, {"evidence": {"path": "/"}}])
def test_explicit_page_coverage_is_accepted(tmp_path, identifier, coverage):
    context = static_context()
    result = evaluate_delivery(context, validation(context, [check("static-html"), check(identifier, **coverage)]),
                               archive(tmp_path, context))
    assert_status(result, "passed")


def test_every_contract_entrypoint_must_be_covered(tmp_path):
    context = static_context(entrypoints=["/", "/students"])
    report = validation(context)
    path = archive(tmp_path, context)
    result = evaluate_delivery(context, report, path)
    assert_status(result, "blocked")
    assert "delivery-page:/students" in result["missing"]
    report["checks"].append(check("browser-students", evidence={"entrypoints": [{"path": "/students/"}]}))
    assert_status(evaluate_delivery(context, report, path), "passed")


@pytest.mark.parametrize("gate", ["backend-test", "backend-startup", "spring-page-entry", "spring-h2-crud", "api-contract"])
def test_maven_requires_each_mandatory_gate(tmp_path, gate):
    context = spring_context()
    report = validation(context)
    report["checks"] = [item for item in report["checks"] if item["id"] != gate]
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


@pytest.mark.parametrize("gate", ["gradle-build", "gradle-test", "backend-startup"])
def test_gradle_requires_build_test_and_startup(tmp_path, gate):
    context = spring_context(gradle=True)
    report = validation(context)
    report["checks"] = [item for item in report["checks"] if item["id"] != gate]
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


@pytest.mark.parametrize("command", ["gradle build -x test", "mvn test -DskipTests", "mvn test -DskipTests=true",
                                     "mvn test -Dmaven.test.skip=true"])
def test_skipped_tests_in_passed_commands_still_block_delivery(tmp_path, command):
    context = spring_context()
    report = validation(context)
    report["checks"][0]["command"] = command
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


def test_explicit_skip_tests_false_is_allowed(tmp_path):
    context = spring_context()
    report = validation(context)
    report["checks"][0]["command"] = "mvn test -DskipTests=false -Dmaven.test.skip=false"
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


def test_vue_needs_frontend_build(tmp_path):
    context = spring_context(vue=True)
    report = validation(context)
    report["checks"] = [item for item in report["checks"] if item["id"] != "frontend-build"]
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-frontend-build" in result["missing"]


@pytest.mark.parametrize("identifier", ["crud-memory", "crud-h2", "spring-h2-crud"])
def test_crud_check_family_is_accepted(tmp_path, identifier):
    context = spring_context()
    report = validation(context)
    next(item for item in report["checks"] if item["id"] == "spring-h2-crud")["id"] = identifier
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


@pytest.mark.parametrize("mutation", ["path", "method", "field", "prose"])
def test_api_evidence_must_cover_exact_path_methods_and_wire_fields(tmp_path, mutation):
    context = spring_context()
    report = validation(context)
    api_check = report["checks"][-1]
    row = api_check["api_contract"][0]
    if mutation == "path":
        row["path"] = "/api/students/other"
    elif mutation == "method":
        row["methods"].remove("DELETE")
    elif mutation == "field":
        row["fields"] = ["id", "name"]
    else:
        del api_check["api_contract"]
        api_check["message"] = "All API paths, CRUD and studentName verified"
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert any(identifier.startswith("delivery-api:") for identifier in result["missing"])


def test_api_methods_and_fields_cannot_be_combined_from_unrelated_routes(tmp_path):
    context = spring_context()
    report = validation(context)
    report["checks"][-1]["api_contract"] = [
        {"path": "/api/students", "methods": ["GET"], "fields": ["id", "studentName"]},
        {"path": "/api/other", "methods": ["POST", "PUT", "DELETE"], "fields": ["id", "studentName"]}]
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


def test_api_evidence_can_be_split_by_method_on_same_route(tmp_path):
    context = spring_context()
    report = validation(context)
    report["checks"].pop()
    report["checks"].extend(check("api-contract-" + method, evidence={"path": "/api/students",
                            "methods": [method.lower()], "fields": ["id", "studentName"]})
                            for method in ["GET", "POST", "PUT", "DELETE"])
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


@pytest.mark.parametrize("failed", ["summary", "mandatory", "additional"])
def test_failed_evidence_cannot_be_hidden_by_aggregate_passed(tmp_path, failed):
    context = static_context()
    report = validation(context)
    if failed == "summary":
        report["status"] = "failed"
    elif failed == "mandatory":
        report["checks"][0]["status"] = "failed"
    else:
        report["checks"].append(check("security-check", "failed"))
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "failed")


def test_unknown_required_capability_needs_its_own_evidence(tmp_path):
    context = static_context(required_capabilities=["frontend", "authentication"])
    report = validation(context)
    path = archive(tmp_path, context)
    result = evaluate_delivery(context, report, path)
    assert_status(result, "blocked")
    assert "delivery-capability:authentication" in result["missing"]
    report["checks"].append(check("capability-authentication"))
    assert_status(evaluate_delivery(context, report, path), "passed")


def test_fingerprint_is_sorted_owner_name_and_content_hash():
    files = [file("a.py", "a\n", "backend"), file("a.py", "b", "frontend")]
    rows = [(item["step_id"], item["name"], hashlib.sha256(item["content"].encode()).hexdigest()) for item in files]
    expected = hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert artifact_fingerprint(files) == expected == artifact_fingerprint(list(reversed(files)))
    aliases = [{"owner_step": item["step_id"], "path": item["name"], "content": item["content"], "language": "ignored"}
               for item in files]
    assert artifact_fingerprint(aliases) == expected


@pytest.mark.parametrize("key,value", [("content", "changed"), ("content", "source\n"),
                                      ("name", "other.py"), ("step_id", "frontend")])
def test_fingerprint_changes_with_owner_name_or_exact_content(key, value):
    files = [file("a.py", owner="backend")]
    changed = copy.deepcopy(files)
    changed[0][key] = value
    assert artifact_fingerprint(changed) != artifact_fingerprint(files)


def test_mutating_source_after_validation_blocks_even_with_fresh_valid_zip(tmp_path):
    context = static_context()
    report = validation(context)
    context["__artifact_files__"][0]["content"] += "\n<!-- changed -->"
    report["checks"].append(check("old-failure", "failed"))
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-fingerprint" in result["missing"]


def test_missing_fingerprint_blocks(tmp_path):
    context = static_context()
    report = validation(context)
    del report["artifactFingerprint"]
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


@pytest.mark.parametrize("files", [None, [], [None], [file("../escape.py")], [file("a.py", "")],
                                   [file("a.py", owner="../bad")], [file("a.py"), file("a.py")]])
def test_invalid_artifact_manifest_fails_and_fingerprint_rejects(files):
    context = static_context()
    context["__artifact_files__"] = files
    assert_status(evaluate_delivery(context, {"status": "passed"}), "failed")
    with pytest.raises(ValueError):
        artifact_fingerprint(files)


@pytest.mark.parametrize("field", ["pom.xml", "src/main/java/demo/Application.java", "src/main/java/demo/Student.java"])
def test_missing_required_backend_and_database_sources_fail(tmp_path, field):
    context = spring_context()
    omitted = {field}
    if field.endswith("Application.java"):
        omitted.add("src/main/java/demo/Student.java")
    if field.endswith("Student.java"):
        omitted.add("src/main/resources/schema.sql")
    context["__artifact_files__"] = [item for item in context["__artifact_files__"] if item["name"] not in omitted]
    assert_status(evaluate_delivery(context, validation(context), archive(tmp_path, context)), "failed")


def test_missing_vue_entry_source_fails(tmp_path):
    context = spring_context(vue=True)
    context["__artifact_files__"] = [item for item in context["__artifact_files__"] if item["name"] != "src/main.ts"]
    assert_status(evaluate_delivery(context, validation(context), archive(tmp_path, context)), "failed")


def test_no_archive_and_nonexistent_archive_block(tmp_path):
    context = static_context()
    for path in (None, tmp_path / "missing.zip"):
        result = evaluate_delivery(context, validation(context), path)
        assert_status(result, "blocked")
        assert "delivery-archive" in result["missing"]


@pytest.mark.parametrize("contents", ["empty", "report", "garbage", "missing-source", "changed-source"])
def test_zip_must_be_valid_and_contain_all_exact_source_bytes(tmp_path, contents):
    context = spring_context()
    path = tmp_path / "delivery.zip"
    if contents == "garbage":
        path.write_bytes(b"not a zip")
    elif contents in {"empty", "report"}:
        with zipfile.ZipFile(path, "w") as output:
            if contents == "report":
                output.writestr("final-report.md", "success")
    else:
        path = archive(tmp_path, context, omit=("pom.xml",) if contents == "missing-source" else (),
                       replacements={"pom.xml": "altered"} if contents == "changed-source" else None)
    result = evaluate_delivery(context, validation(context), path)
    assert_status(result, "failed")
    assert "delivery-archive" in result["missing"]


def test_actual_zip_crc_is_verified_including_additional_report(tmp_path):
    context = static_context()
    path = archive(tmp_path, context)
    with zipfile.ZipFile(path) as output:
        offset = output.getinfo("final-report.md").header_offset
    data = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", data, offset + 26)
    data[offset + 30 + name_len + extra_len] ^= 1
    path.write_bytes(data)
    result = evaluate_delivery(context, validation(context), path)
    assert_status(result, "failed")
    assert "BadZipFile" in result["checks"][-1]["message"]
    assert "ZIP 无法读取或已损坏" in result["checks"][-1]["message"]


@pytest.mark.parametrize("unsafe", ["../escape.txt", "/absolute.txt", "C:/escape.txt"])
def test_unsafe_zip_members_fail_without_extracting(tmp_path, unsafe):
    context = static_context()
    path = archive(tmp_path, context)
    with zipfile.ZipFile(path, "a") as output:
        output.writestr(unsafe, "unsafe")
    assert_status(evaluate_delivery(context, validation(context), path), "failed")


def test_zip_duplicate_and_symlink_members_fail(tmp_path):
    context = static_context()
    path = archive(tmp_path, context)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a") as output:
            output.writestr("index.html", context["__artifact_files__"][0]["content"])
    assert_status(evaluate_delivery(context, validation(context), path), "failed")
    path = archive(tmp_path, context)
    with zipfile.ZipFile(path, "a") as output:
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        output.writestr(link, "index.html")
    assert_status(evaluate_delivery(context, validation(context), path), "failed")


def test_owner_directories_support_same_name_sources_without_flattening(tmp_path):
    context = static_context()
    context["__artifact_files__"].extend([file("shared.txt", "backend copy", "backend"),
                                         file("shared.txt", "frontend copy", "frontend")])
    assert_status(evaluate_delivery(context, validation(context), archive(tmp_path, context, "owner")), "passed")
    # One flat member must not certify two distinct source identities.
    context["__artifact_files__"][-1]["content"] = "backend copy"
    path = tmp_path / "flat.zip"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("index.html", context["__artifact_files__"][0]["content"])
        output.writestr("shared.txt", "backend copy")
    assert_status(evaluate_delivery(context, validation(context), path), "failed")


def test_already_owner_prefixed_names_do_not_need_double_prefix(tmp_path):
    context = spring_context(vue=True)
    for item in context["__artifact_files__"]:
        item["name"] = item["step_id"] + "/" + item["name"]
    assert_status(evaluate_delivery(context, validation(context), archive(tmp_path, context, "owner")), "passed")


@pytest.mark.parametrize("invalid", [False, {}, {"schema_version": "1.0"},
                                    contract(entrypoints=[]), contract(entrypoints=[{}]),
                                    contract(api_contract=[{"path": "/api/x", "methods": [], "fields": []}]),
                                    contract(api_contract=[{"path": "/api/x", "methods": ["GET"], "fields": [{}]}]),
                                    contract(crud_required="false"), contract(required_capabilities=[{}])])
def test_malformed_or_underspecified_contracts_are_blocked(tmp_path, invalid):
    context = static_context()
    context["delivery_contract"] = invalid
    report = {"status": "passed", "artifactFingerprint": artifact_fingerprint(context["__artifact_files__"]),
              "checks": [check("static-html"), check("browser-page", path="/"), check("frontend-page-assets", path="/")]}
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "blocked")


def test_evaluation_is_read_only(tmp_path):
    context = spring_context()
    report = validation(context)
    originals = copy.deepcopy((context, report))
    path = archive(tmp_path, context)
    original_zip = path.read_bytes()
    assert_status(evaluate_delivery(context, report, path), "passed")
    assert (context, report) == originals
    assert path.read_bytes() == original_zip


def test_later_tester_reviewer_and_unrelated_reports_do_not_stale_source_proof(tmp_path):
    context = spring_context()
    report = validation(context)
    context["__artifact_files__"].extend([file("review.json", '{"status":"passed"}', "reviewer"),
                                         file("tests.md", "Tester explanations", "tester"),
                                         file("final-report.md", "Final report"),
                                         file("requirements.md", "Analysis", "requirement")])
    assert artifact_fingerprint(context["__artifact_files__"]) == report["artifactFingerprint"]
    path = archive(tmp_path, context, "spring")
    assert_status(evaluate_delivery(context, report, path), "passed")
    context["__artifact_files__"][-1]["content"] += " changed report"
    assert artifact_fingerprint(context["__artifact_files__"]) == report["artifactFingerprint"]
    assert_status(evaluate_delivery(context, report, path), "passed")


def test_source_owners_include_database_and_unowned_code_but_exclude_reviewer_code():
    files = [file("schema.sql", "create table a(id int)", "database"), file("index.html", "<html />")]
    original = artifact_fingerprint(files)
    assert artifact_fingerprint(files + [file("review.py", "reviewer code", "reviewer")]) == original
    files[0]["content"] += ";"
    assert artifact_fingerprint(files) != original


def test_api_contract_payload_wire_keys_are_checked(tmp_path):
    context = spring_context()
    context["delivery_contract"]["api_contract"][0]["payload"] = {"studentCode": "AT-1"}
    report = validation(context)
    report["checks"][-1]["api_contract"][0]["fields"].append("studentCode")
    path = archive(tmp_path, context)
    assert_status(evaluate_delivery(context, report, path), "passed")
    report["checks"][-1]["api_contract"][0]["fields"].remove("studentCode")
    result = evaluate_delivery(context, report, path)
    assert_status(result, "blocked")
    assert "delivery-api:POST:/api/students" in result["missing"]


@pytest.mark.parametrize("mode", ["template", "static_rest"])
def test_builder_springboot_and_page_modes_with_actual_page_evidence(tmp_path, mode):
    context = spring_context()
    context["delivery_contract"].update(backend_stack="springboot", page_mode=mode)
    if mode == "static_rest":
        context["__artifact_files__"][-1]["name"] = "src/main/resources/static/index.html"
    report = validation(context)
    page = next(item for item in report["checks"] if item["id"] == "spring-page-entry")
    page.pop("entrypoints")
    page["target"] = "backend"
    page["evidence"] = {"path": "/students", "statusCode": 200}
    report["checks"].append(check("spring-page-assets", target="backend", evidence={"path": "/students"}))
    assert not any(item["id"] == "static-html" for item in report["checks"])
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context, "spring")), "passed")


def test_crud_success_evidence_covers_frozen_api_contract(tmp_path):
    context = spring_context()
    report = validation(context)
    report["checks"].pop()
    crud = next(item for item in report["checks"] if item["id"] == "spring-h2-crud")
    crud["evidence"] = {"path": "/api/students", "methods": ["POST", "GET", "PUT", "DELETE"],
                        "fields": ["id", "studentName"]}
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


@pytest.mark.parametrize("mutation", [None, "missing-proof", "different-proof", "changed-contract", "empty-context-hash"])
def test_contract_hash_is_required_and_matches_current_contract_when_context_has_hash(tmp_path, mutation):
    context = static_context()
    digest = hashlib.sha256(json.dumps(context["delivery_contract"], ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    context["delivery_contract_hash"] = digest
    report = validation(context, contractHash=digest)
    if mutation == "missing-proof":
        del report["contractHash"]
    elif mutation == "different-proof":
        report["contractHash"] = "other"
    elif mutation == "changed-contract":
        context["delivery_contract"]["assumptions"] = ["changed"]
    elif mutation == "empty-context-hash":
        context["delivery_contract_hash"] = ""
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "passed" if mutation is None else "blocked")


@pytest.mark.parametrize("extra", ["final-report.md", "delivery-report.json", "old.java", "old.zip", "reviewer/stale.md"])
def test_unregistered_zip_extras_only_allow_pure_delivery_metadata(tmp_path, extra):
    context = static_context()
    path = archive(tmp_path, context)
    if extra != "final-report.md":
        with zipfile.ZipFile(path, "a") as output:
            output.writestr(extra, "extra")
    result = evaluate_delivery(context, validation(context), path)
    assert_status(result, "passed" if extra in {"final-report.md", "delivery-report.json"} else "failed")


def test_stale_second_source_copy_in_zip_is_not_an_allowed_report(tmp_path):
    context = static_context()
    path = archive(tmp_path, context, "owner")
    with zipfile.ZipFile(path, "a") as output:
        output.writestr("index.html", "old source")
    assert_status(evaluate_delivery(context, validation(context), path), "failed")


def test_pure_delivery_metadata_never_enters_source_fingerprint():
    files = [file("index.html", "<html />", "frontend")]
    digest = artifact_fingerprint(files)
    assert artifact_fingerprint(files + [file("delivery-report.json", '{"status":"passed"}'),
                                         file("final-report.md", "Report", "backend")]) == digest


def test_backend_crud_without_frozen_api_contract_is_blocked(tmp_path):
    context = spring_context()
    context["delivery_contract"]["api_contract"] = []
    result = evaluate_delivery(context, validation(context), archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-api-contract" in result["missing"]


def test_failed_skipped_test_command_preserves_actual_failure(tmp_path):
    context = spring_context()
    report = validation(context)
    report["checks"][0].update(status="failed", command="mvn test -DskipTests", enabled=False)
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "failed")


def test_frontend_page_evidence_paths_array_and_human_approval_capability(tmp_path):
    context = static_context(required_capabilities=["frontend", "human_approval"])
    report = validation(context, [check("static-html"), check("frontend-page-entry", target="frontend", evidence={"paths": ["/"]}),
                                 check("capability-human_approval")])
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


def test_each_page_requires_its_own_asset_evidence(tmp_path):
    context = static_context(entrypoints=["/", "/students"])
    report = validation(context, [check("static-html"), check("frontend-page-entry", entrypoints=["/", "/students"])])
    asset = next(item for item in report["checks"] if item["id"] == "frontend-page-assets")
    asset["evidence"]["paths"] = ["/"]
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-page-assets:/students"]


@pytest.mark.parametrize("identifier", ["spring-page-assets", "spring-page-assets-1", "frontend-page-assets", "frontend-page-assets-1"])
def test_asset_evidence_has_explicit_page_coverage(tmp_path, identifier):
    context = static_context()
    report = validation(context)
    asset = next(item for item in report["checks"] if item["id"] == "frontend-page-assets")
    asset.update(id=identifier, target="backend", evidence={"path": "/"})
    path = archive(tmp_path, context)
    assert_status(evaluate_delivery(context, report, path), "passed")
    asset["evidence"]["path"] = "/other"
    assert_status(evaluate_delivery(context, report, path), "blocked")


def test_asset_only_evidence_does_not_replace_page_entry(tmp_path):
    context = static_context()
    report = validation(context, [check("static-html"), check("spring-page-assets", path="/")])
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert "delivery-page:/" in result["missing"]


@pytest.mark.parametrize("required", [None, False, True])
@pytest.mark.parametrize("render", [None, "wrong-path", "blocked", "failed", "passed"])
def test_browser_render_is_explicitly_opted_in_and_entry_scoped(tmp_path, required, render):
    context = static_context()
    if required is not None:
        context["delivery_contract"]["browser_required"] = required
    report = validation(context)
    if render is not None:
        report["checks"].append(check("browser-render", "passed" if render == "wrong-path" else render,
                                      evidence={"path": "/other" if render == "wrong-path" else "/"}))
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    expected = "failed" if render == "failed" else "blocked" if render == "blocked" or (required is True and render != "passed") else "passed"
    assert_status(result, expected)


@pytest.mark.parametrize("crud", [None, "passed", "wrong-page", "blocked", "failed"])
def test_browser_backend_crud_requires_separate_matching_ui_check(tmp_path, crud):
    context = spring_context()
    context["delivery_contract"]["browser_required"] = True
    report = validation(context)
    report["checks"].append(check("browser-render", evidence={"path": "/students", "uiCrud": True}))
    if crud is not None:
        report["checks"].append(check("browser-crud", "passed" if crud == "wrong-page" else crud,
                                      evidence={"path": "/other" if crud == "wrong-page" else "/students"}))
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "passed" if crud == "passed" else "failed" if crud == "failed" else "blocked")
    if crud in {None, "wrong-page"}:
        assert "delivery-browser-crud" in result["missing"]


def test_gate_generated_user_messages_are_chinese(tmp_path):
    context = spring_context()
    report = validation(context)
    path = archive(tmp_path, context)
    for supplied_validation, supplied_archive in ((report, path), (None, None), (report, tmp_path / "missing.zip")):
        result = evaluate_delivery(context, supplied_validation, supplied_archive)
        for item in result["checks"]:
            if item["id"].startswith("delivery-"):
                assert re.search(r"[\u4e00-\u9fff]", item["message"]), item
    context["__artifact_files__"] = [file("../escape.py")]
    result = evaluate_delivery(context, None)
    assert "路径无效" in result["checks"][0]["message"]


def test_browser_render_is_additional_to_explicit_page_entry(tmp_path):
    context = static_context(browser_required=True)
    report = validation(context, [check("static-html"), check("browser-render", evidence={"path": "/"})])
    report["checks"].append(check("browser-crud", evidence={"path": "/"}))
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-page:/"]


def _storage_report(context, **audit):
    report = validation(context)
    crud = next(item for item in report["checks"] if item["id"] == "spring-h2-crud")
    crud["evidence"] = {"path": "/api/students", "methods": ["GET", "POST", "PUT", "DELETE"],
                        "fields": ["id", "studentName"], "storageVerified": True, "databaseProduct": "H2", **audit}
    return report


@pytest.mark.parametrize("audit", [{}, {"storageVerified": False}, {"storageVerified": 1}, {"storageVerified": "true"},
                                   {"databaseProduct": None}, {"databaseProduct": "h2"}, {"databaseProduct": "MySQL"}])
def test_h2_storage_audit_requires_exact_true_and_product_on_passed_crud(tmp_path, audit):
    context = spring_context()
    context["delivery_contract"].update(schema_version="1.1", database_audit_required=True)
    report = _storage_report(context, **audit)
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "passed" if not audit else "blocked")
    if audit:
        assert result["missing"] == ["delivery-database-audit:/api/students"]


@pytest.mark.parametrize("missing", ["storageVerified", "databaseProduct", "evidence", "wrong-path", "generic-crud", "top-level-only"])
def test_missing_or_unrelated_h2_proof_blocks_without_weakening_other_gates(tmp_path, missing):
    context = spring_context()
    context["delivery_contract"]["database_audit_required"] = True
    report = _storage_report(context)
    crud = next(item for item in report["checks"] if item["id"] == "spring-h2-crud")
    if missing in {"storageVerified", "databaseProduct"}:
        del crud["evidence"][missing]
    elif missing == "evidence":
        del crud["evidence"]
    elif missing == "wrong-path":
        crud["evidence"]["path"] = "/api/other"
    elif missing == "generic-crud":
        crud["id"] = "crud-h2"
    else:
        crud.update(storageVerified=True, databaseProduct="H2")
        del crud["evidence"]["storageVerified"]
        del crud["evidence"]["databaseProduct"]
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-database-audit:/api/students"]
    assert "实际 H2 存储审计证据" in next(item["message"] for item in result["checks"]
                                       if item["id"] == "delivery-database-audit:/api/students")


@pytest.mark.parametrize("status", ["blocked", "disabled", "failed"])
def test_nonpassed_crud_does_not_certify_h2_even_with_positive_storage_fields(tmp_path, status):
    context = spring_context()
    context["delivery_contract"]["database_audit_required"] = True
    report = _storage_report(context)
    next(item for item in report["checks"] if item["id"] == "spring-h2-crud")["status"] = status
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "failed" if status == "failed" else "blocked")
    assert "delivery-database-audit:/api/students" in result["missing"]


def test_partial_storage_proofs_cannot_be_combined(tmp_path):
    context = spring_context()
    context["delivery_contract"]["database_audit_required"] = True
    report = _storage_report(context)
    crud = next(item for item in report["checks"] if item["id"] == "spring-h2-crud")
    partial = copy.deepcopy(crud)
    del crud["evidence"]["storageVerified"]
    del partial["evidence"]["databaseProduct"]
    report["checks"].append(partial)
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-database-audit:/api/students"]


def test_each_crud_api_needs_its_own_storage_audit_and_read_only_health_is_not_crud(tmp_path):
    context = spring_context()
    context["delivery_contract"]["database_audit_required"] = True
    second = {"path": "/api/books", "methods": ["GET", "POST", "PUT", "DELETE"], "fields": ["id", "title"]}
    context["delivery_contract"]["api_contract"].extend([second, {"path": "/api/health", "methods": ["GET"], "fields": []}])
    report = _storage_report(context)
    result = evaluate_delivery(context, report, archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-database-audit:/api/books"]
    report["checks"].append(check("spring-h2-crud", evidence={**second, "storageVerified": True, "databaseProduct": "H2"}))
    assert_status(evaluate_delivery(context, report, archive(tmp_path, context)), "passed")


@pytest.mark.parametrize("required", [None, False])
def test_legacy_or_explicitly_unaudited_contract_does_not_require_new_storage_fields(tmp_path, required):
    context = spring_context()
    if required is not None:
        context["delivery_contract"]["database_audit_required"] = required
    assert_status(evaluate_delivery(context, validation(context), archive(tmp_path, context)), "passed")


def test_required_storage_audit_without_any_crud_api_cannot_vacuously_pass(tmp_path):
    context = spring_context()
    context["delivery_contract"].update(database_audit_required=True, crud_required=False, api_contract=[])
    result = evaluate_delivery(context, validation(context), archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-database-audit"]


@pytest.mark.parametrize("required", ["true", 1, None])
def test_storage_audit_contract_flag_must_be_boolean(tmp_path, required):
    context = spring_context()
    context["delivery_contract"]["database_audit_required"] = required
    result = evaluate_delivery(context, validation(context), archive(tmp_path, context))
    assert_status(result, "blocked")
    assert result["missing"] == ["delivery-contract"]
