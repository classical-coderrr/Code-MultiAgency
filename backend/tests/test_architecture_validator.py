import json

from app.workflow.architecture_validator import ArchitectureValidator
from app.workflow.capability_router import CapabilityRouter


def test_validator_corrects_static_html_for_crud_requirement():
    decision = {
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "low",
        "summary": "Student CRUD page",
        "frontend_plan": [],
        "backend_reason": "Use localStorage",
        "risks": [],
    }
    requirement = CapabilityRouter().route("生成学生管理系统，要求增删改查")

    validated = ArchitectureValidator().validate(decision, requirement)

    assert validated is not None
    assert validated.project_type == "web_app"
    assert validated.backend_required is True
    assert validated.routing_guard["correction_applied"] is True
    assert "backend" in validated.required_capabilities


def test_validator_keeps_model_contract_and_adds_capability_fields():
    raw = json.dumps({
        "project_type": "static_html",
        "backend_required": False,
        "complexity": "low",
        "summary": "A display-only advertisement page.",
        "frontend_plan": ["HTML"],
        "backend_reason": "No server needed.",
        "risks": [],
    })
    validator = ArchitectureValidator()
    requirement = CapabilityRouter().route("制作一个简单的 HTML 广告落地页")

    validated = validator.validate(validator.parse(raw), requirement)

    assert validated is not None
    assert validated.project_type == "static_html"
    assert validated.backend_required is False
    assert "frontend" in validated.required_capabilities
    assert validated.routing_guard["correction_applied"] is False

