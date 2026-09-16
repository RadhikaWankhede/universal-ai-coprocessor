"""Risk and reversibility classification.

Final risk is a function of (tool, arguments, resolved targets) - never of the
tool name alone. `fs.delete` on one temp file and `fs.delete` on a whole
directory tree are not the same decision.

Escalation rules are REGISTERED PYTHON FUNCTIONS referenced by name. A manifest
never contains a string that gets evaluated - the file defining the security
policy must not also be a code-execution path.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from uaic.contracts import Rev, Risk

RISK_ORDER: tuple[Risk, ...] = (Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL)

Predicate = Callable[[dict[str, Any], tuple[Path, ...]], "Risk | None"]
_PREDICATES: dict[str, Predicate] = {}


def predicate(name: str) -> Callable[[Predicate], Predicate]:
    def wrap(fn: Predicate) -> Predicate:
        if name in _PREDICATES:
            raise ValueError(f"escalation predicate {name!r} registered twice")
        _PREDICATES[name] = fn
        return fn
    return wrap


def known_predicates() -> set[str]:
    return set(_PREDICATES)


def worse(a: Risk, b: Risk) -> Risk:
    return a if RISK_ORDER.index(a) >= RISK_ORDER.index(b) else b


# ---------------------------------------------------------------- predicates

@predicate("target_is_directory")
def _target_is_directory(args: dict[str, Any], targets: tuple[Path, ...]) -> Risk | None:
    """A whole folder is a much larger blast radius than one file."""
    if not any(t.is_dir() for t in targets):
        return None
    return Risk.HIGH if args.get("recursive") else Risk.MEDIUM


@predicate("target_is_many_files")
def _target_is_many_files(args: dict[str, Any], targets: tuple[Path, ...]) -> Risk | None:
    """More than 50 files in one call is a bulk operation."""
    for t in targets:
        if t.is_dir():
            try:
                if sum(1 for _ in t.iterdir()) > 50:
                    return Risk.HIGH
            except OSError:
                return Risk.HIGH
    return None


# ---------------------------------------------------------------- classify

def classify(spec, args: dict[str, Any], targets: tuple[Path, ...]) -> tuple[Risk, Rev]:
    """spec is a manifest ToolSpec. Returns the final (risk, reversibility)."""
    risk = spec.risk
    for name in spec.escalate:
        got = _PREDICATES[name](args, targets)
        if got is not None:
            risk = worse(risk, got)
    return risk, spec.rev
