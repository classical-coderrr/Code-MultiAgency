"""Deterministic Integration Gate facade.

The existing ArtifactValidator and Delivery Gate remain the source of checks.
This adapter gives them one stable evidence/failure interface for the UI,
repair routing and future non-code company templates.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

from ..code_company.contract_conformance import check_contract_conformance
from .platform_contracts import evidence_from_check, failure_fact_from_check


GATE_CATEGORIES = {
    "process", "build", "runtime", "health", "api", "crud", "browser",
    "database", "integration", "security", "artifact",
}


def _path_allowed(owner: str, path: str, ownership: dict[str, Any]) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("/").lower()
    patterns = ownership.get(owner) or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return bool(patterns) and any(fnmatch.fnmatch(normalized, str(pattern).replace("\\", "/").lower()) for pattern in patterns)


class IntegrationGate:
    """Normalize deterministic check output; it never asks an LLM to test code."""

    @staticmethod
    def _failure_owner(check: dict[str, Any], gate: str) -> str:
        """Resolve the Agent that owns a failed deterministic check.

        Validation checks already carry a concrete target (backend/frontend/
        database). Falling back to ``tester`` for those checks makes the
        failure visible but prevents the Repair Engine from dispatching the
        fix to the Agent that owns the source files.
        """
        explicit = str(check.get("owner") or "").strip()
        if explicit:
            return explicit
        target = str(check.get("target") or "").strip().lower()
        if target in {"backend", "frontend", "database"}:
            return target
        if target in {"tester", "reviewer", "integration_gate"}:
            return target
        return "tester" if gate in {"artifact", "integration"} else gate

    def normalize(self, checks: list[dict[str, Any]], *, gate: str = "integration") -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                continue
            item = dict(check)
            category = str(item.get("category") or gate).lower()
            item["category"] = category if category in GATE_CATEGORIES else gate
            evidence.append(evidence_from_check(item, gate=gate, index=index))
            if str(item.get("status") or "blocked") != "passed":
                failures.append(
                    failure_fact_from_check(
                        item,
                        gate=gate,
                        owner=self._failure_owner(item, gate),
                        index=index,
                    )
                )
        status = "failed" if any(item["status"] == "failed" for item in evidence) else "blocked" if any(item["status"] == "blocked" for item in evidence) else "passed"
        return {"status": status, "checks": checks, "evidence": evidence, "failureFacts": failures}

    @staticmethod
    def targeted_gate_ids(failure_facts: list[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(str(item.get("gate")) for item in failure_facts if str(item.get("gate") or "").strip()))

    @staticmethod
    def contract_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate frozen contracts before expensive build/startup checks."""
        blueprint = context.get("project_blueprint") or {}
        compiled = context.get("compiled_contract") or {}
        raw_files = context.get("__artifact_files__") or []
        checks: list[dict[str, Any]] = []
        if not isinstance(blueprint, dict) or not blueprint or not isinstance(compiled, dict) or not compiled:
            return checks

        expected_hash = str(blueprint.get("blueprint_hash") or "")
        actual_hash = str(compiled.get("source_blueprint_hash") or "")
        checks.append({
            "id": "blueprint-contract-hash", "target": "architecture", "label": "Blueprint 版本绑定",
            "status": "passed" if expected_hash and expected_hash == actual_hash else "failed",
            "message": "Compiled Contract 与冻结 Blueprint Hash 一致。" if expected_hash and expected_hash == actual_hash else "Compiled Contract 不属于当前冻结 Blueprint，禁止继续交付。",
        })

        ownership = compiled.get("artifact_ownership") or {}
        violations: list[str] = []
        seen: dict[str, str] = {}
        for item in raw_files if isinstance(raw_files, list) else []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("name") or "").replace("\\", "/")
            owner = str(item.get("step_id") or item.get("owner") or "")
            if owner in ownership and not _path_allowed(owner, path, ownership):
                violations.append(f"{owner} 无权生成 {path}")
            key = path.lower()
            if key in seen and seen[key] != owner:
                violations.append(f"{path} 被 {seen[key]} 与 {owner} 重复登记")
            seen[key] = owner
        checks.append({
            "id": "artifact-ownership-contract", "target": "artifact", "label": "Artifact Ownership",
            "status": "failed" if violations else "passed",
            "message": "；".join(violations[:12]) if violations else "所有成果物均由唯一且获授权的 Agent 生成。",
        })

        file_map = {str(item.get("name") or ""): item for item in raw_files if isinstance(item, dict)} if isinstance(raw_files, list) else {}
        providers = {"ApiContract", "DatabaseSchema", "BackendDependencies", "FrontendDependencies"}
        for item in file_map.values():
            providers.update(str(value) for value in item.get("provides") or [])
        dependency_errors: list[str] = []
        for path, item in file_map.items():
            for required in item.get("requires") or []:
                if str(required) not in providers:
                    dependency_errors.append(f"{path} 缺少 provider：{required}")
            for dependency in item.get("depends_on_files") or []:
                if str(dependency) not in file_map:
                    dependency_errors.append(f"{path} 缺少依赖文件：{dependency}")
        checks.append({
            "id": "file-dependency-contract", "target": "artifact", "label": "文件依赖 DAG",
            "status": "failed" if dependency_errors else "passed",
            "message": "；".join(dependency_errors[:12]) if dependency_errors else "所有 requires 均存在 provides，文件依赖闭合。",
        })

        manifest = compiled.get("dependency_manifest") or {}
        manifest_errors: list[str] = []
        pom = str((file_map.get("pom.xml") or {}).get("content") or "")
        expected_backend: set[str] = set()
        for dep in (manifest.get("backend") or {}).get("dependencies") or []:
            name = str(dep.get("name") or "") if isinstance(dep, dict) else str(dep)
            if name:
                expected_backend.add(name)
            if name and pom and f"<artifactId>{name}</artifactId>" not in pom:
                manifest_errors.append(f"pom.xml 缺少 {name}")
        if pom and expected_backend:
            dependency_blocks = re.findall(r"<dependency>.*?</dependency>", pom, flags=re.DOTALL | re.IGNORECASE)
            actual_backend = [match.group(1).strip() for block in dependency_blocks if (match := re.search(r"<artifactId>\s*([^<]+)\s*</artifactId>", block, flags=re.IGNORECASE))]
            extras = sorted(set(actual_backend) - expected_backend)
            duplicates = sorted({name for name in actual_backend if actual_backend.count(name) > 1})
            if extras:
                manifest_errors.append("pom.xml 存在未审批依赖：" + ", ".join(extras))
            if duplicates:
                manifest_errors.append("pom.xml 重复声明依赖：" + ", ".join(duplicates))
        package_text = str((file_map.get("package.json") or {}).get("content") or "")
        if package_text:
            try:
                package = json.loads(package_text)
            except (TypeError, ValueError):
                manifest_errors.append("package.json 不是有效 JSON")
                package = {}
            for manifest_section, package_section in (("dependencies", "dependencies"), ("dev_dependencies", "devDependencies")):
                expected = set(((manifest.get("frontend") or {}).get(manifest_section) or {}).keys())
                actual = set((package.get(package_section) or {}).keys()) if isinstance(package, dict) else set()
                missing = sorted(expected - actual)
                extras = sorted(actual - expected)
                if missing:
                    manifest_errors.append(f"package.json {package_section} 缺少：" + ", ".join(missing))
                if extras:
                    manifest_errors.append(f"package.json {package_section} 存在未审批依赖：" + ", ".join(extras))
        checks.append({
            "id": "dependency-manifest-contract", "target": "artifact", "label": "Dependency Manifest",
            "status": "failed" if manifest_errors else "passed",
            "message": "；".join(manifest_errors[:12]) if manifest_errors else "依赖文件与冻结 Dependency Manifest 一致。",
        })
        if isinstance(raw_files, list):
            checks.extend(check_contract_conformance(compiled, raw_files))
        return checks
