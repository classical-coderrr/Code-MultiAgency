"""Structured architecture decisions and safe adaptive run policies."""

from __future__ import annotations

import json
import re
from math import ceil
from typing import Any


PROJECT_TYPES = {"static_html", "web_app", "backend_service", "unknown"}
COMPLEXITIES = {"low", "medium", "high"}

# A runtime plan is a conservative recommendation, not a reason to make a
# normal Agent effectively unusable.  In particular, a low-confidence planner
# must not turn a source-file or structured-output step into a 600-token call.
# This is still below the normal workflow caps and does not force the model to
# consume the full amount.
RUNTIME_PLAN_MIN_AGENT_TOKENS = 1024

# Auto 模式的启动阶段预算不能再是一个对所有需求都相同的固定值。
# 这些是三档“建议值”，最终还会受到用户为 Agent 设置的上限、Provider
# 的 max output 和 context window 约束。Manual 模式不会使用这里的分配。
BOOTSTRAP_BUDGET_PROFILES = {
    "low": {
        "requirement": 1800,
        "architecture": 2200,
    },
    "medium": {
        "requirement": 3000,
        "architecture": 3600,
    },
    "high": {
        "requirement": 4600,
        "architecture": 5200,
    },
}

# 兼容旧调用方：它现在只作为没有可用需求文本时的保守回退，不作为
# 正常 Auto 运行的预算来源。
BOOTSTRAP_BUDGETS = {
    "requirement": BOOTSTRAP_BUDGET_PROFILES["medium"]["requirement"],
    "architecture": BOOTSTRAP_BUDGET_PROFILES["medium"]["architecture"],
}

_DIFFICULTY_GROUPS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    (
        "多模块或多页面",
        ("多页面", "多模块", "后台", "管理端", "dashboard", "admin", "multi-page", "microservice"),
        2,
    ),
    (
        "前后端协作范围",
        ("前后端", "全栈", "前端和后端", "frontend and backend", "full-stack", "full stack"),
        2,
    ),
    (
        "数据与外部集成",
        ("数据库", "api", "接口", "支付", "订单", "商品", "购物车", "购物网站", "电商", "第三方", "集成", "webhook", "database", "payment", "ecommerce", "integration"),
        2,
    ),
    (
        "权限与安全",
        ("登录", "注册", "权限", "角色", "鉴权", "安全", "审计", "oauth", "authentication", "authorization", "security"),
        2,
    ),
    (
        "实时与异步能力",
        ("实时", "消息", "通知", "队列", "并发", "流式", "websocket", "realtime", "streaming", "queue", "concurrency"),
        2,
    ),
    (
        "部署与运维约束",
        ("部署", "监控", "容灾", "高可用", "扩展", "性能", "发布", "deployment", "observability", "scalability"),
        1,
    ),
    (
        "质量与业务约束",
        ("企业级", "合规", "验收", "兼容", "国际化", "多租户", "测试", "enterprise", "compliance", "acceptance", "i18n", "multitenant"),
        1,
    ),
)

_EXPLICIT_HIGH_WORDS = ("复杂", "完整系统", "全方位", "企业级", "高并发", "complex", "production-ready")
_EXPLICIT_LOW_WORDS = ("简单", "静态", "单页", "原型", "demo", "landing page", "simple", "static")


def estimate_requirement_difficulty(requirement: str) -> dict[str, Any]:
    """Estimate startup difficulty before Architecture has produced JSON.

    This is deliberately a conservative preflight heuristic, not a replacement
    for the Architecture Agent. It only decides how much room Requirement and
    Architecture get before the first structured decision exists.
    """
    raw_value = str(requirement or "").strip().lower()
    value = re.sub(r"\s+", " ", raw_value)
    if not value:
        return {"level": "low", "score": 0, "signals": ["需求为空"], "estimated_chars": 0}

    score = 0
    signals: list[str] = []
    char_count = len(value)
    estimated_tokens = ceil(char_count / 4)

    if char_count >= 2400:
        score += 3
        signals.append("需求文本较长")
    elif char_count >= 1000:
        score += 2
        signals.append("需求文本中等偏长")
    elif char_count >= 500:
        score += 1
        signals.append("需求包含较多描述")

    for label, keywords, weight in _DIFFICULTY_GROUPS:
        if any(keyword in value for keyword in keywords):
            score += weight
            signals.append(label)

    if any(keyword in value for keyword in _EXPLICIT_HIGH_WORDS):
        score += 2
        signals.append("需求明确要求高复杂度")
    elif any(keyword in value for keyword in _EXPLICIT_LOW_WORDS) and score <= 3:
        score = max(0, score - 1)
        signals.append("需求明确为轻量任务")

    if raw_value.count("\n") >= 5 or len(re.findall(r"(?:^| )\d+[.)、]", value)) >= 4:
        score += 1
        signals.append("需求包含多个验收或约束项")

    level = "low" if score <= 2 else "medium" if score <= 5 else "high"
    if not signals:
        signals.append("基础需求")
    return {
        "level": level,
        "score": score,
        "signals": signals[:6],
        "estimated_chars": char_count,
        "estimated_tokens": estimated_tokens,
    }


def bootstrap_budget_for_step(step_id: str, requirement: str) -> tuple[int | None, dict[str, Any]]:
    """Return a difficulty-aware startup budget and its explanation."""
    assessment = estimate_requirement_difficulty(requirement)
    return BOOTSTRAP_BUDGET_PROFILES[assessment["level"]].get(step_id), assessment


def parse_runtime_budget_plan(
    text: str,
    allowed_step_ids: set[str],
    provider_max_tokens: int,
) -> dict[str, Any] | None:
    """Validate the small JSON contract produced by a Runtime Plan Agent."""
    raw = _extract_json_object(text)
    if not isinstance(raw, dict):
        return None

    difficulty = str(raw.get("difficulty", "")).strip().lower()
    model_strength = str(raw.get("model_strength", "")).strip().lower()
    if difficulty not in COMPLEXITIES or model_strength not in {"basic", "standard", "strong"}:
        return None

    raw_budgets = raw.get("agent_budgets")
    if not isinstance(raw_budgets, dict):
        return None
    safe_provider_max = max(256, int(provider_max_tokens))
    budgets: dict[str, int] = {}
    for step_id, value in raw_budgets.items():
        normalized_id = str(step_id).strip()
        if normalized_id not in allowed_step_ids:
            continue
        try:
            budget = int(value)
        except (TypeError, ValueError):
            continue
        budgets[normalized_id] = min(safe_provider_max, max(RUNTIME_PLAN_MIN_AGENT_TOKENS, budget))
    if not budgets:
        return None

    thinking: dict[str, str] = {}
    raw_thinking = raw.get("thinking")
    if isinstance(raw_thinking, dict):
        for step_id, value in raw_thinking.items():
            normalized_id = str(step_id).strip()
            level = str(value).strip().lower()
            if normalized_id in allowed_step_ids and level in {"off", "low", "high", "max"}:
                thinking[normalized_id] = level

    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "difficulty": difficulty,
        "model_strength": model_strength,
        "confidence": confidence,
        "agent_budgets": budgets,
        "thinking": thinking,
        "rationale": _string_list(raw.get("rationale"))[:5],
    }

POLICY_BUDGETS = {
    "static_html": {
        "backend": 500,
        "frontend": 3200,
        "tester": 1000,
        "reviewer": 1000,
    },
    "web_app_low": {
        "backend": 3000,
        "frontend": 4000,
        "database": 3600,
        "tester": 2200,
        "reviewer": 2200,
    },
    "web_app_medium": {
        "backend": 4500,
        "frontend": 5000,
        "database": 4500,
        "tester": 3000,
        "reviewer": 3000,
    },
    "web_app_high": {
        "backend": 6000,
        "frontend": 6000,
        "database": 6000,
        "tester": 4000,
        "reviewer": 4000,
    },
}


def build_local_runtime_budget_plan(
    requirement: str,
    allowed_step_ids: set[str],
    provider_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic plan when the optional planner response is unusable.

    Runtime planning is a helpful control-plane optimization, not a prerequisite
    for running the workflow.  A provider may spend the small planner response
    on reasoning or cut it at its output limit.  In that case this local plan
    preserves the same conservative, difficulty-aware behavior without another
    network call or a continuation request.
    """
    assessment = estimate_requirement_difficulty(requirement)
    level = str(assessment.get("level", "medium"))
    capability_spec = infer_requirement_capabilities(requirement)
    is_static = bool(capability_spec.get("display_only")) and not bool(
        capability_spec.get("backend_required")
    )
    policy_key = "static_html" if is_static else f"web_app_{level}"
    policy_budgets = POLICY_BUDGETS.get(policy_key, POLICY_BUDGETS["web_app_medium"])
    bootstrap = BOOTSTRAP_BUDGET_PROFILES.get(level, BOOTSTRAP_BUDGET_PROFILES["medium"])

    provider_limit = 6000
    raw_limit = (provider_capabilities or {}).get("maxTokens", 6000)
    if isinstance(raw_limit, dict):
        raw_limit = raw_limit.get("max", 6000)
    try:
        provider_limit = max(1, int(raw_limit))
    except (TypeError, ValueError):
        provider_limit = 6000

    def clamp(value: int) -> int:
        return min(provider_limit, max(RUNTIME_PLAN_MIN_AGENT_TOKENS, int(value)))

    budgets: dict[str, int] = {}
    for step_id in allowed_step_ids:
        if step_id == "token_estimator":
            continue
        if step_id in bootstrap:
            budgets[step_id] = clamp(int(bootstrap[step_id]))
        elif step_id in policy_budgets:
            budgets[step_id] = clamp(int(policy_budgets[step_id]))

    supports_thinking = bool((provider_capabilities or {}).get("supportsThinking", False))
    default_thinking = {
        "requirement": "low",
        "architecture": "low",
        "backend": "off",
        "frontend": "off",
        "tester": "low",
        "reviewer": "low",
    }
    thinking = {
        step_id: (default_thinking.get(step_id, "off") if supports_thinking else "off")
        for step_id in budgets
    }
    return {
        "difficulty": level if level in COMPLEXITIES else "medium",
        "model_strength": "standard",
        "confidence": 0.35,
        "agent_budgets": budgets,
        "thinking": thinking,
        "rationale": [
            "Runtime Plan 输出不完整，已切换本地难度预估",
            *list(assessment.get("signals", []))[:4],
        ],
        "source": "local_difficulty_preflight",
    }


def parse_architecture_decision(text: str) -> dict[str, Any] | None:
    """Parse the small JSON contract emitted by Architecture.

    A malformed decision intentionally returns None. The executor then keeps the
    conservative full workflow instead of guessing a route from prose.
    """
    raw = _extract_json_object(text)
    if not isinstance(raw, dict):
        return None

    project_type = str(raw.get("project_type", "")).strip().lower()
    complexity = str(raw.get("complexity", "")).strip().lower()
    backend_required = _parse_bool(raw.get("backend_required"))
    if project_type not in PROJECT_TYPES or complexity not in COMPLEXITIES or backend_required is None:
        return None

    result = {
        "project_type": project_type,
        "backend_required": backend_required,
        "complexity": complexity,
        "summary": _short_text(raw.get("summary")),
        "frontend_plan": _string_list(raw.get("frontend_plan")),
        "backend_reason": _short_text(raw.get("backend_reason")),
        "risks": _string_list(raw.get("risks")),
    }
    if "required_capabilities" in raw:
        result["required_capabilities"] = _capability_list(raw.get("required_capabilities"))
    if "optional_capabilities" in raw:
        result["optional_capabilities"] = _capability_list(raw.get("optional_capabilities"))
    if "evidence" in raw:
        result["evidence"] = _string_list(raw.get("evidence"))
    if "clarification_questions" in raw:
        result["clarification_questions"] = _string_list(raw.get("clarification_questions"))
    if "needs_clarification" in raw:
        result["needs_clarification"] = bool(_parse_bool(raw.get("needs_clarification")) or False)
    if isinstance(raw.get("delivery_contract"), dict):
        result["delivery_contract"] = raw["delivery_contract"]
    return result


_ROUTING_MANAGEMENT_TERMS = (
    "学生管理", "学生信息", "用户管理", "用户信息", "订单管理", "商品管理", "库存管理",
    "数据管理", "管理系统", "后台管理", "业务系统", "student management", "student system",
    "user management", "order management", "product management", "inventory management",
    "data management", "management system", "admin dashboard",
)
_ROUTING_CRUD_TERMS = (
    "增删改查", "增删改查功能", "crud", "curd", "create/read/update/delete",
    "create update delete", "create, read, update, delete", "新增", "添加", "编辑", "修改",
    "删除", "查询", "搜索", "list/add/edit/delete", "add/edit/delete", "add edit delete",
)
_ROUTING_BACKEND_TERMS = (
    "前后端", "前端和后端", "前端与后端", "后端", "服务端", "接口", "api", "数据库",
    "sqlite", "mysql", "postgres", "rest", "backend", "server", "server-side",
)
_ROUTING_PURE_FRONTEND_TERMS = (
    "纯前端", "无需后端", "不需要后端", "不用后端", "无后端", "仅前端", "只做前端", "只用前端",
    "localstorage", "local storage", "浏览器本地存储", "前端 demo", "前端演示",
    "frontend only", "frontend-only", "client-side only", "no backend", "without backend",
)
_ROUTING_DISPLAY_ONLY_TERMS = (
    "广告页", "广告页面", "落地页", "宣传页", "展示页", "展示页面", "静态页面", "静态 html",
    "单文件 html", "单页面 html", "作品展示", "作品集", "landing page", "static html",
    "single html", "display-only", "display only", "showcase page",
)
_ROUTING_UI_TERMS = (
    "网页", "网站", "页面", "前端", "界面", "用户界面", "可视化", "html", "css", "javascript",
    "web page", "website", "frontend", "ui", "user interface", "dashboard",
)
_ROUTING_PERSISTENCE_TERMS = (
    "数据库", "数据存储", "数据保存", "持久化", "存储", "sqlite", "mysql", "postgres", "mongodb",
    "database", "persistence", "persist", "storage", "save data", "records",
)
_ROUTING_AUTH_TERMS = (
    "登录", "注册", "鉴权", "认证", "授权", "权限", "角色", "多用户", "login", "sign in",
    "signup", "authentication", "authorization", "permission", "rbac", "multi-user",
)
_ROUTING_EXTERNAL_API_TERMS = (
    "第三方", "外部服务", "外部 api", "接口集成", "webhook", "支付", "地图", "短信", "api 集成",
    "third-party", "external api", "api integration", "webhook", "payment", "integration",
)
_ROUTING_TOOL_TERMS = (
    "工具调用", "外部工具", "插件", "mcp", "tool", "tools", "plugin", "function calling",
)
_ROUTING_RAG_TERMS = (
    "知识库", "知识检索", "rag", "向量库", "文档检索", "knowledge base", "retrieval", "vector database",
)
_ROUTING_ARTIFACT_TERMS = (
    "源码", "代码", "文件", "产物", "下载", "生成 html", "生成网页", "source code", "artifact",
    "file", "download", "generate html", "generate code", "write code", "implement",
)
_ROUTING_APPROVAL_TERMS = (
    "人工确认", "人工审批", "审批", "审核", "人工批准", "human approval", "approval", "review gate",
)
_ROUTING_ASYNC_TERMS = (
    "实时", "消息", "通知", "队列", "异步", "并发", "定时任务", "websocket", "realtime", "queue",
    "async", "concurrency", "scheduled job",
)
_ROUTING_BUSINESS_APP_TERMS = (
    "系统", "应用", "平台", "后台", "管理", "业务", "system", "application", "app", "platform",
    "admin", "business", "crm", "erp", "cms",
)


_NEGATION_PREFIXES = ("无需", "不需要", "不用", "不使用", "不调用", "不接", "不依赖", "无", "不要", "不做", "不包含", "不要求", "禁止")
_ENGLISH_NEGATION_PREFIXES = ("no", "without", "do not use", "don't use", "not use", "not require", "not required")


def _positive_probe(raw: str, terms: tuple[str, ...]) -> tuple[str, str]:
    """Remove explicit negated occurrences before capability matching.

    Keyword routing must treat ``无需数据库`` and ``不做 CRUD`` as negative
    evidence. Removing only ``无需后端`` makes a static request contradictory
    as soon as it explicitly lists the capabilities it does not want.
    """
    probe = raw
    for term in sorted(terms, key=len, reverse=True):
        escaped = re.escape(term.lower())
        chinese_prefixes = "|".join(re.escape(prefix) for prefix in _NEGATION_PREFIXES)
        probe = re.sub(
            rf"(?:{chinese_prefixes})(?:任何)?(?:独立|外部|第三方)?\s*{escaped}",
            " ", probe, flags=re.IGNORECASE,
        )
        # Coordinated exclusions such as "无需登录或外部服务" exclude both
        # capabilities. Do not interpret the second item as a new request.
        probe = re.sub(
            rf"(?:{chinese_prefixes})[^。；;\n]{{0,20}}?(?:或|和|与|、)\s*{escaped}",
            " ", probe, flags=re.IGNORECASE,
        )
        if re.search(r"[a-z]", term.lower()):
            prefixes = "|".join(re.escape(prefix) for prefix in _ENGLISH_NEGATION_PREFIXES)
            probe = re.sub(
                rf"(?:{prefixes})\s+(?:an?\s+)?(?:independent\s+)?{escaped}",
                " ",
                probe,
                flags=re.IGNORECASE,
            )
    return probe, re.sub(r"[\s\-_]+", "", probe)


def _has_positive_signal(raw: str, terms: tuple[str, ...]) -> bool:
    probe, probe_compact = _positive_probe(raw, terms)
    for term in terms:
        normalized = term.lower()
        if re.search(r"[a-z]", normalized):
            if re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", probe):
                return True
        elif normalized in probe_compact:
            return True
    return False


def requirement_explicitly_excludes(requirement: str, terms: tuple[str, ...]) -> bool:
    """Return whether the user negated one of the supplied capabilities."""
    raw = str(requirement or "").lower()
    chinese_prefixes = "|".join(re.escape(prefix) for prefix in _NEGATION_PREFIXES)
    english_prefixes = "|".join(re.escape(prefix) for prefix in _ENGLISH_NEGATION_PREFIXES)
    for term in terms:
        escaped = re.escape(term.lower())
        if re.search(rf"(?:{chinese_prefixes})(?:任何)?(?:独立)?\s*{escaped}", raw):
            return True
        if re.search(rf"(?:{english_prefixes})\s+(?:an?\s+)?(?:independent\s+)?{escaped}", raw):
            return True
    return False


def _has_positive_backend_signal(raw: str, compact: str) -> bool:
    """Match Backend intent without treating explicitly rejected capabilities as requirements."""
    del compact  # retained in the signature for compatibility with older callers
    return _has_positive_signal(raw, _ROUTING_BACKEND_TERMS)


def infer_requirement_capabilities(requirement: str) -> dict[str, Any]:
    """Extract capability requirements without binding routing to an industry.

    The result is a conservative requirement specification. It is not a
    replacement for Architecture: it tells the executor which capabilities
    must not be silently removed and which parts of the scope remain unclear.
    """
    raw = str(requirement or "").strip().lower()
    compact = re.sub(r"[\s\-_]+", "", raw)

    def contains(term: str) -> bool:
        normalized = term.lower()
        return normalized in compact if not re.search(r"[a-z]", normalized) else normalized in raw

    def has_any(terms: tuple[str, ...]) -> bool:
        return any(contains(term) for term in terms)

    has_management = has_any(_ROUTING_MANAGEMENT_TERMS)
    has_crud = _has_positive_signal(raw, _ROUTING_CRUD_TERMS)
    explicit_backend = _has_positive_backend_signal(raw, compact)
    explicit_pure_frontend = has_any(_ROUTING_PURE_FRONTEND_TERMS)
    display_only = has_any(_ROUTING_DISPLAY_ONLY_TERMS)
    has_business_app = has_any(_ROUTING_BUSINESS_APP_TERMS)
    persistence = _has_positive_signal(raw, _ROUTING_PERSISTENCE_TERMS)
    authentication = _has_positive_signal(raw, _ROUTING_AUTH_TERMS)
    external_api = _has_positive_signal(raw, _ROUTING_EXTERNAL_API_TERMS)
    tool = _has_positive_signal(raw, _ROUTING_TOOL_TERMS)
    rag = _has_positive_signal(raw, _ROUTING_RAG_TERMS)
    artifact = _has_positive_signal(raw, _ROUTING_ARTIFACT_TERMS)
    approval = _has_positive_signal(raw, _ROUTING_APPROVAL_TERMS)
    async_capability = _has_positive_signal(raw, _ROUTING_ASYNC_TERMS)

    crud_business_signal = has_crud and (has_management or has_business_app)
    business_data_signal = has_management or crud_business_signal or persistence or authentication or external_api
    backend_required = explicit_backend or business_data_signal
    if explicit_pure_frontend and not explicit_backend:
        backend_required = False

    required: list[str] = []
    optional: list[str] = []
    evidence: list[str] = []

    if has_any(_ROUTING_UI_TERMS) or display_only:
        required.append("frontend")
    if backend_required:
        required.append("backend")
    if not explicit_pure_frontend and (persistence or crud_business_signal):
        required.append("persistence")
    if authentication:
        required.append("authentication")
    if external_api:
        required.append("external_api")
    if tool:
        required.append("tool")
    if rag:
        required.append("rag")
    if artifact:
        required.append("artifact")
    if approval:
        required.append("human_approval")
    if async_capability:
        required.append("async")

    if display_only and not backend_required:
        optional.append("artifact")
    if has_crud and not persistence and not explicit_pure_frontend:
        optional.append("persistence")
    if not required:
        optional.append("clarification")

    if has_management:
        evidence.append("检测到业务/数据管理语义")
    if has_crud:
        evidence.append("检测到 CRUD 或增删改查操作")
    if explicit_backend:
        evidence.append("用户明确提出 Backend/API/数据库能力")
    if persistence:
        evidence.append("检测到数据持久化要求")
    if authentication:
        evidence.append("检测到身份认证或权限要求")
    if external_api:
        evidence.append("检测到外部 API 或第三方集成要求")
    if explicit_pure_frontend:
        evidence.append("用户明确允许纯前端或本地存储")
    if display_only:
        evidence.append("检测到纯展示页面语义")

    ambiguous = bool(
        (business_data_signal or has_crud or has_business_app)
        and not explicit_backend
        and not explicit_pure_frontend
        and not display_only
    )
    if explicit_backend or persistence or authentication or external_api:
        confidence = "high"
    elif business_data_signal or explicit_pure_frontend or display_only:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "required_capabilities": list(dict.fromkeys(required)),
        "optional_capabilities": list(dict.fromkeys(optional)),
        "evidence": evidence[:8],
        "backend_required": backend_required,
        "business_data_signal": business_data_signal,
        "crud_business_signal": crud_business_signal,
        "crud_requested": has_crud,
        "explicit_backend": explicit_backend,
        "explicit_pure_frontend": explicit_pure_frontend,
        "display_only": display_only,
        "ambiguous": ambiguous,
        "confidence": confidence,
    }


def infer_requirement_routing(requirement: str) -> dict[str, Any]:
    """Infer non-negotiable workflow branches from the user's original request.

    Architecture remains responsible for project design, but it must not be the
    only authority for safety-sensitive routing. In particular, a business/data
    management request is not equivalent to a display-only HTML page merely
    because the user did not spell out an API or database.
    """
    raw = str(requirement or "").strip().lower()
    compact = re.sub(r"[\s\-_]+", "", raw)

    def contains(term: str) -> bool:
        normalized = term.lower()
        return normalized in compact if not re.search(r"[a-z]", normalized) else normalized in raw

    matched: list[str] = []
    has_management = any(contains(term) for term in _ROUTING_MANAGEMENT_TERMS)
    has_crud = _has_positive_signal(raw, _ROUTING_CRUD_TERMS)
    explicit_backend = _has_positive_backend_signal(raw, compact)
    explicit_pure_frontend = any(contains(term) for term in _ROUTING_PURE_FRONTEND_TERMS)

    if has_management:
        matched.append("业务/数据管理")
    if has_crud:
        matched.append("CRUD 操作")
    if explicit_backend:
        matched.append("明确的 Backend/API/数据库")
    if explicit_pure_frontend:
        matched.append("明确的纯前端或本地存储")

    # A named management system, or CRUD/data operations over a named domain,
    # are business signals. They require a server branch by default. The user
    # can opt out explicitly for a browser-only demo/localStorage prototype.
    capability_spec = infer_requirement_capabilities(requirement)
    business_data_signal = has_management or (has_crud and explicit_backend) or bool(capability_spec["business_data_signal"])
    crud_management_signal = has_management and has_crud
    explicit_backend = explicit_backend or bool(capability_spec["explicit_backend"])
    explicit_pure_frontend = explicit_pure_frontend or bool(capability_spec["explicit_pure_frontend"])
    backend_required = explicit_backend or business_data_signal
    if explicit_pure_frontend and not explicit_backend:
        backend_required = False

    ambiguous = bool(capability_spec.get("ambiguous", False) or (
        (business_data_signal or has_crud)
        and not explicit_backend
        and not explicit_pure_frontend
    ))
    if backend_required and explicit_backend:
        reason = "原始需求明确包含前后端、接口、数据库或服务端能力。"
    elif backend_required:
        reason = "原始需求包含业务数据管理或 CRUD；未明确纯前端实现时，默认保留 Backend。"
    elif explicit_pure_frontend:
        reason = "原始需求明确允许纯前端或浏览器本地存储，可按静态/前端方案执行。"
    elif ambiguous:
        reason = "原始需求范围不明确，保留可能需要的实现分支，并交由人工确认。"
    else:
        reason = "未检测到必须保留 Backend 的确定性信号，继续参考 Architecture 判断。"

    return {
        "backend_required": backend_required,
        "business_data_signal": business_data_signal,
        "crud_management_signal": crud_management_signal,
        "explicit_backend": explicit_backend,
        "explicit_pure_frontend": explicit_pure_frontend,
        "ambiguous": ambiguous,
        "matched_signals": matched,
        "reason": reason,
        "capability_spec": capability_spec,
    }


def reconcile_architecture_decision(
    decision: Any,
    requirement: str,
    routing_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconcile model routing with deterministic signals from the raw request.

    This is intentionally a narrow routing guard. It does not invent product
    features or rewrite the architecture plan; it only prevents a false
    ``static_html/backend_required=false`` decision from deleting a required
    implementation branch.
    """
    if not isinstance(decision, dict) or not decision:
        return None

    corrected = dict(decision)
    routing = routing_override if isinstance(routing_override, dict) else infer_requirement_routing(requirement)
    raw_capability_spec = routing.get("capability_spec", {})
    capability_spec = raw_capability_spec if isinstance(raw_capability_spec, dict) else {}
    original_type = str(corrected.get("project_type", "unknown"))
    original_backend_required = bool(corrected.get("backend_required", True))
    correction_reason = ""
    correction_applied = False

    if routing["explicit_pure_frontend"] and not routing["explicit_backend"] and original_backend_required:
        corrected["project_type"] = "static_html"
        corrected["backend_required"] = False
        correction_applied = True
        correction_reason = "用户明确要求纯前端或本地存储，已移除不必要的 Backend 分支。"
    elif routing["backend_required"] and not routing["explicit_pure_frontend"]:
        if original_type == "static_html" or not original_backend_required:
            corrected["project_type"] = "web_app"
            corrected["backend_required"] = True
            correction_applied = True
            correction_reason = (
                "原始需求检测到业务数据管理/CRUD 信号，已阻止静态页面策略跳过 Backend；"
                "Backend 与 Frontend 均保留，具体实现范围仍以用户需求为准。"
            )
    elif (
        original_type == "static_html"
        and routing["ambiguous"]
        and not routing["explicit_pure_frontend"]
    ):
        # A vague request must not silently lose an implementation branch. The
        # approval step can still confirm the scope before expensive work.
        corrected["project_type"] = "web_app"
        corrected["backend_required"] = True
        correction_applied = True
        correction_reason = "原始需求范围不明确，已保留 Backend 与 Frontend，避免静默跳过可能需要的实现能力。"
    elif (
        original_type == "web_app"
        and not original_backend_required
        and not routing["explicit_pure_frontend"]
    ):
        # Only static HTML may omit the backend by default. A web_app decision
        # with backend_required=false is internally inconsistent and unsafe.
        corrected["backend_required"] = True
        correction_applied = True
        correction_reason = "Architecture 返回的 Web 应用不能同时声明无需 Backend，已按安全默认值保留。"

    if corrected.get("backend_required") and corrected.get("project_type") == "static_html":
        corrected["project_type"] = "web_app"
        correction_applied = True
        correction_reason = correction_reason or "项目声明需要 Backend，项目类型已统一为 Web 应用。"

    if correction_applied:
        corrected["backend_reason"] = correction_reason[:500]

    # Required capabilities are sourced from the raw user request. The model
    # may suggest extras, but a suggestion must not silently become a hard
    # delivery gate (especially when the user explicitly excluded it).
    proposed_capabilities = _capability_list(corrected.get("required_capabilities"))
    corrected["required_capabilities"] = list(dict.fromkeys(
        capability_spec.get("required_capabilities", [])
    ))
    if corrected.get("backend_required") and "backend" not in corrected["required_capabilities"]:
        corrected["required_capabilities"].insert(0, "backend")
    corrected["optional_capabilities"] = list(dict.fromkeys(
        [capability for capability in proposed_capabilities if capability not in corrected["required_capabilities"]]
        + _capability_list(corrected.get("optional_capabilities"))
        + list(capability_spec.get("optional_capabilities", []))
    ))
    corrected["evidence"] = list(dict.fromkeys(
        _string_list(corrected.get("evidence"))
        + list(capability_spec.get("evidence", []))
    ))[:10]
    corrected["needs_clarification"] = bool(
        corrected.get("needs_clarification", False) or routing["ambiguous"]
    )
    corrected["clarification_questions"] = _string_list(corrected.get("clarification_questions"))
    if routing["ambiguous"] and not corrected["clarification_questions"]:
        corrected["clarification_questions"] = [
            "是否需要真实 Backend/API/数据库，还是明确采用纯前端本地存储？"
        ]

    corrected["routing_guard"] = {
        "correction_applied": correction_applied,
        "original_project_type": original_type,
        "original_backend_required": original_backend_required,
        "backend_required_by_rules": routing["backend_required"],
        "explicit_backend": routing["explicit_backend"],
        "explicit_pure_frontend": routing["explicit_pure_frontend"],
        "ambiguous": routing["ambiguous"],
        "matched_signals": routing["matched_signals"],
        "capability_spec": capability_spec,
        "reason": correction_reason or routing["reason"],
    }
    return corrected


def build_adaptive_policy(decision: Any) -> dict[str, Any]:
    # Keep a malformed Provider response from breaking a workflow recovery
    # path. The explicit Architecture fallback remains the normal route.
    if not isinstance(decision, dict):
        decision = {
            "project_type": "web_app",
            "complexity": "medium",
            "backend_required": True,
            "required_capabilities": ["frontend", "backend"],
            "needs_clarification": True,
            "routing_guard": {"reason": "Architecture 输出不是对象，保留完整实现分支"},
        }
    project_type = str(decision.get("project_type", "unknown"))
    complexity = str(decision.get("complexity", "medium"))
    backend_required = bool(decision.get("backend_required", True))
    required_capabilities = _capability_list(decision.get("required_capabilities"))
    backend_capabilities = {"backend", "persistence", "authentication", "external_api", "async", "multi_user"}
    routing_guard = decision.get("routing_guard") if isinstance(decision.get("routing_guard"), dict) else {}
    explicit_pure_frontend = bool(
        routing_guard.get("explicit_pure_frontend", False)
        or decision.get("explicit_pure_frontend", False)
    )
    explicit_backend = bool(routing_guard.get("explicit_backend", False) or decision.get("explicit_backend", False))
    if not (explicit_pure_frontend and not explicit_backend) and backend_capabilities.intersection(required_capabilities):
        backend_required = True
    if project_type == "backend_service":
        backend_required = True
    elif project_type == "static_html" and backend_required:
        # Keep the decision and policy consistent even when callers provide a
        # model decision without going through reconcile_architecture_decision.
        project_type = "web_app"
    elif project_type != "static_html" and not backend_required:
        # Only an explicitly static route may omit Backend. Unknown or
        # internally inconsistent routes use the safe full implementation path.
        backend_required = True
    if project_type == "static_html":
        budgets = dict(POLICY_BUDGETS["static_html"])
        thinking = {"backend": "off", "frontend": "low", "database": "off", "tester": "off", "reviewer": "off"}
    else:
        key = f"web_app_{complexity}" if complexity in {"low", "medium", "high"} else "web_app_medium"
        budgets = dict(POLICY_BUDGETS[key])
        thinking = {"backend": "low", "frontend": "low", "database": "off", "tester": "low", "reviewer": "low"}

    persistence_required = bool(
        {"persistence", "database"}.intersection(required_capabilities)
    )
    database_required = persistence_required and not (
        explicit_pure_frontend and not explicit_backend
    )
    skip_steps: list[str] = []
    skip_reasons: dict[str, str] = {}
    if not backend_required:
        skip_steps.append("backend")
        skip_reasons["backend"] = str(
            routing_guard.get("reason")
            or "Architecture 判定为静态展示路线，未检测到必须保留 Backend 的能力。"
        )
    if not database_required:
        skip_steps.append("database")
        skip_reasons["database"] = (
            "原始需求未检测到数据库、持久化或 CRUD 业务数据能力，已跳过数据库设计 Agent。"
        )

    return {
        "project_type": project_type,
        "backend_required": backend_required,
        "complexity": complexity,
        "budgets": budgets,
        "thinking": thinking,
        "skip_steps": skip_steps,
        "route_mode": "full_implementation" if backend_required else "static_frontend",
        "skip_reasons": skip_reasons,
        "required_capabilities": required_capabilities,
        "needs_clarification": bool(decision.get("needs_clarification", False)),
        "routing_guard": dict(decision.get("routing_guard", {})) if isinstance(decision.get("routing_guard"), dict) else {},
    }


def _extract_json_object(text: str) -> Any:
    value = str(text or "").strip()
    candidates: list[str] = []
    if "```" in value:
        for block in value.split("```")[1::2]:
            candidate = block.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].lstrip()
            candidates.append(candidate)
    start = value.find("{")
    if start >= 0:
        candidates.append(value[start:])

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _short_text(value: Any) -> str:
    return str(value or "").strip()[:500]


def _capability_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        normalized = re.sub(r"\s+", "_", str(item or "").strip().lower())
        if normalized and re.fullmatch(r"[a-z0-9_.:-]{2,64}", normalized):
            values.append(normalized)
    return list(dict.fromkeys(values))[:16]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:200] for item in value if str(item).strip()][:8]
