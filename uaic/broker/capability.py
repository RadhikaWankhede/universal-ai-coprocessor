"""The frozen capability set, and safe path resolution."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


def resolve(raw: object, session_root: Path | None = None) -> Path | None:
    """Turn a user- or model-supplied path into its real, canonical form.

    Returns None if it cannot be resolved safely. Resolving BEFORE checking
    containment is what defeats `..` traversal and symlink escapes.
    """
    if not isinstance(raw, (str, Path)) or str(raw).strip() == "":
        return None
    text = str(raw)
    if "\x00" in text:
        return None
    try:
        p = Path(os.path.expandvars(os.path.expanduser(text)))
    except (TypeError, ValueError):
        return None

    if not p.is_absolute():
        if session_root is None:
            return None
        p = session_root / p

    try:
        p = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # ValueError covers embedded NUL bytes; OSError covers unreadable or
        # too-deeply-nested paths; RuntimeError covers symlink loops.
        return None

    # Windows alternate data streams: C:\file.txt:hidden
    if os.name == "nt" and any(":" in part for part in p.parts[1:]):
        return None
    return p


def _norm(p: Path) -> str:
    """Case-normalised string form, for comparison on Windows."""
    return os.path.normcase(str(p))


def is_inside(target: Path, root: Path) -> bool:
    """True if `target` lies within `root`. Both must already be resolved."""
    t, r = Path(_norm(target)), Path(_norm(root))
    try:
        t.relative_to(r)
    except ValueError:
        return False
    return True


class CapabilitySet(BaseModel):
    """Frozen at plan approval. Never widened afterwards.

    `extra="forbid"` matters more than it looks. Without it, a caller writing
    `roots=` instead of `path_grants=` gets a capability set granting NO
    paths, silently. That fails closed, so it is not a vulnerability - but it
    surfaces later as a run of puzzling BRK.OUT_OF_SCOPE denials rather than
    as the typo it is. A security object should refuse to be built wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    tools: frozenset[str]
    path_grants: tuple[Path, ...] = ()
    max_steps: int = 25

    def permits_tool(self, tool: str) -> bool:
        return tool in self.tools

    def covers_path(self, target: Path) -> bool:
        return any(is_inside(target, g) for g in self.resolved_grants())

    def resolved_grants(self) -> tuple[Path, ...]:
        out = []
        for g in self.path_grants:
            r = resolve(g)
            if r is not None:
                out.append(r)
        return tuple(out)
