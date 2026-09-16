"""Risk and reversibility classification.

Risk is a function of (tool, arguments, targets) - not of the tool name alone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from uaic.contracts import Rev, Risk

# tool -> (base risk, reversibility, which args must fall inside a grant)
TOOL_SPECS: dict[str, tuple[Risk, Rev, tuple[str, ...]]] = {
    "fs.list":        (Risk.LOW,    Rev.REVERSIBLE,   ("path",)),
    "fs.read":        (Risk.LOW,    Rev.REVERSIBLE,   ("path",)),
    "fs.copy":        (Risk.LOW,    Rev.REVERSIBLE,   ("src", "dst")),
    "fs.move":        (Risk.LOW,    Rev.REVERSIBLE,   ("src", "dst")),
    "fs.delete":      (Risk.MEDIUM, Rev.REVERSIBLE,   ("path",)),
    "fs.overwrite":   (Risk.HIGH,   Rev.IRREVERSIBLE, ("path",)),
    "git.commit":     (Risk.LOW,    Rev.COMPENSABLE,  ("repo",)),
    "git.branch":     (Risk.LOW,    Rev.COMPENSABLE,  ("repo",)),
    "git.push_force": (Risk.HIGH,   Rev.IRREVERSIBLE, ("repo",)),
    "shell.exec":     (Risk.HIGH,   Rev.IRREVERSIBLE, ("cwd",)),
    "mail.send":      (Risk.HIGH,   Rev.IRREVERSIBLE, ()),
}

RISK_ORDER = [Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]

Predicate = Callable[[dict[str, Any], tuple[Path, ...]], Risk | None]
_PREDICATES: dict[str, Predicate] = {}


def predicate(name: str):
    def wrap(fn: Predicate) -> Predicate:
        _PREDICATES[name] = fn
        return fn
    return wrap


# Escalation rules are named Python functions, never strings we evaluate.
ESCALATIONS: dict[str, tuple[str, ...]] = {
    "fs.delete": ("target_is_directory",),
    "fs.move":   ("target_is_directory",),
}


@predicate("target_is_directory")
def _target_is_directory(args: dict[str, Any], targets: tuple[Path, ...]) -> Risk | None:
    if any(t.is_dir() for t in targets):
        return Risk.HIGH if args.get("recursive") else Risk.MEDIUM
    return None


def classify(tool: str, args: dict[str, Any], targets: tuple[Path, ...]) -> tuple[Risk, Rev]:
    base_risk, rev, _ = TOOL_SPECS[tool]
    worst = base_risk
    for name in ESCALATIONS.get(tool, ()):
        got = _PREDICATES[name](args, targets)
        if got is not None and RISK_ORDER.index(got) > RISK_ORDER.index(worst):
            worst = got
    return worst, rev
