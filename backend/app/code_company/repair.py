"""Failure classification, ownership routing and bounded repair decisions.

The workflow scheduler should not guess that the node immediately before a
failed gate owns the defect. This module turns provider/node failures and
deterministic validation checks into an explicit repair plan. It contains no
LLM calls, so routing remains stable, cheap and auditable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..workflow.artifact_validator import ArtifactValidationResult, ValidationCheck
from ..workflow.platform_contracts import failure_fact_from_check
from .failure_classifier import FailureClassifier


class RepairEngine:
    """Classify failures and route repairs to the source-owning Agent."""

    _AGENT_OWNERS = FailureClassifier.AGENT_OWNERS

    def __init__(self, classifier: FailureClassifier | None = None) -> None:
        self.classifier = classifier or FailureClassifier()

    def route_node_failure(self, step_id: str, error: str, *, response_present: bool = False, error_type: str = "") -> dict[str, Any]:
        """Classify a node failure before the scheduler chooses recovery."""
        step = str(step_id or "unknown")
        normalized = str(error or "").strip()
        classification = self.classifier.classify_node(step, normalized, error_type=error_type)
        category = classification.category
        owner = classification.owners[0] if classification.owners else "platform"
        return {
            "owner_step": owner,
            "category": category,
            "stage": classification.stage,
            "action": classification.action,
            "repairable": classification.repairable,
            "retryable": classification.retryable,
            "severity": classification.severity,
            "structured_fallback_allowed": category == "structured_output",
            "response_present": bool(response_present),
            "error_type": error_type,
            "message": normalized[:1000],
        }

    def failure_facts(self, validation: ArtifactValidationResult, *, owner: str = "tester") -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        fingerprint = self.fingerprint(validation)
        for index, check in enumerate(validation.checks):
            if check.status == "passed":
                continue
            classification = self.classifier.classify_check(check)
            owners = list(classification.owners)
            item = check.as_dict()
            item["owner"] = owners[0] if owners else "platform"
            summary = str(check.message)[:240]
            if classification.category == "missing_declaration":
                symbols = re.findall(
                    r"(?im)^\[ERROR\]\s*(?:symbol\s*:\s*class|符号\s*:\s*类)\s+([A-Za-z_][A-Za-z0-9_]*)",
                    str(check.output or ""),
                )
                if symbols:
                    summary = f"Java 编译缺少声明/文件：{symbols[0]}"
                elif check.target == "frontend":
                    module = re.search(r"引用了不存在的模块\s+(\.{1,2}/[^\s；]+)", str(check.message or ""))
                    if module:
                        summary = f"前端编译缺少模块/文件：{module.group(1)}"
            item.update({
                "code": str(check.id),
                "summary": summary,
                "repair_scope": [str(check.target)] if classification.repairable else [],
                "category": classification.category,
                "stage": classification.stage,
                "repairable": classification.repairable,
                "retryable": classification.retryable,
                "repair_action": classification.action,
                "fingerprint": fingerprint,
            })
            facts.append(failure_fact_from_check(item, gate="artifact", owner=owner, index=index))
        return facts

    def targets(self, validation: ArtifactValidationResult) -> list[str]:
        """Return repair owners, not merely validator target labels."""
        owners: list[str] = []
        statuses = {check.id: check.status for check in validation.checks}
        has_contract_or_platform_defect = any(
            check.status == "failed"
            and self.classifier.classify_check(check).defect_scope in {"contract", "platform"}
            for check in validation.checks
        )
        for check in validation.checks:
            if check.status != "failed":
                continue
            classification = self.classifier.classify_check(check)
            if classification.defect_scope in {"contract", "platform"}:
                continue
            # Once a frozen contract itself is invalid, cross-layer owner
            # inference is unsafe. Keep the observed downstream target only
            # for diagnostics; the repair plan remains blocked until the
            # Architecture contract is reopened.
            if has_contract_or_platform_defect:
                target = str(check.target or "").strip().lower()
                if target in self._AGENT_OWNERS:
                    owners.append(target)
                continue
            # The cross-agent route check is attributed to backend for legacy
            # reporting, but a passing backend contract plus a failing frontend
            # contract proves that only the frontend must change.
            if (
                check.id in {"frontend-backend-route-contract", "frontend-api-contract", "frontend-proxy-contract"}
                and statuses.get("backend-api-contract") == "passed"
                and statuses.get("frontend-api-contract") == "failed"
            ):
                owners.append("frontend")
                continue
            owners.extend(self.owners_for_check(check))
        return list(dict.fromkeys(owner for owner in owners if owner in self._AGENT_OWNERS))

    def owners_for_check(self, check: ValidationCheck) -> list[str]:
        return self.classifier.owners_for_check(check)

    @staticmethod
    def category_for_check(check: ValidationCheck) -> str:
        return FailureClassifier.category_for_check(check)

    def plan_validation_repair(
        self,
        validation: ArtifactValidationResult,
        *,
        attempt: int,
        previous_fingerprint: str = "",
        no_progress_count: int = 0,
    ) -> dict[str, Any]:
        """Create a bounded, auditable plan for a failed verification round."""
        fingerprint = self.fingerprint(validation)
        failed = [check for check in validation.checks if check.status == "failed"]
        non_source = [check for check in failed if self.classifier.classify_check(check).defect_scope in {"contract", "platform"}]
        owners = self.targets(validation)
        if non_source:
            owners = []
        repeated = bool(previous_fingerprint and previous_fingerprint == fingerprint)
        # Allow one broader owner-level patch after a precise file patch made
        # no progress. A second unchanged round opens the circuit.
        circuit_open = no_progress_count >= 2
        repairable = bool(failed and owners) and not circuit_open
        strategy = "targeted_file" if attempt <= 1 and no_progress_count == 0 else "grounded_multi_file_patch"
        if circuit_open:
            action = "stop_no_progress"
            reason = "相同失败在候选修复后仍未推进验证阶段，已触发熔断。"
        elif not failed:
            action = "stop_no_failure"
            reason = "没有需要源码整改的失败检查。"
        elif non_source:
            action = "escalate_contract_or_platform"
            reason = "失败归属架构合同或平台验证器；禁止派给源码 Agent 反复重写。"
        elif not owners:
            action = "escalate_unowned_failure"
            reason = "失败无法映射到数据库、后端或前端源码责任域。"
        else:
            action = "dispatch_owner_repair"
            reason = "根据确定性检查、文件路径和错误类型流转给源码责任 Agent。"
        return {
            "repairable": repairable,
            "owners": owners,
            "check_ids": [str(check.id) for check in failed],
            "categories": list(dict.fromkeys(self.category_for_check(check) for check in failed)),
            "strategy": strategy,
            "action": action,
            "reason": reason,
            "fingerprint": fingerprint,
            "repeated": repeated,
            "circuit_open": circuit_open,
        }

    @staticmethod
    def fingerprint(validation: ArtifactValidationResult) -> str:
        payload = []
        for check in validation.checks:
            if check.status != "failed":
                continue
            diagnostic = str(check.output or "")[-3000:]
            diagnostic = re.sub(r"agent-team-validation-[A-Za-z0-9_-]+", "agent-team-validation", diagnostic)
            diagnostic = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|seconds?)\b", "<duration>", diagnostic, flags=re.I)
            payload.append({
                "id": check.id,
                "target": check.target,
                "message": re.sub(r"\s+", " ", str(check.message or "")).strip(),
                "output": re.sub(r"\s+", " ", diagnostic).strip(),
            })
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def quality(cls, validation: ArtifactValidationResult) -> tuple[int, int, int]:
        """Rank actual validation progress instead of accepting changed prose."""
        if validation.passed:
            return (6, 0, 0)
        failures = [check for check in validation.checks if check.status == "failed"]
        if not failures:
            return (0, 0, 0)

        def stage(check: ValidationCheck) -> int:
            identity = f"{check.id} {check.label}".lower()
            if any(word in identity for word in ("crud", "browser", "page-entry", "page-assets", "integration")):
                return 5
            if any(word in identity for word in ("startup", "health", "runtime")):
                return 4
            if "test" in identity:
                return 3
            if any(word in identity for word in ("build", "compile", "install", "dependency")):
                return 2
            return 1

        errors = sum(
            int(count)
            for check in failures
            for count in re.findall(r"(\d+) errors?\b", str(check.output or ""))
        )
        return (min(stage(check) for check in failures), -len(failures), -errors)

    @classmethod
    def made_progress(cls, before: ArtifactValidationResult, after: ArtifactValidationResult) -> bool:
        previous = {check.id: check.status for check in before.checks}
        if any(
            previous.get(check.id) == "passed" and check.status == "failed"
            for check in after.checks
        ):
            return False
        old_compiler = cls.compiler_failure_keys(before)
        new_compiler = cls.compiler_failure_keys(after)
        # Compilers expose errors progressively.  A newly visible missing
        # method/type error after the previous concrete defect disappeared is
        # progress at the same build Gate, so keep the current candidate.
        if (old_compiler and not (new_compiler & old_compiler)
                and cls.quality(after)[0] >= cls.quality(before)[0]):
            return True
        return after.passed or cls.quality(after) > cls.quality(before)

    @staticmethod
    def compiler_failure_keys(validation: ArtifactValidationResult) -> set[str]:
        """Extract stable compiler defect identities for progressive repair."""
        keys: set[str] = set()
        for check in validation.checks:
            if check.status != "failed":
                continue
            output = str(check.output or "").replace("\\", "/")
            for symbol in re.findall(
                r"(?im)^\[ERROR\]\s*(?:symbol\s*:\s*class|符号\s*:\s*类)\s+([A-Za-z_][A-Za-z0-9_]*)",
                output,
            ):
                keys.add(f"missing-class:{symbol}")
            for symbol in re.findall(
                r"(?im)^\[ERROR\]\s*(?:symbol\s*:\s*method|符号\s*:\s*方法)\s+([A-Za-z_][A-Za-z0-9_]*)",
                output,
            ):
                keys.add(f"missing-method:{symbol}")
            for constructor in re.findall(
                r"(?im)(?:constructor|构造函数)\s+([A-Za-z_][A-Za-z0-9_]*)\s+.*?(?:cannot be applied|无法应用)",
                output,
            ):
                keys.add(f"constructor:{constructor}")
            for source, message in re.findall(
                r"(?im)^\[ERROR\]\s+([^\r\n]+?\.(?:java|kt)):\[\d+(?:,\d+)?\]\s+([^\r\n]+)",
                output,
            ):
                normalized_message = re.sub(r"\s+", " ", message).strip().lower()
                if any(marker in normalized_message for marker in (
                    "incompatible types", "不兼容的类型", "cannot be converted", "无法转换",
                    "does not override", "未覆盖", "cannot be applied", "无法应用",
                )):
                    relative_source = re.sub(r"^.*?/(src/(?:main|test)/)", r"\1", source)
                    keys.add(f"compiler:{relative_source}:{normalized_message}")
        return keys
