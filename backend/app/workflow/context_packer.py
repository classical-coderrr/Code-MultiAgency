"""Context selection and deterministic compaction for workflow prompts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .context import WorkflowContext


@dataclass(frozen=True, slots=True)
class ContextPacket:
    text: str
    input_tokens: int
    included_keys: tuple[str, ...]
    truncated_keys: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "includedKeys": list(self.included_keys),
            "truncatedKeys": list(self.truncated_keys),
            "warnings": list(self.warnings),
        }


class ContextPacker:
    """Render only referenced state and compact oversized values predictably."""

    _pattern = re.compile(r"{{\s*([a-zA-Z_][\w.-]*)\s*}}")

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        return str(value)

    @staticmethod
    def _compact(value: str, target_chars: int) -> str:
        if len(value) <= target_chars:
            return value
        target_chars = max(160, target_chars)
        marker = "\n...[上下文已压缩，原始内容保留在 Run State]...\n"
        available = max(2, target_chars - len(marker))
        head = max(1, int(available * 0.68))
        tail = max(1, available - head)
        return f"{value[:head]}{marker}{value[-tail:]}"

    def pack(
        self,
        template: str,
        context: WorkflowContext,
        max_input_tokens: int,
        *,
        preserve_keys: tuple[str, ...] = (),
    ) -> ContextPacket:
        matches = list(self._pattern.finditer(template))
        keys = tuple(dict.fromkeys(match.group(1) for match in matches))
        values = {key: self._serialize(context.get(key)) for key in keys}
        max_chars = max(1024, int(max_input_tokens) * 4)
        truncated: set[str] = set()

        def render() -> str:
            return self._pattern.sub(lambda match: values[match.group(1)], template)

        rendered = render()
        while len(rendered) > max_chars:
            candidate = max((key for key in values if key not in preserve_keys), key=lambda key: len(values[key]), default=None)
            if candidate is None or len(values[candidate]) <= 192:
                break
            current_length = len(values[candidate])
            minimum = 320 if candidate in preserve_keys else 192
            target = max(minimum, int(current_length * 0.68))
            if target >= current_length:
                break
            values[candidate] = self._compact(values[candidate], target)
            truncated.add(candidate)
            rendered = render()

        warnings: list[str] = []
        if len(rendered) > max_chars:
            if any(key in preserve_keys for key in keys):
                raise ValueError("输入预算无法容纳原始需求与冻结合同；请增大上下文窗口或缩小任务，不允许截断关键合同。")
            rendered = self._compact(rendered, max_chars)
            warnings.append("Rendered prompt exceeded the input budget after field compaction.")
        if truncated:
            warnings.append("Large context fields were compacted; original values remain available in Run state.")
        return ContextPacket(
            text=rendered,
            input_tokens=max(1, (len(rendered) + 3) // 4) if rendered else 0,
            included_keys=keys,
            truncated_keys=tuple(key for key in keys if key in truncated),
            warnings=tuple(warnings),
        )
