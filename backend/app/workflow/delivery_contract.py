"""Versioned, run-scoped implementation contract, independent of model/provider."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .adaptive import infer_requirement_capabilities, requirement_explicitly_excludes


def canonical_backend_stack(value: Any) -> str:
    text = str(value or "none").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if compact in {"", "none", "null", "false", "nobackend", "static"}:
        return "none"
    if "springboot" in compact or compact in {"spring", "maven", "gradle"}:
        return "springboot"
    if any(name in compact for name in ("fastapi", "django", "flask")) or compact == "python":
        return "python"
    if any(name in compact for name in ("express", "nestjs", "nodejs")):
        return "node"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "none"


def canonical_frontend_stack(value: Any) -> str:
    text = str(value or "none").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if compact in {"", "none", "null", "false", "nofrontend"}:
        return "none"
    if "vue" in compact:
        return "vue"
    if "react" in compact:
        return "react"
    if any(name in compact for name in ("html", "javascript", "vanillajs", "static")):
        return "html"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "none"


class ApiContract(BaseModel):
    entity_id: str | None = None
    path: str = Field(pattern=r"^/[A-Za-z0-9_/{}/.-]+$", max_length=160)
    collection_path: str | None = None
    detail_path: str | None = None
    methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PUT", "DELETE"], max_length=8)
    fields: list[str] = Field(default_factory=list, max_length=32)
    payload: dict[str, Any] = Field(default_factory=dict)
    identity_field: str = "id"
    query_parameters: list[str] = Field(default_factory=list, max_length=16)
    detail_required: bool = False
    collection_response: str = "array"
    record_response: str = "json_object"

    @field_validator("identity_field", mode="before")
    @classmethod
    def normalize_identity_field(cls, value: Any) -> str:
        # Non-CRUD endpoints have no record identity. Providers commonly emit
        # explicit null; the canonical contract keeps the harmless default.
        if value is None or value == "":
            return "id"
        return value

    @field_validator("methods", mode="before")
    @classmethod
    def valid_methods(cls, value: Any) -> list[str]:
        """Normalize common provider variants without weakening the contract."""
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, (list, tuple, set)):
            raise ValueError("HTTP methods must be a string or list")
        result: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("HTTP method must be text")
            verbs = re.findall(
                r"(?:^|[\s,|/])(GET|POST|PUT|PATCH|DELETE)(?=$|[\s,|/])",
                item.upper(),
            )
            if not verbs:
                raise ValueError("Unsupported HTTP method")
            for verb in verbs:
                if verb not in result:
                    result.append(verb)
        if not set(result) <= {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported HTTP method")
        return result

    @field_validator("fields", mode="before")
    @classmethod
    def normalize_field_schema(cls, value: Any) -> Any:
        # JSON Schema style field:type maps and field-name lists are equivalent.
        return list(value) if isinstance(value, dict) else value

    @field_validator("query_parameters", mode="before")
    @classmethod
    def normalize_query_parameters(cls, value: Any) -> list[str]:
        """Accept the provider's common grouped-query shape.

        The contract is a flat list because downstream generators need one
        canonical representation.  Models sometimes return an object such as
        ``{"collection": ["keyword", "page"], "item": []}``; flatten it
        here instead of making every consumer understand provider-specific
        JSON shapes.
        """
        if value is None:
            return []
        if isinstance(value, dict):
            items: list[Any] = []
            for key, grouped in value.items():
                if isinstance(grouped, (list, tuple, set)):
                    items.extend(grouped)
                elif grouped not in (None, ""):
                    items.append(grouped)
                elif not items and key not in (None, ""):
                    # Preserve a meaningful group name only when no concrete
                    # parameter has been supplied by the model.
                    items.append(key)
        elif isinstance(value, str):
            items = re.split(r"[, \t\r\n|]+", value.strip()) if value.strip() else []
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            raise ValueError("query_parameters must be a list, string, or grouped object")
        result: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in result:
                result.append(text[:120])
        return result[:16]


class DeliveryContract(BaseModel):
    schema_version: str = "2.0"
    backend_stack: str
    frontend_stack: str
    page_mode: str
    database_mode: str
    production_database: str = "none"
    validation_database: str = "none"
    entrypoints: list[str] = Field(default_factory=list)
    api_contract: list[ApiContract] = Field(default_factory=list, max_length=16)
    required_capabilities: list[str] = Field(default_factory=list)
    crud_required: bool = False
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    assumptions: list[str] = Field(default_factory=list)
    requirement_hash: str
    browser_required: bool = True
    database_audit_required: bool = False
    ui_test_ids: dict[str, str] = Field(default_factory=lambda: {"add": "crud-add", "save": "crud-save", "row": "crud-row", "edit": "crud-edit", "delete": "crud-delete", "field": "crud-field-{fieldName}"})

    @field_validator("backend_stack", mode="before")
    @classmethod
    def normalize_backend_stack(cls, value: Any) -> str:
        return canonical_backend_stack(value)

    @field_validator("frontend_stack", mode="before")
    @classmethod
    def normalize_frontend_stack(cls, value: Any) -> str:
        return canonical_frontend_stack(value)


def contract_hash(contract: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _payload_type_hints(api_contracts: list[dict[str, Any]]) -> dict[str, str]:
    """Build conservative field-type hints from executable API payloads."""
    candidates: dict[str, set[str]] = {}
    for contract in api_contracts:
        payload = contract.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        identity = str(contract.get("identity_field") or "id").strip()
        if identity:
            candidates.setdefault(identity, set()).add("long")
        for name, value in payload.items():
            if isinstance(value, bool):
                kind = "boolean"
            elif isinstance(value, int):
                kind = "integer"
            elif isinstance(value, float):
                kind = "number"
            elif isinstance(value, str):
                kind = "string"
            else:
                continue
            candidates.setdefault(str(name), set()).add(kind)
    return {name: next(iter(kinds)) for name, kinds in candidates.items() if len(kinds) == 1}


def _field_list_to_mapping(
    value: Any,
    *,
    entity_name: str,
    type_hints: dict[str, str],
) -> dict[str, Any]:
    """Normalize common cross-provider entity field representations.

    API fields intentionally remain a list. Entity fields are canonicalized to
    ``{field_name: type_declaration}``. Unknown plain names stay untyped so the
    existing strict validator can reject them instead of silently guessing.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        value = [item.strip() for item in re.split(r"[,;\n]+", text) if item.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"实体合同 {entity_name}.fields 必须是字段名到类型的映射；"
            f"实际收到 {type(value).__name__}"
        )

    normalized: dict[str, Any] = {}
    for index, item in enumerate(value):
        name = ""
        declaration: Any = None
        if isinstance(item, str):
            text = item.strip()
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::|=|\(|\[)\s*([^\])]+)\s*[\])]?\s*$", text)
            if match:
                name, declaration = match.group(1), match.group(2).strip()
            else:
                name = text
        elif isinstance(item, dict):
            if len(item) == 1 and not any(
                key in item for key in ("name", "field", "field_name", "fieldName")
            ):
                name, declaration = next(iter(item.items()))
            else:
                name = str(
                    item.get("field_name")
                    or item.get("fieldName")
                    or item.get("field")
                    or item.get("name")
                    or ""
                ).strip()
                declaration = (
                    item.get("type")
                    or item.get("data_type")
                    or item.get("dataType")
                    or item.get("field_type")
                    or item.get("fieldType")
                )
                if item.get("java_type") and item.get("sql_type"):
                    declaration = {
                        key: item[key]
                        for key in ("java_type", "sql_type", "json_type", "nullable", "generated")
                        if key in item
                    }
        else:
            raise ValueError(
                f"实体合同 {entity_name}.fields[{index}] 必须是字段名或字段定义对象；"
                f"实际收到 {type(item).__name__}"
            )
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"实体合同 {entity_name}.fields[{index}] 缺少合法字段名")
        if declaration in (None, ""):
            declaration = type_hints.get(name)
            if declaration is None and name.lower() == "id":
                declaration = "long"
        normalized[name] = declaration
    return normalized


def _entity_contracts(
    entities: list[dict[str, Any]],
    api_contracts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    result = []
    type_hints = _payload_type_hints(api_contracts or [])
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise ValueError(
                f"实体合同 entities[{index}] 必须是对象；实际收到 {type(entity).__name__}"
            )
        entity_name = str(entity.get("name") or f"entities[{index}]")
        fields = _field_list_to_mapping(
            entity.get("fields") or {},
            entity_name=entity_name,
            type_hints=type_hints,
        )
        generated = set(entity.get("generated_fields") or [entity.get("identity_field", "id")])
        required = set(entity.get("required_create_fields") or (set(fields) - generated))
        normalized = {}
        for name, declaration in fields.items():
            if isinstance(declaration, dict) and declaration.get("java_type") and declaration.get("sql_type"):
                normalized[name] = declaration
                continue
            kind = str(declaration).lower()
            if "uuid" in kind:
                java, sql, wire = "UUID", "UUID", "string"
            elif "long" in kind or "bigint" in kind or (name in generated and "int" in kind):
                java, sql, wire = "Long", "BIGINT", "integer"
            elif "datetime" in kind or "timestamp" in kind:
                java, sql, wire = "LocalDateTime", "TIMESTAMP", "string"
            elif "date" in kind:
                java, sql, wire = "LocalDate", "DATE", "string"
            elif "bool" in kind:
                java, sql, wire = "Boolean", "BOOLEAN", "boolean"
            elif "decimal" in kind or "bigdecimal" in kind:
                java, sql, wire = "BigDecimal", "DECIMAL(19,2)", "number"
            elif any(item in kind for item in ("double", "float", "number")):
                java, sql, wire = "Double", "DOUBLE PRECISION", "number"
            elif "int" in kind:
                java, sql, wire = "Integer", "INTEGER", "integer"
            elif "str" in kind or "varchar" in kind or "text" in kind:
                java, sql, wire = "String", "VARCHAR(255)", "string"
            else:
                # These audit fields are conventional across Spring/JPA
                # projects.  Providers frequently emit them without a type;
                # infer only this closed, well-known set and keep rejecting
                # arbitrary unknown business fields.
                compact_name = re.sub(r"[^a-z0-9]", "", str(name).lower())
                if compact_name in {"createdat", "updatedat", "deletedat", "createdtime", "updatedtime"}:
                    java, sql, wire = "LocalDateTime", "TIMESTAMP", "string"
                else:
                    raise ValueError(f"实体字段 {name} 的类型未明确，请在架构节点确认")
            normalized[name] = {"java_type": java, "sql_type": sql, "json_type": wire, "nullable": name not in required and name not in generated, "generated": name in generated}
        result.append({**entity, "fields": normalized})
    return result


def build_delivery_contract(
    requirement: str,
    decision: dict[str, Any],
    blueprint: dict[str, Any] | None = None,
    requirement_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit user constraints override model suggestions; no mutable global state."""
    raw = requirement.lower()
    proposal = decision.get("delivery_contract") or {}
    backend = bool(decision.get("backend_required"))
    backend_stack = canonical_backend_stack(
        "springboot" if re.search(r"spring\s*boot", raw) else
        "python" if any(x in raw for x in ("fastapi", "django", "flask")) else
        proposal.get("backend_stack") or ("springboot" if backend else "none")
    )
    if not backend:
        backend_stack = "none"
    confirmed_defaults = (requirement_spec or {}).get("safe_defaults") or {}
    frontend_stack = canonical_frontend_stack(
        "vue" if "vue" in raw else
        "react" if "react" in raw else
        confirmed_defaults.get("frontend_stack") or proposal.get("frontend_stack") or "html"
    )
    if "html" in raw and "vue" not in raw and "react" not in raw:
        frontend_stack = "html"
    inferred = infer_requirement_capabilities(requirement)
    # The Architecture model can propose capabilities, but only the original
    # request can make one a mandatory delivery gate.
    capabilities = list(inferred["required_capabilities"])
    if backend and "backend" not in capabilities:
        capabilities.append("backend")
    normalized_spec = requirement_spec or {}
    explicit_no_crud = requirement_explicitly_excludes(requirement, ("CRUD", "CURD", "增删改查"))
    crud = bool(inferred["crud_requested"]) or (bool(proposal.get("crud_required")) and not explicit_no_crud)
    template = backend and "thymeleaf" in raw
    page_mode = "template" if template else "spa" if frontend_stack in {"vue", "react"} else "static_rest" if backend else "static"
    blueprint_database = (blueprint or {}).get("database") if isinstance(blueprint, dict) else None
    explicit_database = str(blueprint_database.get("mode") or "").strip() if isinstance(blueprint_database, dict) else ""
    user_database = (
        "mysql" if "mysql" in raw else
        "postgresql" if "postgres" in raw else
        "sqlite" if "sqlite" in raw else
        "h2" if re.search(r"\bh2\b", raw) else ""
    )
    # A model's suggested database is advisory; only the raw request or an
    # answer to the clarification gate can make it part of the contract.
    explicit_database = user_database or str(confirmed_defaults.get("database_mode") or "").strip()
    no_database = requirement_explicitly_excludes(
        requirement, ("数据库", "持久化", "数据存储", "database", "persistence", "storage"),
    )
    # A backend alone does not imply a database. CRUD/persistence receives an
    # explicit local H2 assumption for deterministic integration tests.
    database_mode = (
        "none" if no_database and "persistence" not in inferred["required_capabilities"]
        else explicit_database or (
            "h2" if (crud or "persistence" in capabilities) else "none"
        )
    )
    assumptions = []
    if backend and database_mode == "h2":
        assumptions.append("开发与联调默认 H2 内存数据库，不依赖外部数据库服务")
    if backend and not template and frontend_stack == "html":
        assumptions.append("HTML/CSS/JS 放入 Spring Boot static 资源目录，由 / 提供页面；使用同源 REST API")
    frontend_required = decision.get("project_type") != "backend_service" or "frontend" in capabilities
    if not frontend_required:
        frontend_stack = "none"
        page_mode = "none"
    proposed_entities = [item for item in (proposal.get("entities") or []) if isinstance(item, dict)]
    expected_entities = {str(item).lower() for item in normalized_spec.get("primary_entities") or []}
    discarded_entities: list[str] = []
    if crud and expected_entities:
        kept_entities = []
        for item in proposed_entities:
            name = str(item.get("id") or item.get("name") or "").strip()
            if name.lower() in expected_entities:
                kept_entities.append(item)
            else:
                discarded_entities.append(name or "<unnamed>")
        proposed_entities = kept_entities
    used_default_entities = False
    if crud and not proposed_entities:
        defaults = normalized_spec.get("safe_defaults") or {}
        field_defaults = defaults.get("entity_fields") or {}
        for name in normalized_spec.get("primary_entities") or []:
            fields = field_defaults.get(name) if isinstance(field_defaults, dict) else None
            if isinstance(fields, dict) and fields:
                proposed_entities.append({"name": str(name), "table": f"{str(name).lower()}s", "identity_field": "id", "fields": fields})
                used_default_entities = True
    api_rows: dict[str, dict[str, Any]] = {}
    for row in proposal.get("api_contract") or []:
        candidate = dict(row) if isinstance(row, dict) else {}
        explicit_entity = str(candidate.get("entity_id") or candidate.get("entity") or "").strip()
        if expected_entities and explicit_entity and explicit_entity.lower() not in expected_entities:
            continue
        matched_entity = _match_api_entity(candidate, proposed_entities, requirement)
        if used_default_entities and not explicit_entity and not matched_entity:
            # The previous model API may belong to the discarded entity. A
            # safe default must not silently relabel an unrelated route.
            continue
        candidate["entity_id"] = candidate.get("entity_id") or candidate.get("entity") or matched_entity
        parsed = ApiContract.model_validate(candidate).model_dump(mode="json")
        # Canonical CRUD describes a collection; item operations use /{id}.
        # Only normalize the standard identity suffix, never arbitrary routes.
        base = re.sub(r"/\{(?:id|" + re.escape(parsed["identity_field"]) + r")\}$", "", parsed["path"])
        # A bare collection path is an Architecture suggestion, not an explicit
        # user constraint. Use the platform's /api namespace before freezing
        # the contract; never rewrite a path the user named in the request.
        if (
            backend
            and re.fullmatch(r"/[A-Za-z][\w-]*", base)
            and parsed.get("collection_path") in (None, base)
            and parsed.get("detail_path") in (None, f"{base}/{{{parsed['identity_field']}}}")
            and not re.search(rf"(?<![\w-]){re.escape(base)}(?=$|[\s/，。；,;])", requirement)
        ):
            canonical = f"/api{base}"
            parsed["collection_path"] = canonical
            if parsed.get("detail_path") == f"{base}/{{{parsed['identity_field']}}}":
                parsed["detail_path"] = f"{canonical}/{{{parsed['identity_field']}}}"
            parsed["path"] = f"{canonical}{parsed['path'][len(base):]}"
            assumptions.append(f"未指定 API 路径，集合路径采用平台默认值 {canonical}")
            base = canonical
        parsed["collection_path"] = parsed.get("collection_path") or base
        parsed["detail_path"] = parsed.get("detail_path") or f"{base}/{{{parsed['identity_field']}}}"
        parsed["detail_required"] = bool(
            parsed["detail_required"]
            or (base != parsed["path"] and "GET" in parsed["methods"])
            or (crud and parsed.get("entity_id") and "GET" in parsed["methods"])
        )
        if base not in api_rows:
            api_rows[base] = {**parsed, "path": base}
        else:
            prior = api_rows[base]
            prior["methods"] = list(dict.fromkeys([*prior["methods"], *parsed["methods"]]))
            prior["fields"] = list(dict.fromkeys([*prior["fields"], *parsed["fields"]]))
            prior["query_parameters"] = list(dict.fromkeys([*prior["query_parameters"], *parsed["query_parameters"]]))
            prior["detail_required"] = prior["detail_required"] or parsed["detail_required"]
            prior["entity_id"] = prior.get("entity_id") or parsed.get("entity_id")
            if not prior["payload"]:
                prior["payload"] = parsed["payload"]
    if backend and crud and not api_rows and len(proposed_entities) == 1:
        entity_name = str(proposed_entities[0].get("name") or "Entity")
        field_mapping = proposed_entities[0].get("fields")
        if isinstance(field_mapping, dict):
            fields = list(field_mapping.keys())
            editable = [name for name in fields if name != str(proposed_entities[0].get("identity_field") or "id")]
            base = f"/api/{entity_name.lower()}s"
            api_rows[base] = ApiContract(
                entity_id=entity_name,
                path=base,
                collection_path=base,
                detail_path=f"{base}/{{id}}",
                methods=["GET", "POST", "PUT", "DELETE"],
                fields=fields,
                payload={name: _sample_payload_value(field_mapping.get(name)) for name in editable},
                identity_field="id",
                detail_required=True,
            ).model_dump(mode="json")
    if frontend_stack in {"vue", "react"} and backend and api_rows:
        proxy_paths = sorted({"/" + path.strip("/").split("/", 1)[0] for path in api_rows})
        assumptions.append(
            f"Vite proxy 的 {', '.join(proxy_paths)} target 使用 process.env.VITE_API_PROXY || "
            "'http://127.0.0.1:2198'；后端本地端口 2198，验证时通过环境变量注入隔离端口"
        )
    contract = DeliveryContract(
        backend_stack=backend_stack, frontend_stack=frontend_stack, page_mode=page_mode,
        database_mode=database_mode if backend else "none",
        production_database=database_mode if backend else "none",
        validation_database="h2" if backend and database_mode != "none" else "none",
        entrypoints=["/"] if frontend_required else [],
        api_contract=list(api_rows.values()),
        entities=_entity_contracts(proposed_entities, list(api_rows.values())),
        required_capabilities=capabilities, crud_required=crud,
        assumptions=assumptions,
        requirement_hash=hashlib.sha256(requirement.encode()).hexdigest(),
        browser_required=frontend_required,
        database_audit_required=backend and backend_stack == "springboot" and database_mode != "none",
    )
    # Unknown API is a visible contract gap, never guessed from a business noun.
    if backend and crud and not contract.api_contract:
        contract.assumptions.append("缺少 CRUD API/字段合同；交付门禁将阻止成功，需架构确认或修复")
    if discarded_entities:
        contract.assumptions.append("已忽略与原始需求实体不一致的架构草案：" + ", ".join(discarded_entities))
    return contract.model_dump(mode="json")


def _sample_payload_value(declaration: Any) -> Any:
    value = str(declaration or "").lower() if not isinstance(declaration, dict) else str(declaration.get("json_type") or declaration.get("java_type") or "").lower()
    if any(term in value for term in ("integer", "long", "short", "int")):
        return 1
    if any(term in value for term in ("decimal", "double", "float", "number", "bigdecimal")):
        return 1.0
    if "bool" in value:
        return True
    return "example"


def _match_api_entity(row: dict[str, Any], entities: list[dict[str, Any]], requirement: str = "") -> str | None:
    """Resolve a multi-entity route only when user scope uniquely names it.

    Ambiguous paths remain unbound and are rejected by ContractCompiler.
    This is not free-form URL guessing: both the exact collection route and
    its entity name must be present in the frozen user scope.
    """
    names = [str(item.get("id") or item.get("name") or "").strip() for item in entities]
    names = [name for name in names if name]
    collection = str(row.get("collection_path") or row.get("path") or "")
    collection = re.sub(r"/\{[^/]+\}$", "", collection).rstrip("/")
    if not collection or not re.search(rf"{re.escape(collection)}(?![\w/-])", requirement, re.IGNORECASE):
        return None
    route_noun = collection.rsplit("/", 1)[-1].lower()
    matches = []
    for name in names:
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", requirement, re.IGNORECASE):
            continue
        noun = re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower().replace("_", "-")
        plurals = {noun, noun + "s", noun + "es"}
        if noun.endswith("y"):
            plurals.add(noun[:-1] + "ies")
        if route_noun in plurals:
            matches.append(name)
    if len(matches) == 1:
        return matches[0]
    return None
