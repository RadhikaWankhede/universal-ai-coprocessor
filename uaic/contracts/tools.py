"""Shared data contracts. This package imports nothing else from uaic."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Origin(StrEnum):
    PLANNER = "planner"
    USER = "user"
    UNDO = "undo"


class Provenance(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Rev(StrEnum):
    """How badly wrong an action can go: can we take it back?"""
    REVERSIBLE = "reversible"
    COMPENSABLE = "compensable"
    IRREVERSIBLE = "irreversible"


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM_PLAN = "confirm_plan"
    CONFIRM_STEP = "confirm_step"
    DENY = "deny"


class ToolCall(BaseModel):
    """One proposed action. The only thing that crosses module boundaries."""
    model_config = ConfigDict(frozen=True)

    call_id: str
    plan_id: str
    tool: str
    args: dict[str, Any] = {}
    seq: int = 0
    origin: Origin = Origin.PLANNER


class ToolResult(BaseModel):
    """What a plugin hands back after executing a call."""
    call_id: str
    ok: bool
    payload: Any | None = None
    provenance: Provenance = Provenance.UNTRUSTED
    inverse: ToolCall | None = None
    result_hash: str | None = None     # lets undo prove it restored the prior state
    error: str | None = None
    duration_ms: int = 0


class Verdict(BaseModel):
    """The broker's answer. The only thing the broker returns."""
    model_config = ConfigDict(frozen=True)

    decision: Decision
    risk: Risk
    rev: Rev
    reasons: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def needs_user(self) -> bool:
        return self.decision in (Decision.CONFIRM_PLAN, Decision.CONFIRM_STEP)
