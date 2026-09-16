"""The Undo Engine: walk the journal backwards, replaying inverse operations.

It STOPS at the first irreversible step rather than skipping past it - skipping
would leave a state that is neither the before nor the after. Undo calls are
dispatched through the broker like any other call, because an undo path that
reached plugins directly would be an unaudited write path.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from uaic.contracts import Origin, Rev, ToolCall, ToolResult
from uaic.journal.store import ActionJournal, JournalRow, Status

Executor = Callable[[ToolCall], ToolResult]


@dataclass
class UndoReport:
    plan_id: str
    undone: list[str] = field(default_factory=list)
    stopped_at: JournalRow | None = None
    stop_reason: str | None = None
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stopped_at is None and not self.failed

    def describe(self) -> str:
        if self.complete:
            return f"undid {len(self.undone)} step(s); plan fully reversed"
        parts = [f"undid {len(self.undone)} step(s)"]
        if self.stopped_at is not None:
            parts.append(
                f"stopped at step {self.stopped_at.seq} "
                f"({self.stopped_at.tool}): {self.stop_reason}")
        for call_id, err in self.failed:
            parts.append(f"inverse of {call_id} failed: {err}")
        return "; ".join(parts)


class UndoEngine:
    def __init__(self, journal: ActionJournal, execute: Executor) -> None:
        self._journal = journal
        self._execute = execute

    def undo_plan(self, plan_id: str) -> UndoReport:
        report = UndoReport(plan_id=plan_id)

        for row in self._journal.rows(plan_id, status=Status.EXECUTED,
                                      newest_first=True):
            if row.rev is Rev.IRREVERSIBLE:
                report.stopped_at = row
                report.stop_reason = "irreversible"
                break
            if row.inverse is None:
                report.stopped_at = row
                report.stop_reason = "no inverse was recorded"
                break

            inverse = row.inverse.model_copy(update={"origin": Origin.UNDO})
            result = self._execute(inverse)
            if not result.ok:
                report.failed.append((row.call_id, result.error or "unknown error"))
                report.stopped_at = row
                report.stop_reason = "the inverse operation failed"
                break

            self._journal.mark_undone(row.call_id)
            report.undone.append(row.call_id)

        return report
