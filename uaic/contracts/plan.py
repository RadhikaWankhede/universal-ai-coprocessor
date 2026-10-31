"""A plan: what the planner proposes and the user approves.

Approval is the moment the capability set is frozen, so the plan is the
authoritative record of what was permitted.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

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


class SourceRead(BaseModel):
    """One untrusted source a plan was proposed after, in a form a preview can
    show: "this plan was proposed after reading report.pdf (doc.extract_text)"."""
    model_config = ConfigDict(frozen=True)

    ref: str                       # the raw source_ref, as recorded
    kind: str                      # "file", "tool", "planner" or "runtime"
    name: str                      # report.pdf, or the tool / plan id
    path: str | None = None
    tool: str | None = None


def parse_source(ref: str) -> SourceRead:
    """Read a source_ref as the runtime writes it (see runtime/loop.py)."""
    if ref.startswith("file:"):
        parts = urlsplit(ref)
        path = unquote(parts.path)
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]                              # /C:/x -> C:/x
        return SourceRead(ref=ref, kind="file", name=PurePosixPath(path).name, path=path,
                          tool=unquote(parts.fragment) or None)
    kind, _, rest = ref.partition(":")
    if kind in ("tool", "planner", "runtime") and rest:
        rest = unquote(rest)
        return SourceRead(ref=ref, kind=kind, name=rest,
                          tool=rest if kind == "tool" else None)
    return SourceRead(ref=ref, kind="unknown", name=ref)


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    prompt: str                                  # the user's original words
    steps: tuple[PlanStep, ...] = ()
    summary: str = ""
    requested_paths: tuple[str, ...] = ()        # folders the plan needs
    # Set by the runtime from the context the planner was given, never by the
    # planner: a compromised model must not be able to clear its own taint.
    tainted: bool = False
    taint_sources: tuple[str, ...] = ()

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
                     tool=s.tool, args=s.args, seq=s.seq, origin=origin,
                     tainted=self.tainted, taint_sources=self.taint_sources)
            for s in self.steps
        ]

    def sources_read(self) -> tuple[SourceRead, ...]:
        """The untrusted sources this plan was proposed after, as preview data."""
        return tuple(parse_source(ref) for ref in self.taint_sources)

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
