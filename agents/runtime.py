"""Per-node context: records every tool call in the trajectory and every guard flag.

The agent identity is fixed by the graph when it builds the context for a node; code in a
node cannot choose who it calls tools as, and cannot claim approval (only a signed approval
token from the graph's approval node is accepted by the gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.llm import LLM
from agents.tools import GatedToolClient, UnauditedToolResult
from controls.injection_guard import GuardResult


@dataclass
class NodeContext:
    run_id: str
    agent: str
    llm: LLM
    tools: GatedToolClient
    called: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    async def call(
        self, tool: str, args: dict, *, approval_token: str | None = None
    ) -> GuardResult:
        # Recorded before the call so denied or failed attempts still show in the trajectory.
        self.called.append(tool)
        try:
            guard = await self.tools.call(
                self.run_id, self.agent, tool, args, approval_token=approval_token
            )
        except UnauditedToolResult as exc:
            # The tool ran but its outcome is missing from the audit log: flag it in the run.
            self.flags.append(f"audit_failed:{tool}")
            if exc.guard.flagged:
                self.flags.append(f"injection_flagged:{tool}")
            raise
        if guard.flagged:
            self.flags.append(f"injection_flagged:{tool}")
        return guard

    def record(self, actor: str, event: str, data: dict) -> None:
        self.tools.record(self.run_id, actor, event, data)
