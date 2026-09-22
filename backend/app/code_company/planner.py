"""Deterministic dynamic task planning for Code Company runs.

The planner computes an auditable plan but does not silently replace the
configured YAML graph.  This keeps workflow boundaries stable while allowing
the next scheduler to consume the same plan as a Dynamic TaskGraph.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class PlannedTask:
    task_id: str
    owner: str
    title: str
    depends_on: tuple[str, ...] = ()
    enabled: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DynamicPlan:
    plan_id: str
    mode: str
    tasks: tuple[PlannedTask, ...]
    skipped: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"planId": self.plan_id, "mode": self.mode, "tasks": [asdict(item) for item in self.tasks], "skipped": list(self.skipped)}


class DynamicPlanner:
    """Plan only the capabilities supported by the current requirement."""

    def plan(self, requirement: str, *, blueprint: dict[str, Any] | None = None, available_steps: Iterable[str] = ()) -> DynamicPlan:
        raw = str(requirement or "")
        lower = raw.lower()
        blueprint = blueprint or {}
        available = {str(item) for item in available_steps}
        if blueprint:
            has_backend = bool((blueprint.get("backend") or {}).get("required"))
            has_frontend = bool((blueprint.get("frontend") or {}).get("required"))
            has_database = bool((blueprint.get("database") or {}).get("mode") not in (None, "", "none"))
        else:
            has_backend = any(term in lower for term in ("backend", "后端", "spring", "api", "crud", "增删改查", "数据库"))
            has_frontend = any(term in lower for term in ("frontend", "前端", "vue", "html", "页面", "界面"))
            has_database = any(term in lower for term in ("database", "数据库", "mysql", "postgres", "h2", "持久化"))
        tasks: list[PlannedTask] = [PlannedTask("requirements", "requirement", "确认需求与验收标准")]
        if has_database:
            tasks.append(PlannedTask("database", "database", "设计数据库与迁移", ("requirements",), reason="冻结 Blueprint 要求持久化"))
        if has_backend:
            tasks.append(PlannedTask("backend", "backend", "实现后端接口与业务逻辑", ("requirements",), reason="冻结 Blueprint 要求 Backend"))
        if has_frontend:
            tasks.append(PlannedTask("frontend", "frontend", "实现前端页面与交互", ("requirements",), reason="冻结 Blueprint 要求 Frontend"))
        if has_backend or has_frontend:
            tasks.append(PlannedTask("tester", "tester", "执行构建、测试与启动验证", tuple(task.task_id for task in tasks if task.task_id != "tester"), reason="存在可执行代码产物"))
            tasks.append(PlannedTask("reviewer", "reviewer", "审查证据并形成交付结论", ("tester",), reason="验证完成后才允许交付"))
        else:
            tasks.append(PlannedTask("reviewer", "reviewer", "审查需求分析结果", ("requirements",), reason="未识别可执行代码能力"))
        if available:
            tasks = [task for task in tasks if task.owner in available or task.task_id == "requirements"]
        skipped = tuple(sorted({name for name in ("database", "backend", "frontend", "tester") if name not in {task.task_id for task in tasks}}))
        plan_id = "plan_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return DynamicPlan(plan_id, "capability_plan", tuple(tasks), skipped)
