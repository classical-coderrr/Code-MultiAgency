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
