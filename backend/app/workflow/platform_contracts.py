"""Provider-neutral platform contracts shared by Runtime, Gates and UI.

The executor remains the compatibility layer for the existing YAML workflow.
These contracts make the important decisions durable and inspectable without
forcing individual Agents to know about SQLite, LangGraph or HTTP routes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RequirementState = Literal["CLEAR", "ASSUMPTION_ALLOWED", "USER_CLARIFICATION_REQUIRED"]
ClarificationStatus = Literal["OPEN", "ANSWERED", "CANCELLED"]
BlueprintStatus = Literal["DRAFT", "FROZEN", "CHANGE_REQUESTED"]
EvidenceStatus = Literal["passed", "failed", "blocked", "not_run", "skipped"]
ArtifactVersionStatus = Literal["Candidate", "Stable", "Rejected"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    unresolved_fields: list[str] = Field(default_factory=list, max_length=32)
    reason: str = ""
    impact: str = ""
    options: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    recommended_default: dict[str, Any] = Field(default_factory=dict)
    prompt_reference: str = ""
    field_prompts: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    status: ClarificationStatus = "OPEN"
    created_at: str = Field(default_factory=utc_now)
    answered_at: str | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


class ContractChangeRequest(BaseModel):
    """A proposed change to a frozen Blueprint; never applied implicitly."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    source_agent: str
    reason: str
    affected_sections: list[str] = Field(default_factory=list, max_length=32)
    proposed_changes: dict[str, Any] = Field(default_factory=dict)
    status: Literal["OPEN", "APPROVED", "REJECTED"] = "OPEN"
    created_at: str = Field(default_factory=utc_now)


class ProjectBlueprint(BaseModel):
    """The single source of truth frozen before expensive implementation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "2.0"
    blueprint_id: str
    version: int = 1
    status: BlueprintStatus = "DRAFT"
    project_type: str = "unknown"
    backend: dict[str, Any] = Field(default_factory=dict)
    frontend: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)
    capability_profile: dict[str, bool] = Field(default_factory=dict)
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    dto: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    api_contract: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    entrypoints: list[str] = Field(default_factory=list, max_length=32)
    build_command: str | None = None
    start_command: str | None = None
    port_policy: dict[str, Any] = Field(default_factory=dict)
    dependency_manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_ownership: dict[str, Any] = Field(default_factory=dict)
    file_dependencies: list[dict[str, Any]] = Field(default_factory=list, max_length=512)
    role_contracts: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    delivery_requirements: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    assumption_records: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    source_requirement_hash: str = ""
    contract_hash: str | None = None
    blueprint_hash: str | None = None
    approved_requirement: str = ""
    change_requests: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    frozen_at: str | None = None


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    gate: str
    status: EvidenceStatus
    category: str = "integration"
    expected: str = ""
    actual: str = ""
    message: str = ""
    related_files: list[str] = Field(default_factory=list, max_length=64)
    command: str | None = None
    output: str = ""
    timestamp: str = Field(default_factory=utc_now)


class FailureFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_id: str
    code: str = "UNCLASSIFIED"
    category: str
    owner: str
    gate: str
    file: str | None = None
    related_files: list[str] = Field(default_factory=list, max_length=64)
    evidence: dict[str, Any] = Field(default_factory=dict)
    expected: str = ""
    actual: str = ""
    evidence_id: str | None = None
    related_contract: str | None = None
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    message: str = ""
    summary: str = ""
    stage: str = "execution"
    repairable: bool = False
    retryable: bool = False
    repair_action: str = ""
    repair_scope: list[str] = Field(default_factory=list, max_length=32)
    fingerprint: str = ""
    attempt: int = 0
    resolved: bool = False


class ArtifactVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    run_id: str
    name: str
    version: int = 1
    status: ArtifactVersionStatus = "Candidate"
    owner_step: str | None = None
    parent_version: int | None = None
    sha256: str | None = None
    created_at: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_project_blueprint(
    requirement: str,
    requirement_spec: dict[str, Any],
    decision: dict[str, Any],
    delivery_contract: dict[str, Any] | None = None,
    *,
    frozen: bool = False,
) -> dict[str, Any]:
    """Build a conservative blueprint from already validated decisions.

    This function only copies normalized facts. It deliberately does not infer
    a database or an entrypoint merely because a backend exists.
    """
    contract = delivery_contract or {}
    source_hash = hashlib.sha256(str(requirement).encode("utf-8")).hexdigest()
    blueprint_id = f"bp_{source_hash[:16]}"
    backend_stack = str(contract.get("backend_stack") or decision.get("backend_stack") or "none")
    frontend_stack = str(contract.get("frontend_stack") or decision.get("frontend_stack") or "none")
    database_mode = str(contract.get("database_mode") or decision.get("database_mode") or "none")
    assumptions = list(contract.get("assumptions") or [])
    safe_defaults = dict(requirement_spec.get("safe_defaults") or {})
    assumptions.extend(
        f"{key}={value}（平台安全默认值）"
        for key, value in safe_defaults.items()
        if value not in (None, "")
    )
    if database_mode == "none" and bool(decision.get("backend_required")):
        assumptions.append("未指定持久化能力，数据库分支保持关闭")
    backend_required = bool(decision.get("backend_required"))
    validation_database = "h2" if backend_required and database_mode != "none" else "none"
    ownership = dict(decision.get("artifact_ownership") or {})
    if not ownership:
        ownership = _default_artifact_ownership(
            backend_stack,
            frontend_stack,
            str(contract.get("page_mode") or ""),
            database_mode,
        )
    blueprint = ProjectBlueprint(
        blueprint_id=blueprint_id,
        status="FROZEN" if frozen else "DRAFT",
        project_type=str(decision.get("project_type") or "unknown"),
        backend={"required": backend_required, "stack": backend_stack},
        frontend={"required": frontend_stack != "none", "stack": frontend_stack},
        database={
            "mode": database_mode,
            "production_engine": database_mode,
            "validation_engine": validation_database,
            "compatibility_mode": "mysql" if database_mode == "mysql" and validation_database == "h2" else None,
            "explicit": database_mode != "none",
        },
        capability_profile=dict(requirement_spec.get("capability_profile") or {}),
        entities=list(contract.get("entities") or decision.get("entities") or []),
        dto=list(decision.get("dto") or []),
        api_contract=list(contract.get("api_contract") or decision.get("api_contract") or []),
        entrypoints=list(contract.get("entrypoints") or decision.get("entrypoints") or []),
        build_command=decision.get("build_command"),
        start_command=decision.get("start_command"),
        port_policy=dict(decision.get("port_policy") or {}),
        artifact_ownership=ownership,
        delivery_requirements={
            "required_capabilities": list(contract.get("required_capabilities") or requirement_spec.get("required_capabilities") or []),
            "crud_required": bool(contract.get("crud_required") or requirement_spec.get("crud_business_signal")),
            "browser_required": bool(contract.get("browser_required", False)),
            "database_audit_required": bool(contract.get("database_audit_required", False)),
            "page_mode": str(contract.get("page_mode") or "none"),
        },
        assumptions=list(dict.fromkeys(str(item) for item in assumptions if str(item).strip())),
        assumption_records=[
            {"source": "platform_default", "field": key, "value": value, "impact": "low", "overridable": True}
            for key, value in safe_defaults.items()
        ],
        constraints={
            "contract_authority": "project_blueprint",
            "agents_may_change_contract": False,
            "platform_policy_overrides_blueprint": True,
            "require_change_request_for_core_contract": True,
        },
        decisions=[{"source": "architecture", "value": decision}],
        source_requirement_hash=source_hash,
        contract_hash=contract.get("contract_hash"),
        approved_requirement=str(requirement)[:100_000],
        frozen_at=utc_now() if frozen else None,
    )
    result = blueprint.model_dump(mode="json")
    hash_payload = {key: value for key, value in result.items() if key not in {"blueprint_hash", "frozen_at"}}
    result["blueprint_hash"] = hashlib.sha256(stable_json(hash_payload).encode("utf-8")).hexdigest()
    return result


def _default_artifact_ownership(
    backend_stack: str,
    frontend_stack: str,
    page_mode: str,
    database_mode: str,
) -> dict[str, list[str]]:
    ownership: dict[str, list[str]] = {}
    if database_mode != "none":
        ownership["database"] = [
            "src/main/resources/schema.sql",
            "src/main/resources/data.sql",
            "src/main/resources/db/migration/**",
            "database/**",
        ]
    if backend_stack == "springboot":
        ownership["backend"] = [
            "pom.xml",
            "src/main/java/**",
            "src/test/java/**",
            "src/main/resources/application.yml",
            "src/main/resources/application.yaml",
            "src/main/resources/application.properties",
        ]
    elif backend_stack != "none":
        ownership["backend"] = ["main.py", "models.py", "service.py", "requirements.txt", "backend/**", "tests/**"]
    if frontend_stack != "none":
        if page_mode in {"static_rest", "template"} and backend_stack == "springboot":
            ownership["frontend"] = ["src/main/resources/static/**", "src/main/resources/templates/**"]
        elif frontend_stack in {"vue", "react"}:
            ownership["frontend"] = [
                "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                "vite.config.*", "index.html", "src/**", "public/**", "frontend/**",
            ]
        else:
            ownership["frontend"] = ["index.html", "style.css", "script.js", "assets/**"]
    return ownership


def evidence_from_check(check: dict[str, Any], *, gate: str = "integration", index: int = 0) -> dict[str, Any]:
    """Normalize a validator/delivery check into an auditable EvidenceRecord."""
    status = str(check.get("status") or "blocked").lower()
    if status not in {"passed", "failed", "blocked", "not_run", "skipped"}:
        status = "blocked"
    return EvidenceRecord(
        evidence_id=str(check.get("evidenceId") or check.get("id") or f"{gate}-{index + 1}"),
        gate=gate,
        status=status,  # type: ignore[arg-type]
        category=str(check.get("category") or gate),
        expected=str(check.get("expected") or ""),
        actual=str(check.get("actual") or ""),
        message=str(check.get("message") or ""),
        related_files=[str(item) for item in (check.get("relatedFiles") or check.get("files") or []) if str(item).strip()],
        command=check.get("command"),
        output=str(check.get("output") or ""),
    ).model_dump(mode="json")


def failure_fact_from_check(
    check: dict[str, Any],
    *,
    gate: str = "integration",
    owner: str = "tester",
    index: int = 0,
) -> dict[str, Any]:
    evidence_id = str(check.get("evidenceId") or check.get("id") or f"{gate}-{index + 1}")
    severity = str(check.get("severity") or ("high" if str(check.get("status")) == "failed" else "medium"))
    if severity not in {"low", "medium", "high", "critical"}:
        severity = "medium"
    files = check.get("relatedFiles") or check.get("files") or []
    return FailureFact(
        failure_id=f"failure_{evidence_id}",
        code=str(check.get("code") or check.get("id") or "VALIDATION_FAILED"),
        category=str(check.get("category") or "validation"),
        owner=str(check.get("owner") or owner),
        gate=gate,
        file=str(files[0]) if isinstance(files, list) and files else None,
        related_files=[str(item) for item in files if str(item).strip()] if isinstance(files, list) else [],
        evidence={"check_id": evidence_id, "output": str(check.get("output") or "")[-3000:]},
        expected=str(check.get("expected") or ""),
        actual=str(check.get("actual") or check.get("message") or ""),
        evidence_id=evidence_id,
        related_contract=check.get("relatedContract"),
        severity=severity,  # type: ignore[arg-type]
        message=str(check.get("message") or ""),
        summary=str(check.get("summary") or check.get("message") or "")[:240],
        stage=str(check.get("stage") or "validation"),
        repairable=bool(check.get("repairable", False)),
        retryable=bool(check.get("retryable", False)),
        repair_action=str(check.get("repairAction") or check.get("repair_action") or ""),
        repair_scope=[str(item) for item in (check.get("repairScope") or check.get("repair_scope") or []) if str(item).strip()],
        fingerprint=str(check.get("fingerprint") or ""),
        attempt=max(0, int(check.get("attempt") or 0)),
    ).model_dump(mode="json")


def normalize_failure_fact(value: Any, *, index: int = 0) -> dict[str, Any] | None:
    """Upgrade legacy failure rows to the current FailureFact contract.

    Older Runs used validator-shaped dictionaries (``id``, ``target``,
    ``files`` and ``action``).  Recovery must retain their evidence while
    applying today's bounded retry policy rather than failing model validation.
    """
    if not isinstance(value, dict):
        return None
    raw = dict(value)
    # Preserve already persisted FailureFact rows byte-for-byte. Some early
    # versions omitted optional fields, but their identity and resolution
    # ordering are still authoritative during recovery.
    if str(raw.get("failure_id") or "").strip():
        return raw
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    related = raw.get("related_files") or raw.get("relatedFiles") or raw.get("files") or []
    if isinstance(related, str):
        related = [related]
    if not isinstance(related, list):
        related = []
    code = str(raw.get("code") or raw.get("check_id") or raw.get("id") or "UNCLASSIFIED")
    owner = str(raw.get("owner") or raw.get("responsibility") or raw.get("target") or "platform")
    category = str(raw.get("category") or raw.get("failure_type") or raw.get("type") or "validation")
    gate = str(raw.get("gate") or raw.get("phase") or raw.get("stage") or "legacy")
    message = str(raw.get("message") or raw.get("error") or raw.get("reason") or "")
    summary = str(raw.get("summary") or message or code)[:240]
    severity = str(raw.get("severity") or "medium").lower()
    if severity not in {"low", "medium", "high", "critical"}:
        severity = "medium"
    action = str(raw.get("repair_action") or raw.get("repairAction") or raw.get("action") or "")
    repair_scope = raw.get("repair_scope") or raw.get("repairScope") or []
    if isinstance(repair_scope, str):
        repair_scope = [repair_scope]
    if not isinstance(repair_scope, list):
        repair_scope = []
    failure_id = str(raw.get("failureId") or "").strip()
    if not failure_id:
        digest = hashlib.sha256(stable_json([code, owner, gate, summary]).encode("utf-8")).hexdigest()[:16]
        failure_id = f"failure_legacy_{digest}"
    legacy = not all(key in raw for key in ("failure_id", "category", "owner", "gate"))
    normalized_evidence = dict(evidence)
    if legacy:
        normalized_evidence["legacy"] = True
        normalized_evidence["legacyFields"] = sorted(str(key) for key in raw.keys())[:64]
    return FailureFact(
        failure_id=failure_id,
        code=code,
        category=category,
        owner=owner,
        gate=gate,
        file=str(raw.get("file") or related[0]) if raw.get("file") or related else None,
        related_files=[str(item) for item in related if str(item).strip()],
        evidence=normalized_evidence,
        expected=str(raw.get("expected") or ""),
        actual=str(raw.get("actual") or message),
        evidence_id=str(raw.get("evidence_id") or raw.get("evidenceId") or raw.get("check_id") or "") or None,
        related_contract=raw.get("related_contract") or raw.get("relatedContract"),
        severity=severity,  # type: ignore[arg-type]
        message=message,
        summary=summary,
        stage=str(raw.get("stage") or raw.get("phase") or "execution"),
        repairable=bool(raw.get("repairable", raw.get("can_auto_repair", False))),
        retryable=bool(raw.get("retryable", raw.get("retry", False))),
        repair_action=action,
        repair_scope=[str(item) for item in repair_scope if str(item).strip()],
        fingerprint=str(raw.get("fingerprint") or raw.get("retry_fingerprint") or ""),
        attempt=max(0, int(raw.get("attempt") or raw.get("repair_round") or 0)),
        resolved=bool(raw.get("resolved") or str(raw.get("status") or "").lower() == "resolved"),
    ).model_dump(mode="json")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def request_blueprint_change(
    blueprint: dict[str, Any],
    *,
    source_agent: str,
    reason: str,
    affected_sections: list[str],
    proposed_changes: dict[str, Any],
) -> dict[str, Any]:
    """Record a proposal without mutating any frozen contract section."""
    if str(blueprint.get("status") or "") != "FROZEN":
        raise ValueError("Only a frozen Blueprint can receive a change request")
    digest = hashlib.sha256(
        stable_json([blueprint.get("blueprint_id"), blueprint.get("version"), source_agent, reason, proposed_changes]).encode("utf-8")
    ).hexdigest()[:16]
    request = ContractChangeRequest(
        request_id=f"bcr_{digest}",
        source_agent=source_agent,
        reason=reason,
        affected_sections=affected_sections,
        proposed_changes=proposed_changes,
    ).model_dump(mode="json")
    updated = dict(blueprint)
    updated["status"] = "CHANGE_REQUESTED"
    updated["change_requests"] = [*(blueprint.get("change_requests") or []), request]
    return updated
