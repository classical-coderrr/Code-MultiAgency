import asyncio

import httpx
import pytest

from app.workflow.adaptive import parse_architecture_decision
from app.workflow.context import WorkflowContext
from app.workflow.context_packer import ContextPacker
from app.workflow.delivery_contract import ApiContract, build_delivery_contract, contract_hash
from app.workflow.artifact_validator import ArtifactValidator, _SpringCrudSpec


def test_api_methods_accept_route_qualified_provider_output():
    contract = ApiContract.model_validate({
        "path": "/api/students",
        "methods": [
            "GET /api/students",
            "POST /api/students",
            "PUT /api/students/{id}",
            "DELETE /api/students/{id}",
        ],
    })

    assert contract.methods == ["GET", "POST", "PUT", "DELETE"]


def test_api_methods_accept_compact_text_but_reject_unknown_verbs():
    assert ApiContract.model_validate({"path": "/api/items", "methods": "GET, POST"}).methods == ["GET", "POST"]
    with pytest.raises(ValueError, match="Unsupported HTTP method"):
        ApiContract.model_validate({"path": "/api/items", "methods": ["TRACE /api/items"]})


def test_query_parameters_accept_grouped_provider_shape():
    contract = ApiContract.model_validate({
        "path": "/api/items",
        "query_parameters": {"collection": ["keyword", "page", "size"], "item": []},
    })
    assert contract.query_parameters == ["keyword", "page", "size"]


def test_non_crud_api_accepts_null_identity_field_without_changing_endpoint():
    contract = ApiContract.model_validate({
        "path": "/api/convert",
        "methods": ["POST"],
        "identity_field": None,
        "payload": {"amount": 12, "from": "USD", "to": "CNY"},
    })
    assert contract.identity_field == "id"
    assert contract.path == "/api/convert"
    assert contract.methods == ["POST"]


def test_explicit_stack_and_page_override_model():
    contract = build_delivery_contract("Spring Boot + HTML 学生 CRUD", {
        "backend_required": True, "delivery_contract": {"frontend_stack": "vue", "backend_stack": "python"},
    })
    assert contract["backend_stack"] == "springboot"
    assert contract["frontend_stack"] == "html"
    assert contract["page_mode"] == "static_rest"
    assert contract["database_mode"] == "h2"
    assert contract["crud_required"]
    assert "缺少" in contract["assumptions"][-1]


def test_new_requirement_has_distinct_contract():
    a = build_delivery_contract("Spring Boot + Vue 学生 CRUD", {"backend_required": True})
    b = build_delivery_contract("纯 HTML 广告页", {"backend_required": False})
    assert a["page_mode"] == "spa"
    assert b["page_mode"] == "static"
    assert contract_hash(a) != contract_hash(b)


def test_descriptive_model_stack_is_canonicalized_before_contract_freeze():
    contract = build_delivery_contract(
        "开发一个糖果广告网站，前端展示糖果信息，后端管理员可以增删改查这些糖果",
        {
            "project_type": "web_app",
            "backend_required": True,
            "delivery_contract": {
                "backend_stack": "Spring Boot + Spring Data JPA + H2",
                "frontend_stack": "HTML/CSS/JavaScript，static/index.html，同源 REST",
                "crud_required": True,
                "entities": [{"name": "Candy", "fields": {"id": "Long", "name": "String"}}],
                "api_contract": [{
                    "entity_id": "Candy",
                    "path": "/api/candies",
                    "methods": ["GET", "POST", "PUT", "DELETE"],
                    "fields": ["id", "name"],
                    "payload": {"name": "草莓软糖"},
                }],
            },
        },
    )

    assert contract["backend_stack"] == "springboot"
    assert contract["frontend_stack"] == "html"
    assert contract["page_mode"] == "static_rest"
    assert contract["database_audit_required"] is True


def test_explicit_no_database_and_no_crud_override_model_proposal():
    contract = build_delivery_contract(
        "开发 Spring Boot REST API 和 HTML 汇率换算网页，明确不使用数据库、不需要持久化、不做 CRUD。",
        {
            "project_type": "web_app",
            "backend_required": True,
            "required_capabilities": ["backend", "frontend"],
            "delivery_contract": {
                "database_mode": "h2",
                "database_required": True,
                "crud_required": True,
                "api_contract": [{"path": "/api/convert", "methods": ["POST"], "identity_field": None}],
            },
        },
    )

    assert contract["backend_stack"] == "springboot"
    assert contract["database_mode"] == "none"
    assert contract["crud_required"] is False
    assert contract["database_audit_required"] is False
    assert not any("默认 H2" in item for item in contract["assumptions"])
    assert contract["api_contract"][0]["path"] == "/api/convert"


def test_model_suggested_database_is_not_made_mandatory_without_user_scope():
    contract = build_delivery_contract(
        "开发 Spring Boot 与 HTML 汇率换算页面，提供 POST /api/convert 接口。",
        {"project_type": "web_app", "backend_required": True,
         "delivery_contract": {"database_mode": "mysql", "database_required": True,
                               "required_capabilities": ["persistence"]}},
    )
    assert contract["database_mode"] == "none"
    assert contract["database_audit_required"] is False


def test_entity_types_pin_database_and_java():
    contract = build_delivery_contract("Spring Boot CRUD", {"backend_required": True, "delivery_contract": {"entities": [{"name": "Book", "fields": {"id": "integer", "copies": "integer", "title": "string", "externalId": "UUID"}}]}})
    fields = contract["entities"][0]["fields"]
    assert fields["id"]["java_type"] == "Long"
    assert fields["id"]["sql_type"] == "BIGINT"
    assert fields["copies"]["java_type"] == "Integer"
    assert fields["externalId"]["java_type"] == "UUID"
    assert fields["title"]["sql_type"] == "VARCHAR(255)"


def test_entity_field_definition_list_is_normalized_to_mapping():
    contract = build_delivery_contract("Spring Boot CRUD", {
        "backend_required": True,
        "delivery_contract": {
            "api_contract": [{
                "path": "/api/students",
                "methods": ["GET", "POST", "PUT", "DELETE"],
                "fields": ["id", "name", "email"],
                "payload": {"name": "张三", "email": "zhangsan@example.com"},
            }],
            "entities": [{
                "name": "Student",
                "fields": [
                    {"name": "id", "type": "Long"},
                    {"field": "name", "data_type": "String"},
                    {"email": "String"},
                ],
            }],
        },
    })

    fields = contract["entities"][0]["fields"]
    assert fields["id"]["java_type"] == "Long"
    assert fields["name"]["sql_type"] == "VARCHAR(255)"
    assert fields["email"]["json_type"] == "string"


def test_plain_entity_field_names_use_api_payload_type_hints():
    contract = build_delivery_contract("Spring Boot CRUD", {
        "backend_required": True,
        "delivery_contract": {
            "api_contract": [{
                "path": "/api/students",
                "methods": ["GET", "POST", "PUT", "DELETE"],
                "fields": ["id", "name", "active"],
                "payload": {"name": "张三", "active": True},
            }],
            "entities": [{"name": "Student", "fields": ["id", "name", "active"]}],
        },
    })

    fields = contract["entities"][0]["fields"]
    assert fields["id"]["java_type"] == "Long"
    assert fields["name"]["java_type"] == "String"
    assert fields["active"]["java_type"] == "Boolean"


def test_unknown_plain_entity_field_still_requires_an_explicit_type():
    with pytest.raises(ValueError, match="类型未明确"):
        build_delivery_contract("Spring Boot CRUD", {
            "backend_required": True,
            "delivery_contract": {
                "entities": [{"name": "Book", "fields": ["customBusinessValue"]}],
            },
        })


def test_invalid_entity_fields_reports_json_location_and_actual_type():
    with pytest.raises(ValueError, match=r"Book\.fields.*int"):
        build_delivery_contract("Spring Boot CRUD", {
            "backend_required": True,
            "delivery_contract": {"entities": [{"name": "Book", "fields": 42}]},
        })


def test_common_audit_timestamp_field_is_normalized_when_provider_omits_type():
    contract = build_delivery_contract("Spring Boot CRUD", {
        "backend_required": True,
        "delivery_contract": {"entities": [{"name": "Order", "fields": {"id": "long", "createdAt": None}}]},
    })
    assert contract["entities"][0]["fields"]["createdAt"]["java_type"] == "LocalDateTime"


def test_unrecognized_type_cannot_be_silently_invented():
    with pytest.raises(ValueError, match="类型未明确"):
        build_delivery_contract("Spring Boot CRUD", {"backend_required": True, "delivery_contract": {"entities": [{"name": "Book", "fields": {"custom": "unknown"}}]}})


def test_architecture_keeps_api_contract():
    parsed = parse_architecture_decision('{"project_type":"web_app","complexity":"low","backend_required":true,"delivery_contract":{"api_contract":[{"path":"/api/books","payload":{"title":"book"}}]}}')
    assert parsed["delivery_contract"]["api_contract"][0]["path"] == "/api/books"


def test_bare_architecture_route_uses_api_prefix_before_freezing():
    contract = build_delivery_contract("Spring Boot + Vue 商品 CRUD", {
        "backend_required": True,
        "delivery_contract": {"api_contract": [{
            "entity_id": "Product", "path": "/products",
            "methods": ["GET", "POST", "PUT", "DELETE"],
        }]},
    })
    route = contract["api_contract"][0]
    assert route["collection_path"] == "/api/products"
    assert route["detail_path"] == "/api/products/{id}"
    assert any("/api/products" in item for item in contract["assumptions"])


def test_user_explicit_route_is_not_rewritten():
    contract = build_delivery_contract("Spring Boot + Vue 商品 CRUD，接口路径使用 /products", {
        "backend_required": True,
        "delivery_contract": {"api_contract": [{
            "entity_id": "Product", "path": "/products",
            "methods": ["GET", "POST", "PUT", "DELETE"],
        }]},
    })
    route = contract["api_contract"][0]
    assert route["collection_path"] == "/products"
    assert route["detail_path"] == "/products/{id}"
    assert any("Vite proxy 的 /products" in item for item in contract["assumptions"])


def test_field_map_and_item_endpoints_normalize_without_api_drift():
    contract = build_delivery_contract("Spring Boot HTML CRUD", {"backend_required": True, "delivery_contract": {"api_contract": [
        {"path": "/api/books", "methods": ["GET", "POST"], "fields": {"id": "long", "title": "string"}, "payload": {"title": "Book"}},
        {"path": "/api/books/{id}", "methods": ["GET", "PUT", "DELETE"], "fields": {"id": "long", "title": "string"}, "payload": {"title": "Updated"}},
    ]}})
    api = contract["api_contract"][0]
    assert len(contract["api_contract"]) == 1
    assert set(api["methods"]) == {"GET", "POST", "PUT", "DELETE"}
    assert api["payload"] == {"title": "Book"}
    assert api["fields"] == ["id", "title"]
    assert api["detail_required"]


def test_required_contract_never_compacted():
    context = WorkflowContext({"requirement": "原始需求", "delivery_contract": "a" * 1800, "report": "b" * 6000})
    packet = ContextPacker().pack("{{requirement}}{{delivery_contract}}{{report}}", context, 650, preserve_keys=("requirement", "delivery_contract"))
    assert "a" * 1800 in packet.text
    assert "delivery_contract" not in packet.truncated_keys
    with pytest.raises(ValueError, match="输入预算"):
        ContextPacker().pack("{{delivery_contract}}", context, 100, preserve_keys=("delivery_contract",))


def test_api_health_does_not_prove_page_or_asset():
    async def run():
        def handler(request):
            if request.url.path == "/":
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            checks = await ArtifactValidator._probe_pages(client, "http://localhost", ("/",), "backend")
        assert checks[0].status == "failed"
    asyncio.run(run())


def test_asset_html_fallback_fails():
    async def run():
        def handler(request):
            return httpx.Response(200, text='<html><script src="/app.js"></script></html>', headers={"content-type": "text/html"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            checks = await ArtifactValidator._probe_pages(client, "http://localhost", ("/",), "backend")
        assert checks[0].status == "passed"
        assert checks[1].status == "failed"
    asyncio.run(run())


def test_custom_record_id_and_real_update():
    async def run():
        records = []
        def handler(request):
            import json
            if request.method == "POST":
                row = {**json.loads(request.content), "bookId": 9}
                records.append(row)
                return httpx.Response(201, json=row)
            if request.method == "GET":
                return httpx.Response(200, json=records)
            if request.method == "PUT":
                records[0].update(json.loads(request.content))
                return httpx.Response(200, json=records[0])
            records.clear()
            return httpx.Response(204)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await ArtifactValidator._probe_spring_crud(client, "http://localhost", _SpringCrudSpec("/api/books", {"title": "Book"}, "title", frozenset({"GET", "POST", "PUT", "DELETE"}), "bookId"))
        assert result.passed if hasattr(result, "passed") else result.status == "passed"
        assert result.evidence["fields"] == ["bookId", "title"]
    asyncio.run(run())
