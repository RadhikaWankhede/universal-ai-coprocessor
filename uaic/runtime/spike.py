"""Week-1 spike: a ToolCall travels end to end with no AI anywhere in the path.

    ToolCall (built by hand)  ->  Broker.decide  ->  fs plugin  ->  printed result
"""
from __future__ import annotations

from pathlib import Path

from uaic.broker import CapabilitySet, decide
from uaic.contracts import Origin, ToolCall


def run(folder: Path) -> None:
    caps = CapabilitySet(
        plan_id="spike-1",
        tools=frozenset({"fs.list"}),
        path_grants=(folder,),
    )
    call = ToolCall(
        call_id="call-1",
        plan_id="spike-1",
        tool="fs.list",
        args={"path": str(folder)},
        origin=Origin.USER,
    )

    verdict = decide(call, caps)
    print(f"broker  : {verdict.decision.value}  "
          f"({verdict.rev.value}/{verdict.risk.value})")
    for r in verdict.reasons:
        print(f"          {r}")

    if not verdict.allowed:
        print("plugin  : not dispatched")
        return

    from uaic.plugins import fs
    result = fs.execute(call)
    print(f"plugin  : ok={result.ok}  provenance={result.provenance.value}")
    if result.ok:
        names = result.payload or []
        print(f"contents: {len(names)} item(s)")
        for n in names[:10]:
            print(f"          {n}")
    else:
        print(f"error   : {result.error}")


if __name__ == "__main__":
    run(Path.home() / "Downloads")
