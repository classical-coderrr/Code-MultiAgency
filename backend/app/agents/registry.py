"""Agent loading and lookup, kept independent from workflow definitions."""

from __future__ import annotations

from pathlib import Path

import yaml

from ..workflow.models import AgentDefinition


class AgentNotFoundError(LookupError):
    pass


class AgentRegistry:
    def __init__(self, agents: list[AgentDefinition] | None = None) -> None:
        self._agents = {agent.id: agent for agent in agents or []}

    @classmethod
    def from_directory(cls, directory: str | Path) -> "AgentRegistry":
        loaded: list[AgentDefinition] = []
        for path in sorted(Path(directory).glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            loaded.append(
                AgentDefinition(
                    id=str(raw["id"]),
                    name=str(raw.get("name", raw["id"])),
                    description=str(raw.get("description", "")),
                    system_prompt=str(raw.get("system_prompt", "")),
                )
            )
        return cls(loaded)

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise AgentNotFoundError(f"Agent not found: {agent_id}") from exc

    def list(self) -> list[AgentDefinition]:
        return list(self._agents.values())

