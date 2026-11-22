"""Per-agent tool allowlist with approval-gated tools. Default deny."""

from __future__ import annotations

from pathlib import Path

import yaml

from regulated_agent_lab import paths


class PolicyViolation(Exception):
    def __init__(self, agent: str, tool: str, reason: str) -> None:
        super().__init__(f"policy violation: agent={agent!r} tool={tool!r}: {reason}")
        self.agent = agent
        self.tool = tool
        self.reason = reason


class ToolPolicy:
    def __init__(self, agents: dict[str, list[str]], requires_approval: list[str]) -> None:
        self._agents = {name: frozenset(tools or []) for name, tools in agents.items()}
        self._requires_approval = frozenset(requires_approval)

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> ToolPolicy:
        path = paths.tool_policy_path() if path is None else Path(path)
        if not path.is_absolute() and not path.exists():
            path = paths.BACKEND_DIR / path
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if raw.get("default", "deny") != "deny":
            raise ValueError("tool policy must use 'default: deny'")
        return cls(raw.get("agents") or {}, raw.get("requires_approval") or [])

    def check(self, agent: str, tool: str, *, approved: bool = False) -> None:
        allowed = self._agents.get(agent)
        if allowed is None:
            raise PolicyViolation(agent, tool, "unknown agent (default deny)")
        if tool not in allowed:
            raise PolicyViolation(agent, tool, "tool not allowed for agent (default deny)")
        if tool in self._requires_approval and not approved:
            raise PolicyViolation(agent, tool, "tool requires human approval")
