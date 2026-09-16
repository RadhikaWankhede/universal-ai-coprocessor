"""Filesystem plugin. Owned by M3 - this is the minimal version for the spike."""
from __future__ import annotations

import time
from pathlib import Path

from uaic.contracts import Provenance, ToolCall, ToolResult


def execute(call: ToolCall) -> ToolResult:
    started = time.perf_counter()
    try:
        if call.tool == "fs.list":
            target = Path(call.args["path"])
            names = sorted(p.name for p in target.iterdir())
            payload: object = names
        else:
            raise NotImplementedError(f"fs plugin does not implement {call.tool}")
        ok, error = True, None
    except Exception as exc:
        ok, payload, error = False, None, f"{type(exc).__name__}: {exc}"

    return ToolResult(
        call_id=call.call_id,
        ok=ok,
        payload=payload,
        provenance=Provenance.UNTRUSTED,   # anything read off the disk is untrusted
        error=error,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
