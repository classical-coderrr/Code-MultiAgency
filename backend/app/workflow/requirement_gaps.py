"""Deterministic requirement normalization and gap classification.

The service deliberately avoids an LLM.  It identifies only facts that are
safe to derive from explicit user text and records every default so downstream
agents never need to reinterpret an incomplete request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RequirementGap:
    field: str
    severity: str
    reason: str
    requires_user: bool
    safe_default: Any = None
    affected_capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "severity": self.severity,
            "reason": self.reason,
            "requires_user": self.requires_user,
            "safe_default": self.safe_default,
            "affected_capabilities": list(self.affected_capabilities),
        }


class RequirementGapAnalyzer:
    """Classify architectural gaps and provide conservative safe defaults."""

    VERSION = "2.0"
    _entity_aliases = {
        "学生": "Student", "student": "Student",
        "用户": "User", "user": "User",
        "商品": "Product", "产品": "Product", "product": "Product",
        "订单": "Order", "order": "Order",
        "图书": "Book", "书籍": "Book", "book": "Book",
        "课程": "Course", "course": "Course",
        "员工": "Employee", "employee": "Employee",
        "客户": "Customer", "customer": "Customer",
        "库存": "Inventory", "inventory": "Inventory",
        "客房": "Room", "room": "Room",
    }
    _ambiguous_domains = ("购物网站", "商城", "电商", "shop", "shopping", "e-commerce", "ecommerce")

    def analyze(self, raw: str, inferred: dict[str, Any]) -> dict[str, Any]:
        text = str(raw or "").strip()
        lower = text.lower()
        entities = self._entities(text)
        crud = bool(inferred.get("crud_requested") or inferred.get("crud_business_signal"))
        gaps: list[RequirementGap] = []
        defaults: dict[str, Any] = {
            "package_name": "com.example.app",
            "backend_port": 2198,
            "page_size": 20,
            "exception_format": "problem_details",
        }

        if crud and (not entities or any(term in lower for term in self._ambiguous_domains) and len(entities) != 1):
            suggestion = self._suggest_primary_entity(lower)
            gaps.append(RequirementGap(
                "primary_entity", "high",
                "CRUD 的主要业务对象不明确，会改变数据库表、API 与页面字段。",
                True, suggestion, ("persistence", "backend", "frontend"),
            ))

        backend_required = bool(inferred.get("backend_required"))
        pure_frontend = bool(inferred.get("explicit_pure_frontend"))
        database_explicit = any(term in lower for term in ("h2", "mysql", "postgres", "postgresql", "sqlite", "数据库", "database"))
        if backend_required and crud and not database_explicit:
            defaults.update({"production_database": "h2", "validation_database": "h2"})
            gaps.append(RequirementGap(
                "database_mode", "low",
                "需求需要持久化但未指定数据库，使用可离线联调的 H2。",
                False, "h2", ("persistence",),
            ))
        elif not pure_frontend:
            defaults.setdefault("validation_database", "h2")

        requested_stacks = self._stacks(lower)
        explicit_fields = self._explicit_entity_fields(text, entities)
        if entities:
            defaults["entity_fields"] = {name: self._default_fields(name) for name in entities}
            for name, fields in explicit_fields.items():
                defaults["entity_fields"].setdefault(name, {}).update(fields)
            defaults["api_style"] = "rest_collection_and_detail"
        capability_profile = {
            "ui": bool("frontend" in inferred.get("required_capabilities", []) or not backend_required),
            "backend_api": backend_required,
            "persistence": bool(crud and not pure_frontend),
            "authentication": any(term in lower for term in ("登录", "认证", "权限", "auth", "login")),
            "file_upload": any(term in lower for term in ("上传", "upload")),
            "external_api": any(term in lower for term in ("第三方接口", "外部接口", "external api")),
            "async_processing": any(term in lower for term in ("异步任务", "消息队列", "kafka", "rabbitmq")),
            "search": any(term in lower for term in ("搜索", "查询", "筛选", "search", "filter")),
        }
        high = [item for item in gaps if item.requires_user]
        support = "CLARIFICATION_REQUIRED" if high else "SUPPORTED_WITH_DEFAULTS" if gaps else "SUPPORTED"
        return {
            "gaps": [item.as_dict() for item in gaps],
            "safe_defaults": defaults,
            "primary_entities": entities,
            "entity_evidence": self._entity_evidence(text, entities),
            "explicit_entity_fields": explicit_fields,
            "explicit_constraints": self._explicit_constraints(text),
            "explicit_api_paths": self._explicit_api_paths(text),
            "requested_stacks": requested_stacks,
            "capability_profile": capability_profile,
            "impactful_gaps": [item.field for item in high],
            "support_status": support,
        }

    def _entity_evidence(self, text: str, entities: list[str]) -> list[dict[str, str]]:
        """Attach auditable raw-request evidence to each routed entity."""
        evidence: list[dict[str, str]] = []
        for entity in entities:
            aliases = [alias for alias, canonical in self._entity_aliases.items() if canonical == entity]
            aliases.append(entity)
            matched: str | None = None
            for alias in aliases:
                if alias.isascii():
                    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?:s|es)?(?![A-Za-z0-9_])", text, re.IGNORECASE)
                    if match:
                        matched = match.group(0)
                        break
                else:
                    offset = text.find(alias)
                    if offset >= 0:
                        matched = alias
                        break
            evidence.append({
                "entity": entity,
                "source": "raw_requirement",
                "text": matched or entity,
            })
        return evidence

    @staticmethod
    def _explicit_entity_fields(text: str, entities: list[str]) -> dict[str, dict[str, str]]:
        """Extract only field/type pairs explicitly declared beside an entity."""
        type_map = {
            "string": "string", "str": "string", "varchar": "string",
            "long": "long", "bigint": "long", "int": "integer", "integer": "integer",
            "bigdecimal": "decimal", "decimal": "decimal", "double": "number", "float": "number",
            "boolean": "boolean", "bool": "boolean", "localdate": "date", "date": "date",
            "localdatetime": "datetime", "datetime": "datetime", "timestamp": "datetime",
            "uuid": "uuid",
        }
        result: dict[str, dict[str, str]] = {}
        for entity in entities:
            # Match either “Room 实体字段 id(Long), name(String)” or a
            # compact declaration such as “Room(id: Long, name: String)”.
            escaped = re.escape(entity)
            patterns = (
                rf"(?<![A-Za-z0-9_]){escaped}\s*(?:实体字段|fields?)\s*[:：]?\s*([^。；;\n]+)",
                rf"(?<![A-Za-z0-9_]){escaped}\s*\(([^()]{{1,1200}})\)",
            )
            declaration = ""
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    declaration = match.group(1)
                    break
            fields: dict[str, str] = {}
            for match in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*([A-Za-z][A-Za-z0-9_.]*)\s*\)|:\s*([A-Za-z][A-Za-z0-9_.]*))",
                declaration,
            ):
                field_name = match.group(1)
                raw_type = (match.group(2) or match.group(3) or "").rsplit(".", 1)[-1].lower()
                field_type = type_map.get(raw_type)
                if field_type:
                    fields[field_name] = field_type
            if fields:
                result[entity] = fields
        return result

    @staticmethod
    def _explicit_constraints(text: str) -> list[str]:
        """Retain explicit must/only/forbidden clauses for downstream review."""
        required = re.compile(
            r"必须|不得|不允许|不能|只能|仅限|唯一|至少|最多|must(?: not)?|should(?: not)?|cannot|only|unique|at least|at most",
            re.IGNORECASE,
        )
        fragments = re.split(r"(?<=[。！？!?；;])\s*|[\r\n]+", text)
        return list(dict.fromkeys(
            fragment.strip()[:1000] for fragment in fragments
            if fragment.strip() and required.search(fragment)
        ))[:32]

    @staticmethod
    def _default_fields(entity: str) -> dict[str, str]:
        templates = {
            "Student": {"id": "long", "name": "string", "studentNumber": "string", "email": "string"},
            "Product": {"id": "long", "name": "string", "price": "decimal", "stock": "integer", "description": "string"},
            "User": {"id": "long", "name": "string", "email": "string"},
            "Order": {"id": "long", "status": "string", "total": "decimal"},
            "Book": {"id": "long", "title": "string", "author": "string", "isbn": "string"},
            "Room": {"id": "long", "roomNumber": "string", "type": "string", "price": "decimal", "status": "string"},
        }
        return dict(templates.get(entity) or {"id": "long", "name": "string"})

    @staticmethod
    def _suggest_primary_entity(lower: str) -> str | None:
        # Suggestions are displayed to the user; they are never silently applied.
        if any(term in lower for term in ("酒店", "旅馆", "hotel")):
            return "Room"
        if any(term in lower for term in RequirementGapAnalyzer._ambiguous_domains):
            return "Product"
        return None

    def _entities(self, text: str) -> list[str]:
        lower = text.lower()
        result: list[str] = []
        for alias, canonical in self._entity_aliases.items():
            if canonical == "User":
                matched = self._user_entity_is_explicit(text, alias)
            elif canonical == "Inventory":
                matched = self._inventory_entity_is_explicit(text, alias)
            else:
                matched = (
                    re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?:s|es)?(?![A-Za-z])", lower) is not None
                    if alias.isascii() else alias in text
                )
            if matched and canonical not in result:
                result.append(canonical)
        # Explicit entity declarations are stronger evidence than the
        # built-in domain aliases. They must survive unchanged so a second
        # entity such as Booking is not discarded from the frozen contract.
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]{1,63})\s*(?:实体(?:字段)?|[Ee]ntity\s+[Ff]ields?)",
            text,
        ):
            entity = match.group(1)
            if entity not in result:
                result.append(entity)
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]*)\s*\(([^()]{1,800})\)", text):
            declarations = match.group(2)
            if re.search(
                r"\b[A-Za-z_][A-Za-z0-9_]*\s*(?::\s*|=\s*|\(\s*)(?:String|Long|Integer|Int|BigDecimal|Decimal|Boolean|LocalDate|LocalDateTime|UUID)",
                declarations,
                re.IGNORECASE,
            ):
                entity = match.group(1)
                if entity not in result:
                    result.append(entity)
        # Generic Chinese management-domain extraction keeps the router
        # extensible without hard-coding every future business noun.
        for match in re.finditer(r"([\u4e00-\u9fff]{1,8})(?:信息)?管理(?:系统|平台|网站)?", text):
            noun = match.group(1)
            noun = re.sub(r"^(?:开发|实现|创建|生成|一个|简单的|前后端的)+", "", noun)
            if noun and not any(alias in noun for alias in ("系统", "平台", "网站")):
                canonical = self._entity_aliases.get(noun)
                if canonical and canonical not in result:
                    result.append(canonical)
        return result[:32]

    @staticmethod
    def _user_entity_is_explicit(text: str, alias: str) -> bool:
        """Do not turn generic prose (e.g. a user entering an amount) into a DB entity."""
        if alias.isascii():
            return any(re.search(pattern, text, re.IGNORECASE) for pattern in (
                r"\busers?\s+(?:management|profiles?|records?|entities|tables|accounts?|crud|lists?|data)\b",
                r"\b(?:manage|create|update|delete|edit|query|list|crud)\s+(?:the\s+)?users?\b",
                r"\bcrud\s+(?:for|of)\s+users?\b",
            ))
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in (
            r"用户(?:管理|信息|资料|数据|列表|表|实体|档案|账户|帐号|账号|CRUD|增删改查|注册)",
            r"(?:管理|维护|新增|创建|编辑|修改|删除|查询|检索|增删改查|CRUD)(?:\s*的)?用户",
            r"用户的(?:姓名|邮箱|账号|帐号|角色|权限|资料|档案)",
        ))

    @staticmethod
    def _inventory_entity_is_explicit(text: str, alias: str) -> bool:
        if alias.isascii():
            return any(re.search(pattern, text, re.IGNORECASE) for pattern in (
                r"\binventory\s+(?:management|records?|tables?|entities|crud|lists?|data)\b",
                r"\b(?:manage|create|update|delete|edit|query|list|crud)\s+(?:the\s+)?inventory\b",
                r"\bcrud\s+(?:for|of)\s+inventory\b",
            ))
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in (
            r"库存(?:管理|记录|列表|信息|数据|CRUD|增删改查|表|实体|模块)",
            r"(?:管理|维护|增删改查|CRUD)(?:库存)",
        ))

    @staticmethod
    def _explicit_api_paths(text: str) -> list[str]:
        """Preserve exact API paths explicitly named by the user; never infer routes."""
        paths = re.findall(r"(?<![\w.-])(/api/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_{}-]+)*)", text, re.IGNORECASE)
        return list(dict.fromkeys(paths))[:32]

    @staticmethod
    def _stacks(lower: str) -> dict[str, str]:
        backend = "springboot" if re.search(r"spring\s*boot", lower) else "fastapi" if "fastapi" in lower else "none"
        frontend = "vue" if "vue" in lower else "react" if "react" in lower else "html" if "html" in lower else "unspecified"
        database = "mysql" if "mysql" in lower else "postgresql" if "postgres" in lower else "sqlite" if "sqlite" in lower else "h2" if "h2" in lower else "unspecified"
        return {"backend": backend, "frontend": frontend, "database": database}
