"""State passing and strict template rendering."""

from __future__ import annotations

import json
import re
from typing import Any


class UndefinedWorkflowVariable(ValueError):
    pass


class WorkflowContext:
    """A run-scoped key/value store shared by steps through the executor.

    Agent code receives rendered prompt text, never the context object itself.
    This keeps agent execution decoupled from orchestration state.
    """

    _pattern = re.compile(r"{{\s*([a-zA-Z_][\w.-]*)\s*}}")

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(initial or {})

    def set(self, key: str, value: Any) -> None:
        # 成功的 Step 或明确的策略跳过分支才会调用 set，失败分支不会静默写入 null。
        self._values[key] = value

    def delete(self, key: str) -> None:
        """Remove a stale output before a failed step is retried."""
        self._values.pop(key, None)

    def get(self, key: str) -> Any:
        if key not in self._values:
            raise UndefinedWorkflowVariable(f"Undefined workflow variable: {key}")
        return self._values[key]

    def render(self, template: str) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = self.get(key)
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, indent=2)
            return str(value)

        return self._pattern.sub(replace, template)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._values)
