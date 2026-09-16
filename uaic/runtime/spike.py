"""End-to-end demonstration: request -> broker -> journal -> plugin -> journal.

Run it against a real folder:  python -m uaic.runtime.spike
"""
from __future__ import annotations

from pathlib import Path

from uaic.broker import CapabilitySet
from uaic.contracts import Origin, ToolCall
from uaic.journal import ActionJournal
from uaic.plugins import FileSystemPlugin
from uaic.runtime import Executor


def always_confirm(call: ToolCall, verdict) -> bool:
    print(f"  [confirm] {call.tool} is {verdict.rev.value} "
          f"({verdict.risk.value}) - approving for the demo")
    return True


def run(folder: Path, db: Path | None = None, trash: Path | None = None) -> None:
    db = db or Path(".uaic/journal.db")
    trash = trash or Path(".uaic/trash")
    db.parent.mkdir(parents=True, exist_ok=True)

    journal = ActionJournal(db)
    plugin = FileSystemPlugin(trash)
    executor = Executor(journal, {"fs": plugin.execute}, confirm=always_confirm)

    caps = CapabilitySet(
        plan_id="spike-1",
        tools=frozenset({"fs.list"}),
        path_grants=(folder,),
    )
    call = ToolCall(call_id="call-1", plan_id="spike-1", tool="fs.list",
                    args={"path": str(folder)}, origin=Origin.USER)

    outcome = executor.run_one(call, caps)
    v = outcome.verdict
    print(f"broker  : {v.decision.value}  ({v.rev.value}/{v.risk.value})  "
          f"{v.rule_ids[0]}")
    print(f"status  : {outcome.status}")

    if outcome.ran and outcome.result.ok:
        names = outcome.result.payload or []
        print(f"contents: {len(names)} item(s)")
        for n in names[:10]:
            print(f"          {n}")
    elif outcome.ran:
        print(f"error   : {outcome.result.error}")

    print("\njournal :")
    for row in journal.rows("spike-1"):
        print(f"          seq={row.seq} {row.tool} {row.status} "
              f"mutates={row.mutates}")

    # a call the plan never approved
    sneaky = ToolCall(call_id="call-2", plan_id="spike-1", tool="fs.delete",
                      args={"path": str(folder)}, seq=1)
    blocked = executor.run_one(sneaky, caps)
    print(f"\nunapproved fs.delete -> {blocked.verdict.decision.value} "
          f"({blocked.verdict.rule_ids[0]})")

    journal.close()


if __name__ == "__main__":
    run(Path.home() / "Downloads")
