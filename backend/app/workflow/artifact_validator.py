"""Deterministic validation for generated source Artifacts.

The Tester Agent can explain failures, but it must not decide that a project
is runnable from prose alone.  This module executes a small, allow-listed set
of build and startup checks inside a temporary Run workspace and returns a
structured result that the executor can use as a delivery gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

import httpx
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from ..services.artifact_paths import normalize_artifact_path
from .integration_gate import IntegrationGate


EmitValidationEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


def validation_stage(check_id: str) -> str:
    identity = str(check_id or "").lower()
    if any(word in identity for word in ("crud", "browser", "integration", "api-contract")):
        return "integration"
    if any(word in identity for word in ("startup", "health", "runtime", "page-assets")):
        return "runtime"
    if "test" in identity:
        return "test"
    if any(word in identity for word in ("build", "compile", "install", "dependency")):
        return "build"
    return "preflight"


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    id: str
    target: str
    label: str
    status: str
    message: str
    command: str = ""
    duration_ms: int = 0
    output: str = ""
    evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "command": self.command,
            "durationMs": self.duration_ms,
            "output": self.output,
            "evidence": self.evidence or {},
            "stage": validation_stage(self.id),
        }


@dataclass(frozen=True, slots=True)
class ArtifactValidationResult:
    status: str
    summary: str
    targets: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "targets": list(self.targets),
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class _SpringCrudSpec:
    collection_path: str
    payload: dict[str, Any]
    identity_field: str
    expected_methods: frozenset[str]
    record_id_field: str = "id"
    detail_required: bool = False
    database_probe_path: str = ""
    entity_id: str = ""


class ArtifactValidator:
    """Run safe project checks without asking an LLM to fake a test result."""

    _package_import = re.compile(r"(?:from|import)\s*[\"']([^\"']+)[\"']")
    _java_package = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)

    def validate_owner_preflight(self, raw_files: list[dict[str, Any]], owner: str) -> ArtifactValidationResult:
        """Cheap owner-only checks before the independent integration Tester.

        Do not evaluate a cross-agent contract against incomplete parallel
        branches. This gate never claims build, browser or API success.
        """
        files, error = self._normalize_files(raw_files)
        target = str(owner or "").strip().lower()
        if error:
            return ArtifactValidationResult("failed", error, (target,), (
                ValidationCheck("artifact-files", target, "文件清单", "failed", error),
            ))
        checks: list[ValidationCheck] = []
        if target == "frontend":
            if "package.json" in files:
                checks.extend(self._frontend_structure(Path("."), files))
            else:
                html_name = next((name for name in ("index.html", "src/main/resources/static/index.html") if name in files), None)
                if html_name:
                    html_error = self._html_error(files[html_name])
                    checks.append(ValidationCheck("static-html", "frontend", "HTML 结构", "failed" if html_error else "passed", html_error or "HTML 结构完整。"))
                else:
                    checks.append(ValidationCheck("frontend-entry", "frontend", "前端入口", "failed", "缺少可验证的前端入口。"))
        elif target == "backend":
            checks.extend(self._backend_structure(Path("."), files))
        elif target == "database":
            has_schema = any(name.endswith(("schema.sql", ".sql")) for name in files)
            checks.append(ValidationCheck("database-schema-file", "database", "数据库 Schema", "passed" if has_schema else "failed", "已生成 SQL Schema。" if has_schema else "缺少 SQL Schema 文件。"))
        else:
            return ArtifactValidationResult("blocked", "没有该责任方的早期验证档位。", (target,), ())
        failed = [check for check in checks if check.status == "failed"]
        return ArtifactValidationResult(
            "failed" if failed else "passed",
            "责任方预检失败：" + "；".join(check.message for check in failed[:3]) if failed else "责任方结构预检通过；仍需最终构建、启动与集成验收。",
            (target,), tuple(checks),
        )

    async def validate(
        self,
        context: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        emit: EmitValidationEvent | None = None,
    ) -> ArtifactValidationResult:
        profile = dict(config or {})
        preflight_started = time.perf_counter()
        contract = dict(context.get("delivery_contract") or {})
        if contract.get("browser_required"):
            run_label = re.sub(r"[^A-Za-z0-9_-]", "-", str(profile.get("run_id") or "validation"))[:80]
            evidence_dir = Path(__file__).resolve().parents[2] / "data" / "validation-evidence" / run_label
            evidence_dir.mkdir(parents=True, exist_ok=True)
            contract["_validation_evidence_dir"] = str(evidence_dir)
        raw_files = context.get("__artifact_files__") if isinstance(context, dict) else None
        checks: list[ValidationCheck] = []

        async def record(check: ValidationCheck) -> None:
            checks.append(check)
            if emit:
                await emit("step.validation_check", {"check": check.as_dict()})

        if emit:
            await emit(
                "step.validation_started",
                {
                    "profile": self._public_profile(profile),
                    "fileCount": len(raw_files) if isinstance(raw_files, list) else 0,
                },
            )
            await emit("step.validation_stage_started", {"stage": "preflight", "scope": profile.get("validation_scope", "full")})

        files, file_error = self._normalize_files(raw_files)
        if file_error:
            await record(
                ValidationCheck(
                    "artifact-files",
                    "artifact",
                    "成果物文件清单",
                    "failed",
                    file_error,
                )
            )
            if emit:
                await emit("step.validation_stage_completed", {"stage": "preflight", "scope": profile.get("validation_scope", "full"), "durationMs": int((time.perf_counter() - preflight_started) * 1000), "passed": False})
            result = self._result("failed", ("artifact",), checks)
            return result

        for item in IntegrationGate.contract_checks(context):
            await record(ValidationCheck(
                str(item.get("id") or "contract-conformance"),
                str(item.get("target") or "artifact"),
                str(item.get("label") or "冻结合同一致性"),
                str(item.get("status") or "failed"),
                str(item.get("message") or ""),
                evidence=item.get("evidence") if isinstance(item.get("evidence"), dict) else None,
            ))

        targets = self._targets(files, profile)
        if not targets:
            if "index.html" in files:
                html_error = self._html_error(files["index.html"])
                await record(
                    ValidationCheck(
                        "static-html",
                        "frontend",
                        "静态 HTML 结构",
                        "failed" if html_error else "passed",
                        html_error or "检测到完整 HTML 文档；无需执行项目构建命令。",
                    )
                )
                if emit:
                    await emit("step.validation_stage_completed", {"stage": "preflight", "scope": profile.get("validation_scope", "full"), "durationMs": int((time.perf_counter() - preflight_started) * 1000), "passed": not bool(html_error)})
                if contract and not html_error:
                    execution_started = time.perf_counter()
                    if emit:
                        await emit("step.validation_stage_started", {"stage": "execution", "scope": profile.get("validation_scope", "full")})
                    with tempfile.TemporaryDirectory(prefix="agent-team-validation-") as workspace:
                        root = Path(workspace)
                        for name, content in files.items():
                            destination = root / name
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_text(content, encoding="utf-8", newline="")
                        await self._validate_startup(root, sys.executable, [str(Path(__file__).with_name("static_server.py")), "{port}"], "frontend", "静态页面 HTTP", ("/",), 10, record, page_paths=tuple(contract.get("entrypoints", ["/"])), browser_contract=contract)
                    if emit:
                        await emit("step.validation_stage_completed", {"stage": "execution", "scope": profile.get("validation_scope", "full"), "durationMs": int((time.perf_counter() - execution_started) * 1000), "passed": not any(check.status == "failed" for check in checks)})
                result = self._result("failed" if any(check.status == "failed" for check in checks) else "blocked" if any(check.status == "blocked" for check in checks) else "passed", ("frontend",), checks)
            else:
                await record(
                    ValidationCheck(
                        "target-detection",
                        "artifact",
                        "项目类型识别",
                        "failed",
                        "未识别到可验证的前端或后端项目入口。",
                    )
                )
                if emit:
                    await emit("step.validation_stage_completed", {"stage": "preflight", "scope": profile.get("validation_scope", "full"), "durationMs": int((time.perf_counter() - preflight_started) * 1000), "passed": False})
                result = self._result("failed", (), checks)
            return result

        with tempfile.TemporaryDirectory(prefix="agent-team-validation-") as temporary:
            root = Path(temporary)
            for name, content in files.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="")

            target_set = set(targets)
            structural_ok: dict[str, bool] = {}
            database_probe_path = ""
            if "frontend" in target_set:
                frontend_checks = self._frontend_structure(root, files)
                for check in frontend_checks:
                    await record(check)
                structural_ok["frontend"] = all(check.status == "passed" for check in frontend_checks)
            if "backend" in target_set:
                backend_checks = self._backend_structure(root, files)
                for check in backend_checks:
                    await record(check)
                structural_ok["backend"] = all(check.status == "passed" for check in backend_checks)
                for check in self._spring_template_contract(files):
                    await record(check)

            # Build success does not prove that independently generated
            # frontend and backend files agree on their HTTP contract.
            for check in self._integration_contract(files):
                await record(check)
            if contract:
                for check in self._delivery_contract_structure(files, contract):
                    await record(check)
                if contract.get("validation_database", contract.get("database_mode")) == "h2" and contract.get("backend_stack") == "springboot":
                    database_probe_path = self._install_h2_probe(root, files)
                    if contract.get("database_audit_required") and not database_probe_path:
                        await record(ValidationCheck("database-probe-contract", "database", "H2 存储核验协议", "blocked", "无法从标准启动入口与 SQL 表定义建立 H2 核验；不能把未核验存储标为通过。"))

            # Structure and contract checks are cheap and deterministic. Do
            # not spend minutes installing dependencies or starting services
            # when they already prove that the candidate cannot pass.
            preflight_failures = [check for check in checks if check.status == "failed"]
            if emit:
                await emit(
                    "step.validation_stage_completed",
                    {
                        "stage": "preflight",
                        "scope": profile.get("validation_scope", "full"),
                        "durationMs": int((time.perf_counter() - preflight_started) * 1000),
                        "passed": not preflight_failures,
                    },
                )
            if bool(profile.get("fail_fast", False)) and preflight_failures:
                if emit:
                    await emit(
                        "step.validation_short_circuited",
                        {
                            "stage": "preflight",
                            "failedCheckIds": [check.id for check in preflight_failures],
                            "targets": list(dict.fromkeys(check.target for check in preflight_failures)),
                            "reason": "预检已发现结构或契约错误，跳过依赖安装、构建与启动检查。",
                        },
                    )
                return self._result("failed", targets, checks)

            run_build = bool(profile.get("build", True))
            run_startup = bool(profile.get("startup", True))
            install_dependencies = bool(profile.get("install_dependencies", True))
            command_timeout = self._positive_number(profile.get("timeout_seconds"), 120.0)
            startup_timeout = self._positive_number(profile.get("startup_timeout_seconds"), 30.0)

            execution_started = time.perf_counter()
            if emit:
                await emit("step.validation_stage_started", {"stage": "execution", "scope": profile.get("validation_scope", "full")})
            if run_build and structural_ok.get("frontend"):
                await self._validate_frontend_build(
                    root,
                    files,
                    install_dependencies,
                    command_timeout,
                    run_startup,
                    startup_timeout,
                    record,
                    contract=contract,
                )
            if run_build and structural_ok.get("backend"):
                await self._validate_backend_build(
                    root,
                    files,
                    command_timeout,
                    run_startup,
                    startup_timeout,
                    record,
                    contract=contract,
                    database_probe_path=database_probe_path,
                )
            if emit:
                execution_failures = [check for check in checks if check.status == "failed" and validation_stage(check.id) != "preflight"]
                await emit(
                    "step.validation_stage_completed",
                    {
                        "stage": "execution",
                        "scope": profile.get("validation_scope", "full"),
                        "durationMs": int((time.perf_counter() - execution_started) * 1000),
                        "passed": not execution_failures,
                    },
                )

        failed = [check for check in checks if check.status == "failed"]
        blocked = [check for check in checks if check.status == "blocked"]
        if failed:
            result_status = "failed"
        elif blocked:
            result_status = "blocked"
        else:
            result_status = "passed"
        result = self._result(result_status, targets, checks)
        return result

    @staticmethod
    def _positive_number(value: Any, default: float) -> float:
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "build": bool(profile.get("build", True)),
            "startup": bool(profile.get("startup", True)),
            "installDependencies": bool(profile.get("install_dependencies", True)),
            "timeoutSeconds": ArtifactValidator._positive_number(profile.get("timeout_seconds"), 120.0),
            "startupTimeoutSeconds": ArtifactValidator._positive_number(profile.get("startup_timeout_seconds"), 30.0),
            "repairAttempts": max(0, min(4, int(profile.get("repair_attempts", 0) or 0))),
            "targets": profile.get("targets", "auto"),
            "testDatabase": str(profile.get("test_database") or "h2"),
            "integration": bool(profile.get("integration", True)),
            "failFast": bool(profile.get("fail_fast", False)),
            "validationScope": str(profile.get("validation_scope") or "full"),
        }

    @staticmethod
    def _normalize_files(raw_files: Any) -> tuple[dict[str, str], str | None]:
        if not isinstance(raw_files, list) or not raw_files:
            return {}, "Run 没有可验证的结构化成果物。"
        files: dict[str, str] = {}
        canonical_names: dict[str, str] = {}
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            name = normalize_artifact_path(item.get("name") or item.get("path"))
            content = item.get("content")
            if not name:
                return {}, "成果物包含不安全或无效的相对路径。"
            if not isinstance(content, str) or not content.strip():
                return {}, f"成果物 {name} 没有非空文件内容。"
            canonical_name = name.casefold()
            if canonical_name in canonical_names:
                return {}, f"成果物文件 {name} 被重复登记。"
            canonical_names[canonical_name] = name
            files[name] = content
        return files, None

    @staticmethod
    def _targets(files: dict[str, str], profile: dict[str, Any]) -> tuple[str, ...]:
        configured = profile.get("targets")
        if isinstance(configured, str):
            configured = [configured] if configured.strip().lower() != "auto" else []
        if isinstance(configured, list):
            requested = {
                str(item).strip().lower()
                for item in configured
                if str(item).strip() and str(item).strip().lower() != "auto"
            }
        else:
            requested = set()
        detected: list[str] = []
        if "package.json" in files:
            detected.append("frontend")
        if any(name in files for name in ("pom.xml", "build.gradle", "build.gradle.kts", "requirements.txt", "pyproject.toml")):
            detected.append("backend")
        if requested:
            # A plain HTML/CSS/JS page has no npm manifest. A repair scoped
            # to "frontend" must not suddenly require package.json. For a
            # Spring-served page, its backend is the only runnable target.
            if "frontend" in requested and "package.json" not in files:
                return ("backend",) if "backend" in detected else ()
            return tuple(target for target in ("frontend", "backend") if target in requested)
        return tuple(detected)

    def _frontend_structure(self, root: Path, files: dict[str, str]) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        package = files.get("package.json")
        if package is None:
            return [ValidationCheck("frontend-package", "frontend", "package.json", "failed", "缺少前端 package.json。")]
        try:
            document = json.loads(package)
        except json.JSONDecodeError as exc:
            return [ValidationCheck("frontend-package", "frontend", "package.json", "failed", f"package.json 不是合法 JSON：{exc.msg}。")]
        if not isinstance(document, dict):
            return [ValidationCheck("frontend-package", "frontend", "package.json", "failed", "package.json 必须是 JSON 对象。")]
        scripts = document.get("scripts") if isinstance(document.get("scripts"), dict) else {}
        if not str(scripts.get("build") or "").strip():
            checks.append(ValidationCheck("frontend-build-script", "frontend", "构建脚本", "failed", "package.json 缺少 scripts.build。"))
        else:
            checks.append(ValidationCheck("frontend-build-script", "frontend", "构建脚本", "passed", "已找到 scripts.build。"))
        entry = next(
            (
                name
                for name in (
                    "src/main.js",
                    "src/main.ts",
                    "src/main.jsx",
                    "src/main.tsx",
                    "src/main.mjs",
                )
                if name in files
            ),
            None,
        )
        if not entry:
            checks.append(ValidationCheck("frontend-entry", "frontend", "前端入口", "failed", "缺少 src/main.js、src/main.ts 或等效标准入口。"))
        else:
            checks.append(ValidationCheck("frontend-entry", "frontend", "前端入口", "passed", f"已找到 {entry}。"))
        if "index.html" not in files:
            checks.append(ValidationCheck("frontend-index", "frontend", "HTML 入口", "failed", "缺少根目录 index.html。"))
        else:
            checks.append(ValidationCheck("frontend-index", "frontend", "HTML 入口", "passed", "已找到根目录 index.html。"))

        dependencies = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(document.get(key), dict):
                dependencies.update({str(name): value for name, value in document[key].items()})
        imports: set[str] = set()
        for name, content in files.items():
            if Path(name).suffix.lower() not in {".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue"}:
                continue
            for imported in self._package_import.findall(content):
                if imported.startswith((".", "/", "@/", "#")) or imported.startswith(("node:", "vue")):
                    continue
                root_name = imported.split("/", 1)[0]
                if root_name.startswith("@") and "/" in imported:
                    root_name = "/".join(imported.split("/", 2)[:2])
                imports.add(root_name)
        missing = sorted(name for name in imports if name not in dependencies)
        if missing:
            checks.append(ValidationCheck("frontend-dependencies", "frontend", "前端依赖", "failed", f"源码引用了未声明依赖：{', '.join(missing)}。"))
        else:
            checks.append(ValidationCheck("frontend-dependencies", "frontend", "前端依赖", "passed", "源码引用的第三方依赖均已在 package.json 声明。"))
        return checks

    def _backend_structure(self, root: Path, files: dict[str, str]) -> list[ValidationCheck]:
        if "pom.xml" in files:
            checks: list[ValidationCheck] = []
            java_files = sorted(name for name in files if name.startswith("src/main/java/") and name.endswith(".java"))
            if not java_files:
                checks.append(ValidationCheck("backend-java-layout", "backend", "Java 源码目录", "failed", "缺少 src/main/java 下的 Java 源码；根目录散落的 Java 文件不会被 Maven 编译。"))
            else:
                checks.append(ValidationCheck("backend-java-layout", "backend", "Java 源码目录", "passed", f"已找到 {len(java_files)} 个 src/main/java Java 文件。"))
            test_java_files = sorted(name for name in files if name.startswith("src/test/java/") and name.endswith(".java"))
            root_java = sorted(
                name
                for name in files
                if name.endswith(".java")
                and not name.startswith("src/main/java/")
                and not name.startswith("src/test/java/")
            )
            if root_java:
                checks.append(ValidationCheck("backend-root-java", "backend", "Java 文件归位", "failed", f"Java 文件不在标准源码目录：{', '.join(root_java[:5])}。"))
            elif test_java_files:
                checks.append(ValidationCheck("backend-test-java-layout", "backend", "Java 测试源码目录", "passed", f"已找到 {len(test_java_files)} 个 src/test/java 测试文件。"))
            package_mismatches: list[str] = []
            for name in [*java_files, *test_java_files]:
                package_match = self._java_package.search(files[name])
                if not package_match:
                    package_mismatches.append(f"{name} 缺少 package 声明")
                    continue
                package_path = package_match.group(1).replace(".", "/")
                source_root = "src/test/java" if name.startswith("src/test/java/") else "src/main/java"
                expected_prefix = f"{source_root}/{package_path}/"
                if not name.startswith(expected_prefix):
                    package_mismatches.append(f"{name} 与 package {package_match.group(1)} 不匹配")
            if package_mismatches:
                checks.append(ValidationCheck("backend-package-path", "backend", "Java 包路径", "failed", "；".join(package_mismatches[:5]) + "。"))
            else:
                checks.append(ValidationCheck("backend-package-path", "backend", "Java 包路径", "passed", "Java 文件路径与 package 声明一致。"))
            resource_files = [name for name in files if Path(name).name in {"application.yml", "application.yaml", "application.properties"}]
            misplaced_resources = [name for name in resource_files if not name.startswith("src/main/resources/")]
            if misplaced_resources:
                checks.append(ValidationCheck("backend-resources", "backend", "后端配置目录", "failed", f"配置文件应放在 src/main/resources：{', '.join(misplaced_resources)}。"))
            else:
                checks.append(ValidationCheck("backend-resources", "backend", "后端配置目录", "passed", "后端配置文件目录正确，或当前项目未声明运行配置。"))
            checks.extend(self._spring_yaml_contract(files))
            h2_check = self._spring_h2_contract(files)
            if h2_check is not None:
                checks.append(h2_check)
            if re.search(r"<artifactId>\s*flyway-database-h2\s*</artifactId>", files["pom.xml"]):
                checks.append(ValidationCheck(
                    "backend-flyway-dependencies", "backend", "Flyway H2 依赖合同", "failed",
                    "pom.xml 声明了错误模块 flyway-database-h2；H2 支持属于 flyway-core，不能靠补随机版本修复。",
                ))
            migrations = [name for name in files if "/db/migration/" in f"/{name.lower()}" and name.endswith(".sql")]
            has_flyway = bool(re.search(r"<artifactId>\s*(?:flyway-core|spring-boot-starter-flyway)\s*</artifactId>", files["pom.xml"]))
            if migrations and not has_flyway and "src/main/resources/schema.sql" not in files:
                checks.append(ValidationCheck(
                    "backend-migration-engine", "backend", "数据库初始化执行器", "failed",
                    "项目仅提供 Flyway 迁移 SQL，但 pom.xml 没有 flyway-core；必须提供迁移执行依赖或可执行的 schema.sql。",
                ))
            java_source = "\n".join(content for name, content in files.items() if name.endswith(".java"))
            if ("jakarta.persistence" in java_source or "JpaRepository" in java_source) and not re.search(
                r"<artifactId>\s*spring-boot-starter-data-jpa\s*</artifactId>", files["pom.xml"]
            ):
                checks.append(ValidationCheck(
                    "backend-jpa-dependencies", "backend", "JPA 源码依赖一致性", "failed",
                    "源码使用 jakarta.persistence/JpaRepository，但 pom.xml 缺少 spring-boot-starter-data-jpa；JDBC 和 H2 不能替代 JPA 依赖。",
                ))
            return checks

        if any(name in files for name in ("build.gradle", "build.gradle.kts")):
            has_source = any(name.startswith("src/main/") for name in files)
            return [ValidationCheck("backend-gradle-layout", "backend", "Gradle 源码目录", "passed" if has_source else "failed", "已找到标准 Gradle 源码目录。" if has_source else "缺少 src/main 下的 Gradle 源码。")]
        if "pyproject.toml" in files or "requirements.txt" in files:
            has_python = any(Path(name).suffix == ".py" for name in files)
            return [ValidationCheck("backend-python-layout", "backend", "Python 后端源码", "passed" if has_python else "failed", "已找到 Python 源码。" if has_python else "未找到 Python 源码文件。")]
        return [ValidationCheck("backend-entry", "backend", "后端入口", "failed", "未找到可识别的后端构建入口。")]

    @staticmethod
    def _spring_yaml_contract(files: dict[str, str]) -> list[ValidationCheck]:
        """Catch invalid Spring YAML before Maven or the startup repair loop.

        PyYAML normally accepts duplicate mapping keys by keeping the last
        value, whereas Spring's SnakeYAML rejects them at startup.
        """
        checks: list[ValidationCheck] = []
        for name, content in files.items():
            if Path(name).name.lower() not in {"application.yml", "application.yaml"}:
                continue
            try:
                documents = list(yaml.compose_all(content))
                visited: set[int] = set()

                def inspect(node: Any) -> None:
                    if id(node) in visited:
                        return
                    visited.add(id(node))
                    if isinstance(node, MappingNode):
                        keys: set[str] = set()
                        for key, value in node.value:
                            if not isinstance(key, ScalarNode):
                                raise ValueError(f"第 {key.start_mark.line + 1} 行使用了非标量配置键")
                            if key.value in keys:
                                raise ValueError(f"第 {key.start_mark.line + 1} 行重复定义配置键 {key.value}")
                            keys.add(key.value)
                            inspect(value)
                    elif isinstance(node, SequenceNode):
                        for child in node.value:
                            inspect(child)

                for document in documents:
                    if document is not None:
                        if not isinstance(document, MappingNode):
                            raise ValueError("配置文档根节点必须是键值映射")
                        inspect(document)
            except (yaml.YAMLError, ValueError) as exc:
                checks.append(ValidationCheck(
                    "backend-config-yaml", "backend", "Spring 配置 YAML", "failed",
                    f"{name} 无法被 Spring 正常加载：{exc}",
                    evidence={"file": name, "reason": str(exc)},
                ))
            else:
                checks.append(ValidationCheck(
                    "backend-config-yaml", "backend", "Spring 配置 YAML", "passed",
                    f"{name} 语法与配置键检查通过。", evidence={"file": name},
                ))
        return checks

    @staticmethod
    def _spring_h2_contract(files: dict[str, str]) -> ValidationCheck | None:
        """Require an in-process H2 database for deterministic Spring tests.

        Generated projects may still document or configure a production
        database, but the delivery gate must never depend on an external
        MySQL/PostgreSQL service.  Persistence projects therefore need the H2
        runtime driver; startup validation supplies an isolated in-memory URL.
        """
        pom = files.get("pom.xml", "")
        if not pom:
            return None
        persistence_markers = (
            "spring-boot-starter-data-jpa",
            "spring-boot-starter-jdbc",
            "spring-jdbc",
            "flyway-core",
            "liquibase-core",
        )
        config = "\n".join(
            content
            for name, content in files.items()
            if Path(name).name.lower() in {"application.yml", "application.yaml", "application.properties"}
        )
        has_database_artifact = any(
            name.endswith(".sql") or "/db/migration/" in f"/{name.lower()}"
            for name in files
        )
        persistence_required = (
            any(marker in pom for marker in persistence_markers)
            or "spring.datasource" in config
            or "datasource:" in config
            or has_database_artifact
        )
        if not persistence_required:
            return None
        has_h2 = bool(
            re.search(
                r"<groupId>\s*com\.h2database\s*</groupId>\s*<artifactId>\s*h2\s*</artifactId>",
                pom,
                re.IGNORECASE | re.DOTALL,
            )
            or re.search(r"<artifactId>\s*h2\s*</artifactId>", pom, re.IGNORECASE)
        )
        if not has_h2:
            return ValidationCheck(
                "backend-h2-test-database",
                "backend",
                "H2 测试数据库依赖",
                "failed",
                "项目包含持久化能力，但 pom.xml 缺少 com.h2database:h2 测试运行依赖；前后端联调默认必须使用隔离的 H2 数据库。",
            )
        return ValidationCheck(
            "backend-h2-test-database",
            "backend",
            "H2 测试数据库依赖",
            "passed",
            "已声明 H2 运行依赖；联调启动时将使用隔离的内存数据库，不依赖外部数据库服务。",
        )

    @staticmethod
    def _spring_template_contract(files: dict[str, str]) -> list[ValidationCheck]:
        """Template generation requires a renderer and matching MVC handlers."""
        templates = {
            name: content for name, content in files.items()
            if name.startswith("src/main/resources/templates/") and name.endswith(".html")
            and re.search(r"\bth:(?:text|each|field|object|action|href|if)\s*=", content)
        }
        if not templates or "pom.xml" not in files:
            return []
        checks: list[ValidationCheck] = []
        if not re.search(r"<artifactId>\s*spring-boot-starter-thymeleaf\s*</artifactId>", files["pom.xml"]):
            checks.append(ValidationCheck(
                "backend-template-dependencies", "backend", "模板渲染依赖", "failed",
                "前端交付 Thymeleaf 模板，但 pom.xml 缺少 spring-boot-starter-thymeleaf。不能仅靠 REST API 返回页面。",
            ))
        mvc_sources = "\n".join(content for name, content in files.items()
                                if name.endswith(".java") and re.search(r"@Controller\b", content))
        missing = [name for name in templates
                   if not re.search(r"[\"']" + re.escape(name.removeprefix("src/main/resources/templates/").removesuffix(".html")) + r"[\"']", mvc_sources)]
        if missing:
            checks.append(ValidationCheck(
                "backend-template-views", "backend", "前后端页面渲染合同", "failed",
                "模板缺少配套 MVC Controller 视图处理：" + "、".join(missing)
                + "。需要保留 REST API 并补齐 @Controller 的页面路由、模型和表单处理；禁止只改 Maven 版本。",
            ))
        return checks

    @staticmethod
    def _integration_contract(files: dict[str, str]) -> list[ValidationCheck]:
        """Compare the browser HTTP contract with Spring controllers."""
        browser_sources = {
            name: content
            for name, content in files.items()
            if Path(name).suffix.lower() in {".html", ".js", ".mjs", ".ts", ".tsx", ".vue"}
        }
        controller_sources = {
            name: content
            for name, content in files.items()
            if name.endswith(".java")
            and any(marker in content for marker in ("@RequestMapping", "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping"))
        }
        if not browser_sources or not controller_sources:
            return []

        checks = ArtifactValidator._spring_route_contract(browser_sources, controller_sources)

        frontend_params: set[str] = set()
        frontend_owners: dict[str, str] = {}
        append_pattern = re.compile(
            r"\.append\(\s*['\"](?P<name>[A-Za-z_][\w.-]*)['\"]\s*,"
        )
        for name, content in browser_sources.items():
            for match in append_pattern.finditer(content):
                parameter = match.group("name")
                frontend_params.add(parameter)
                frontend_owners.setdefault(parameter, name)

        backend_params: set[str] = set()
        annotation_pattern = re.compile(r"@RequestParam\s*\((?P<body>[^)]*)\)")
        named_value_pattern = re.compile(
            r"(?:value|name)\s*=\s*['\"](?P<name>[A-Za-z_][\w.-]*)['\"]"
        )
        direct_value_pattern = re.compile(r"^\s*['\"](?P<name>[A-Za-z_][\w.-]*)['\"]")
        for content in controller_sources.values():
            for annotation in annotation_pattern.finditer(content):
                body = annotation.group("body")
                match = named_value_pattern.search(body) or direct_value_pattern.search(body)
                if match:
                    backend_params.add(match.group("name"))

        if not frontend_params or not backend_params:
            return checks
        unsupported = sorted(frontend_params - backend_params)
        if not unsupported:
            checks.append(
                ValidationCheck(
                    "frontend-backend-query-contract",
                    "frontend",
                    "前后端查询参数契约",
                    "passed",
                    f"前端查询参数与 Controller 一致：{', '.join(sorted(frontend_params))}。",
                )
            )
            return checks

        source_names = sorted({frontend_owners[item] for item in unsupported})
        checks.append(
            ValidationCheck(
                "frontend-backend-query-contract",
                "frontend",
                "前后端查询参数契约",
                "failed",
                (
                    f"{', '.join(source_names)} 发送了 Controller 未声明的查询参数："
                    f"{', '.join(unsupported)}；Controller 当前支持："
                    f"{', '.join(sorted(backend_params))}。请统一前端 URLSearchParams 与 @RequestParam。"
                ),
            )
        )
        return checks

    @staticmethod
    def _spring_route_contract(
        browser_sources: dict[str, str],
        controller_sources: dict[str, str],
    ) -> list[ValidationCheck]:
        """Verify that a browser JSON client and Spring expose the same API."""
        browser_text = "\n".join(browser_sources.values())
        if not any(marker in browser_text for marker in ("fetch(", "axios.", "XMLHttpRequest")):
            return []

        frontend_paths: set[str] = set()
        api_value_pattern = re.compile(
            r"(?:const|let|var)\s+(?:api\w*|url|endpoint)\s*=\s*['\"](?P<path>/[A-Za-z0-9_./{}-]+)['\"]",
            re.I,
        )
        literal_pattern = re.compile(
            r"(?:fetch|axios(?:\.\w+)?)\s*\(\s*['\"`](?P<path>/[A-Za-z0-9_./{}$-]+)"
        )
        for pattern in (api_value_pattern, literal_pattern):
            for match in pattern.finditer(browser_text):
                frontend_paths.add(ArtifactValidator._normalize_api_path(match.group("path")))
        if not frontend_paths:
            return []

        frontend_methods = ArtifactValidator._browser_http_methods(browser_text)

        backend_paths: set[str] = set()
        backend_methods: set[str] = set()
        has_json_controller = False
        mapping_patterns = {
            "GET": re.compile(r"@GetMapping(?:\s*\((?P<body>[^)]*)\))?"),
            "POST": re.compile(r"@PostMapping(?:\s*\((?P<body>[^)]*)\))?"),
            "PUT": re.compile(r"@PutMapping(?:\s*\((?P<body>[^)]*)\))?"),
            "PATCH": re.compile(r"@PatchMapping(?:\s*\((?P<body>[^)]*)\))?"),
            "DELETE": re.compile(r"@DeleteMapping(?:\s*\((?P<body>[^)]*)\))?"),
        }
        for content in controller_sources.values():
            has_json_controller = has_json_controller or "@RestController" in content or "@ResponseBody" in content
            class_match = re.search(
                r"@RequestMapping\s*\((?P<body>[^)]*)\)\s*(?:public\s+)?(?:final\s+)?class\s+",
                content,
                re.DOTALL,
            )
            base_path = ArtifactValidator._annotation_path(class_match.group("body") if class_match else "")
            for method, pattern in mapping_patterns.items():
                matches = list(pattern.finditer(content))
                if not matches:
                    continue
                backend_methods.add(method)
                for mapping in matches:
                    child_path = ArtifactValidator._annotation_path(mapping.groupdict().get("body") or "")
                    backend_paths.add(ArtifactValidator._normalize_api_path(f"{base_path}/{child_path}"))

        normalized_backend_bases = {
            re.sub(r"/\{[^/]+\}.*$", "", path).rstrip("/") or "/"
            for path in backend_paths
        }
        matching_paths = {
            frontend
            for frontend in frontend_paths
            if any(
                frontend == backend
                or frontend.startswith(f"{backend}/")
                or backend.startswith(f"{frontend}/")
                for backend in normalized_backend_bases
            )
        }
        problems: list[str] = []
        unmatched_paths = frontend_paths - matching_paths
        if unmatched_paths:
            problems.append(
                f"前端 API 路径 {', '.join(sorted(unmatched_paths))} 与 Controller 路径 "
                f"{', '.join(sorted(normalized_backend_bases)) or '（未识别）'} 不一致"
            )
        if not has_json_controller:
            problems.append("前端按 JSON 调用接口，但后端使用 @Controller 且没有 @ResponseBody/@RestController")
        missing_methods = sorted(frontend_methods - backend_methods)
        if missing_methods:
            problems.append(f"后端缺少前端需要的 HTTP 方法：{', '.join(missing_methods)}")

        if problems:
            return [
                ValidationCheck(
                    "frontend-backend-route-contract",
                    "backend",
                    "前后端 API 路由契约",
                    "failed",
                    "；".join(problems) + "。请让 REST Controller 与前端请求路径、方法和 JSON 语义保持一致。",
                )
            ]
        return [
            ValidationCheck(
                "frontend-backend-route-contract",
                "backend",
                "前后端 API 路由契约",
                "passed",
                f"前端 API 与 Spring REST Controller 一致：{', '.join(sorted(matching_paths))}；方法 {', '.join(sorted(frontend_methods))}。",
            )
        ]

    @staticmethod
    def _annotation_path(body: str) -> str:
        match = re.search(r"(?:value|path)\s*=\s*['\"](?P<path>[^'\"]+)['\"]", body)
        if match:
            return match.group("path")
        values = re.findall(r"['\"]([^'\"]*)['\"]", body)
        if not values:
            return ""
        return next((value for value in values if value.strip()), "")

    @staticmethod
    def _normalize_api_path(value: str) -> str:
        path = re.sub(r"//+", "/", str(value or "").strip())
        path = re.sub(r"/\$\{[^/]+\}", "/{id}", path)
        if not path.startswith("/"):
            path = f"/{path}"
        return path.rstrip("/") or "/"

    async def _validate_frontend_build(
        self,
        root: Path,
        files: dict[str, str],
        install_dependencies: bool,
        timeout: float,
        run_startup: bool,
        startup_timeout: float,
        record: Callable[[ValidationCheck], Awaitable[None]],
        *, contract: dict[str, Any] | None = None,
    ) -> None:
        executable = self._executable("npm.cmd", "npm")
        if not executable:
            await record(ValidationCheck("frontend-toolchain", "frontend", "Node.js 工具链", "blocked", "找不到 npm，无法执行前端构建。"))
            return
        if install_dependencies:
            install_args = ["ci", "--ignore-scripts", "--no-audit", "--no-fund"] if (root / "package-lock.json").exists() else ["install", "--ignore-scripts", "--no-audit", "--no-fund"]
            command = self._display_command(executable, install_args)
            outcome = await self._run_command(executable, install_args, root, timeout)
            await record(self._command_check("frontend-install", "frontend", "安装前端依赖", command, outcome))
            if outcome.returncode != 0:
                return
        build_args = ["run", "build"]
        command = self._display_command(executable, build_args)
        outcome = await self._run_command(executable, build_args, root, timeout)
        await record(self._command_check("frontend-build", "frontend", "前端构建", command, outcome))
        if outcome.returncode != 0 or not run_startup:
            return
        scripts = self._json_scripts(files.get("package.json", ""))
        start_script = next((name for name in ("dev", "start", "preview") if name in scripts), None)
        if not start_script:
            await record(ValidationCheck("frontend-startup", "frontend", "前端启动脚本", "failed", "构建通过但 package.json 缺少 dev、start 或 preview 启动脚本。"))
            return
        await self._validate_startup(
            root,
            executable,
            ["run", start_script, "--", "--host", "127.0.0.1", "--port", "{port}"],
            "frontend",
            "前端启动检查",
            ("/",),
            startup_timeout,
            record,
            page_paths=tuple((contract or {}).get("entrypoints", [])),
            browser_contract=contract if contract and contract.get("backend_stack") == "none" else None,
        )

    async def _validate_backend_build(
        self,
        root: Path,
        files: dict[str, str],
        timeout: float,
        run_startup: bool,
        startup_timeout: float,
        record: Callable[[ValidationCheck], Awaitable[None]],
        *, contract: dict[str, Any] | None = None, database_probe_path: str = "",
    ) -> None:
        if "pom.xml" in files:
            executable = self._executable("mvn.cmd", "mvn")
            if not executable:
                await record(ValidationCheck("backend-toolchain", "backend", "Java 工具链", "blocked", "找不到 Maven，无法执行后端编译。"))
                return
            compile_args = ["-B", "-Dstyle.color=never", "test"]
            command = self._display_command(executable, compile_args)
            outcome = await self._run_command(executable, compile_args, root, timeout)
            await record(self._command_check("backend-test", "backend", "后端 Maven 测试", command, outcome))
            if outcome.returncode != 0 or not run_startup:
                return
            spring_arguments = ["--server.port={port}", *self._spring_h2_test_arguments(files)]
            if database_probe_path:
                spring_arguments.append("--spring.profiles.include=agent-team-validation")
            probe_paths = self._spring_probe_paths(files)
            if contract:
                declared_paths = [row["path"] for row in contract.get("api_contract", []) if "{" not in row["path"]]
                probe_paths = tuple(dict.fromkeys([*declared_paths, *probe_paths]))
            crud_spec = self._spring_crud_spec(files)
            crud_specs = []
            if (contract or {}).get("crud_required") and (contract or {}).get("api_contract"):
                for api in contract["api_contract"]:
                    methods = set(api.get("methods", []))
                    if not {"POST", "DELETE"}.issubset(methods) or not methods & {"PUT", "PATCH"}:
                        continue
                    payload = api.get("payload") or {}
                    marker = next((key for key, value in payload.items() if isinstance(value, str) and key != api.get("identity_field", "id")), "")
                    crud_specs.append(_SpringCrudSpec(
                        api["path"], payload, marker, frozenset(methods),
                        api.get("identity_field", "id"), api.get("detail_required", False),
                        database_probe_path, str(api.get("entity_id") or ""),
                    ))
                crud_spec = None
            await self._validate_startup(
                root,
                executable,
                [
                    "-B",
                    "-Dstyle.color=never",
                    "spring-boot:run",
                    f"-Dspring-boot.run.arguments={' '.join(spring_arguments)}",
                ],
                "backend",
                "后端启动检查",
                probe_paths,
                startup_timeout,
                record,
                crud_spec=crud_spec,
                crud_specs=tuple(crud_specs),
                page_paths=tuple(contract.get("entrypoints", [])) if contract and contract.get("page_mode") != "spa" else (),
                browser_contract=contract,
                companion_files=files if contract and contract.get("page_mode") == "spa" else None,
            )
            return
        if "build.gradle" in files or "build.gradle.kts" in files:
            executable = self._executable("gradlew.bat", "gradlew", "gradle")
            if not executable:
                await record(ValidationCheck("backend-toolchain", "backend", "Gradle 工具链", "blocked", "找不到 Gradle，无法执行后端构建。"))
                return
            args = ["build"]
            outcome = await self._run_command(executable, args, root, timeout)
            await record(self._command_check("backend-build", "backend", "后端 Gradle 构建", self._display_command(executable, args), outcome))
            return
        await record(ValidationCheck("backend-build", "backend", "后端构建", "blocked", "当前后端技术栈暂无受控构建适配器。"))

    @staticmethod
    def _spring_h2_test_arguments(files: dict[str, str]) -> list[str]:
        """Return an isolated H2 profile for Spring delivery validation."""
        pom = files.get("pom.xml", "")
        if not re.search(r"<artifactId>\s*h2\s*</artifactId>", pom, re.IGNORECASE):
            return []
        persistence_markers = (
            "spring-boot-starter-data-jpa",
            "spring-boot-starter-jdbc",
            "flyway-core",
            "liquibase-core",
        )
        if not any(marker in pom for marker in persistence_markers) and not any(name.endswith(".sql") for name in files):
            return []

        arguments = [
            "--spring.datasource.url=jdbc:h2:mem:agent_team_test;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1",
            "--spring.datasource.driver-class-name=org.h2.Driver",
            "--spring.datasource.username=sa",
            "--spring.datasource.password=",
            "--spring.h2.console.enabled=false",
        ]
        migrations = [name for name in files if "/db/migration/" in f"/{name.lower()}" and name.endswith(".sql")]
        has_schema = any(Path(name).name.lower() == "schema.sql" for name in files)
        has_flyway = bool(re.search(r"<artifactId>\s*(?:flyway-core|spring-boot-starter-flyway)\s*</artifactId>", pom))
        if migrations and has_flyway:
            arguments.extend(
                [
                    "--spring.flyway.enabled=true",
                    "--spring.sql.init.mode=never",
                    "--spring.jpa.hibernate.ddl-auto=validate",
                    "--spring.jpa.defer-datasource-initialization=false",
                ]
            )
        elif has_schema:
            arguments.extend(
                [
                    "--spring.flyway.enabled=false",
                    "--spring.sql.init.mode=always",
                    "--spring.jpa.hibernate.ddl-auto=validate",
                    "--spring.jpa.defer-datasource-initialization=false",
                    "--spring.sql.init.schema-locations=classpath:schema.sql",
                    "--spring.sql.init.data-locations=optional:classpath:data.sql",
                ]
            )
        else:
            arguments.extend(
                [
                    "--spring.flyway.enabled=false",
                    "--spring.sql.init.mode=never",
                    "--spring.jpa.hibernate.ddl-auto=create-drop",
                ]
            )
        return arguments

    @staticmethod
    def _spring_get_routes(files: dict[str, str]) -> tuple[str, ...]:
        """Discover concrete GET routes from actual controller source."""
        routes: set[str] = set()
        for name, source in files.items():
            if not name.endswith(".java"):
                continue
            class_mapping = re.search(
                r"@RequestMapping\s*\((?P<body>[^)]*)\)\s*(?:public\s+)?(?:final\s+)?class\s+",
                source, re.DOTALL,
            )
            base = ArtifactValidator._annotation_path(class_mapping.group("body") if class_mapping else "")
            for match in re.finditer(r"@GetMapping(?:\s*\((?P<body>[^)]*)\))?", source):
                child = ArtifactValidator._annotation_path(match.group("body") or "")
                route = ArtifactValidator._normalize_api_path(f"{base}/{child}")
                if "{" not in route and "*" not in route:
                    routes.add(route)
        return tuple(sorted(routes))

    @staticmethod
    def _spring_probe_paths(files: dict[str, str]) -> tuple[str, ...]:
        """Probe real API collection paths before generic health endpoints."""
        browser_text = "\n".join(
            content
            for name, content in files.items()
            if Path(name).suffix.lower() in {".html", ".js", ".mjs", ".ts", ".tsx", ".vue"}
        )
        paths = {
            ArtifactValidator._normalize_api_path(match)
            for match in re.findall(r"['\"](/api/[A-Za-z0-9_./{}-]+)['\"]", browser_text)
        }
        collection_paths = sorted(re.sub(r"/\{[^/]+\}.*$", "", path) for path in paths)
        return tuple(dict.fromkeys([*collection_paths, *ArtifactValidator._spring_get_routes(files), "/api/health", "/actuator/health", "/"]))

    @staticmethod
    def _browser_http_methods(source: str) -> set[str]:
        """Handle literal methods, common ternary selectors and axios helpers."""
        methods = {"GET"}
        for expression in re.findall(r"\bmethod\s*:\s*([^\n,}]+)", source):
            methods.update(method.upper() for method in re.findall(
                r"['\"](GET|POST|PUT|PATCH|DELETE)['\"]", expression, re.IGNORECASE,
            ))
        methods.update(method.upper() for method in re.findall(
            r"\baxios\.(get|post|put|patch|delete)\s*\(", source, re.IGNORECASE,
        ))
        return methods

    @staticmethod
    def _spring_crud_spec(files: dict[str, str]) -> _SpringCrudSpec | None:
        browser_text = "\n".join(
            content
            for name, content in files.items()
            if Path(name).suffix.lower() in {".html", ".js", ".mjs", ".ts", ".tsx", ".vue"}
        )
        api_paths = [
            ArtifactValidator._normalize_api_path(path)
            for path in re.findall(r"['\"](/api/[A-Za-z0-9_./{}-]+)['\"]", browser_text)
        ]
        collection_path = next(
            (re.sub(r"/\{[^/]+\}.*$", "", path) for path in api_paths if "{" not in path),
            None,
        )
        methods = ArtifactValidator._browser_http_methods(browser_text)
        if not collection_path:
            # Server-rendered pages may contain no literal JSON API URL.
            # Still exercise an actual REST CRUD controller, not default URLs.
            for name, source in files.items():
                if not (name.endswith(".java") and "@RestController" in source
                        and "@PostMapping" in source and "@DeleteMapping" in source
                        and ("@PutMapping" in source or "@PatchMapping" in source)):
                    continue
                routes = ArtifactValidator._spring_get_routes({name: source})
                if routes:
                    collection_path = routes[0]
                    methods.update({"POST", "DELETE", "PUT" if "@PutMapping" in source else "PATCH"})
                    break
        if not collection_path or not {"POST", "DELETE"}.issubset(methods) or not ({"PUT", "PATCH"} & methods):
            return None

        entity_source = next((content for content in files.values() if "@Entity" in content), "")
        if not entity_source:
            return None
        fields = re.findall(
            r"(?P<annotations>(?:@[A-Za-z_][\w.]*(?:\([^)]*\))?\s*)*)\bprivate\s+(?P<type>[A-Za-z_][\w<>?, .]*)\s+(?P<name>[A-Za-z_][\w]*)\s*;",
            entity_source,
        )
        payload: dict[str, Any] = {}
        identity_field = ""
        unique_suffix = str(int(time.time() * 1000))[-8:]
        for annotations, raw_type, name in fields:
            lowered = name.lower()
            if lowered == "id" or "static" in raw_type.lower():
                continue
            json_name = re.search(r'@JsonProperty\s*\(\s*"([^\"]+)"', annotations)
            wire_name = json_name.group(1) if json_name else name
            if "string" in raw_type.lower() or "char" in raw_type.lower():
                if "email" in lowered:
                    value: Any = f"agent-{unique_suffix}@example.test"
                elif any(marker in lowered for marker in ("no", "number", "code")):
                    value = f"AT-{unique_suffix}"
                elif "phone" in lowered or "mobile" in lowered:
                    value = "13800000000"
                else:
                    value = f"AT-{unique_suffix}"
            elif any(marker in raw_type.lower() for marker in ("localdate", "date")):
                value = "2000-01-02"
            elif "boolean" in raw_type.lower():
                value = True
            elif any(marker in raw_type.lower() for marker in ("int", "long", "double", "float", "decimal", "number")):
                value = 1
            else:
                continue
            if isinstance(value, str):
                maximum = re.search(r"@Size\s*\([^)]*\bmax\s*=\s*(\d+)", annotations)
                if maximum:
                    value = value[:int(maximum.group(1))]
            payload[wire_name] = value
            if not identity_field:
                identity_field = wire_name
        if not payload or not identity_field:
            return None
        return _SpringCrudSpec(collection_path, payload, identity_field, frozenset(methods))

    async def _validate_startup(
        self,
        root: Path,
        executable: str,
        argument_template: list[str],
        target: str,
        label: str,
        paths: tuple[str, ...],
        timeout: float,
        record: Callable[[ValidationCheck], Awaitable[None]],
        *,
        crud_spec: _SpringCrudSpec | None = None,
        crud_specs: tuple[_SpringCrudSpec, ...] = (),
        page_paths: tuple[str, ...] = (),
        browser_contract: dict[str, Any] | None = None,
        companion_files: dict[str, str] | None = None,
    ) -> None:
        # Pick an OS-assigned port by binding a short-lived socket in the
        # startup helper.  A fixed project port would create false failures
        # when multiple Runs are being validated concurrently.
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        args = [str(item).replace("{port}", str(port)) for item in argument_template]
        command = self._display_command(executable, args)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        started = time.perf_counter()
        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [executable, *args],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._child_environment(),
                creationflags=creationflags,
            )
        except FileNotFoundError:
            await record(ValidationCheck(f"{target}-startup", target, label, "blocked", f"找不到启动工具：{executable}。", command=command))
            return
        except OSError as exc:
            await record(ValidationCheck(f"{target}-startup", target, label, "failed", f"启动进程失败：{exc}。", command=command))
            return

        status = "failed"
        message = "启动超时，未收到可用 HTTP 响应。"
        output = ""
        integration_checks: list[ValidationCheck] = []
        communication = asyncio.create_task(asyncio.to_thread(process.communicate))
        observed_responses: dict[str, int] = {}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(1.0, connect=0.5)) as client:
                deadline = time.perf_counter() + timeout
                while time.perf_counter() < deadline:
                    if process.poll() is not None:
                        break
                    for path in paths:
                        try:
                            response = await client.get(f"http://127.0.0.1:{port}{path}")
                        except httpx.RequestError:
                            continue
                        observed_responses[path] = response.status_code
                        # A 404 proves that a process is listening, not that
                        # the generated application exposes the requested
                        # entry point. Delivery validation must require an
                        # actually usable endpoint.
                        if 200 <= response.status_code < 400:
                            status = "passed"
                            message = f"服务已启动，{path} 返回 HTTP {response.status_code}。"
                            break
                    if status == "passed":
                        break
                    await asyncio.sleep(0.25)
                if status == "passed":
                    # Startup polling must fail quickly, but the first JPA
                    # transaction after startup can take longer than 1 s.
                    # Use a separate bounded read budget for integration work.
                    client.timeout = httpx.Timeout(5.0, connect=0.5)
                    for spec in (*crud_specs, *((crud_spec,) if crud_spec is not None else ())):
                        integration_checks.append(await self._probe_spring_crud_with_fixtures(
                            client, f"http://127.0.0.1:{port}", spec, crud_specs,
                        ))
                if status == "passed" and page_paths:
                    integration_checks.extend(await self._probe_pages(client, f"http://127.0.0.1:{port}", page_paths, target))
                if status == "passed" and browser_contract and browser_contract.get("browser_required"):
                    if companion_files is not None:
                        integration_checks.extend(await self._probe_joint_frontend(root, port, companion_files, browser_contract, client))
                    else:
                        integration_checks.extend(await self._probe_browser(root, f"http://127.0.0.1:{port}", browser_contract))
            if process.poll() is not None and status != "passed":
                message = f"进程提前退出，退出码 {process.returncode}。"
            elif status != "passed" and observed_responses:
                responses = "、".join(f"{path}=HTTP {code}" for path, code in observed_responses.items())
                message = f"服务已有 HTTP 响应，但探测端点未通过：{responses}；请检查路由、权限或接口错误，不是无响应启动超时。"
            await self._terminate_process(process)
            try:
                output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=5)
                output = self._clip_output((output_bytes or b"").decode("utf-8", errors="replace"))
            except Exception:
                output = ""
        except asyncio.CancelledError:
            await self._terminate_process(process)
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=5)
            except Exception:
                pass
            raise
        finally:
            await self._terminate_process(process)
            if not communication.done():
                try:
                    await asyncio.wait_for(asyncio.shield(communication), timeout=5)
                except Exception:
                    pass
            duration_ms = int((time.perf_counter() - started) * 1000)
        result_target = target
        if output:
            integration_checks = [
                ValidationCheck(check.id, check.target, check.label, check.status, check.message,
                                check.command, check.duration_ms, self._clip_output((check.output + "\n[服务日志]\n" + output).strip()), check.evidence)
                if check.status == "failed" else check
                for check in integration_checks
            ]
        if target == "backend" and status == "failed" and ArtifactValidator._is_database_startup_failure(output):
            result_target = "database"
            label = "数据库迁移与启动检查"
        await record(ValidationCheck(f"{result_target}-startup", result_target, label, status, message, command, duration_ms, output))
        for check in integration_checks:
            await record(check)

    async def _probe_browser(self, root: Path, base: str, contract: dict[str, Any]) -> list[ValidationCheck]:
        node = self._executable("node.exe", "node")
        if not node:
            return [ValidationCheck("browser-render", "frontend", "页面浏览器验证", "blocked", "缺少 Node.js/Playwright，不能验证页面真实渲染。")]
        checks: list[ValidationCheck] = []
        script = Path(__file__).with_name("browser_probe.cjs")
        for path in contract.get("entrypoints", ["/"]):
            # The frozen contract contains example values which may also be
            # present in generated seed data. Keep it immutable and isolate
            # this browser probe's create payload from existing rows.
            probe_contract = dict(contract)
            probe_contract["api_contract"] = [
                {**api, "payload": self._unique_probe_payload(api.get("payload") or {})}
                if isinstance(api, dict) else api
                for api in contract.get("api_contract", [])
            ]
            spec = json.dumps({
                "url": base + path,
                "contract": probe_contract,
                "evidenceDir": str(contract.get("_validation_evidence_dir") or ""),
                "evidenceId": f"browser-{uuid.uuid4().hex[:12]}",
            }, ensure_ascii=False).encode("utf-8")
            # Browser probes emit one structured JSON line containing the phase,
            # failure and evidence. Clipping before parsing can cut that line in
            # half and misclassify a real browser result as an unknown failure.
            outcome = await self._run_command(node, [str(script)], root, 90, input_data=spec, clip_output=False)
            try:
                data = json.loads(outcome.output.strip().splitlines()[-1])
            except (ValueError, IndexError):
                data = {"status": "failed", "message": outcome.output[-3000:]}
            status = "passed" if outcome.returncode == 0 and data.get("status") == "passed" else "blocked" if "Cannot find module" in outcome.output or "Executable doesn't exist" in outcome.output else "failed"
            browser_evidence = self._browser_evidence(data, path)
            checks.append(ValidationCheck("browser-render", "frontend", "页面浏览器验证", status, data.get("message") or "页面已真实渲染，无同源资源错误或 JavaScript 异常。", duration_ms=outcome.duration_ms, output=self._clip_output(outcome.output), evidence=browser_evidence))
            if contract.get("crud_required") and contract.get("backend_stack") != "none":
                checks.append(ValidationCheck("browser-crud", "frontend", "浏览器 CRUD 操作", status if data.get("uiCrud") or status == "blocked" else "failed", "已通过真实页面完成新增、查询、修改、删除" if data.get("uiCrud") else f"真实页面 CRUD 未通过（{data.get('phase') or '未知步骤'}）：{str(data.get('message') or '未获得浏览器错误')[:400]}", evidence=browser_evidence))
            elif contract.get("crud_required"):
                checks.append(ValidationCheck("browser-local-crud", "frontend", "浏览器本地 CRUD 操作", status if data.get("uiCrud") or status == "blocked" else "failed", "已验证新增、刷新后持久化、编辑与删除" if data.get("uiCrud") else f"本地 CRUD 未通过（{data.get('phase') or '未知步骤'}）：{str(data.get('message') or '未获得浏览器错误')[:400]}", evidence={**browser_evidence, "persistentAcrossReload": bool(data.get("uiCrud"))}))
        return checks

    @staticmethod
    def _browser_evidence(data: dict[str, Any], path: str) -> dict[str, Any]:
        """Keep browser diagnostics useful but bounded in persisted Run logs."""
        def bounded_list(key: str, limit: int) -> list[Any]:
            value = data.get(key)
            return list(value[:limit]) if isinstance(value, list) else []

        return {
            "path": path,
            "phase": str(data.get("phase") or ""),
            "uiCrud": bool(data.get("uiCrud")),
            "visibleTextChars": int(data.get("visibleTextChars", 0) or 0),
            "screenshotPath": str(data.get("screenshotPath") or ""),
            "screenshotPaths": bounded_list("screenshotPaths", 8),
            "domSnapshot": str(data.get("domSnapshot") or "")[:12000],
            "consoleMessages": bounded_list("consoleMessages", 50),
            "networkEvents": bounded_list("networkEvents", 100),
            "formValues": data.get("formValues") if isinstance(data.get("formValues"), dict) else {},
            "crudStates": bounded_list("crudStates", 8),
        }

    async def _ensure_joint_frontend_dependencies(self, root: Path, executable: str) -> ValidationCheck | None:
        """A targeted backend gate uses a fresh workspace, not the prior npm install."""
        if (root / "node_modules" / "vite").exists():
            return None
        install_args = (
            ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
            if (root / "package-lock.json").exists()
            else ["install", "--ignore-scripts", "--no-audit", "--no-fund"]
        )
        outcome = await self._run_command(executable, install_args, root, 120.0)
        return self._command_check(
            "frontend-install", "frontend", "安装联调前端依赖",
            self._display_command(executable, install_args), outcome,
        )

    async def _probe_joint_frontend(self, root: Path, backend_port: int, files: dict[str, str], contract: dict[str, Any], client: httpx.AsyncClient) -> list[ValidationCheck]:
        """Keep backend/H2 alive while exercising Vue UI through its own proxy."""
        import socket
        executable = self._executable("npm.cmd", "npm")
        if not executable:
            return [ValidationCheck("browser-render", "frontend", "页面浏览器验证", "blocked", "缺少 npm，无法执行前后端浏览器联调。")]
        script = next((name for name in ("dev", "start", "preview") if name in self._json_scripts(files.get("package.json", ""))), None)
        if not script:
            return [ValidationCheck("browser-render", "frontend", "页面浏览器验证", "failed", "缺少前端启动脚本。")]
        install_check = await self._ensure_joint_frontend_dependencies(root, executable)
        if install_check is not None and install_check.status != "passed":
            return [install_check]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = await asyncio.to_thread(subprocess.Popen, [executable, "run", script, "--", "--host", "127.0.0.1", "--port", str(port)], cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**self._child_environment(), "VITE_API_PROXY": f"http://127.0.0.1:{backend_port}"}, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        communication = asyncio.create_task(asyncio.to_thread(process.communicate))
        try:
            for _ in range(180):
                if process.poll() is not None:
                    break
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/")
                    if response.status_code == 200:
                        page_checks = await self._probe_pages(client, f"http://127.0.0.1:{port}", tuple(contract.get("entrypoints", ["/"])), "frontend")
                        return [*([install_check] if install_check else []), *page_checks, *await self._probe_browser(root, f"http://127.0.0.1:{port}", contract)]
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.25)
            if process.poll() is None:
                await self._terminate_process(process)
            try:
                captured, _ = await asyncio.wait_for(asyncio.shield(communication), 5)
            except (asyncio.TimeoutError, OSError):
                captured = b""
            startup_log = captured.decode("utf-8", errors="replace")[-5000:] if isinstance(captured, bytes) else str(captured or "")[-5000:]
            return [ValidationCheck("browser-render", "frontend", "页面浏览器验证", "failed", "联调前端启动失败或超时。", output=startup_log,
                                    evidence={"exitCode": process.returncode, "startupLogCaptured": bool(startup_log)})]
        finally:
            await self._terminate_process(process)
            try:
                await asyncio.wait_for(asyncio.shield(communication), 5)
            except Exception:
                pass

    @staticmethod
    def _delivery_contract_structure(files: dict[str, str], contract: dict[str, Any]) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        if contract.get("validation_database", contract.get("database_mode")) == "h2" and contract.get("backend_stack") == "springboot":
            pom = files.get("pom.xml", "")
            persistence_dependency = any(marker in pom for marker in ("spring-boot-starter-data-jpa", "spring-boot-starter-jdbc", "spring-jdbc"))
            if not persistence_dependency:
                checks.append(ValidationCheck("backend-database-contract", "backend", "真实数据库合同", "failed", "冻结合同要求 H2，但缺少 JPA/JDBC 数据源依赖；禁止用内存 Map 冒充数据库 CRUD。"))
        mode = contract.get("page_mode")
        if mode == "static_rest" and "src/main/resources/static/index.html" not in files:
            checks.append(ValidationCheck("spring-page-contract", "frontend", "页面打包合同", "failed", "缺少 src/main/resources/static/index.html；根目录 HTML 不会被 Spring Boot 打包。"))
        if contract.get("browser_required") and contract.get("crud_required"):
            ui_sources = {
                name: source for name, source in files.items()
                if name.endswith((".html", ".vue", ".js", ".jsx", ".ts", ".tsx"))
            }
            if ui_sources:
                ui_text = "\n".join(ui_sources.values())
                required_ids = (contract.get("ui_test_ids") or {
                    "add": "crud-add", "save": "crud-save", "row": "crud-row",
                    "edit": "crud-edit", "delete": "crud-delete",
                })
                missing_ids = [
                    str(value) for key, value in required_ids.items()
                    if key != "field" and isinstance(value, str) and value not in ui_text
                ]
                crud_apis = [
                    item for item in contract.get("api_contract") or []
                    if isinstance(item, dict) and item.get("payload") and "POST" in (item.get("methods") or [])
                ]
                if len(crud_apis) > 1:
                    missing_ids.extend(
                        f"crud-panel-{item.get('entity_id')}"
                        for item in crud_apis
                        if not item.get("entity_id") or f"crud-panel-{item['entity_id']}" not in ui_text
                    )
                if missing_ids:
                    affected_components = sorted({
                        name for marker in missing_ids if marker.startswith("crud-panel-")
                        for name in ui_sources
                        if name.endswith(f"/{marker.removeprefix('crud-panel-')}Manager.vue")
                    })
                    checks.append(ValidationCheck(
                        "frontend-ui-hooks", "frontend", "浏览器 CRUD 控件合同", "failed",
                        "页面缺少浏览器验收所需的控件标识：" + ", ".join(missing_ids),
                        output="关联前端文件：" + ", ".join(sorted(ui_sources)),
                        evidence={"files": affected_components} if affected_components else None,
                    ))
                else:
                    checks.append(ValidationCheck(
                        "frontend-ui-hooks", "frontend", "浏览器 CRUD 控件合同", "passed",
                        "浏览器 CRUD 控件标识已声明；真实交互仍需浏览器验证。",
                    ))
        if contract.get("crud_required") and contract.get("backend_stack") != "none":
            api = contract.get("api_contract") or []
            if not any(item.get("payload") and {"GET", "POST", "DELETE"}.issubset(item.get("methods", [])) and set(item.get("methods", [])) & {"PUT", "PATCH"} for item in api):
                checks.append(ValidationCheck("delivery-api-contract", "backend", "CRUD 请求合同", "failed", "冻结合同缺少完整 CRUD 方法或合法创建 payload；不能以自行猜测的接口代替验收。"))
        if contract.get("backend_stack") == "springboot" and contract.get("crud_required"):
            controller_files = {
                name: source for name, source in files.items()
                if name.endswith("Controller.java")
            }
            routes_by_method: dict[str, set[str]] = {
                method: set() for method in ("GET", "PUT", "PATCH", "DELETE")
            }
            annotation_names = {
                "GET": "GetMapping",
                "PUT": "PutMapping",
                "PATCH": "PatchMapping",
                "DELETE": "DeleteMapping",
            }
            for source_name, source in controller_files.items():
                class_match = re.search(
                    r"@RequestMapping\s*\((?P<body>[^)]*)\)\s*(?:public\s+)?(?:final\s+)?class\s+",
                    source,
                    re.DOTALL,
                )
                base_path = ArtifactValidator._annotation_path(class_match.group("body") if class_match else "")
                method_sources = [source]
                parent_match = re.search(r"\bclass\s+\w+\s+extends\s+([A-Za-z_]\w*)\b", source)
                if parent_match:
                    parent_name = f"{parent_match.group(1)}.java"
                    parent_path = str(PurePosixPath(source_name).parent / parent_name)
                    parent_source = controller_files.get(parent_path)
                    if parent_source is None:
                        named_parents = [value for path, value in controller_files.items() if PurePosixPath(path).name == parent_name]
                        parent_source = named_parents[0] if len(named_parents) == 1 else None
                    if parent_source and re.search(r"\babstract\s+class\s+" + re.escape(parent_match.group(1)) + r"\b", parent_source):
                        method_sources.append(parent_source)
                for method, annotation in annotation_names.items():
                    pattern = re.compile(rf"@{annotation}(?:\s*\((?P<body>[^)]*)\))?")
                    for method_source in method_sources:
                        for mapping in pattern.finditer(method_source):
                            child = ArtifactValidator._annotation_path(mapping.groupdict().get("body") or "")
                            routes_by_method[method].add(ArtifactValidator._normalize_api_path(f"{base_path}/{child}"))

            missing_item_routes: list[str] = []
            for api in contract.get("api_contract") or []:
                collection = ArtifactValidator._normalize_api_path(str(api.get("path") or ""))
                required = set(api.get("methods") or []) & {"PUT", "PATCH", "DELETE"}
                if api.get("detail_required"):
                    required.add("GET")
                item_pattern = re.compile(rf"^{re.escape(collection)}/\{{[^/{{}}]+\}}$")
                for method in sorted(required):
                    if not any(item_pattern.fullmatch(route) for route in routes_by_method[method]):
                        missing_item_routes.append(f"{method} {collection}/{{id}}")
            if missing_item_routes:
                owner_names = sorted(controller_files) or ["（未找到 *Controller.java）"]
                checks.append(ValidationCheck(
                    "backend-item-route-contract",
                    "backend",
                    "CRUD 明细路由合同",
                    "failed",
                    "Spring CRUD 的修改、删除及约定的详情查询必须使用集合路径后的 /{id}；缺少："
                    + "、".join(missing_item_routes)
                    + "。禁止改成在请求体中传 id 的集合 PUT/DELETE。",
                    output="\n".join(f"{name}: 缺少冻结合同要求的明细路由" for name in owner_names),
                ))
            elif any(set(api.get("methods") or []) & {"PUT", "PATCH", "DELETE"} for api in contract.get("api_contract") or []):
                checks.append(ValidationCheck(
                    "backend-item-route-contract",
                    "backend",
                    "CRUD 明细路由合同",
                    "passed",
                    "Spring Controller 已实现冻结合同要求的 /{id} 修改与删除路由。",
                ))
        return checks

    @staticmethod
    def _install_h2_probe(root: Path, files: dict[str, str]) -> str:
        """Read-only inspection in temporary copy, never shipped as an Artifact."""
        app = next((source for name, source in files.items() if name.endswith(".java") and "@SpringBootApplication" in source), "")
        package = ArtifactValidator._java_package.search(app)
        tables = sorted(set(re.findall(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?[`\"]?([A-Za-z_][A-Za-z0-9_]*)[`\"]?\s*\(", "\n".join(source for name, source in files.items() if name.endswith(".sql")), re.I)))
        if not package or not tables:
            return ""
        suffix = uuid.uuid4().hex[:10]
        class_name = "AgentTeamDatabaseProbe" + suffix
        probe_path = "/__agent_team_validation/database/" + suffix
        table_literals = ",".join('"' + table + '"' for table in tables)
        source = f'''package {package.group(1)};
import javax.sql.DataSource;
import java.sql.*;
import java.util.*;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.*;
@Profile("agent-team-validation")
@RestController
public class {class_name} {{
  private final DataSource datasource;
  public {class_name}(DataSource datasource) {{ this.datasource = datasource; }}
  @GetMapping("{probe_path}")
  public Map<String,Object> inspect() throws Exception {{
    Map<String,Object> result = new LinkedHashMap<>();
    try (Connection connection = datasource.getConnection()) {{
      result.put("databaseProduct", connection.getMetaData().getDatabaseProductName());
      Map<String,Object> tables = new LinkedHashMap<>();
      for (String table : new String[]{{{table_literals}}}) {{
        List<Map<String,Object>> rows = new ArrayList<>();
        String actualName=table, schema="PUBLIC";
        try (ResultSet known=connection.getMetaData().getTables(null,null,null,null)) {{
          while(known.next()) {{
            if(table.equalsIgnoreCase(known.getString("TABLE_NAME")) && !"INFORMATION_SCHEMA".equalsIgnoreCase(known.getString("TABLE_SCHEM"))) {{ actualName=known.getString("TABLE_NAME");schema=known.getString("TABLE_SCHEM");break; }}
          }}
        }}
        String quote=connection.getMetaData().getIdentifierQuoteString().trim();
        String qualified=quote+schema+quote+"."+quote+actualName+quote;
        try (Statement statement = connection.createStatement(); ResultSet rs = statement.executeQuery("SELECT * FROM " + qualified)) {{
          ResultSetMetaData columns = rs.getMetaData();
          while (rs.next() && rows.size() < 100) {{
            Map<String,Object> row = new LinkedHashMap<>();
            for (int index=1; index<=columns.getColumnCount(); index++) row.put(columns.getColumnLabel(index).toLowerCase(Locale.ROOT), rs.getObject(index));
            rows.add(row);
          }}
        }}
        tables.put(table, rows);
      }}
      result.put("tables", tables);
    }}
    return result;
  }}
}}'''
        destination = root / "src/main/java" / package.group(1).replace(".", "/") / (class_name + ".java")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8", newline="")
        return probe_path

    @staticmethod
    async def _probe_pages(client: httpx.AsyncClient, base: str, paths: tuple[str, ...], target: str) -> list[ValidationCheck]:
        """Actual page HTTP and referenced same-origin resources, not API health."""
        prefix = "spring" if target == "backend" else "frontend"
        checks: list[ValidationCheck] = []
        for index, path in enumerate(paths):
            suffix = "" if index == 0 else f"-{index}"
            try:
                response = await client.get(base + path)
                valid = response.status_code == 200 and bool(re.search(r"<html\b|<!doctype\s+html", response.text, re.I))
                checks.append(ValidationCheck(f"{prefix}-page-entry{suffix}", target, "页面入口 HTTP", "passed" if valid else "failed", f"{path} HTTP {response.status_code}；" + ("已返回 HTML 页面。" if valid else "没有返回完整 HTML 页面，API 成功不代表界面可用。"), evidence={"path": path, "httpStatus": response.status_code}))
                if not valid:
                    continue
                errors: list[str] = []
                for reference in re.findall(r"<(?:script|link|img)\b[^>]*(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", response.text, re.I):
                    url = urljoin(str(response.url), reference)
                    if urlparse(url).netloc != urlparse(base).netloc or reference.startswith(("data:", "#")):
                        continue
                    asset = await client.get(url)
                    if asset.status_code != 200 or (urlparse(url).path.endswith((".js", ".css")) and "text/html" in asset.headers.get("content-type", "")):
                        errors.append(f"{reference}=HTTP {asset.status_code}（或错误 HTML fallback）")
                checks.append(ValidationCheck(f"{prefix}-page-assets{suffix}", target, "页面资源 HTTP", "failed" if errors else "passed", "；".join(errors) or "页面声明的同源脚本、样式和图片均可访问。", evidence={"path": path}))
            except httpx.RequestError as exc:
                checks.append(ValidationCheck(f"{prefix}-page-entry{suffix}", target, "页面入口 HTTP", "failed", f"{path} 请求失败：{type(exc).__name__}"))
        return checks

    @staticmethod
    def _is_database_startup_failure(output: str) -> bool:
        normalized = str(output or "").lower().replace("\\", "/")
        if "cannot load driver class" in normalized:
            return False
        markers = (
            "flyway",
            "liquibase",
            "scriptstatementfailedexception",
            "failed to execute sql script",
            "syntax error in sql statement",
            "migration checksum mismatch",
            "db/migration/",
            "schema.sql",
            "data.sql",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _unique_probe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Avoid collisions between contract examples and generated seed rows."""
        result = dict(payload)
        for key, value in result.items():
            if not isinstance(value, str) or "email" not in key.lower() or "@" not in value:
                continue
            local, domain = value.rsplit("@", 1)
            if local and domain:
                result[key] = f"{local}+probe-{uuid.uuid4().hex[:8]}@{domain}"
        return result

    @staticmethod
    async def _probe_spring_crud_with_fixtures(
        client: httpx.AsyncClient,
        base_url: str,
        spec: _SpringCrudSpec,
        all_specs: tuple[_SpringCrudSpec, ...],
    ) -> ValidationCheck:
        """Create valid referenced records without replacing the real CRUD probe.

        Each entity is still tested through POST/GET/PUT/DELETE. A child such
        as Order needs an existing Product; the fixture is created separately
        and removed after the child probe, so a valid referential-integrity
        check is not mistaken for a broken POST endpoint.
        """
        payload = dict(spec.payload)
        fixtures: list[tuple[str, Any]] = []
        result: ValidationCheck | None = None
        cleanup_error = ""
        try:
            for field in payload:
                if field == spec.record_id_field or not field.lower().endswith("id"):
                    continue
                parent_name = field[:-2].casefold()
                parent = next((item for item in all_specs if item.entity_id.casefold() == parent_name), None)
                if parent is None:
                    continue
                parent_payload = dict(parent.payload)
                quantity = payload.get("quantity")
                if isinstance(quantity, (int, float)) and isinstance(parent_payload.get("stock"), (int, float)):
                    parent_payload["stock"] = max(parent_payload["stock"], quantity + 1)
                create = await client.post(f"{base_url}{parent.collection_path}", json=parent_payload)
                if not 200 <= create.status_code < 400:
                    raise RuntimeError(
                        f"依赖记录 {parent.entity_id} 创建失败：HTTP {create.status_code} · {create.text[:300]}"
                    )
                try:
                    record = create.json()
                except ValueError as exc:
                    raise RuntimeError(f"依赖记录 {parent.entity_id} 创建后未返回 JSON") from exc
                parent_id = record.get(parent.record_id_field) if isinstance(record, dict) else None
                if parent_id is None:
                    raise RuntimeError(f"依赖记录 {parent.entity_id} 创建后缺少 {parent.record_id_field}")
                fixtures.append((f"{base_url}{parent.collection_path}/{parent_id}", parent_id))
                payload[field] = parent_id
            result = await ArtifactValidator._probe_spring_crud(
                client, base_url, replace(spec, payload=payload),
            )
        except (httpx.RequestError, RuntimeError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            result = ValidationCheck(
                "spring-h2-crud", "backend", "前后端与 H2 CRUD 联调", "failed",
                f"{spec.collection_path} 关联测试数据准备失败：{detail}",
                evidence={"phase": "related-fixture", "path": spec.collection_path},
            )
        finally:
            for url, _ in reversed(fixtures):
                try:
                    delete = await client.delete(url)
                    if not 200 <= delete.status_code < 400:
                        cleanup_error = f"清理关联测试记录返回 HTTP {delete.status_code}"
                except httpx.RequestError as exc:
                    cleanup_error = f"清理关联测试记录失败：{type(exc).__name__}"
        assert result is not None
        if cleanup_error and result.status == "passed":
            return ValidationCheck(
                "spring-h2-crud", "backend", "前后端与 H2 CRUD 联调", "failed",
                f"{spec.collection_path} {cleanup_error}",
                evidence={"phase": "related-fixture-cleanup", "path": spec.collection_path},
            )
        return result

    @staticmethod
    async def _probe_spring_crud(
        client: httpx.AsyncClient,
        base_url: str,
        spec: _SpringCrudSpec,
    ) -> ValidationCheck:
        """Exercise create/read/update/delete against the H2-backed service."""
        started = time.perf_counter()
        spec = replace(spec, payload=ArtifactValidator._unique_probe_payload(spec.payload))
        path = spec.collection_path
        created_id: Any = None
        storage_table: str | None = None
        phase = "create"
        try:
            create = await client.post(f"{base_url}{path}", json=spec.payload)
            if not 200 <= create.status_code < 400:
                return ValidationCheck(
                    "spring-h2-crud",
                    "backend",
                    "前后端与 H2 CRUD 联调",
                    "failed",
                    f"POST {path} 返回 HTTP {create.status_code}：{create.text[:500]}",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            try:
                created = create.json()
            except ValueError:
                created = None
            if isinstance(created, dict):
                created_id = created.get(spec.record_id_field)

            phase = "list-after-create"
            listing = await client.get(f"{base_url}{path}")
            if not 200 <= listing.status_code < 300:
                raise RuntimeError(f"GET {path} 返回 HTTP {listing.status_code}")
            try:
                listed: Any = listing.json()
            except ValueError as exc:
                raise RuntimeError("查询接口没有返回 JSON") from exc
            if isinstance(listed, dict):
                listed = next((listed.get(key) for key in ("content", "data", "records", "items") if isinstance(listed.get(key), list)), listed)
            if not isinstance(listed, list):
                raise RuntimeError("查询接口返回值不是数组或可识别的分页数组")
            if created_id is None:
                marker = spec.payload.get(spec.identity_field)
                matched = next((item for item in listed if isinstance(item, dict) and item.get(spec.identity_field) == marker), None)
                created_id = matched.get(spec.record_id_field) if isinstance(matched, dict) else None
            if created_id is None:
                raise RuntimeError("新增后无法从响应或查询结果取得记录 id")
            if not any(isinstance(item, dict) and str(item.get(spec.record_id_field)) == str(created_id) for item in listed):
                raise RuntimeError("POST 返回成功，但新增记录未出现在查询结果中")
            if spec.database_probe_path:
                phase = "database-after-create"
                tables = await ArtifactValidator._h2_rows(client, base_url + spec.database_probe_path)
                storage_table = next((table for table, rows in tables.items() if any(ArtifactValidator._h2_record_matches(row, spec, created_id, spec.payload.get(spec.identity_field)) for row in rows)), None)
                if storage_table is None:
                    raise RuntimeError("接口新增成功，但 H2 表中没有对应记录；可能使用内存 Map 或数据库字段映射错误")
            if spec.detail_required:
                phase = "detail"
                detail = await client.get(f"{base_url}{path}/{created_id}")
                if not 200 <= detail.status_code < 300:
                    raise RuntimeError(f"详情 GET {path}/{{id}} 返回 HTTP {detail.status_code}")

            update_method = "PATCH" if "PATCH" in spec.expected_methods and "PUT" not in spec.expected_methods else "PUT"
            updated_payload = dict(spec.payload)
            mutable_fields: list[tuple[int, str, Any]] = []
            for field, value in spec.payload.items():
                if field == spec.record_id_field or field.lower().endswith("id"):
                    continue
                changed = ArtifactValidator._mutated_contract_value(value)
                if changed != value:
                    lowered = field.lower()
                    priority = (
                        0 if lowered in {"name", "title", "guestname", "number", "description"}
                        else 1 if isinstance(value, str) and not re.search(r"status|state|type|date|time|email", lowered)
                        else 2 if lowered in {"quantity", "stock"}
                        else 3 if isinstance(value, (int, float))
                        else 4
                    )
                    mutable_fields.append((priority, field, changed))
            changed_fields: dict[str, Any] = {}
            if mutable_fields:
                safe_fields = [item for item in mutable_fields if item[0] < 4]
                for _, field, changed in safe_fields or [min(mutable_fields, key=lambda item: (item[0], item[1]))]:
                    updated_payload[field] = changed
                    changed_fields[field] = changed
            if not changed_fields:
                raise RuntimeError("CRUD 合同缺少可安全修改的字段，不能用未改变的数据冒充 UPDATE 验证")
            phase = "update"
            update = await client.request(update_method, f"{base_url}{path}/{created_id}", json=updated_payload)
            if not 200 <= update.status_code < 400:
                raise RuntimeError(f"{update_method} {path}/{{id}} 返回 HTTP {update.status_code}：{update.text[:500]}")
            phase = "list-after-update"
            after_update = await client.get(f"{base_url}{path}")
            if not 200 <= after_update.status_code < 300:
                raise RuntimeError(f"修改后 GET {path} 返回 HTTP {after_update.status_code}")
            try:
                updated_records: Any = after_update.json()
            except ValueError as exc:
                raise RuntimeError("修改后的查询接口没有返回 JSON") from exc
            if isinstance(updated_records, dict):
                updated_records = next((updated_records.get(key) for key in ("content", "data", "records", "items") if isinstance(updated_records.get(key), list)), updated_records)
            updated_record = next((item for item in updated_records if isinstance(item, dict) and str(item.get(spec.record_id_field)) == str(created_id)), None) if isinstance(updated_records, list) else None
            if updated_record is None:
                raise RuntimeError("修改接口返回成功，但查询结果中找不到对应记录")
            missing_updates = [
                field for field, value in changed_fields.items()
                if updated_record.get(field) != value
            ]
            if missing_updates:
                raise RuntimeError("修改接口返回成功，但查询结果未反映修改后的合同字段：" + "、".join(missing_updates))
            if storage_table:
                phase = "database-after-update"
                tables = await ArtifactValidator._h2_rows(client, base_url + spec.database_probe_path)
                stored_record = next((row for row in tables.get(storage_table, []) if str(ArtifactValidator._h2_column(row, spec.record_id_field)) == str(created_id)), None)
                if stored_record is None:
                    raise RuntimeError("接口修改成功，但 H2 中找不到对应记录")
                missing_storage_updates = [
                    field for field, value in changed_fields.items()
                    if ArtifactValidator._h2_column(stored_record, field) != value
                ]
                if missing_storage_updates:
                    raise RuntimeError("接口修改成功，但 H2 未更新合同字段：" + "、".join(missing_storage_updates))

            phase = "delete"
            delete = await client.delete(f"{base_url}{path}/{created_id}")
            if not 200 <= delete.status_code < 400:
                raise RuntimeError(f"DELETE {path}/{{id}} 返回 HTTP {delete.status_code}：{delete.text[:500]}")
            phase = "list-after-delete"
            after_delete = await client.get(f"{base_url}{path}")
            if not 200 <= after_delete.status_code < 300:
                raise RuntimeError(f"删除后 GET {path} 返回 HTTP {after_delete.status_code}")
            try:
                remaining: Any = after_delete.json()
            except ValueError as exc:
                raise RuntimeError("删除后的查询接口没有返回 JSON") from exc
            if isinstance(remaining, dict):
                remaining = next((remaining.get(key) for key in ("content", "data", "records", "items") if isinstance(remaining.get(key), list)), remaining)
            if not isinstance(remaining, list):
                raise RuntimeError("删除后的查询结果不是可识别的数组")
            if any(isinstance(item, dict) and str(item.get(spec.record_id_field)) == str(created_id) for item in remaining):
                raise RuntimeError("DELETE 返回成功，但记录仍存在于查询结果中")
            if storage_table:
                phase = "database-after-delete"
                tables = await ArtifactValidator._h2_rows(client, base_url + spec.database_probe_path)
                if any(str(ArtifactValidator._h2_column(row, spec.record_id_field)) == str(created_id) for row in tables.get(storage_table, [])):
                    raise RuntimeError("接口删除成功，但 H2 对应记录仍存在")
        except (httpx.RequestError, RuntimeError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            return ValidationCheck(
                "spring-h2-crud",
                "backend",
                "前后端与 H2 CRUD 联调",
                "failed",
                f"H2 CRUD 闭环失败（{phase}）：{detail}。",
                duration_ms=int((time.perf_counter() - started) * 1000),
                evidence={"phase": phase, "exceptionClass": type(exc).__name__}
                if isinstance(exc, httpx.RequestError) else None,
            )
        return ValidationCheck(
            "spring-h2-crud",
            "backend",
            "前后端与 H2 CRUD 联调",
            "passed",
            f"已在隔离 H2 数据库完成 POST、GET、{update_method}、DELETE 闭环：{path}。",
            duration_ms=int((time.perf_counter() - started) * 1000),
            evidence={"path": path, "methods": ["GET", "POST", update_method, "DELETE"], "fields": sorted(updated_record), "storageVerified": bool(storage_table), "databaseProduct": "H2" if storage_table else "unverified"},
        )

    @staticmethod
    def _h2_column(row: dict[str, Any], name: str) -> Any:
        normalize = lambda value: re.sub(r"_", "", str(value)).lower()
        return next((value for key, value in row.items() if normalize(key) == normalize(name)), None)

    @staticmethod
    def _h2_record_matches(row: dict[str, Any], spec: _SpringCrudSpec, identity: Any, marker: Any) -> bool:
        return isinstance(row, dict) and str(ArtifactValidator._h2_column(row, spec.record_id_field)) == str(identity) and ArtifactValidator._h2_column(row, spec.identity_field) == marker

    @staticmethod
    def _mutated_contract_value(value: Any) -> Any:
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return value + 1
        if isinstance(value, float):
            return value + 1.0
        if not isinstance(value, str) or not value:
            return value
        if re.fullmatch(r"\d+", value):
            return value[:-1] + ("1" if value[-1] != "1" else "2")
        # Decimal values are often represented as JSON strings for Java
        # BigDecimal. Preserve their numeric syntax in UPDATE probes.
        if re.fullmatch(r"[+-]?\d+\.\d+", value):
            whole, fraction = value.rsplit(".", 1)
            return f"{whole}.{fraction[:-1]}{'1' if fraction[-1] != '1' else '2'}"
        if "@" in value:
            local, domain = value.split("@", 1)
            return (("z" if not local.startswith("z") else "y") + local[1:]) + "@" + domain
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
            return value
        return ("Z" if not value.startswith("Z") else "Y") + value[1:]

    @staticmethod
    async def _h2_rows(client: httpx.AsyncClient, url: str) -> dict[str, list[dict[str, Any]]]:
        response = await client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"H2 数据库只读核验接口返回 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("H2 数据库核验未返回 JSON") from exc
        if data.get("databaseProduct") != "H2" or not isinstance(data.get("tables"), dict):
            raise RuntimeError("测试数据源不是可验证的 H2 数据库")
        return data["tables"]

    async def _run_command(self, executable: str, args: list[str], cwd: Path, timeout: float, *, input_data: bytes | None = None, clip_output: bool = True) -> "_CommandOutcome":
        started = time.perf_counter()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [executable, *args],
                cwd=str(cwd),
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._child_environment(),
                creationflags=creationflags,
            )
        except FileNotFoundError:
            return _CommandOutcome(None, "找不到可执行文件。", int((time.perf_counter() - started) * 1000), blocked=True)
        except OSError as exc:
            return _CommandOutcome(None, str(exc), int((time.perf_counter() - started) * 1000))
        communication = asyncio.create_task(asyncio.to_thread(process.communicate, input_data))
        try:
            output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
            output = (output_bytes or b"").decode("utf-8", errors="replace")
            if clip_output:
                output = self._clip_output(output)
            return _CommandOutcome(process.returncode, output, int((time.perf_counter() - started) * 1000))
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            try:
                output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=5)
            except Exception:
                output_bytes = b""
            output = (output_bytes or b"").decode("utf-8", errors="replace")
            if clip_output:
                output = self._clip_output(output)
            return _CommandOutcome(None, f"命令超过 {timeout:g} 秒超时。\n{output}".strip(), int((time.perf_counter() - started) * 1000), timed_out=True)
        except asyncio.CancelledError:
            await self._terminate_process(process)
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=5)
            except Exception:
                pass
            raise

    @staticmethod
    async def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
            if taskkill:
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                except Exception:
                    pass
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.to_thread(process.wait, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _child_environment() -> dict[str, str]:
        environment = os.environ.copy()
        sensitive_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTHORIZATION")
        for key in list(environment):
            if any(marker in key.upper() for marker in sensitive_markers):
                environment.pop(key, None)
        environment.update({"CI": "true", "BROWSER": "none", "NO_COLOR": "1"})
        return environment

    @staticmethod
    def _executable(*names: str) -> str | None:
        for name in names:
            path = shutil.which(name)
            if path:
                return path
        return None

    @staticmethod
    def _display_command(executable: str, args: list[str]) -> str:
        return " ".join([Path(executable).name, *args])

    @staticmethod
    def _json_scripts(package: str) -> dict[str, Any]:
        try:
            parsed = json.loads(package)
        except (TypeError, json.JSONDecodeError):
            return {}
        scripts = parsed.get("scripts") if isinstance(parsed, dict) else {}
        return scripts if isinstance(scripts, dict) else {}

    @staticmethod
    def _clip_output(value: str) -> str:
        value = value.strip()
        if len(value) <= 12000:
            return value
        lines = value.splitlines()
        root_lines = [
            line for line in lines
            if any(marker in line.lower() for marker in (
                "caused by:", "exception:", "sql error", "sqlstate", "constraint", "[error]", "error:"
            ))
        ]
        roots = "\n".join(root_lines)
        # Keep both ends of the root chain rather than discarding its head.
        if len(roots) > 6000:
            roots = roots[:3000] + "\n[根因链中段已压缩]\n" + roots[-3000:]
        return value[:1500] + "\n[关键错误与根因链]\n" + roots + "\n[日志尾部]\n" + value[-3500:]

    @staticmethod
    def _html_error(content: str) -> str | None:
        lowered = content.lower()
        if "<html" not in lowered or "</html>" not in lowered:
            return "index.html 缺少完整的 html 根节点。"
        if "<body" not in lowered or "</body>" not in lowered:
            return "index.html 缺少完整的 body 节点。"
        return None

    @staticmethod
    def _command_check(check_id: str, target: str, label: str, command: str, outcome: "_CommandOutcome") -> ValidationCheck:
        if outcome.blocked:
            status = "blocked"
            message = f"{label}无法执行：{outcome.output or '工具不可用'}"
        elif outcome.timed_out:
            status = "failed"
            message = f"{label}超时。"
        elif outcome.returncode == 0:
            status = "passed"
            message = f"{label}通过。"
        else:
            status = "failed"
            message = f"{label}失败，退出码 {outcome.returncode}。"
        return ValidationCheck(check_id, target, label, status, message, command, outcome.duration_ms, outcome.output)

    @staticmethod
    def _result(status: str, targets: tuple[str, ...], checks: list[ValidationCheck]) -> ArtifactValidationResult:
        failed = [check for check in checks if check.status == "failed"]
        blocked = [check for check in checks if check.status == "blocked"]
        if status == "passed":
            summary = "成果物结构、构建和启动检查全部通过。"
        elif failed:
            summary = "成果物验证失败：" + "；".join(check.message for check in failed[:3])
        elif blocked:
            summary = "成果物验证被阻塞：" + "；".join(check.message for check in blocked[:3])
        else:
            summary = "成果物未完成验证。"
        return ArtifactValidationResult(status, summary, targets, tuple(checks))


@dataclass(frozen=True, slots=True)
class _CommandOutcome:
    returncode: int | None
    output: str
    duration_ms: int
    timed_out: bool = False
    blocked: bool = False
