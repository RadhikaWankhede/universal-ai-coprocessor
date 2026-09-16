"""The capability freeze.

When the user approves a plan, the set of permitted tools and paths is fixed
for that plan's lifetime. This is the structural defence the project claims,
and it is almost entirely the one line marked below.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from uaic.broker import CapabilitySet
from uaic.contracts import Plan

STEP_HEADROOM = 2      # allows a couple of retries, not an unbounded loop


def freeze_plan(plan: Plan, granted_paths: Sequence[Path | str],
                *, max_steps: int | None = None) -> CapabilitySet:
    """Mint the immutable capability set for an approved plan.

    The tool set is frozen, NOT just the paths. That is what stops an injected
    instruction invoking a tool the user never approved - an email address is
    not a path, so path-scoping alone would let `mail.send` through.
    """
    return CapabilitySet(
        plan_id=plan.plan_id,
        tools=plan.tools(),                                   # <-- the freeze
        path_grants=tuple(Path(p) for p in granted_paths),
        max_steps=max_steps or len(plan.steps) + STEP_HEADROOM,
    )


def escalation_needed(plan: Plan, caps: CapabilitySet) -> frozenset[str]:
    """Tools the plan wants that the frozen set does not permit.

    A non-empty result must go back to the user as a NEW approval minting a
    NEW plan_id. The existing capability set is never widened.
    """
    return frozenset(t for t in plan.tools() if not caps.permits_tool(t))
