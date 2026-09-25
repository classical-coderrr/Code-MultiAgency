"""Cheap, deterministic checks against the approved Code Company contract.

These checks inspect only claims that can be extracted reliably from source.
The compiler/build/browser gates remain responsible for dynamic behaviour.
"""

from __future__ import annotations

import re
import posixpath
from pathlib import PurePosixPath
from typing import Any


def _check(
    check_id: str, target: str, label: str, errors: list[str], success: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "id": check_id, "target": target, "label": label,
        "status": "failed" if errors else "passed",
        "message": "；".join(errors[:12]) if errors else success,
    }
    if evidence:
        result["evidence"] = evidence
    return result


def _normalize_api_path(path: str) -> str:
    path = re.sub(r"/\$\{[^}]+\}", "/{id}", path.split("?", 1)[0])
    path = re.sub(r"/\{[^}]+\}", "/{id}", path)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    return path.rstrip("/") or "/"


def _entity_fields_with_mapped_superclass(
    path: str, source: str, files: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """A JPA @MappedSuperclass contributes persistent inherited fields."""
    actual: dict[str, str] = {}
    visited = {path}
    current = source
    for depth in range(5):
        actual.update({field: java_type for java_type, field in re.findall(
            r"\b(?:private|protected|public)\s+(?:final\s+)?([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:=|;)", current,
        ) if field not in actual})
        parent = re.search(r"\bextends\s+([A-Za-z_]\w*)\b", current)
        if not parent:
            break
        parent_path = str(PurePosixPath(path).parent / f"{parent.group(1)}.java")
        if parent_path in visited:
            break
        visited.add(parent_path)
        item = files.get(parent_path)
        parent_source = str(item.get("content") or "") if isinstance(item, dict) else ""
        if not re.search(r"@(?:[A-Za-z_]\w*\.)*MappedSuperclass\b", parent_source):
            break
        current = parent_source
    return actual


def _frontend_paths(files: dict[str, dict[str, Any]], allowed_paths: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for path, item in files.items():
        if PurePosixPath(path).name.startswith("vite.config."):
            continue
        if not path.endswith((".vue", ".js", ".ts", ".tsx", ".jsx", ".html")):
            continue
        if str(item.get("step_id") or item.get("owner")) != "frontend":
            continue
        source = str(item.get("content") or "")
        for match in re.finditer(r"[\'\"`](/[A-Za-z0-9_./{}$()-]+)", source):
            api_path = _normalize_api_path(match.group(1))
            first_segment = "/" + api_path.strip("/").split("/", 1)[0]
            expected_segment = any(
                first_segment == "/" + allowed.strip("/").split("/", 1)[0]
                for allowed in allowed_paths
            )
            preceding = source[max(0, match.start() - 80):match.start()]
            request_literal = bool(re.search(
                r"(?:\bfetch|\baxios(?:\.\w+)?|\brequest)\s*\(\s*$|\b(?:apiUrl|baseUrl|url|endpoint|apiPath)\s*[:=]\s*$",
                preceding, re.I,
            ))
            # A configured API base prefix is not itself a request endpoint.
            # A literal fetch('/api') remains an endpoint and must be checked.
            direct_request = bool(re.search(r"(?:\bfetch|\baxios(?:\.\w+)?|\brequest)\s*\(\s*$", preceding, re.I))
            if api_path == "/api" and not direct_request:
                continue
            if api_path.startswith("/api/") or expected_segment or request_literal:
                found.setdefault(api_path, path)
    return found


def _controller_routes(files: dict[str, dict[str, Any]], expected_paths: set[str]) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for path, item in files.items():
        if not path.endswith("Controller.java") or str(item.get("step_id") or item.get("owner")) != "backend":
            continue
        source = str(item.get("content") or "")
        class_match = re.search(r"\bclass\s+\w+", source)
        header = source[:class_match.start()] if class_match else source[:500]
        bases = re.findall(r"@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?[\'\"]([^\'\"]+)", header)
        base = bases[-1].rstrip("/") if bases else ""
        method_sources = [source]
        parent = re.search(r"\bclass\s+\w+\s+extends\s+([A-Za-z_]\w*)\b", source)
        if parent:
            parent_path = str(PurePosixPath(path).parent / f"{parent.group(1)}.java")
            parent_item = files.get(parent_path)
            if parent_item is None:
                named = [item for candidate_path, item in files.items() if PurePosixPath(candidate_path).name == f"{parent.group(1)}.java"]
                parent_item = named[0] if len(named) == 1 else None
            parent_source = str(parent_item.get("content") or "") if isinstance(parent_item, dict) else ""
            if re.search(r"\babstract\s+class\s+" + re.escape(parent.group(1)) + r"\b", parent_source):
                method_sources.append(parent_source)
        for method_source in method_sources:
            for match in re.finditer(r"@(Get|Post|Put|Patch|Delete)Mapping\s*(?:\(\s*(?:value\s*=\s*|path\s*=\s*)?[\'\"]([^\'\"]*)[\'\"])?", method_source):
                child = (match.group(2) or "").strip("/")
                route = f"{base}/{child}" if child else base or "/"
                normalized = _normalize_api_path(re.sub(r"//+", "/", route))
                if normalized.startswith("/api/") or normalized in expected_paths:
                    routes.add((match.group(1).upper(), normalized))
    return routes


def _sql_tables(source: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?([A-Za-z_]\w*)[\"`]?\s*\(", source, re.I):
        depth = 1
        start = match.end()
        end = start
        while end < len(source) and depth:
            if source[end] == "(":
                depth += 1
            elif source[end] == ")":
                depth -= 1
            end += 1
        body = source[start:end - 1]
        columns = {item.lower() for item in re.findall(r"(?:^|,)\s*[\"`]?([A-Za-z_]\w*)[\"`]?\s+[A-Za-z]", body, re.M) if item.upper() not in {"PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"}}
        tables[match.group(1).lower()] = columns
    return tables


def _snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def check_contract_conformance(compiled: dict[str, Any], raw_files: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return actionable violations with owner, expected and actual values."""
    files = {str(item.get("name") or "").replace("\\", "/"): item for item in raw_files if isinstance(item, dict)}
    checks: list[dict[str, str]] = []

    plan_errors: list[str] = []
    first_missing_owner = "artifact"
    for row in compiled.get("file_plan") or []:
        if not isinstance(row, dict):
            continue
        expected = str(row.get("path") or "")
        owner = str(row.get("owner") or "")
        if not expected or not owner:
            continue
        matches = [path for path, item in files.items() if str(item.get("step_id") or item.get("owner")) == owner and (path == expected or (expected.endswith(".java") and PurePosixPath(path).name == PurePosixPath(expected).name))]
        if not matches and expected.endswith("/Application.java"):
            matches = [path for path, item in files.items() if str(item.get("step_id") or item.get("owner")) == owner and path.endswith("Application.java")]
        if not matches and expected == "src/main.js":
            matches = [path for path, item in files.items() if str(item.get("step_id") or item.get("owner")) == owner and path == "src/main.ts"]
        if not matches:
            if first_missing_owner == "artifact":
                first_missing_owner = owner
            plan_errors.append(f"{owner} 缺少冻结文件 {expected}")
    checks.append(_check("frozen-file-plan", first_missing_owner, "冻结文件计划", plan_errors, "文件计划中的必需文件均已登记。"))

    allowed_paths = {_normalize_api_path(str(path)) for path in (compiled.get("openapi") or {}).get("paths") or {}}
    frontend_paths = _frontend_paths(files, allowed_paths)
    api_errors = [f"{source} 调用了合同外 API {path}；允许：{', '.join(sorted(allowed_paths))}" for path, source in frontend_paths.items() if path not in allowed_paths]
    if frontend_paths and allowed_paths:
        checks.append(_check("frontend-api-contract", "frontend", "前端 API 合同", api_errors, "前端使用的显式 API 路径均在冻结合同内。"))

    frontend_role = ((compiled.get("role_contracts") or {}).get("frontend") or {})
    frontend_stack = str((frontend_role.get("frontend") or {}).get("stack") or "")
    if frontend_stack == "vue" and allowed_paths and any(
        str(item.get("step_id") or item.get("owner")) == "frontend" for item in files.values()
    ):
        required_proxy_paths = {"/" + path.strip("/").split("/", 1)[0] for path in allowed_paths}
        vite_files = [
            (path, item) for path, item in files.items()
            if re.fullmatch(r"vite\.config\.(?:js|mjs|ts)", path)
            and str(item.get("step_id") or item.get("owner")) == "frontend"
        ]
        proxy_errors: list[str] = []
        if not vite_files:
            proxy_errors.append("缺少 vite.config.js，无法代理冻结 API 路径")
        else:
            for path, item in vite_files:
                source = str(item.get("content") or "")
                configured = set(re.findall(r"[\'\"](/[A-Za-z0-9_./-]+)[\'\"]\s*:", source))
                for prefix in sorted(required_proxy_paths - configured):
                    proxy_errors.append(f"{path} 缺少冻结 API 前缀 {prefix} 的 Vite 代理")
        checks.append(_check("frontend-proxy-contract", "frontend", "前端 Vite 代理合同", proxy_errors, "Vite 代理覆盖所有冻结 API 前缀。"))

    contract_fields = {str(field) for schema in ((compiled.get("json_schema") or {}).values()) if isinstance(schema, dict) for field in (schema.get("properties") or {})}
    form_errors: list[str] = []
    ignored_ui_fields = {"search", "keyword", "query", "filter", "page", "size"}
    for path, item in files.items():
        if str(item.get("step_id") or item.get("owner")) != "frontend" or not path.endswith((".vue", ".html")):
            continue
        source = str(item.get("content") or "")
        for match in re.finditer(r"\bv-model(?:\.[\w]+)*\s*=\s*[\'\"](?:form|editForm|studentForm|productForm)\.([A-Za-z_]\w*)[\'\"]", source):
            field = match.group(1)
            if field not in contract_fields and field not in ignored_ui_fields:
                form_errors.append(f"{path} 表单字段 {field} 不在冻结实体/DTO 合同中")
    if contract_fields and any(str(item.get("step_id") or item.get("owner")) == "frontend" for item in files.values()):
        checks.append(_check("frontend-field-contract", "frontend", "前端表单字段", form_errors, "显式表单字段属于冻结实体合同。"))

    import_errors: list[str] = []
    for path, item in files.items():
        if str(item.get("step_id") or item.get("owner")) != "frontend" or not path.endswith((".vue", ".js", ".ts", ".tsx", ".jsx")):
            continue
        source = str(item.get("content") or "")
        for match in re.finditer(r"\bimport\s+(?:[^;\n]*?\s+from\s+)?[\'\"](\.{1,2}/[^\'\"]+)[\'\"]", source):
            imported = match.group(1)
            base = posixpath.normpath(posixpath.join(posixpath.dirname(path), imported))
            candidates = {base, *(base + ext for ext in (".js", ".ts", ".vue", ".json", ".css", ".tsx", ".jsx"))}
            candidates.update(f"{base}/index{ext}" for ext in (".js", ".ts", ".vue"))
            if not any(candidate in files for candidate in candidates):
                import_errors.append(f"{path} 引用了不存在的模块 {imported}")
    if any(str(item.get("step_id") or item.get("owner")) == "frontend" for item in files.values()):
        checks.append(_check("frontend-import-contract", "frontend", "前端本地模块引用", import_errors, "相对 import 均指向已登记文件。"))

    expected_routes = {(method.upper(), _normalize_api_path(path)) for path, methods in ((compiled.get("openapi") or {}).get("paths") or {}).items() if isinstance(methods, dict) for method in methods if method.lower() in {"get", "post", "put", "patch", "delete"}}
    controller_routes = _controller_routes(files, {path for _, path in expected_routes})
    if controller_routes and expected_routes:
        route_errors = [f"后端实现了合同外路由 {method} {path}" for method, path in sorted(controller_routes - expected_routes)]
        route_errors.extend(f"后端缺少合同路由 {method} {path}" for method, path in sorted(expected_routes - controller_routes))
        checks.append(_check("backend-api-contract", "backend", "后端 API 合同", route_errors, "Controller 路径和方法与冻结合同一致。"))

    schema = str((files.get("src/main/resources/schema.sql") or {}).get("content") or "")
    tables = _sql_tables(schema) if schema else {}
    expected_tables = (compiled.get("database_schema") or {}).get("tables") or {}
    if expected_tables and schema:
        schema_errors: list[str] = []
        for table, definition in expected_tables.items():
            actual_columns = tables.get(str(table).lower())
            if actual_columns is None:
                schema_errors.append(f"schema.sql 缺少表 {table}")
                continue
            for field in (definition.get("columns") or {}):
                if str(field).lower() not in actual_columns and _snake(str(field)) not in actual_columns:
                    schema_errors.append(f"表 {table} 缺少实体字段 {field}")
        checks.append(_check("database-entity-contract", "database", "实体与数据库字段", schema_errors, "SQL 表和字段覆盖冻结实体合同。", {
            "relatedFiles": ["src/main/resources/schema.sql"],
            "expected": ", ".join(str(table) for table in expected_tables),
            "actual": ", ".join(sorted(tables)),
        }))

    # Spring Boot's default physical naming converts Java camelCase fields to
    # snake_case columns. The logical contract accepts either spelling, but a
    # generated unquoted schema with productId will not satisfy product_id at
    # runtime. Detect this before the slower Maven/startup gate.
    config_source = "\n".join(
        str(item.get("content") or "")
        for path, item in files.items()
        if PurePosixPath(path).name in {"application.yml", "application.yaml", "application.properties"}
    )
    if expected_tables and schema and not re.search(r"physical[-.]strategy", config_source, re.I):
        physical_errors: list[str] = []
        for table, definition in expected_tables.items():
            if not isinstance(definition, dict):
                continue
            actual_columns = tables.get(str(table).lower()) or set()
            entity_id = str(definition.get("entity_id") or "")
            entity_sources = [
                str(item.get("content") or "")
                for path, item in files.items()
                if str(item.get("step_id") or item.get("owner")) == "backend"
                and PurePosixPath(path).name in {f"{entity_id}.java", f"{entity_id}Entity.java"}
            ]
            if not entity_sources:
                continue
            for field in (definition.get("columns") or {}):
                field = str(field)
                physical = _snake(field)
                if (
                    physical != field.lower()
                    and field.lower() in actual_columns
                    and physical not in actual_columns
                    and re.search(r"\b" + re.escape(field) + r"\s*;", entity_sources[0])
                ):
                    physical_errors.append(
                        f"schema.sql 表 {table} 的列 {field} 与 Spring JPA 默认物理列名 {physical} 不一致"
                    )
        checks.append(_check(
            "database-physical-column-contract", "database", "SQL 与 JPA 物理列名",
            physical_errors, "SQL 列名与 Spring JPA 默认命名策略一致。",
            {"relatedFiles": ["src/main/resources/schema.sql"]},
        ))

    entity_errors: list[str] = []
    table_errors: list[str] = []
    reserved_table_errors: list[str] = []
    checked_entities = 0
    expected_entity_tables = {
        str(definition.get("entity_id")): str(table)
        for table, definition in expected_tables.items()
        if isinstance(definition, dict) and definition.get("entity_id")
    }
    for entity_name, dto_source in (compiled.get("backend_dto") or {}).items():
        candidates = [(path, item) for path, item in files.items() if str(item.get("step_id") or item.get("owner")) == "backend" and PurePosixPath(path).name in {f"{entity_name}.java", f"{entity_name}Entity.java"}]
        if not candidates:
            continue  # Missing files are reported by frozen-file-plan.
        checked_entities += 1
        path, item = candidates[0]
        source = str(item.get("content") or "")
        expected_table = expected_entity_tables.get(str(entity_name))
        if expected_table:
            annotation = re.search(r"@(?:[A-Za-z_]\w*\.)*Table\s*\(([^)]*)\)", source)
            declared = re.search(r'\bname\s*=\s*"((?:\\.|[^"\\])*)"', annotation.group(1)) if annotation else None
            raw_table_name = declared.group(1) if declared else ""
            normalized_table_name = raw_table_name.replace('\\"', '"').strip('"`')
            if declared and normalized_table_name.lower() != expected_table.lower():
                table_errors.append(
                    f"{path} @Table(name=\"{declared.group(1)}\") 与冻结表 {expected_table} 不一致"
                )
            if (
                expected_table.lower() == "order"
                and (
                    not declared
                    or (
                        normalized_table_name.lower() == "order"
                        and not (raw_table_name.startswith('\\"') or raw_table_name.startswith('`'))
                    )
                )
            ):
                reserved_table_errors.append(
                    f"{path} 的 order 是 SQL 关键字；保留冻结表名，并在 @Table 中引用标识符"
                )
        actual_fields = _entity_fields_with_mapped_superclass(path, source, files)
        expected_fields = {field: java_type for java_type, field in re.findall(r"\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:,|\))", str(dto_source))}
        for field, java_type in expected_fields.items():
            actual = actual_fields.get(field)
            if actual is None:
                entity_errors.append(f"{path} 缺少冻结实体字段 {field}")
            elif actual != java_type:
                entity_errors.append(f"{path} 字段 {field} 类型为 {actual}，合同要求 {java_type}")
    if checked_entities:
        checks.append(_check("backend-entity-contract", "backend", "后端实体字段", entity_errors, "Java Entity 字段与冻结实体合同一致。"))
        if expected_entity_tables:
            checks.append(_check("backend-table-contract", "backend", "后端实体表名合同", table_errors, "Java Entity 显式表名与冻结数据库合同一致。", {
                "relatedFiles": [
                    path for path in files
                    if any(error.startswith(path + " ") for error in table_errors)
                ],
                "expected": ", ".join(f"{entity}={table}" for entity, table in expected_entity_tables.items()),
            }))
            checks.append(_check(
                "backend-reserved-table-contract", "backend", "JPA 保留字表名",
                reserved_table_errors, "JPA 保留字表名已正确引用。",
                {"relatedFiles": [
                    path for path in files
                    if any(error.startswith(path + " ") for error in reserved_table_errors)
                ]},
            ))

    return checks
