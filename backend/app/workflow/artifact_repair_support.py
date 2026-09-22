"""Evidence-grounded candidate selection and owner-repair safety helpers."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any

from ..code_company.artifact_contracts import path_allowed
from ..code_company.repair import RepairEngine
from ..llm.base import LLMResponse
from .artifact_validator import ArtifactValidationResult
from .models import StepStatus
from .run_state import RunState


class ArtifactRepairSupportMixin:
    """Narrow support layer used by the artifact repair coordinator path."""
    @staticmethod
    def _select_repair_context_files(
        files: list[dict[str, Any]],
        *,
        target: str,
        candidate_name: str,
        diagnostics: str,
    ) -> list[dict[str, Any]]:
        """Select only source files that can explain the current failure.

        The file being repaired is appended separately to the prompt. Keeping
        unrelated source out of the context cuts latency and reduces the risk
        that an Agent rewrites a correct module while fixing another one.
        """
        diagnostic_text = str(diagnostics or "").lower()
        shared_names = {
            "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
            "src/main/resources/application.yml", "src/main/resources/application.yaml",
            "src/main/resources/schema.sql", "src/main/resources/data.sql",
        }

        def priority(item: dict[str, Any]) -> tuple[int, str]:
            name = str(item.get("name") or "")
            lowered = name.lower()
            owner = str(item.get("step_id") or "").lower()
            if name == candidate_name:
                return (99, lowered)
            if lowered and lowered in diagnostic_text:
                return (0, lowered)
            if lowered in shared_names or lowered.startswith("src/main/resources/db/migration/"):
                return (1, lowered)
            if target == "backend" and (
                lowered.endswith(".java")
                or any(word in lowered for word in ("controller", "service", "repository", "dto", "entity"))
            ):
                return (2, lowered)
            if target == "frontend" and (
                lowered.startswith("src/")
                or any(word in lowered for word in ("api", "router", "store", "app.vue"))
            ):
                return (2, lowered)
            if target == "database" and (
                lowered.endswith(".sql")
                or any(word in lowered for word in ("entity", "repository", "application.yml", "application.yaml"))
            ):
                return (2, lowered)
            if owner == target:
                return (3, lowered)
            return (99, lowered)

        ranked = sorted(files, key=priority)
        return [item for item in ranked if priority(item)[0] < 99]

    @staticmethod
    def _validation_failure_fingerprint(validation: ArtifactValidationResult) -> str:
        """Return a stable identity for no-progress detection across repairs."""
        return RepairEngine.fingerprint(validation)

    async def _reexecute_validation_owners(
        self,
        state: RunState,
        validation: ArtifactValidationResult,
        repair_attempt: int,
        validation_step_id: str,
    ) -> tuple[list[str], list[LLMResponse]]:
        """Patch related owner files against real source, never regenerate a project.

        The caller stages these changes and commits only after deterministic
        validation. Architecture, file layout and passing tests remain frozen.
        """
        validator_step = next(step for step in state.workflow.steps if step.id == validation_step_id)
        await self.event_bus.emit(
            "step.validation_owner_reexecuting",
            state.run_id,
            {
                "stepId": validation_step_id,
                "repairAttempt": repair_attempt,
                "repairMode": "grounded_multi_file_patch",
                "status": StepStatus.RUNNING.value,
                "reason": "基于现有源码整改关联文件，保留目录、技术栈和测试；验证通过前不提交。",
            },
        )
        before = state.context.snapshot()
        before_files = before.get("__artifact_files__", [])
        repaired_files, responses = await self._repair_failed_artifacts(
            state, validation, repair_attempt, validation_step_id,
            validator_step.timeout_seconds,
            self.token_budget_manager.provider_max_tokens(self.provider.capabilities()),
            bundle=True,
        )
        after = state.context.snapshot()
        allowed_owners = {
            str(check.target or "").lower()
            for check in validation.checks
            if check.status == "failed" and str(check.target or "").lower() in {"backend", "frontend", "database"}
        }
        violations = self._owner_reexecution_violations(
            before,
            after,
            allowed_owners=allowed_owners,
        )
        if violations:
            state.context.set("__artifact_files__", before_files if isinstance(before_files, list) else [])
            for protected_key in ("blueprint", "delivery_contract", "delivery_contract_hash", "compiled_contract"):
                if protected_key in before:
                    state.context.set(protected_key, before[protected_key])
            await self.event_bus.emit(
                "step.validation_owner_reexecution_blocked",
                state.run_id,
                {
                    "stepId": validation_step_id,
                    "repairAttempt": repair_attempt,
                    "status": StepStatus.RUNNING.value,
                    "violations": violations,
                    "reason": "责任 Agent 的候选修改越过文件归属或冻结合同边界，已撤回本轮修改。",
                },
            )
            return [], responses
        return repaired_files, responses

    @staticmethod
    def _owner_reexecution_violations(
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        allowed_owners: set[str],
    ) -> list[str]:
        """Detect contract mutation and cross-owner writes during owner replay."""
        violations: list[str] = []
        for key in ("blueprint", "delivery_contract", "delivery_contract_hash", "compiled_contract"):
            if before.get(key) != after.get(key):
                violations.append(f"protected_contract_changed:{key}")

        def file_map(snapshot: dict[str, Any]) -> dict[tuple[str, str], Any]:
            files = snapshot.get("__artifact_files__")
            if not isinstance(files, list):
                return {}
            return {
                (str(item.get("step_id") or "").lower(), str(item.get("name") or "").replace("\\", "/")): item.get("content")
                for item in files
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }

        old_files = file_map(before)
        new_files = file_map(after)
        changed = {
            key
            for key in old_files.keys() | new_files.keys()
            if old_files.get(key) != new_files.get(key)
        }
        ownership = (before.get("blueprint") or {}).get("artifact_ownership") if isinstance(before.get("blueprint"), dict) else {}
        for owner, name in sorted(changed):
            if owner not in allowed_owners:
                violations.append(f"owner_boundary:{owner or 'unowned'}:{name}")
                continue
            if isinstance(ownership, dict) and ownership and not path_allowed(owner, name, ownership):
                violations.append(f"path_boundary:{owner}:{name}")
        return violations

    @staticmethod
    def _validation_quality(validation: ArtifactValidationResult) -> tuple[int, int, int]:
        """A different error is not progress: compilation outranks CRUD checks."""
        return RepairEngine.quality(validation)

    @staticmethod
    def _commit_artifact_revision(state: RunState, before: dict[str, Any]) -> None:
        current = state.context.snapshot()
        files = current.get("__artifact_files__", [])
        old_files = before.get("__artifact_files__", [])
        old_content = {
            (item.get("step_id"), item.get("name")): item.get("content")
            for item in old_files if isinstance(item, dict)
        }
        owners = {
            str(item.get("step_id") or "")
            for item in files if isinstance(item, dict)
            and old_content.get((item.get("step_id"), item.get("name"))) != item.get("content")
        }
        revisions = dict(current.get("__artifact_owner_revisions__", {}) or {})
        for owner in owners:
            revisions[owner] = int(revisions.get(owner, 0)) + 1
        state.context.set("__artifact_owner_revisions__", revisions)
        state.context.set("__artifact_files__", [
            {**item, "owner_revision": revisions[str(item.get("step_id") or "")]}
            if isinstance(item, dict) and str(item.get("step_id") or "") in owners else item
            for item in files
        ])
        for step in state.workflow.steps:
            if step.id in owners and step.output:
                state.context.set(f"{step.output}_files", json.dumps([
                    {"name": item["name"], "content": item["content"]}
                    for item in files if isinstance(item, dict) and item.get("step_id") == step.id
                ], ensure_ascii=False))

    @staticmethod
    def _missing_java_declaration_candidates(
        owned_files: list[dict[str, Any]],
        all_files: list[dict[str, Any]],
        diagnostics: str,
        ownership: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Create candidates only for compiler-proven, locally referenced classes.

        Unknown symbols, third-party imports and ambiguous source locations stay
        with the referencing source Agent; the platform never invents a class
        merely because a name appears in arbitrary validator output.
        """
        normalized = str(diagnostics or "").replace("\\", "/")
        source: dict[str, Any] | None = None
        proposed: dict[str, dict[str, Any]] = {}
        existing_paths = {str(item.get("name") or "") for item in all_files if isinstance(item, dict)}
        declared = "\n".join(str(item.get("content") or "") for item in all_files if isinstance(item, dict))
        for line in normalized.splitlines():
            location = re.search(r"(?i)([A-Za-z0-9_./-]+\.java):\[\d+", line)
            if location:
                path = location.group(1)
                matches = [
                    item for item in owned_files
                    if path.endswith(str(item.get("name") or ""))
                ]
                if not matches:
                    matches = [
                        item for item in owned_files
                        if posixpath.basename(path) == posixpath.basename(str(item.get("name") or ""))
                    ]
                source = matches[0] if len(matches) == 1 else None
                continue
            symbol_match = re.search(r"(?i)^\[ERROR\]\s*(?:symbol\s*:\s*class|符号\s*:\s*类)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if not symbol_match or source is None:
                continue
            symbol = symbol_match.group(1)
            source_path = str(source.get("name") or "")
            content = str(source.get("content") or "")
            # An unqualified constructor call is strong evidence that the
            # project intended to own this class, not just import a library.
            if not re.search(r"\bnew\s+" + re.escape(symbol) + r"\s*\(", content):
                continue
            if re.search(r"\b(?:class|interface|enum|record)\s+" + re.escape(symbol) + r"\b", declared):
                continue
            if (symbol.endswith("NotFoundException") and re.search(
                r"\bclass\s+(?:ResourceNotFoundException|EntityNotFoundException|NotFoundException)\b",
                declared,
            )):
                # The owner Agent should inspect the existing exception's
                # constructor and correct this source reference instead.
                continue
            package_match = re.search(r"(?m)^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;", content)
            source_root = (
                "src/main/java/" if source_path.startswith("src/main/java/") else
                "src/test/java/" if source_path.startswith("src/test/java/") else ""
            )
            if not package_match or not source_root:
                continue
            package_name = package_match.group(1)
            imported = re.search(r"(?m)^\s*import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\." + re.escape(symbol) + r"\s*;", content)
            if imported:
                import_package = imported.group(1)
                root = ".".join(package_name.split(".")[:2])
                if not import_package.startswith(root + "."):
                    continue
                package_name = import_package
            expected_source_dir = source_root + "/".join(package_match.group(1).split("."))
            # A malformed existing path/package is a source-repair issue, not
            # permission to create a class in a second tree.
            if not imported and posixpath.dirname(source_path) != expected_source_dir:
                continue
            target_path = source_root + "/".join(package_name.split(".")) + f"/{symbol}.java"
            if target_path in existing_paths or target_path in proposed:
                continue
            if ownership and not path_allowed("backend", target_path, ownership):
                continue
            proposed[target_path] = {
                "name": target_path, "content": "", "step_id": "backend",
                "create": True, "symbol": symbol, "source_file": source_path,
            }
        return list(proposed.values())[:8]

    @staticmethod
    def _evidence_file_paths(checks: list[Any] | tuple[Any, ...] | None) -> list[str]:
        """Return normalized file paths explicitly named by validator evidence.

        Only file-oriented evidence keys are considered.  Generic evidence such
        as an HTTP ``path`` (``/api/rooms``) must not accidentally become a
        workspace repair target.
        """
        if not checks:
            return []
        file_keys = {
            "file", "files", "filename", "filenames", "filepath", "filepaths",
            "sourcefile", "sourcefiles", "relatedfile", "relatedfiles",
            "artifact", "artifacts", "artifactpath", "artifactpaths",
        }
        found: list[str] = []

        def collect(value: Any, *, file_context: bool = False) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
                    collect(child, file_context=normalized_key in file_keys)
                return
            if isinstance(value, (list, tuple, set)):
                for child in value:
                    collect(child, file_context=file_context)
                return
            if not file_context or not isinstance(value, str):
                return
            path = value.strip().replace("\\", "/").lstrip("./")
            if not path or path.startswith("/") or "/api/" in path.lower():
                return
            if path not in found:
                found.append(path)

        for check in checks:
            collect(getattr(check, "evidence", None))
        return found

    @staticmethod
    def _match_owned_evidence_files(
        owned_files: list[dict[str, Any]],
        evidence_paths: list[str],
    ) -> list[dict[str, Any]]:
        """Resolve evidence paths without guessing between duplicate basenames."""
        if not evidence_paths:
            return []
        normalized_files = {
            str(item.get("name") or "").replace("\\", "/").lstrip("./"): item
            for item in owned_files
            if str(item.get("name") or "").strip()
        }
        selected: dict[str, dict[str, Any]] = {}
        for raw_path in evidence_paths:
            path = raw_path.replace("\\", "/").lstrip("./")
            exact = normalized_files.get(path)
            if exact is not None:
                selected[path] = exact
                continue
            suffix_matches = [
                (name, item)
                for name, item in normalized_files.items()
                if path.endswith("/" + name) or name.endswith("/" + path)
            ]
            if len(suffix_matches) == 1:
                name, item = suffix_matches[0]
                selected[name] = item
                continue
            basename = Path(path).name.lower()
            basename_matches = [
                (name, item)
                for name, item in normalized_files.items()
                if Path(name).name.lower() == basename
            ]
            if len(basename_matches) == 1:
                name, item = basename_matches[0]
                selected[name] = item
        return list(selected.values())

    @staticmethod
    def _validation_repair_candidates(
        owned_files: list[dict[str, Any]],
        diagnostics: str,
        checks: list[Any] | tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate repair files from structured evidence before using heuristics."""
        normalized = diagnostics.replace("\\", "/")
        evidence_candidates = ArtifactRepairSupportMixin._match_owned_evidence_files(
            owned_files,
            ArtifactRepairSupportMixin._evidence_file_paths(checks),
        )
        if evidence_candidates:
            return evidence_candidates
        # Maven output may mention dependency names while downloading them.
        # Explicit compiler locations are stronger evidence than those incidental
        # mentions and must be checked before dependency-based routing.
        compiler_paths = [
            match.replace("\\", "/")
            for match in re.findall(
                r"(?m)^\[ERROR\]\s+([^\r\n]+?\.java):\[\d+",
                normalized,
            )
        ]
        if compiler_paths:
            source_files = ArtifactRepairSupportMixin._match_owned_evidence_files(owned_files, compiler_paths)
            if source_files:
                return source_files
        missing_table = re.search(r"Schema-validation:\s*missing table\s*\[([^\]]+)\]", normalized, re.IGNORECASE)
        if missing_table:
            table_name = missing_table.group(1).split(".")[-1].strip('"`')
            entity_files = [
                item for item in owned_files
                if str(item.get("name") or "").endswith(".java")
                and re.search(
                    r'@(?:[A-Za-z_]\w*\.)*Table\s*\([^)]*\bname\s*=\s*"'
                    + re.escape(table_name) + r'"',
                    str(item.get("content") or ""),
                    re.IGNORECASE,
                )
            ]
            if entity_files:
                return entity_files
        if "后端实体表名合同" in normalized:
            entity_files = [
                item for item in owned_files
                if str(item.get("name") or "").endswith(".java")
                and str(item.get("name") or "").replace("\\", "/") in normalized
            ]
            if entity_files:
                return entity_files
        if "前端 API 合同" in normalized or "前端 Vite 代理合同" in normalized:
            api_sources = [
                item for item in owned_files
                if str(item.get("name") or "").endswith((".vue", ".jsx", ".tsx", ".html", ".js", ".ts"))
                and "vite.config" not in str(item.get("name") or "")
                and str(item.get("name") or "") in normalized
            ]
            if "前端 Vite 代理合同" in normalized:
                api_sources.extend(
                    item for item in owned_files
                    if Path(str(item.get("name") or "")).name.startswith("vite.config.")
                )
            if api_sources:
                return list({str(item["name"]): item for item in api_sources}.values())
        if "真实数据库合同" in normalized:
            return [item for item in owned_files if item.get("name") == "pom.xml"]
        if re.search(r"接口(?:新增|修改|删除)成功，但 H2|可能使用内存 Map", normalized):
            return [item for item in owned_files if str(item.get("name", "")).endswith(("Service.java", "Controller.java", "Repository.java"))]
        if (
            "CRUD 明细路由合同" in normalized
            or re.search(r"H2 CRUD 闭环失败：(?:PUT|PATCH|DELETE)\s+[^\s]+/\{id\}\s+返回 HTTP 404", normalized)
        ):
            controllers = [
                item for item in owned_files
                if str(item.get("name", "")).endswith("Controller.java")
            ]
            if controllers:
                return controllers

        # A startup probe can reach the application while one or more
        # collection entrypoints still return 404 (for example
        # ``/api/products`` and ``/api/cart``). This is a routing failure,
        # not a process-startup failure. Route the repair request to the
        # owner Agent's controller/router instead of leaving it with Tester.
        if re.search(
            r"(?:/api/[^\s=;,]+).*?(?:HTTP\s*404|\b404\b|not found|未找到|未通过)",
            normalized,
            re.IGNORECASE | re.DOTALL,
        ):
            api_paths: list[str] = []
            for raw_path in re.findall(r"/api/[A-Za-z0-9_./{}-]+", normalized):
                path = raw_path.rstrip(".,;:)]}")
                if path and path not in api_paths:
                    api_paths.append(path)
            api_tokens = {
                token.lower()
                for path in api_paths
                for token in path.split("/")
                if token and not token.startswith("{") and token.lower() != "api"
            }
            route_candidates: list[dict[str, Any]] = []
            route_files: list[dict[str, Any]] = []
            for item in owned_files:
                name = str(item.get("name") or "").replace("\\", "/")
                lowered_name = name.lower()
                lowered_content = str(item.get("content") or "").lower()
                route_like = bool(re.search(r"(?:controller|router|routes?|resource|endpoint|api)", lowered_name))
                path_hit = any(path.lower() in lowered_content for path in api_paths)
                token_hit = bool(api_tokens) and any(token in lowered_content for token in api_tokens)
                if route_like and (path_hit or token_hit):
                    route_candidates.append(item)
                if route_like and lowered_name not in {"pom.xml", "package.json"}:
                    route_files.append(item)
            if route_candidates:
                return route_candidates
            if route_files:
                return route_files
            source_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).suffix.lower() in {".java", ".kt", ".py", ".ts", ".js", ".vue"}
            ]
            if source_files:
                return source_files
        if "页面浏览器验证" in normalized or "浏览器 CRUD 操作" in normalized:
            ui_sources = [item for item in owned_files if str(item.get("name", "")).endswith((".html", ".vue", ".jsx", ".tsx", "vite.config.js", "vite.config.ts"))]
            if ui_sources:
                if "VITE_API_PROXY" in normalized or "HTTP 404" in normalized:
                    ui_sources.sort(key=lambda item: 0 if "vite.config" in item["name"] else 1)
                return ui_sources
        if "页面入口 HTTP" in normalized or "页面资源 HTTP" in normalized or "页面打包合同" in normalized:
            html = [item for item in owned_files if str(item.get("name", "")).endswith(".html")]
            if html:
                return html
        if "模板渲染依赖" in normalized or "spring-boot-starter-thymeleaf" in normalized:
            return [item for item in owned_files if item.get("name") == "pom.xml"]
        if "前后端页面渲染合同" in normalized or "模板缺少配套 MVC Controller" in normalized:
            return [item for item in owned_files if str(item.get("name", "")).endswith("Controller.java")]
        export_source = re.search(r"is not exported by\s+[\"'](?P<path>[^\"']+)[\"']", normalized)
        if export_source:
            source_path = export_source.group("path").lstrip("./")
            matched = [
                item
                for item in owned_files
                if str(item.get("name") or "").replace("\\", "/").lstrip("./") == source_path
            ]
            if matched:
                return matched

        # A missing JDBC driver is a dependency/configuration error.  Spring's
        # stack trace usually contains Application.java, but that frame is only
        # where startup was invoked and must never outrank pom.xml.
        if re.search(
            r"Cannot load driver class:\s*org\.h2\.Driver|pom\.xml 缺少 com\.h2database:h2|H2 测试数据库依赖|JPA 源码依赖一致性|spring-boot-starter-data-jpa|package jakarta\.persistence does not exist|flyway-database-h2|dependencies\.dependency\.version|数据库初始化执行器",
            normalized,
            re.IGNORECASE,
        ):
            pom_files = [
                item
                for item in owned_files
                if str(item.get("name") or "").replace("\\", "/").lower() == "pom.xml"
            ]
            if pom_files:
                return pom_files
            config_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"application.yml", "application.yaml", "application.properties"}
            ]
            if config_files:
                return config_files

        if "前后端 api 路由契约" in normalized.lower() or "rest controller" in normalized.lower():
            controllers = [
                item
                for item in owned_files
                if str(item.get("name") or "").lower().endswith("controller.java")
            ]
            if controllers:
                return controllers

        # Spring Boot reports resources using their compiled path
        # (``target/classes/data.sql``), while the Artifact name is usually
        # ``src/main/resources/data.sql``. Map those paths back to the source
        # Artifact. When a data script runs before JPA creates its table, the
        # application configuration is the smallest and safest repair target.
        if re.search(r"table\s+[\"']?[A-Za-z_][\w$]*[\"']?\s+not found", normalized, re.IGNORECASE) and re.search(
            r"(?:^|/)data\.sql\b", normalized, re.IGNORECASE
        ):
            config_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"application.yml", "application.yaml", "application.properties"}
            ]
            seed_files = [
                item
                for item in owned_files
                if Path(str(item.get("name") or "")).name.lower() in {"data.sql", "schema.sql"}
            ]
            if config_files:
                return config_files
            if seed_files:
                return seed_files

        located: list[dict[str, Any]] = []
        for item in owned_files:
            name = str(item.get("name") or "").replace("\\", "/")
            if not name:
                continue
            escaped = re.escape(name)
            if re.search(rf"{escaped}(?::\[?\d|\s*\(\d|:\d)", normalized):
                located.append(item)
        if located:
            return located

        diagnostic_basenames = {
            Path(match.group("path")).name.lower()
            for match in re.finditer(r"(?P<path>(?:[A-Za-z]:)?[^\s'\"()]+\.[A-Za-z0-9]+)", normalized)
        }
        owned_by_basename: dict[str, list[dict[str, Any]]] = {}
        for item in owned_files:
            basename = Path(str(item.get("name") or "")).name.lower()
            if basename:
                owned_by_basename.setdefault(basename, []).append(item)
        basename_matches = [
            items[0]
            for basename, items in owned_by_basename.items()
            if basename in diagnostic_basenames and len(items) == 1
        ]
        if basename_matches:
            return basename_matches

        # Last resort for tools whose logs omit line numbers. Avoid build
        # descriptors unless the diagnostic explicitly identifies them as the
        # failing source; regenerating a healthy pom/package file can introduce
        # unrelated dependency drift.
        return [
            item
            for item in owned_files
            if str(item.get("name") or "").replace("\\", "/") in normalized
            and str(item.get("name") or "").lower() not in {"pom.xml", "package.json"}
        ]

    @staticmethod
    def _clean_artifact_repair_output(value: str) -> str:
        content = str(value or "").strip()
        match = re.fullmatch(r"```[^\r\n]*\r?\n(?P<body>.*?)\r?\n?```", content, flags=re.DOTALL)
        return match.group("body").strip() if match else content

