"""Contract-scoped delivery decisions; no commands, models or database writes.

Only ``context.delivery_contract`` opts a run in. Legacy/analysis runs return
``not_required`` with ``deliverable=False`` even if ``context.required`` is set.
A present contract cannot be bypassed by setting ``required=False``.

Validation must describe checks on the exact ``__artifact_files__`` snapshot:
stamp ``artifactFingerprint=artifact_fingerprint(files)`` when validating, not
when delivering. Fingerprints hash sorted (owner, name, SHA256(content)) rows;
content is UTF-8, including whitespace, and owner uses step_id/owner_step.
Only database/backend/frontend owners (or unowned source/config/assets) enter
the snapshot; Tester/Reviewer and other report owners never invalidate it.

Page checks must have id spring-page-*, frontend-page-* or browser-*, pass,
and explicitly cover every contract entrypoint using path, entrypoint,
entrypoints, paths, coveredEntrypoints or a concrete target path. The same keys
may live in an evidence object. Health/startup, prose and static-html alone
never prove page delivery. Missing browser/page evidence therefore BLOCKS,
even when validation.status is passed. API checks (api-contract*, spring-api-*,
frontend-backend-*-contract, or CRUD checks) must expose api_contract/apiContract
rows or path/methods/fields evidence covering the frozen API contract.
Each entrypoint also needs separate spring-page-assets* or frontend-page-assets*
passed evidence covering that page. Assets, render and UI CRUD checks cannot
replace explicit entry checks.
If browser_required is explicitly true, browser-render or browser-render-*
must additionally pass for each entry; absent browser_required retains legacy
contract compatibility. A missing render check never passes that opt-in gate.
Opted-in backend CRUD also requires a separate browser-crud* passed check on
a contract page. browser-render.uiCrud alone cannot replace actual UI evidence.
If database_audit_required is explicitly true, each mutating CRUD API path
also requires a passed spring-h2-crud check whose evidence on that exact path
has storageVerified=True and databaseProduct="H2". Configuration, generic CRUD
checks and separate partial proofs cannot establish actual H2 storage. Missing
storage evidence or a contract without an auditable CRUD path blocks delivery.
The read-only temporary audit probe is not a registered/shipped source file.

An archive is required for opted-in runs. Read all ZIP members to verify CRC,
then compare every registered SOURCE byte-for-byte. Both flat and owner-root
layouts are supported, including backend-owned Spring resources produced by
frontend/database agents. No extraction is performed. If materialization trims
content, callers must align that snapshot BEFORE validation; trimming cannot
be excused after a successful check. Registered non-source reports are optional
and CRC-checked, not fingerprinted. Unregistered files are rejected except
final-report.md and delivery-report.json (pure delivery metadata). When context
has delivery_contract_hash, it must match both the current contract and the
validation.contractHash proof. API payload keys join the required fields;
payload values are test inputs, not necessarily the resulting stored values.
``missing`` contains unsatisfied check IDs.
Failed evidence wins over ordinary missing gates; an obsolete fingerprint
always blocks because its evidence cannot certify the current snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
import zlib
from pathlib import Path
from typing import Any

from ..services.artifact_paths import normalize_artifact_path


_NONE = {"", "none", "disabled", "not_required", "no", "off"}
_SOURCE_OWNERS = {"database", "backend", "frontend"}
_SOURCE_SUFFIXES = {".py", ".java", ".kt", ".kts", ".gradle", ".xml", ".html", ".htm",
                    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".css", ".scss",
                    ".sass", ".less", ".json", ".yaml", ".yml", ".sql", ".properties", ".toml",
                    ".txt", ".svg", ".csv"}
_DELIVERY_METADATA = {"final-report.md", "delivery-report.json"}


def _source_files(files: Any) -> list[tuple[str, str, str]]:
    if not isinstance(files, list) or not files:
        raise ValueError("缺少可验证的结构化 Artifact 源文件。")
    rows = []
    identities = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Artifact 文件清单项必须是对象。")
        raw_owner = item.get("step_id") or item.get("owner_step") or item.get("ownerStep") or ""
        owner = normalize_artifact_path(raw_owner) if raw_owner else ""
        if owner is None:
            raise ValueError("Artifact 所属节点路径无效。")
        name = normalize_artifact_path(item.get("name") or item.get("path"))
        if name in _DELIVERY_METADATA:
            continue
        if owner and owner.split("/", 1)[0] not in _SOURCE_OWNERS:
            continue
        if not owner and name and Path(name).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        content = item.get("content")
        if not name or owner is None or not isinstance(content, str) or not content.strip():
            raise ValueError("Artifact 所属节点或文件路径无效，或文件内容为空。")
        identity = (owner, name)
        if identity in identities:
            raise ValueError(f"Artifact 文件重复登记：{owner}/{name}。")
        identities.add(identity)
        rows.append((owner, name, content))
    if not rows:
        raise ValueError("缺少可验证的结构化 Artifact 源文件。")
    return sorted(rows)


def _fingerprint(rows: list[tuple[str, str, str]]) -> str:
    hashes = [(owner, name, hashlib.sha256(content.encode("utf-8")).hexdigest())
              for owner, name, content in rows]
    return hashlib.sha256(json.dumps(hashes, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def artifact_fingerprint(files: list[dict[str, Any]]) -> str:
    """Hash source only; later report additions do not change validated proof.

    Invalid source manifests or snapshots without any source raise ValueError.
    """
    return _fingerprint(_source_files(files))


def _mode(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _path(value: Any) -> str:
    # Endpoint matching is exact: /api/health must never cover / or /students.
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip().replace("\\", "/")
    if not value.startswith("/") or "://" in value or "?" in value or "#" in value or "*" in value:
        return ""
    return value.rstrip("/") or "/"


def _entrypoints(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {_path(item.get("path") if isinstance(item, dict) else item)
            for item in value} - {""}


def _covered_paths(check: dict[str, Any]) -> set[str]:
    paths = set()
    evidence = check.get("evidence")
    for data in (check, evidence if isinstance(evidence, dict) else {}):
        for key in ("path", "entrypoint", "target"):
            paths.add(_path(data.get(key)))
        for key in ("entrypoints", "paths", "coveredEntrypoints"):
            paths.update(_entrypoints(data.get(key)))
    return paths - {""}


def _page_check(check: dict[str, Any]) -> bool:
    identifier = str(check.get("id", ""))
    return (identifier.startswith(("spring-page-", "frontend-page-", "browser-"))
            and not _asset_check(check) and not _render_check(check) and not _browser_crud_check(check))


def _asset_check(check: dict[str, Any]) -> bool:
    identifier = str(check.get("id", ""))
    return any(identifier == prefix or identifier.startswith(prefix + "-")
               for prefix in ("spring-page-assets", "frontend-page-assets"))


def _render_check(check: dict[str, Any]) -> bool:
    identifier = str(check.get("id", ""))
    return identifier == "browser-render" or identifier.startswith("browser-render-")


def _browser_crud_check(check: dict[str, Any]) -> bool:
    identifier = str(check.get("id", ""))
    return identifier == "browser-crud" or identifier.startswith("browser-crud-")


def _crud_check(check: dict[str, Any]) -> bool:
    identifier = str(check.get("id", ""))
    return identifier in {"spring-h2-crud", "browser-local-crud"} or identifier.startswith("crud-")


def _api_rows(check: dict[str, Any]) -> list[dict[str, Any]]:
    identifier = str(check.get("id", ""))
    if not (_crud_check(check) or identifier.startswith(("api-contract", "spring-api-"))
            or (identifier.startswith("frontend-backend-") and identifier.endswith("-contract"))):
        return []
    rows = []
    evidence = check.get("evidence")
    for data in (check, evidence if isinstance(evidence, dict) else {}):
        if "path" in data and "methods" in data:
            rows.append(data)
        for key in ("api_contract", "apiContract"):
            if isinstance(data.get(key), list):
                rows.extend(item for item in data[key] if isinstance(item, dict))
    return rows


def _strings(value: Any, *, upper: bool = False) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        return set()
    return {item.upper() if upper else item for item in value}


def _archive_names(owner: str, name: str, owners: set[str]) -> set[str]:
    candidates = {name}
    root = owner.split("/", 1)[0] if owner else ""
    if root and not name.startswith(root + "/"):
        candidates.add(f"{root}/{name}")
    if ("backend" in owners and root in {"frontend", "database"}
            and name.startswith("src/main/resources/")):
        candidates.add(f"backend/{name}")
    return candidates


def _archive_check(path: Path | None, rows: list[tuple[str, str, str]],
                   registered_files: list[dict[str, Any]]) -> tuple[str, str]:
    if path is None:
        return "blocked", "尚未提供交付 ZIP，无法验证归档完整性。"
    try:
        if not Path(path).is_file():
            return "blocked", "交付 ZIP 不存在或不是可读取的文件。"
        with zipfile.ZipFile(path) as archive:
            members: dict[str, bytes] = {}
            for info in archive.infolist():
                name = normalize_artifact_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                if not name or name in members or stat.S_ISLNK(info.external_attr >> 16):
                    return "failed", "ZIP 包含不安全路径、重复文件或符号链接。"
                # ZipFile.read verifies the actual uncompressed bytes against CRC.
                data = archive.read(info)
                if info.is_dir():
                    if data:
                        return "failed", "ZIP 目录项异常包含文件内容。"
                    continue
                members[name] = data
            if not members:
                return "failed", "交付 ZIP 不包含任何文件。"
            owners = {owner.split("/", 1)[0] for owner, _, _ in rows if owner}
            consumed = set()
            for owner, name, content in rows:
                candidates = _archive_names(owner, name, owners)
                present = candidates & members.keys()
                if not present:
                    return "failed", f"ZIP 缺少已登记源文件：{owner}/{name}。"
                expected = content.encode("utf-8")
                matches = {candidate for candidate in present
                           if members[candidate] == expected and candidate not in consumed}
                if not matches:
                    return "failed", f"ZIP 源文件内容不一致或文件身份冲突：{owner}/{name}。"
                # Prefer the owner's directory if flat names are also present.
                chosen = sorted(matches, key=lambda candidate: (candidate == name, candidate))[0]
                consumed.add(chosen)
            source_identities = {(owner, name) for owner, name, _ in rows}
            allowed_reports = set(_DELIVERY_METADATA)
            for item in registered_files:
                name = normalize_artifact_path(item.get("name") or item.get("path"))
                raw_owner = item.get("step_id") or item.get("owner_step") or item.get("ownerStep") or ""
                owner = normalize_artifact_path(raw_owner) if raw_owner else ""
                if name and owner is not None and (owner, name) not in source_identities:
                    allowed_reports.update(_archive_names(owner, name, owners))
            extras = members.keys() - consumed - allowed_reports
            if extras:
                return "failed", "ZIP 包含未登记文件：" + "、".join(sorted(extras)) + "。"
        return "passed", "ZIP CRC 校验通过，全部已登记源文件与验证快照逐字节一致。"
    except (OSError, ValueError, TypeError, zipfile.BadZipFile, RuntimeError,
            NotImplementedError, EOFError, zlib.error) as exc:
        return "failed", f"ZIP 无法读取或已损坏：{type(exc).__name__}。"


def evaluate_delivery(context: dict, validation: dict | None,
                      archive_path: Path | None = None) -> dict:
    """Return status, deliverable, checks, missing and fingerprint for one snapshot.

    Checks use the validator's id/status/message dictionary convention. Missing
    or disabled mandatory evidence is blocked; failed checks, invalid sources
    and corrupt/incomplete ZIPs fail. No aggregate passed status bypasses gates.
    """
    contract = context.get("delivery_contract")
    if contract is None:
        return {"status": "not_required", "deliverable": False, "checks": [],
                "missing": [], "fingerprint": None}
    checks: list[dict[str, Any]] = []

    def record(identifier: str, status: str, message: str) -> None:
        checks.append({"id": identifier, "status": status, "message": message})

    def require(identifier: str, candidates: list[dict[str, Any]]) -> None:
        status = ("failed" if any(item["status"] == "failed" for item in candidates)
                  else "passed" if any(item["status"] == "passed" for item in candidates)
                  else "blocked")
        record(identifier, status, "必需检查的验证证据已通过。" if status == "passed"
               else "必需检查失败，或缺少已启用且通过的验证证据。")

    fingerprint = None
    stale = False

    def result() -> dict:
        status = ("blocked" if stale else "failed" if any(item["status"] == "failed" for item in checks)
                  else "blocked" if any(item["status"] == "blocked" for item in checks) else "passed")
        return {"status": status, "deliverable": status == "passed", "checks": checks,
                "missing": list(dict.fromkeys(item["id"] for item in checks if item["status"] != "passed")),
                "fingerprint": fingerprint}

    if (not isinstance(contract, dict) or not isinstance(contract.get("schema_version"), str)
            or not contract["schema_version"].strip()):
        record("delivery-contract", "blocked", "交付合同或 Schema 版本缺失、无效。")
        return result()
    for key in ("backend_stack", "frontend_stack", "page_mode", "database_mode"):
        if key not in contract or not isinstance(contract[key], str):
            record("delivery-contract", "blocked", f"交付合同字段 {key} 缺失或无效。")
    for key in ("entrypoints", "api_contract", "required_capabilities"):
        if not isinstance(contract.get(key), list):
            record("delivery-contract", "blocked", f"交付合同字段 {key} 缺失或不是数组。")
    if not isinstance(contract.get("crud_required"), bool):
        record("delivery-contract", "blocked", "交付合同字段 crud_required 必须是布尔值。")
    if "browser_required" in contract and not isinstance(contract["browser_required"], bool):
        record("delivery-contract", "blocked", "交付合同字段 browser_required 必须是布尔值。")
    if "database_audit_required" in contract and not isinstance(contract["database_audit_required"], bool):
        record("delivery-contract", "blocked", "交付合同字段 database_audit_required 必须是布尔值。")
    if checks:
        return result()
    entries = _entrypoints(contract["entrypoints"])
    if len(entries) != len(contract["entrypoints"]):
        record("delivery-contract", "blocked", "交付合同的页面入口必须是明确且不重复的路径。")
    capabilities = _strings(contract["required_capabilities"])
    if len(capabilities) != len(contract["required_capabilities"]):
        record("delivery-contract", "blocked", "交付合同的必需能力列表包含无效或重复项。")
    api_contract = contract["api_contract"]
    for api in api_contract:
        if (not isinstance(api, dict) or not _path(api.get("path"))
                or not _strings(api.get("methods"), upper=True)
                or not _strings(api.get("methods"), upper=True) <= {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                or not isinstance(api.get("fields"), list)
                or len(_strings(api["fields"])) != len(api["fields"])):
            record("delivery-contract", "blocked", "API 合同必须包含有效路径、HTTP 方法和字段名列表。")
        elif "payload" in api and (not isinstance(api["payload"], dict)
                                   or any(not isinstance(key, str) or not key for key in api["payload"])):
            record("delivery-contract", "blocked", "API 请求数据必须是包含有效字段名的对象。")
    if checks:
        return result()
    scope = contract.get("scope_evidence")
    if scope is not None and not isinstance(scope, dict):
        record("delivery-scope-evidence", "blocked", "原始需求范围证据格式无效。")
    elif isinstance(scope, dict):
        known_entities = {
            str(item.get("name") or item.get("id") or "").casefold(): item
            for item in contract.get("entities", []) if isinstance(item, dict)
        }
        for source in scope.get("entities") or []:
            entity = str(source.get("entity") or "").strip() if isinstance(source, dict) else ""
            if entity and entity.casefold() not in known_entities:
                record(f"delivery-scope-entity:{entity}", "failed", f"原始需求明确提到实体 {entity}，但冻结实体合同中缺失。")
        fields_by_entity = scope.get("entity_fields") or {}
        if not isinstance(fields_by_entity, dict):
            record("delivery-scope-fields", "blocked", "原始需求字段证据格式无效。")
        else:
            for entity, fields in fields_by_entity.items():
                declared = known_entities.get(str(entity).casefold(), {}).get("fields") or {}
                missing = set(fields) - set(declared) if isinstance(fields, dict) else set()
                if missing:
                    record(f"delivery-scope-fields:{entity}", "failed", f"实体 {entity} 缺少原始需求字段：" + "、".join(sorted(missing)))
        explicit_paths = scope.get("api_paths") or []
        frozen_paths = {
            _path(row.get(key)) for row in api_contract if isinstance(row, dict)
            for key in ("path", "collection_path", "detail_path") if row.get(key)
        }
        for path in explicit_paths:
            if _path(path) not in frozen_paths:
                record(f"delivery-scope-api:{path}", "failed", f"冻结 API 合同缺少用户明确指定的路径 {_path(path)}。")
    if checks:
        return result()
    try:
        rows = _source_files(context.get("__artifact_files__"))
        fingerprint = _fingerprint(rows)
        record("delivery-artifacts", "passed", "结构化 Artifact 源文件清单校验通过。")
    except (ValueError, UnicodeError) as exc:
        record("delivery-artifacts", "failed", "Artifact 源码无法按 UTF-8 编码。" if isinstance(exc, UnicodeError) else str(exc))
        return result()

    validation = validation if isinstance(validation, dict) else {}
    raw_checks = validation.get("checks")
    evidence: list[dict[str, Any]] = []
    if not isinstance(raw_checks, list) or not raw_checks:
        record("delivery-validation", "blocked", "缺少确定性验证检查，无法确认可交付。")
    else:
        for item in raw_checks:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                record("delivery-validation", "blocked", "验证检查格式无效或缺少检查标识。")
                continue
            item = dict(item)
            if (item.get("status") not in ("passed", "failed", "blocked")
                    or (item.get("enabled") is False and item.get("status") != "failed")):
                item["status"] = "blocked"
            command = str(item.get("command") or "")
            if item["status"] == "passed" and re.search(
                    r"(?:-x\s+test\b|(?:-D|\b)(?:skipTests|maven\.test\.skip)(?:=true)?(?=\s|$))", command, re.IGNORECASE):
                item["status"] = "blocked"
            evidence.append(item)
            checks.append(dict(item))
    overall = validation.get("status")
    record("delivery-validation-status", overall if overall in ("passed", "failed", "blocked") else "blocked",
           "已核对验证汇总状态；仍须逐项满足全部交付门禁。")
    if validation.get("enabled") is False:
        record("delivery-validation-enabled", "blocked", "确定性验证已禁用，无法确认可交付。")
    expected = validation.get("artifactFingerprint")
    stale = bool(expected and expected != fingerprint)
    record("delivery-fingerprint", "passed" if expected == fingerprint else "blocked",
           "当前源码与已验证快照的指纹一致。" if expected == fingerprint else "验证指纹缺失或已过期，当前源码需要重新验证。")
    if "delivery_contract_hash" in context:
        contract_digest = hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                                                    separators=(",", ":")).encode("utf-8")).hexdigest()
        contract_matches = context["delivery_contract_hash"] == contract_digest == validation.get("contractHash")
        stale = stale or not contract_matches
        record("delivery-contract-hash", "passed" if contract_matches else "blocked",
               "当前合同、冻结合同哈希及验证证据一致。" if contract_matches else "冻结合同哈希或验证证据缺失、不一致，需要重新验证。")

    backend = _mode(contract["backend_stack"]) not in _NONE or "backend" in capabilities
    frontend_mode = _mode(contract["frontend_stack"])
    page_mode = _mode(contract["page_mode"])
    frontend = frontend_mode not in _NONE or page_mode not in _NONE or "frontend" in capabilities
    database = _mode(contract["database_mode"]) not in _NONE or "persistence" in capabilities
    names = set()
    for owner, name, _ in rows:
        root = owner.split("/", 1)[0] if owner else ""
        names.add(name[len(root) + 1:] if root and name.startswith(root + "/") else name)

    def source_requirement(identifier: str, satisfied: bool) -> None:
        record(identifier, "passed" if satisfied else "failed", "已找到合同要求的源文件。" if satisfied else "缺少合同要求的源文件。")

    if backend:
        maven = "pom.xml" in names
        gradle = bool({"build.gradle", "build.gradle.kts"} & names)
        source_requirement("delivery-backend-artifacts", (maven or gradle) and any(
            name.startswith(("src/main/java/", "src/main/kotlin/")) and name.endswith((".java", ".kt")) for name in names))
        if _mode(contract["backend_stack"]) not in {"spring", "spring_boot", "springboot", "java", "maven", "gradle"}:
            record("delivery-backend-stack", "blocked", "当前后端技术栈缺少受控的交付构建验证策略。")
        tests = [item for item in evidence if item["id"] in {"backend-test", "maven-test" if maven else "gradle-test"}]
        builds = [item for item in evidence if item["id"] in {"backend-build", "maven-build" if maven else "gradle-build"}]
        # Maven test includes compilation. Gradle build with -x test does not.
        require("delivery-backend-build", builds + ([item for item in tests if item["id"] != "gradle-test"] if maven else []))
        require("delivery-backend-test", tests)
        require("delivery-backend-startup", [item for item in evidence if item["id"] == "backend-startup"])
    if frontend:
        if frontend_mode in {"vue", "vue3", "vue_3", "vue_ts", "vue_typescript"} or page_mode in {"vue", "spa"}:
            source_requirement("delivery-frontend-artifacts", "package.json" in names and "index.html" in names
                               and bool({"src/main.js", "src/main.ts", "src/main.mjs"} & names)
                               and any(name.endswith(".vue") for name in names))
            require("delivery-frontend-build", [item for item in evidence if item["id"] == "frontend-build"])
        elif page_mode in {"template", "thymeleaf", "spring_mvc", "server_rendered"}:
            source_requirement("delivery-page-artifacts", any(name.startswith("src/main/resources/templates/")
                                                               and name.endswith(".html") for name in names))
        elif page_mode == "static_rest":
            source_requirement("delivery-page-artifacts", backend and any(
                name.startswith("src/main/resources/static/") and name.endswith(".html") for name in names))
        elif frontend_mode in {"static", "static_html", "html"} or page_mode in {"static", "static_html", "html"}:
            source_requirement("delivery-frontend-artifacts", any(name.endswith(".html") for name in names))
            require("delivery-static-html", [item for item in evidence if item["id"] == "static-html"])
        else:
            record("delivery-frontend-stack", "blocked", "当前前端技术栈或页面模式缺少受控的交付验证策略。")
        if not entries:
            record("delivery-page-entrypoints", "blocked", "页面交付必须在合同中声明明确的入口路径。")
    for entry in sorted(entries):
        require(f"delivery-page:{entry}", [item for item in evidence if _page_check(item) and entry in _covered_paths(item)])
        require(f"delivery-page-assets:{entry}", [item for item in evidence if _asset_check(item) and entry in _covered_paths(item)])
        if contract.get("browser_required") is True:
            require(f"delivery-browser-render:{entry}", [item for item in evidence if _render_check(item) and entry in _covered_paths(item)])
    if database:
        source_requirement("delivery-database-artifacts", any(name.endswith(".sql") or "@Entity" in content
                           or "spring.datasource" in content or "datasource:" in content for _, name, content in rows))
    if contract["crud_required"] or "crud" in capabilities:
        require("delivery-crud", [item for item in evidence if _crud_check(item)])
        if backend and not api_contract:
            record("delivery-api-contract", "blocked", "后端 CRUD 交付缺少冻结的 API 合同。")
        if backend and contract.get("browser_required") is True:
            require("delivery-browser-crud", [item for item in evidence
                    if _browser_crud_check(item)
                    and _covered_paths(item) & entries])
    acceptance_spec = contract.get("acceptance_spec")
    if acceptance_spec is not None:
        if (not isinstance(acceptance_spec, dict)
                or not isinstance(acceptance_spec.get("version"), str)
                or not isinstance(acceptance_spec.get("checks"), list)):
            record("delivery-acceptance-spec", "blocked", "验收规范缺少有效版本或检查列表。")
        else:
            api_by_entity = {
                str(item.get("entity_id") or "").casefold(): _path(item.get("path"))
                for item in api_contract if isinstance(item, dict) and item.get("entity_id")
            }
            for assertion in acceptance_spec["checks"]:
                if not isinstance(assertion, dict):
                    record("delivery-acceptance-spec", "blocked", "验收项格式无效。")
                    continue
                identifier = str(assertion.get("id") or "").strip()
                kind = str(assertion.get("kind") or "")
                entity = str(assertion.get("entity_id") or "").casefold()
                path = _path(assertion.get("api_path"))
                operations = assertion.get("operations")
                if (not identifier or kind not in {"crud_lifecycle", "database_roundtrip"}
                        or not isinstance(operations, list) or not operations
                        or entity not in api_by_entity or path != api_by_entity.get(entity)):
                    record("delivery-acceptance:" + (identifier or "invalid"), "blocked",
                           "验收项必须绑定冻结的实体、API 路径和操作列表。")
                    continue
                candidates = [
                    item for item in evidence
                    if item["id"] == "spring-h2-crud"
                    and item["status"] == "passed"
                    and isinstance(item.get("evidence"), dict)
                    and _path(item["evidence"].get("path")) == path
                ]
                if kind == "database_roundtrip":
                    candidates = [item for item in candidates
                                  if item.get("evidence", {}).get("storageVerified") is True
                                  and item.get("evidence", {}).get("databaseProduct") == "H2"]
                record(
                    f"delivery-acceptance:{identifier}",
                    "passed" if candidates else "blocked",
                    "合同验收项已由对应的真实 CRUD/数据库验证证据覆盖。" if candidates
                    else "缺少该实体与 API 路径对应的通过证据；不得以其他实体的验收结果替代。",
                )
    api_rows = [row for item in evidence if item["status"] == "passed" for row in _api_rows(item)]
    for api in api_contract:
        path = _path(api["path"])
        fields = _strings(api["fields"]) | set(api.get("payload", {}))
        for method in sorted(_strings(api["methods"], upper=True)):
            covered = any(_path(row.get("path")) == path and method in _strings(row.get("methods"), upper=True)
                          and fields <= _strings(row.get("fields")) for row in api_rows)
            record(f"delivery-api:{method}:{path}", "passed" if covered else "blocked",
                   "冻结 API 合同的方法及字段验证通过。" if covered else "缺少覆盖冻结 API 方法及字段的明确验证证据。")
    if contract.get("database_audit_required") is True:
        audit_paths = sorted({_path(api["path"]) for api in api_contract
                              if _strings(api["methods"], upper=True) & {"POST", "PUT", "PATCH", "DELETE"}})
        if not audit_paths:
            record("delivery-database-audit", "blocked", "合同要求实际 H2 存储审计，但缺少明确的 CRUD API 路径。")
        for path in audit_paths:
            verified = any(item["id"] == "spring-h2-crud" and item["status"] == "passed"
                           and isinstance(item.get("evidence"), dict)
                           and _path(item["evidence"].get("path")) == path
                           and item["evidence"].get("storageVerified") is True
                           and item["evidence"].get("databaseProduct") == "H2" for item in evidence)
            record(f"delivery-database-audit:{path}", "passed" if verified else "blocked",
                   f"CRUD API {path} 的实际 H2 存储审计已通过。" if verified
                   else f"CRUD API {path} 缺少通过的实际 H2 存储审计证据，需要重新验证。")
    for capability in sorted(capabilities - {"backend", "frontend", "persistence", "crud", "artifact"}):
        require(f"delivery-capability:{capability}", [item for item in evidence
                if item["id"] in {capability, f"capability-{capability}"}])
    archive_status, message = _archive_check(archive_path, rows, context["__artifact_files__"])
    record("delivery-archive", archive_status, message)
    return result()
