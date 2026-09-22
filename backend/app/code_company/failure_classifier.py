"""Deterministic failure classification for workflow and validation errors."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from ..workflow.artifact_validator import ValidationCheck


@dataclass(frozen=True, slots=True)
class FailureClassification:
    category: str
    owners: tuple[str, ...]
    stage: str
    action: str
    repairable: bool
    retryable: bool
    severity: str = "high"
    defect_scope: str = "implementation"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["owners"] = list(self.owners)
        return value


class FailureClassifier:
    """Map raw failures to a stable category, stage and source owner."""

    AGENT_OWNERS = ("database", "backend", "frontend")
    PLATFORM_MARKERS = (
        "401", "403", "api key", "apikey", "authentication", "unauthorized",
        "forbidden", "incorrect api key", "model_not_found", "unknown model",
        "unsupported model", "provider configuration",
    )
    TIMEOUT_MARKERS = (
        "timeout", "timed out", "readtimeout", "connecttimeout", "pooltimeout",
        "connection refused", "connection reset", "temporarily unavailable",
        "rate limit", "429", "502", "503", "504",
    )
    STRUCTURED_MARKERS = (
        "json", "pydantic", "validation error", "attributeerror",
        "has no attribute", "schema", "expected a valid", "字段", "类型未明确",
    )

    def classify_node(self, step_id: str, error: str, *, error_type: str = "") -> FailureClassification:
        step = str(step_id or "unknown")
        lowered = str(error or "").lower()
        if step in {"requirement", "token_estimator"} and error_type in {"ValueError", "ValidationError"}:
            return FailureClassification(
                "requirement_routing", ("requirement",), "requirement",
                "recheck_original_requirement", False, False, "high", "contract",
            )
        if step in {"architecture", "tester"} and error_type in {"AttributeError", "TypeError", "KeyError", "AssertionError"}:
            return FailureClassification(
                "platform_defect", ("platform",), "validation",
                "inspect_platform", False, False, "critical", "platform",
            )
        if step == "architecture" and any(marker in lowered for marker in (
            "entity_id", "实体合同", "api 合同", "字段", "query_parameters", "contract",
            "成果物文件计划", "file plan", "file_plan", "artifact_ownership",
            "归属边界", "backend_stack", "frontend_stack", "page_mode",
            "entrypoints", "database_mode", "技术栈", "页面模式", "数据库模式",
        )):
            return FailureClassification(
                "architecture_contract", ("architecture",), "architecture_contract",
                "repair_architecture_before_freeze", True, True, "high", "contract",
            )
        if step == "tester" and (
            error_type in {"AttributeError", "TypeError", "KeyError", "AssertionError"}
            or any(marker in lowered for marker in ("validator internal", "验证器内部错误"))
        ):
            return FailureClassification(
                "validator_defect", ("platform",), "validation",
                "repair_platform_validator", False, False, "critical", "platform",
            )
        if any(marker in lowered for marker in self.PLATFORM_MARKERS):
            return FailureClassification(
                "provider_configuration", ("platform",), "provider",
                "pause_for_configuration", False, False, "critical",
            )
        if any(marker in lowered for marker in self.TIMEOUT_MARKERS):
            return FailureClassification(
                "provider_transport", (step,), "provider",
                "retry_same_node", True, True,
            )
        if any(marker in lowered for marker in self.STRUCTURED_MARKERS):
            return FailureClassification(
                "structured_output", (step,), "contract",
                "retry_same_node", True, True,
            )
        return FailureClassification(
            "node_execution", (step,), "execution",
            "retry_same_node", True, True,
        )

    def classify_check(self, check: ValidationCheck) -> FailureClassification:
        identity = f"{check.id} {check.label} {check.message}".lower()
        if check.id in {"validator-error", "validator-exception"}:
            return FailureClassification(
                "validator_defect", ("platform",), "validation",
                "repair_platform_validator", False, False, "critical", "platform",
            )
        if any(marker in identity for marker in ("blueprint-contract-hash", "delivery-api-contract", "冻结合同缺少", "contract schema")):
            return FailureClassification(
                "contract_defect", ("architecture",), "contract",
                "request_blueprint_change", False, False, "critical", "contract",
            )
        frontend_diagnostic = f"{check.message}\n{check.output}"
        if check.target == "frontend" and re.search(
            r"引用了不存在的模块\s+\.{1,2}/|Cannot find module\s+['\"]\.{1,2}/",
            frontend_diagnostic,
            re.IGNORECASE,
        ):
            return FailureClassification(
                "missing_declaration", ("frontend",), "build",
                "create_missing_source_or_repair_reference", True, True, "high",
            )
        if check.target == "backend" and re.search(
            r"(?im)^\[ERROR\]\s*(?:symbol\s*:\s*class|符号\s*:\s*类)\s+[A-Za-z_][A-Za-z0-9_]*",
            str(check.output or ""),
        ):
            return FailureClassification(
                "missing_declaration", ("backend",), "build",
                "create_missing_source_or_repair_reference", True, True, "high",
            )
        owners = tuple(self.owners_for_check(check))
        category = self.category_for_check(check)
        stage = self.stage_for_check(check)
        repairable = bool(owners)
        return FailureClassification(
            category,
            owners,
            stage,
            "dispatch_owner_repair" if repairable else "escalate_unowned_failure",
            repairable,
            repairable,
            "high" if check.status == "failed" else "medium",
        )

    def owners_for_check(self, check: ValidationCheck) -> list[str]:
        evidence = check.evidence if isinstance(check.evidence, dict) else {}
        explicit = str(evidence.get("owner") or "").strip().lower()
        if explicit in self.AGENT_OWNERS:
            return [explicit]
        explicit_many = [
            str(owner).strip().lower()
            for owner in (evidence.get("owners") or [])
            if str(owner).strip().lower() in self.AGENT_OWNERS
        ]
        if explicit_many:
            return list(dict.fromkeys(explicit_many))
        diagnostic = " ".join(
            (str(check.id), str(check.label), str(check.message), str(check.output))
        ).lower()
        check_id = str(check.id or "").lower()
        related_values = evidence.get("relatedFiles") or evidence.get("files") or []
        related = " ".join(str(item) for item in related_values)

        # Cross-layer Gates are deliberately resolved before the legacy target
        # field.  The target says where the validator observed the symptom; it
        # does not always identify every source owner needed for a coherent fix.
        if check_id in {
            "frontend-backend-route-contract", "integration-api-contract",
            "frontend-api-contract", "backend-api-contract",
        }:
            return ["backend", "frontend"]
        if (
            check_id in {"backend-database-contract", "database-probe-contract", "h2-crud", "backend-h2-crud"}
            or any(marker in diagnostic for marker in (
                "schema-validation: missing table", "table contract", "database contract",
                "h2 crud", "真实数据库合同", "实体表名合同",
            ))
        ):
            return ["database", "backend"]
        # A browser timeout is only the visible symptom when the page called a
        # missing REST endpoint. Keep Frontend involved for path/proxy checks,
        # but also route the failure to Backend instead of repeatedly rewriting
        # an otherwise working form.
        if check.id in {"browser-render", "browser-crud"} and re.search(
            r"/api/[a-z0-9_/{}/.-]+\s*=\s*http\s+(?:404|405|5\d\d)",
            diagnostic,
            re.IGNORECASE,
        ):
            return ["backend", "frontend"]
        if check_id in {"browser-render", "browser-crud", "browser-local-crud"}:
            return ["frontend"]

        inferred_from_files: list[str] = []
        for raw_path in related_values if isinstance(related_values, (list, tuple, set)) else [related_values]:
            path = str(raw_path or "").replace("\\", "/").lower()
            if not path:
                continue
            if path.endswith(".sql") or "/db/migration/" in path:
                inferred_from_files.append("database")
            elif path.endswith((".java", ".kt")) or path.endswith(("pom.xml", "build.gradle", "build.gradle.kts")):
                inferred_from_files.append("backend")
            elif path.endswith((".vue", ".tsx", ".jsx", ".css", ".html")) or path.endswith(("package.json", "vite.config.js", "vite.config.ts")):
                inferred_from_files.append("frontend")
        if len(set(inferred_from_files)) > 1:
            return list(dict.fromkeys(inferred_from_files))
        target = str(check.target or "").strip().lower()
        if target in self.AGENT_OWNERS:
            return [target]

        diagnostic = f"{diagnostic} {related.lower()}"
        if any(marker in diagnostic for marker in ("api contract", "integration", "接口", "契约", "crud")):
            return ["backend", "frontend"]
        if any(marker in diagnostic for marker in ("pom.xml", "maven", "gradle", "java", "spring", "controller", "service")):
            return ["backend"]
        if any(marker in diagnostic for marker in ("package.json", "npm", "vite", ".vue", ".tsx", ".jsx", "css", "frontend")):
            return ["frontend"]
        if any(marker in diagnostic for marker in ("schema.sql", "data.sql", ".sql", "migration", "database", "数据库", "h2")):
            return ["database"]
        if any(marker in diagnostic for marker in ("index.html", "html", "browser", "page", "页面")):
            return ["frontend"]
        return []

    @staticmethod
    def category_for_check(check: ValidationCheck) -> str:
        identity = f"{check.id} {check.label} {check.message}".lower()
        if any(marker in identity for marker in ("duplicate", "重复", "artifact-files", "structure", "package")):
            return "artifact_structure"
        if any(marker in identity for marker in ("dependency", "maven", "npm", "install", "build", "compile", "test")):
            return "build"
        if any(marker in identity for marker in ("startup", "health", "启动")):
            return "runtime"
        if any(marker in identity for marker in ("crud", "api", "contract", "integration", "browser", "database")):
            return "integration"
        return "validation"

    @staticmethod
    def stage_for_check(check: ValidationCheck) -> str:
        identity = f"{check.id} {check.label}".lower()
        if any(marker in identity for marker in ("crud", "browser", "integration", "api-contract")):
            return "integration"
        if any(marker in identity for marker in ("startup", "health", "runtime", "page-assets")):
            return "runtime"
        if "test" in identity:
            return "test"
        if any(marker in identity for marker in ("build", "compile", "install", "dependency")):
            return "build"
        return "preflight"
