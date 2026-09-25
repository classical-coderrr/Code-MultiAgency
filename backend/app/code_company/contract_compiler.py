"""Deterministic compiler for one approved Code Company Blueprint."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


_JAVA_TO_JSON = {
    "String": "string", "UUID": "string", "LocalDate": "string", "LocalDateTime": "string",
    "Integer": "integer", "Long": "integer", "Short": "integer", "BigDecimal": "number",
    "Double": "number", "Float": "number", "Boolean": "boolean",
}


class ContractCompiler:
    """Compile a Blueprint into role contracts, manifests and validation rules."""

    VERSION = "2.0"

    def enrich_blueprint(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        enriched = deepcopy(blueprint)
        enriched["schema_version"] = "2.0"
        enriched["dependency_manifest"] = self._dependency_manifest(enriched)
        enriched["artifact_ownership"] = self._ownership(enriched)
        enriched["file_dependencies"] = self._file_plan(enriched)
        enriched["role_contracts"] = self._role_contracts(enriched)
        payload = {key: value for key, value in enriched.items() if key not in {"blueprint_hash", "frozen_at"}}
        enriched["blueprint_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        return enriched

    def compile(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        blueprint = self.enrich_blueprint(blueprint)
        entities = [item for item in blueprint.get("entities", []) if isinstance(item, dict)]
        api_rows = [item for item in blueprint.get("api_contract", []) if isinstance(item, dict)]
        stateless_api = (
            str((blueprint.get("database") or {}).get("mode") or "none") == "none"
            and not bool((blueprint.get("delivery_requirements") or {}).get("crud_required"))
        )
        schemas: dict[str, Any] = {}
        java_dto: dict[str, str] = {}
        frontend_types: dict[str, str] = {}
        database_tables: dict[str, Any] = {}

        for entity in entities:
            name = self._type_name(entity.get("id") or entity.get("name") or entity.get("table") or "Entity")
            properties: dict[str, Any] = {}
            required: list[str] = []
            columns: dict[str, Any] = {}
            fields = entity.get("fields") or {}
            if not isinstance(fields, dict):
                raise ValueError(f"实体合同 {name}.fields 必须是字段名到类型的映射")
            for field_name, declaration in fields.items():
                declaration = declaration if isinstance(declaration, dict) else {"java_type": str(declaration)}
                java_type = str(declaration.get("java_type") or "String")
                json_type = str(declaration.get("json_type") or _JAVA_TO_JSON.get(java_type, "string"))
                properties[str(field_name)] = {"type": json_type}
                if declaration.get("format"):
                    properties[str(field_name)]["format"] = declaration["format"]
                if not declaration.get("nullable", True) or declaration.get("generated") is False:
                    required.append(str(field_name))
                columns[str(field_name)] = {
                    "sql_type": str(declaration.get("sql_type") or "VARCHAR(255)"),
                    "nullable": bool(declaration.get("nullable", True)),
                    "generated": bool(declaration.get("generated", False)),
                }
            schemas[name] = {"type": "object", "properties": properties, "required": sorted(set(required))}
            java_dto[name] = self._java_record(name, properties, entity)
            frontend_types[name] = self._typescript_interface(name, properties)
            table = str(entity.get("table") or self._snake_case(name))
            database_tables[table] = {"entity_id": name, "columns": columns, "primary_key": str(entity.get("identity_field") or "id")}

        paths: dict[str, Any] = {}
        validator_rules: list[dict[str, Any]] = []
        integration_tests: list[dict[str, Any]] = []
        for row in api_rows:
            collection_path = str(row.get("collection_path") or row.get("path") or "").rstrip("/")
            if not collection_path:
                continue
            entity_name = self._resolve_entity(row, entities, allow_unbound=stateless_api)
            identity = str(row.get("identity_field") or "id")
            detail_path = str(row.get("detail_path") or f"{collection_path}/{{{identity}}}")
            entity_schema = {"$ref": f"#/components/schemas/{entity_name}"} if entity_name else None
            request_schema = entity_schema or self._inline_api_schema(row, "request_schema", use_payload=True)
            response_schema = entity_schema or self._inline_api_schema(row, "response_schema", use_payload=False)
            fields = list(row.get("fields") or [])
            for method in row.get("methods") or []:
                verb = str(method).lower()
                target_path = detail_path if verb in {"put", "patch", "delete"} else collection_path
                operation: dict[str, Any] = {"operationId": f"{verb}_{self._snake_case(entity_name or 'operation')}", "x-entity-id": entity_name}
                if verb in {"post", "put", "patch"}:
                    operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": request_schema}}}
                if verb == "delete":
                    operation["responses"] = {"204": {"description": "Deleted"}}
                    expected_status = 204
                else:
                    response_shape = response_schema
                    if entity_name and verb == "get" and target_path == collection_path and str(row.get("collection_response") or "array") == "array":
                        response_shape = {"type": "array", "items": entity_schema}
                    operation["responses"] = {"200": {"description": "OK", "content": {"application/json": {"schema": response_shape}}}}
                    expected_status = 200
                paths.setdefault(target_path, {})[verb] = operation
                validator_rules.append({"entityId": entity_name, "path": target_path, "method": str(method).upper(), "requiredFields": fields})
                integration_tests.append({"entityId": entity_name, "path": target_path, "method": str(method).upper(), "expectedStatus": expected_status})
                if verb == "get" and row.get("detail_required"):
                    detail_operation = {
                        "operationId": f"get_{self._snake_case(entity_name or 'operation')}_by_id",
                        "x-entity-id": entity_name,
                        "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": response_schema}}}},
                    }
                    paths.setdefault(detail_path, {})["get"] = detail_operation
                    validator_rules.append({"entityId": entity_name, "path": detail_path, "method": "GET", "requiredFields": fields})
                    integration_tests.append({"entityId": entity_name, "path": detail_path, "method": "GET", "expectedStatus": 200})

        return {
            "schema_version": self.VERSION,
            "source_blueprint_id": blueprint.get("blueprint_id"),
            "source_blueprint_version": blueprint.get("version", 1),
            "source_blueprint_hash": blueprint.get("blueprint_hash"),
            "openapi": {
                "openapi": "3.0.3",
                "info": {"title": str(blueprint.get("project_type") or "Code Company project"), "version": str(blueprint.get("version", 1))},
                "paths": paths,
                "components": {"schemas": schemas},
            },
            "json_schema": schemas,
            "database_schema": {**dict(blueprint.get("database") or {}), "tables": database_tables},
            "backend_dto": java_dto,
            "frontend_types": frontend_types,
            "dependency_manifest": blueprint.get("dependency_manifest") or {},
            "artifact_ownership": blueprint.get("artifact_ownership") or {},
            "file_plan": blueprint.get("file_dependencies") or [],
            "role_contracts": blueprint.get("role_contracts") or {},
            "execution_policy": self.execution_policy(blueprint),
            "validator_rules": validator_rules,
            "integration_tests": integration_tests,
        }

    @staticmethod
    def execution_policy(blueprint: dict[str, Any]) -> dict[str, Any]:
        skip: list[str] = []
        if not bool((blueprint.get("backend") or {}).get("required")):
            skip.append("backend")
        if not bool((blueprint.get("frontend") or {}).get("required")):
            skip.append("frontend")
        if str((blueprint.get("database") or {}).get("mode") or "none") == "none":
            skip.append("database")
        return {"skip_steps": skip, "source": "frozen_blueprint"}

    def _resolve_entity(
        self,
        row: dict[str, Any],
        entities: list[dict[str, Any]],
        *,
        allow_unbound: bool = False,
    ) -> str | None:
        explicit = str(row.get("entity_id") or row.get("entity") or "").strip()
        if explicit:
            resolved = self._type_name(explicit)
            known = {self._type_name(item.get("id") or item.get("name") or "") for item in entities}
            if resolved not in known:
                raise ValueError(f"API 合同引用了不存在的 entity_id：{explicit}")
            return resolved
        path = str(row.get("collection_path") or row.get("path") or "")
        methods = {str(item).upper() for item in row.get("methods") or []}
        if allow_unbound:
            return None
        if row.get("fields") or row.get("payload") or methods & {"POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"API 合同 {path or '<unknown>'} 缺少明确 entity_id，禁止通过 URL 模糊推断")
        return None

    @staticmethod
    def _inline_api_schema(
        row: dict[str, Any], schema_field: str, *, use_payload: bool,
    ) -> dict[str, Any]:
        declared = row.get(schema_field)
        if isinstance(declared, dict) and declared.get("type"):
            return deepcopy(declared)
        payload = row.get("payload") if use_payload and isinstance(row.get("payload"), dict) else {}
        fields = list(row.get("fields") or [])
        names = list(dict.fromkeys([*fields, *payload.keys()])) if use_payload else []
        properties: dict[str, Any] = {}
        for name in names:
            value = payload.get(name)
            if isinstance(value, bool):
                value_type = "boolean"
            elif isinstance(value, int):
                value_type = "integer"
            elif isinstance(value, float):
                value_type = "number"
            else:
                value_type = "string"
            properties[str(name)] = {"type": value_type}
        return {"type": "object", "properties": properties}

    @staticmethod
    def _dependency_manifest(blueprint: dict[str, Any]) -> dict[str, Any]:
        backend_stack = str((blueprint.get("backend") or {}).get("stack") or "none")
        frontend_stack = str((blueprint.get("frontend") or {}).get("stack") or "none")
        database = dict(blueprint.get("database") or {})
        result: dict[str, Any] = {"schema_version": "1.0", "managed_files": {}}
        if backend_stack == "springboot":
            dependencies = [
                {"group": "org.springframework.boot", "name": "spring-boot-starter-web"},
                {"group": "org.springframework.boot", "name": "spring-boot-starter-validation"},
                {"group": "org.springframework.boot", "name": "spring-boot-starter-test", "scope": "test"},
            ]
            if str(database.get("mode") or "none") != "none":
                dependencies.extend([
                    {"group": "org.springframework.boot", "name": "spring-boot-starter-data-jpa"},
                    {"group": "com.h2database", "name": "h2", "scope": "runtime"},
                ])
                if str(database.get("production_engine")) == "mysql":
                    dependencies.append({"group": "com.mysql", "name": "mysql-connector-j", "scope": "runtime"})
            result["backend"] = {"manager": "maven", "java_version": "17", "spring_boot_version": "3.3.5", "dependencies": dependencies, "plugins": ["spring-boot-maven-plugin"]}
            result["managed_files"]["pom.xml"] = "backend"
            result["managed_files"]["src/main/java/com/example/app/Application.java"] = "backend"
        if frontend_stack == "vue":
            result["frontend"] = {
                "manager": "npm", "node_version": ">=18",
                "dependencies": {"vue": "^3.5.0"},
                "dev_dependencies": {"@vitejs/plugin-vue": "^5.2.0", "vite": "^6.0.0"},
                "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
            }
            result["managed_files"]["package.json"] = "frontend"
            result["managed_files"]["index.html"] = "frontend"
            result["managed_files"]["src/main.js"] = "frontend"
        return result

    @staticmethod
    def _ownership(blueprint: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(blueprint.get("artifact_ownership") or {})

    def _file_plan(self, blueprint: dict[str, Any]) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        backend_stack = str((blueprint.get("backend") or {}).get("stack") or "none")
        frontend_stack = str((blueprint.get("frontend") or {}).get("stack") or "none")
        database_mode = str((blueprint.get("database") or {}).get("mode") or "none")
        page_mode = str((blueprint.get("delivery_requirements") or {}).get("page_mode") or "")

        def add(path: str, owner: str, provides: list[str], requires: list[str] | None = None, depends: list[str] | None = None) -> None:
            plan.append({"path": path, "owner": owner, "provides": provides, "requires": requires or [], "depends_on_files": depends or []})

        if database_mode != "none":
            add("src/main/resources/schema.sql", "database", ["DatabaseSchema"])
        if backend_stack == "springboot":
            add("pom.xml", "backend", ["BackendDependencies"])
            add("src/main/resources/application.yml", "backend", ["BackendConfiguration"], ["BackendDependencies"], ["pom.xml"])
            package_root = "src/main/java/com/example/app"
            add(f"{package_root}/Application.java", "backend", ["ApplicationEntry"], ["BackendDependencies"], ["pom.xml"])
            for entity in blueprint.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                name = self._type_name(entity.get("id") or entity.get("name") or "Entity")
                entity_path = f"{package_root}/{name}.java"
                repository_path = f"{package_root}/{name}Repository.java"
                service_path = f"{package_root}/{name}Service.java"
                controller_path = f"{package_root}/{name}Controller.java"
                add(entity_path, "backend", [name], ["DatabaseSchema"] if database_mode != "none" else [])
                add(repository_path, "backend", [f"{name}Repository"], [name], [entity_path])
                add(service_path, "backend", [f"{name}Service"], [name, f"{name}Repository"], [entity_path, repository_path])
                add(controller_path, "backend", [f"{name}Controller"], [name, f"{name}Service", "ApiContract"], [entity_path, service_path])
        elif backend_stack == "python":
            add("requirements.txt", "backend", ["BackendDependencies"])
            add("models.py", "backend", ["BackendModels"], ["DatabaseSchema"] if database_mode != "none" else [])
            add("service.py", "backend", ["BackendService"], ["BackendModels"], ["models.py"])
            add("main.py", "backend", ["ApplicationEntry", "ApiContract"], ["BackendService"], ["service.py"])
        if frontend_stack == "vue":
            add("package.json", "frontend", ["FrontendDependencies"])
            add("index.html", "frontend", ["FrontendEntry"], ["FrontendDependencies"], ["package.json"])
            add("src/main.js", "frontend", ["FrontendBootstrap"], ["FrontendDependencies", "AppComponent"], ["package.json", "src/App.vue"])
            entities = [row for row in blueprint.get("entities") or [] if isinstance(row, dict)]
            if len(entities) > 1:
                component_paths: list[str] = []
                component_symbols: list[str] = []
                for entity in entities:
                    name = self._type_name(entity.get("id") or entity.get("name") or "Entity")
                    path = f"src/components/{name}Manager.vue"
                    symbol = f"{name}ManagerComponent"
                    add(path, "frontend", [symbol], ["ApiContract"])
                    component_paths.append(path)
                    component_symbols.append(symbol)
                add("src/App.vue", "frontend", ["AppComponent"], component_symbols, component_paths)
            else:
                add("src/App.vue", "frontend", ["AppComponent"], ["ApiContract"])
            add("src/style.css", "frontend", ["FrontendStyles"])
        elif frontend_stack != "none":
            root = "src/main/resources/static/" if backend_stack == "springboot" and page_mode != "static" else ""
            add(f"{root}index.html", "frontend", ["FrontendEntry"], ["ApiContract"] if backend_stack != "none" else [])
            add(f"{root}style.css", "frontend", ["FrontendStyles"])
            add(f"{root}script.js", "frontend", ["FrontendBehavior"], ["ApiContract"] if backend_stack != "none" else [])

        # A frozen manifest is the source of truth for all downstream agents.
        # Reject both same-owner duplicates and cross-owner collisions here,
        # before parallel generation can create two competing artifacts.
        seen_paths: dict[str, dict[str, Any]] = {}
        for row in plan:
            path = str(row.get("path") or "").replace("\\", "/").strip("/")
            key = path.casefold()
            previous = seen_paths.get(key)
            if previous is not None:
                previous_path = str(previous.get("path") or "")
                previous_owner = str(previous.get("owner") or "unknown")
                owner = str(row.get("owner") or "unknown")
                raise ValueError(
                    f"Frozen artifact file plan contains duplicate path {path!r} "
                    f"(owners: {previous_owner}, {owner}; first path: {previous_path!r})"
                )
            seen_paths[key] = row
        return plan

    @staticmethod
    def _role_contracts(blueprint: dict[str, Any]) -> dict[str, Any]:
        common = {"blueprint_id": blueprint.get("blueprint_id"), "blueprint_version": blueprint.get("version", 1), "constraints": blueprint.get("constraints") or {}}
        ownership = blueprint.get("artifact_ownership") or {}
        manifest = blueprint.get("dependency_manifest") or {}
        file_plan = blueprint.get("file_dependencies") or []
        api_rows = [row for row in blueprint.get("api_contract") or [] if isinstance(row, dict)]
        proxy_paths = sorted({
            "/" + path.strip("/").split("/", 1)[0]
            for row in api_rows
            if (path := str(row.get("collection_path") or row.get("path") or "")).startswith("/") and path != "/"
        })
        def scoped(owner: str) -> list[dict[str, Any]]:
            return [item for item in file_plan if isinstance(item, dict) and item.get("owner") == owner]
        return {
            "database": {**common, "database": blueprint.get("database"), "entities": blueprint.get("entities"), "ownership": ownership.get("database", []), "file_plan": scoped("database")},
            "backend": {**common, "backend": blueprint.get("backend"), "database": blueprint.get("database"), "entities": blueprint.get("entities"), "apis": blueprint.get("api_contract"), "dependencies": manifest.get("backend", {}), "ownership": ownership.get("backend", []), "file_plan": scoped("backend")},
            "frontend": {**common, "frontend": blueprint.get("frontend"), "entities": blueprint.get("entities"), "apis": blueprint.get("api_contract"), "proxy_paths": proxy_paths, "entrypoints": blueprint.get("entrypoints"), "dependencies": manifest.get("frontend", {}), "ownership": ownership.get("frontend", []), "file_plan": scoped("frontend")},
            "tester": {**common, "delivery_requirements": blueprint.get("delivery_requirements"), "entrypoints": blueprint.get("entrypoints")},
        }

    @staticmethod
    def _type_name(value: Any) -> str:
        words = re.findall(r"[A-Za-z0-9]+", str(value or "Entity"))
        return "".join(word[:1].upper() + word[1:] for word in words) or "Entity"

    @staticmethod
    def _snake_case(value: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()

    @staticmethod
    def _java_record(name: str, properties: dict[str, Any], entity: dict[str, Any]) -> str:
        fields = entity.get("fields") or {}
        values = []
        for field_name in properties:
            declaration = fields.get(field_name) if isinstance(fields, dict) else {}
            java_type = declaration.get("java_type", "String") if isinstance(declaration, dict) else str(declaration)
            values.append(f"    {java_type} {field_name}")
        return "\n".join([f"public record {name}Dto(", ",\n".join(values), ") {}"])

    @staticmethod
    def _typescript_interface(name: str, properties: dict[str, Any]) -> str:
        types = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}
        rows = [f"export interface {name} {{"]
        for field_name, declaration in properties.items():
            rows.append(f"  {field_name}: {types.get(declaration.get('type'), 'unknown')};")
        rows.append("}")
        return "\n".join(rows)
