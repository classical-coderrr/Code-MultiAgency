"""Deterministic requirement clarification gate.

Only this service may ask the user a scope question. Individual Agents are
given the resulting RequirementSpec/Blueprint and cannot invent a second,
conflicting clarification protocol.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .platform_contracts import ClarificationRequest, RequirementState, utc_now
from .requirement_gaps import RequirementGapAnalyzer
from .requirements import RequirementSpec


class RequirementClarificationService:
    VERSION = "2.0"

    def assess(self, spec: RequirementSpec) -> tuple[RequirementState, ClarificationRequest | None]:
        raw = spec.raw_requirement.strip()
        if not raw:
            return "USER_CLARIFICATION_REQUIRED", self._request(
                spec, ["requirement"], "未提供可执行的需求文本", "没有原始目标，无法安全选择工作流或交付边界。",
                [{"value": "补充需求", "label": "请描述目标、技术栈和交付物"}], {},
            )
        high_impact = [item for item in spec.gaps if isinstance(item, dict) and item.get("requires_user")]
        if high_impact:
            fields = [str(item.get("field")) for item in high_impact if item.get("field")]
            defaults = {
                str(item.get("field")): item.get("safe_default")
                for item in high_impact if item.get("field") and item.get("safe_default") is not None
            }
            has_entity_gap = "primary_entity" in fields
            entity_suggestion = str(defaults.get("primary_entity") or "")
            options = []
            if defaults and (not has_entity_gap or entity_suggestion):
                label = (f"采用平台建议：{entity_suggestion}（可改选）" if has_entity_gap
                         else "采用平台推荐值")
                options.append({"value": "use_recommended", "label": label})
            options.append({"value": "custom", "label": "自行填写管理对象" if has_entity_gap else "自定义缺失信息"})
            reference = (
                "你想管理的对象是什么？例如客房（Room）、预订（Booking）或订单（Order）。"
                "请选择平台建议，或填写自己的对象；平台建议不会自动生效。"
            ) if has_entity_gap else ""
            return "USER_CLARIFICATION_REQUIRED", self._request(
                spec,
                fields,
                "需求存在会改变数据模型、API 或交付结构的高影响歧义",
                "确认后才能冻结 Project Blueprint，避免不同 Agent 分别猜测。",
                options,
                defaults,
                reference,
            )
        # Explicit scope is sufficient. Missing minor details become recorded
        # assumptions instead of blocking every simple request.
        complete_stack_boundary = (
            spec.requested_stacks.get("backend") not in {None, "none", "unspecified"}
            and spec.requested_stacks.get("frontend") not in {None, "none", "unspecified"}
        )
        if spec.explicit_pure_frontend or spec.explicit_backend or complete_stack_boundary:
            return "ASSUMPTION_ALLOWED", None
        # Business/data signals without a declared boundary are high impact:
        # silently choosing static HTML was the source of earlier skipped
        # backend/frontend branches.
        if spec.business_data_signal or spec.crud_business_signal or spec.ambiguous:
            return "USER_CLARIFICATION_REQUIRED", self._request(
                spec,
                ["backend_required", "frontend_stack", "database_mode"],
                "需求包含业务数据或 CRUD，但尚未明确技术边界",
                "将影响是否保留 Backend、Frontend、数据库联调以及最终交付结构。",
                [
                    {"value": "full_stack_h2", "label": "保留前后端，使用 H2 进行本地联调"},
                    {"value": "backend_html", "label": "Spring Boot 后端 + HTML 页面"},
                    {"value": "frontend_only", "label": "仅前端，本地数据存储"},
                    {"value": "custom", "label": "自行填写技术边界"},
                ],
                {"backend_required": True, "frontend_stack": "html", "database_mode": "h2"},
                "请确认是否需要后端、前端技术栈以及数据存储方式；可采用平台推荐，也可逐项自定义。",
            )
        return "CLEAR", None

    def apply_answer(
        self,
        spec: RequirementSpec,
        request: ClarificationRequest,
        answers: dict[str, Any],
    ) -> tuple[RequirementSpec, ClarificationRequest, dict[str, Any]]:
        normalized = self._normalize_answers(request, answers)
        if normalized.get("option") == "use_recommended":
            if not any(item.get("value") == "use_recommended" for item in request.options):
                raise ValueError("当前需求没有可靠的推荐对象，请填写要管理的对象。")
            normalized.update(request.recommended_default)
        if "primary_entity" in request.unresolved_fields:
            value = str(normalized.get("primary_entity") or "").strip()
            match = re.search(r"[（(]([A-Za-z][A-Za-z0-9_]{0,63})[）)]", value)
            if match:
                value = match.group(1)
            value = RequirementGapAnalyzer._entity_aliases.get(value.lower() if value.isascii() else value, value)
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
                raise ValueError("请填写要管理的对象，例如 Room（客房）；英文名称只能包含字母、数字和下划线。")
            normalized["primary_entity"] = value
        defaults = dict(spec.safe_defaults)
        if normalized.get("primary_entity"):
            entity = str(normalized["primary_entity"])
            fields = dict(defaults.get("entity_fields") or {})
            fields[entity] = RequirementGapAnalyzer._default_fields(entity)
            defaults["entity_fields"] = fields
            defaults["api_style"] = "rest_collection_and_detail"
        defaults.update({
            key: normalized[key] for key in ("frontend_stack", "database_mode") if key in normalized
        })
        backend_required = bool(normalized.get("backend_required", spec.backend_required))
        pure_frontend = normalized.get("mode") == "frontend_only" or (
            normalized.get("backend_required") is False
        )
        updated = spec.model_copy(update={
            "backend_required": backend_required,
            "explicit_backend": bool(backend_required or spec.explicit_backend),
            "explicit_pure_frontend": bool(pure_frontend),
            "ambiguous": False,
            "needs_clarification": False,
            "state": "ASSUMPTION_ALLOWED",
            "assumptions": [*spec.assumptions, "用户已通过 Requirement Clarification Gate 确认交付边界"],
            "safe_defaults": defaults,
            "primary_entities": (
                [str(normalized["primary_entity"]), *[
                    name for name in spec.primary_entities if name != str(normalized["primary_entity"])
                ]]
                if normalized.get("primary_entity") else spec.primary_entities
            ),
            "support_status": "SUPPORTED_WITH_DEFAULTS",
            "impactful_gaps": [],
            "gaps": [
                {**item, "resolved": True, "resolved_value": normalized.get(str(item.get("field")))}
                for item in spec.gaps
            ],
        })
        answered = request.model_copy(update={"status": "ANSWERED", "answered_at": utc_now(), "answers": normalized})
        updates = {
            "clarification_answers": normalized,
            "requirement_state": "ASSUMPTION_ALLOWED",
            "assumption_log": [{"source": "user", "fields": normalized, "timestamp": utc_now()}],
        }
        return updated, answered, updates

    @staticmethod
    def _normalize_answers(request: ClarificationRequest, answers: dict[str, Any]) -> dict[str, Any]:
        payload = dict(answers or {})
        option = str(payload.get("option") or "").strip()
        if option == "full_stack_h2":
            payload.update({"backend_required": True, "frontend_stack": "html", "database_mode": "h2"})
        elif option == "backend_html":
            payload.update({"backend_required": True, "frontend_stack": "html", "database_mode": payload.get("database_mode") or "h2"})
        elif option == "frontend_only":
            payload.update({"backend_required": False, "frontend_stack": payload.get("frontend_stack") or "html", "database_mode": "none"})
        elif option == "custom":
            missing = [
                field for field in request.unresolved_fields
                if payload.get(field) is None or str(payload.get(field)).strip() == ""
            ]
            if missing:
                if missing == ["primary_entity"]:
                    raise ValueError("请填写要管理的对象。")
                raise ValueError("请补充全部待确认项：" + "、".join(missing))
        if "backend_required" in payload and not isinstance(payload["backend_required"], bool):
            normalized_bool = str(payload["backend_required"]).strip().lower()
            if normalized_bool not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError("backend_required 必须选择需要或不需要后端。")
            payload["backend_required"] = normalized_bool in {"true", "1", "yes"}
        allowed = set(request.unresolved_fields) | {"option", "mode", "primary_entity"}
        return {key: value for key, value in payload.items() if key in allowed}

    @staticmethod
    def _field_prompts(fields: list[str], default: dict[str, Any]) -> list[dict[str, Any]]:
        definitions: dict[str, dict[str, Any]] = {
            "requirement": {
                "label": "请补充完整目标、技术栈和期望交付物",
                "placeholder": "例如：开发酒店客房管理系统，Vue + Spring Boot + H2，支持客房增删改查",
                "type": "textarea",
            },
            "primary_entity": {
                "label": "你想管理的主要对象是什么？",
                "placeholder": "例如：客房（Room）、预订（Booking）或订单（Order）",
                "type": "text",
            },
            "backend_required": {
                "label": "是否需要真实后端 API？",
                "type": "select",
                "options": [
                    {"value": "true", "label": "需要后端"},
                    {"value": "false", "label": "不需要，仅前端本地存储"},
                ],
            },
            "frontend_stack": {
                "label": "前端技术栈",
                "type": "select",
                "options": [
                    {"value": "vue", "label": "Vue"},
                    {"value": "html", "label": "HTML/CSS/JavaScript"},
                ],
            },
            "database_mode": {
                "label": "数据存储方式",
                "type": "select",
                "options": [
                    {"value": "h2", "label": "H2（平台推荐的本地联调数据库）"},
                    {"value": "none", "label": "无独立数据库"},
                ],
            },
        }
        prompts: list[dict[str, Any]] = []
        for field in fields:
            item = {"field": field, **definitions.get(field, {
                "label": field,
                "placeholder": f"请输入 {field}",
                "type": "text",
            })}
            if field in default:
                item["recommended"] = default[field]
            prompts.append(item)
        return prompts

    @staticmethod
    def _request(
        spec: RequirementSpec,
        fields: list[str],
        reason: str,
        impact: str,
        options: list[dict[str, Any]],
        default: dict[str, Any],
        prompt_reference: str = "",
    ) -> ClarificationRequest:
        digest = hashlib.sha256(spec.raw_requirement.encode("utf-8")).hexdigest()[:16]
        return ClarificationRequest(
            request_id=f"clarify_{digest}",
            unresolved_fields=fields,
            reason=reason,
            impact=impact,
            options=options,
            recommended_default=default,
            prompt_reference=prompt_reference,
            field_prompts=RequirementClarificationService._field_prompts(fields, default),
        )
