"""A plan: what the planner proposes and the user approves.

Approval is the moment the capability set is frozen, so the plan is the
authoritative record of what was permitted.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from uaic.contracts.tools import Origin, ToolCall


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    tool: str
    args: dict[str, Any] = {}
    rationale: str = ""        # shown to the user in the plan preview

    @field_validator("seq")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("step numbers start at 1")
        return v


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    prompt: str                                  # the user's original words
    steps: tuple[PlanStep, ...] = ()
    summary: str = ""
    requested_paths: tuple[str, ...] = ()        # folders the plan needs

    @field_validator("steps")
    @classmethod
    def _sequential(cls, v: tuple[PlanStep, ...]) -> tuple[PlanStep, ...]:
        expected = list(range(1, len(v) + 1))
        if [s.seq for s in v] != expected:
            raise ValueError(f"steps must be numbered {expected}")
        return v

    def tools(self) -> frozenset[str]:
        """Exactly the tools this plan uses - the set that gets frozen."""
        return frozenset(s.tool for s in self.steps)

    def to_calls(self, *, origin: Origin = Origin.PLANNER) -> list[ToolCall]:
        return [
            ToolCall(call_id=f"{self.plan_id}-{s.seq}", plan_id=self.plan_id,
                     tool=s.tool, args=s.args, seq=s.seq, origin=origin)
            for s in self.steps
        ]

    def preview(self) -> str:
        """What the user reads before approving anything."""
        lines = [f'Plan for: "{self.prompt}"']
        if self.summary:
            lines.append(self.summary)
        for s in self.steps:
            arg_text = ", ".join(f"{k}={v}" for k, v in s.args.items())
            lines.append(f"  {s.seq}. {s.tool}({arg_text})"
                         + (f"  - {s.rationale}" if s.rationale else ""))
        lines.append(f"  tools required: {', '.join(sorted(self.tools())) or 'none'}")
        return "\n".join(lines)
