from app.workflow.capability_router import CapabilityRouter


def test_router_returns_versioned_requirement_spec_for_business_app():
    spec = CapabilityRouter().route("开发一个 CRM 应用，支持新增、编辑、删除和搜索客户")

    assert spec.schema_version == "1.0"
    assert spec.router_version == "1.0"
    assert spec.raw_requirement.endswith("搜索客户")
    assert spec.backend_required is True
    assert "backend" in spec.required_capabilities
    assert "persistence" in spec.required_capabilities
    assert spec.needs_clarification is True


def test_router_accepts_explicit_browser_only_scope():
    spec = CapabilityRouter().route("做一个纯前端学生 CRUD demo，使用 localStorage，不需要后端")

    assert spec.backend_required is False
    assert spec.explicit_pure_frontend is True
    assert spec.explicit_backend is False
    assert spec.needs_clarification is False


def test_router_respects_explicitly_negated_backend_database_and_login():
    spec = CapabilityRouter().route(
        "开发响应式咖啡宣传网页，使用纯 HTML、CSS 和原生 JavaScript；"
        "明确无需后端、无需数据库、无需登录，可直接打开 index.html。"
    )

    assert spec.backend_required is False
    assert spec.explicit_backend is False
    assert spec.explicit_pure_frontend is True
    assert "backend" not in spec.required_capabilities
    assert "persistence" not in spec.required_capabilities
    assert "authentication" not in spec.required_capabilities


def test_router_respects_negation_scope_with_qualified_final_item():
    spec = CapabilityRouter().route(
        "做一个可直接打开的咖啡店宣传单页，使用纯 HTML/CSS/JavaScript。"
        "不需要后端、数据库、登录或外部 API，交付完整源码。"
    )

    assert spec.backend_required is False
    assert spec.explicit_backend is False
    assert spec.explicit_pure_frontend is True
    assert "backend" not in spec.required_capabilities
    assert "external_api" not in spec.required_capabilities


def test_router_keeps_backend_but_respects_no_database_and_no_crud():
    spec = CapabilityRouter().route(
        "开发 Spring Boot REST API 和原生 HTML 前端的汇率换算系统，"
        "明确不使用数据库、不需要持久化、不做 CRUD。"
    )

    assert spec.backend_required is True
    assert spec.explicit_backend is True
    assert spec.crud_business_signal is False
    assert "backend" in spec.required_capabilities
    assert "persistence" not in spec.required_capabilities


def test_router_does_not_mistake_generic_user_input_for_user_entity_and_keeps_named_route():
    spec = CapabilityRouter().route(
        "开发汇率计算演示：Spring Boot REST 后端和原生 HTML/CSS/JavaScript，"
        "固定汇率由 /api/convert 提供；用户输入金额和币种，不接外部 API、"
        "不使用数据库、不做 CRUD。"
    )

    assert spec.primary_entities == []
    assert spec.explicit_api_paths == ["/api/convert"]
    assert spec.backend_required is True


def test_router_still_recognizes_user_as_an_entity_when_user_records_are_managed():
    spec = CapabilityRouter().route("Build a user management CRUD system with account records")

    assert "User" in spec.primary_entities
