"""Load portable, prompt-only Skills from local ``SKILL.md`` files.

Skills are deliberately data, not Python plug-ins: loading one cannot execute
arbitrary code.  A Skill supplements an Agent's stable system prompt for a
single Run after the resolver has selected it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


_SKILL_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_COMPLEXITIES = {"low", "medium", "high"}


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    version: str = "1.0"
    description: str = ""
    applies_to: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    min_complexity: str = "medium"
    always: bool = False
    max_injection_tokens: int = 600
    body: str = ""
    source: str = ""

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "appliesTo": list(self.applies_to),
            "capabilities": list(self.capabilities),
            "minComplexity": self.min_complexity,
            "always": self.always,
        }


class SkillRegistry:
    """Registry with deterministic precedence: earlier directories win."""

    def __init__(self, skills: Iterable[SkillDefinition] = ()) -> None:
        self._skills: dict[str, SkillDefinition] = {}
        for skill in skills:
            if skill.id not in self._skills:
                self._skills[skill.id] = skill

    @classmethod
    def from_directories(cls, directories: Iterable[str | Path | None]) -> "SkillRegistry":
        skills: list[SkillDefinition] = []
        seen: set[str] = set()
        for directory in directories:
            if not directory:
                continue
            root = Path(directory)
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                try:
                    skill = _read_skill(path)
                except (OSError, ValueError, yaml.YAMLError):
                    # A bad optional Skill must never prevent a workflow from
                    # running.  It will be absent from the resolver audit.
                    continue
                if skill.id not in seen:
                    skills.append(skill)
                    seen.add(skill.id)
        return cls(skills)

    def get(self, skill_id: str) -> SkillDefinition | None:
        return self._skills.get(str(skill_id).strip())

    def list_public(self) -> list[dict[str, Any]]:
        return [skill.public_metadata() for skill in self._skills.values()]


def _read_skill(path: Path) -> SkillDefinition:
    text = path.read_text(encoding="utf-8")
    metadata: dict[str, Any] = {}
    body = text.strip()
    if text.startswith("---"):
        closing = text.find("\n---", 3)
        if closing < 0:
            raise ValueError(f"Skill front matter is not closed: {path}")
        raw_metadata = yaml.safe_load(text[3:closing])
        if raw_metadata is not None and not isinstance(raw_metadata, dict):
            raise ValueError(f"Skill front matter must be an object: {path}")
        metadata = raw_metadata or {}
        body = text[closing + 4 :].strip()

    skill_id = str(metadata.get("id") or path.parent.name).strip()
    if not _SKILL_ID.fullmatch(skill_id):
        raise ValueError(f"Invalid Skill id: {skill_id}")
    min_complexity = str(metadata.get("min_complexity", "medium")).strip().lower()
    if min_complexity not in _COMPLEXITIES:
        raise ValueError(f"Invalid Skill min_complexity: {min_complexity}")
    max_injection_tokens = int(metadata.get("max_injection_tokens", 600))
    if not 32 <= max_injection_tokens <= 4000:
        raise ValueError("Skill max_injection_tokens must be between 32 and 4000")

    return SkillDefinition(
        id=skill_id,
        version=str(metadata.get("version", "1.0"))[:40],
        description=str(metadata.get("description", ""))[:300],
        applies_to=_string_tuple(metadata.get("applies_to")),
        capabilities=_string_tuple(metadata.get("capabilities")),
        min_complexity=min_complexity,
        always=bool(metadata.get("always", False)),
        max_injection_tokens=max_injection_tokens,
        body=body,
        source=str(path),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return tuple(str(item).strip() for item in values if str(item).strip())
