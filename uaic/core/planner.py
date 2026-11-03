"""Planners produce a Plan. Nothing here touches the machine.

`ScriptedPlanner` exists so that every test in the project can run without a
model, a network or an API key.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from uaic.contracts import Plan, PlanStep
from uaic.core.context import ContextAssembler


class Planner(Protocol):
    def propose(self, prompt: str, plan_id: str,
                context: ContextAssembler | None = None) -> Plan: ...


class ScriptedPlanner:
    """Returns a fixed list of steps. Used everywhere in the test suite."""

    def __init__(self, steps: Sequence[PlanStep] | Sequence[dict],
                 *, summary: str = "", requested_paths: Sequence[str] = ()) -> None:
        self._steps = tuple(
            s if isinstance(s, PlanStep) else PlanStep(**s) for s in steps
        )
        self._summary = summary
        self._paths = tuple(requested_paths)

    def propose(self, prompt: str, plan_id: str,
                context: ContextAssembler | None = None) -> Plan:
        return Plan(plan_id=plan_id, prompt=prompt, steps=self._steps,
                    summary=self._summary, requested_paths=self._paths)


class PlanBuilder:
    """Assembles a Plan incrementally, keeping step numbering correct."""

    def __init__(self, plan_id: str, prompt: str) -> None:
        self._plan_id = plan_id
        self._prompt = prompt
        self._steps: list[PlanStep] = []
        self._paths: list[str] = []
        self.summary = ""

    def step(self, tool: str, rationale: str = "", **args) -> PlanBuilder:
        self._steps.append(PlanStep(seq=len(self._steps) + 1, tool=tool,
                                    args=args, rationale=rationale))
        return self

    def needs_path(self, path: str) -> PlanBuilder:
        if path not in self._paths:
            self._paths.append(path)
        return self

    def build(self) -> Plan:
        return Plan(plan_id=self._plan_id, prompt=self._prompt,
                    steps=tuple(self._steps), summary=self.summary,
                    requested_paths=tuple(self._paths))
