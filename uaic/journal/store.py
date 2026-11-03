"""The Action Journal: an append-only record of every call, written BEFORE it runs.

A crash mid-operation therefore always leaves evidence that something was
attempted - which is the entire point of an audit trail.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from uaic.contracts import Decision, Provenance, Rev, Risk, ToolCall, ToolResult, Verdict

SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# Argument names whose values must never reach the audit log.
REDACTED_ARGS = frozenset({"api_key", "token", "password", "secret", "authorization"})
REDACTION = "***redacted***"


class JournalError(Exception):
    """The journal refused a write."""


class Status:
    INTENDED = "intended"      # written before dispatch; still open
    EXECUTED = "executed"
    FAILED = "failed"
    UNDONE = "undone"
    DENIED = "denied"          # the broker refused it; never dispatched
    DECLINED = "declined"      # the user refused it at a confirmation gate


class JournalRow(BaseModel):
    call_id: str
    plan_id: str
    seq: int
    ts_intent: str
    ts_outcome: str | None = None
    tool: str
    args: dict[str, Any]
    decision: Decision
    risk: Risk
    rev: Rev
    status: str
    mutates: bool = True
    inverse: ToolCall | None = None
    result_hash: str | None = None
    provenance: Provenance = Provenance.UNTRUSTED
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def redact(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (REDACTION if k.lower() in REDACTED_ARGS else v) for k, v in args.items()}


class ActionJournal:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- writes

    def write_intent(self, call: ToolCall, verdict: Verdict, *,
                     mutates: bool = True) -> None:
        """Record the intent and COMMIT, before the plugin is invoked."""
        self._insert(call, verdict, Status.INTENDED, mutates=mutates)

    def write_refusal(self, call: ToolCall, verdict: Verdict, *,
                      declined_by_user: bool = False) -> None:
        """Record a call that was never dispatched. The evaluation harness
        counts these, so refusals must be in the audit trail too."""
        self._insert(call, verdict,
                     Status.DECLINED if declined_by_user else Status.DENIED,
                     mutates=False, closed=True)

    def _insert(self, call: ToolCall, verdict: Verdict, status: str, *,
                mutates: bool, closed: bool = False) -> None:
        try:
            self._conn.execute(
                "INSERT INTO journal (call_id, plan_id, seq, ts_intent, ts_outcome, "
                "tool, args_json, decision, risk, rev, status, mutates) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (call.call_id, call.plan_id, call.seq, _now(),
                 _now() if closed else None, call.tool,
                 json.dumps(redact(call.args), sort_keys=True),
                 verdict.decision.value, verdict.risk.value, verdict.rev.value,
                 status, int(mutates)),
            )
        except sqlite3.IntegrityError as exc:
            raise JournalError(f"call_id {call.call_id!r} already journalled") from exc

    def write_outcome(self, result: ToolResult, *, result_hash: str | None = None) -> None:
        status = Status.EXECUTED if result.ok else Status.FAILED
        inverse = result.inverse.model_dump_json() if result.inverse else None
        self._update(
            result.call_id,
            "ts_outcome = ?, status = ?, inverse_json = ?, result_hash = ?, "
            "provenance = ?, error = ?",
            (_now(), status, inverse, result_hash or result.result_hash,
             result.provenance.value, result.error),
        )

    def write_grants(self, plan_id: str, paths: Sequence[Path | str]) -> None:
        """Record the folders the user granted this plan, at the freeze."""
        try:
            self._conn.execute(
                "INSERT INTO plan_grants (plan_id, ts, paths_json) VALUES (?,?,?)",
                (plan_id, _now(), json.dumps([str(p) for p in paths])))
        except sqlite3.IntegrityError as exc:
            raise JournalError(f"grants for {plan_id!r} already recorded") from exc

    def grants(self, plan_id: str) -> tuple[str, ...] | None:
        """The folders granted to a plan; None if none were recorded for it."""
        row = self._conn.execute("SELECT paths_json FROM plan_grants WHERE plan_id = ?",
                                 (plan_id,)).fetchone()
        return tuple(json.loads(row["paths_json"])) if row else None

    def mark_undone(self, call_id: str) -> None:
        self._update(call_id, "status = ?, ts_outcome = ?", (Status.UNDONE, _now()))

    def _update(self, call_id: str, assignments: str, params: tuple) -> None:
        try:
            cur = self._conn.execute(
                f"UPDATE journal SET {assignments} WHERE call_id = ?",
                (*params, call_id),
            )
        except sqlite3.IntegrityError as exc:
            raise JournalError(str(exc)) from exc
        if cur.rowcount == 0:
            raise JournalError(f"no journal row for call_id {call_id!r}")

    # ----------------------------------------------------------------- reads

    def rows(self, plan_id: str | None = None, *, status: str | None = None,
             newest_first: bool = False) -> list[JournalRow]:
        sql = "SELECT * FROM journal"
        where, params = [], []
        if plan_id is not None:
            where.append("plan_id = ?")
            params.append(plan_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC" if newest_first else " ORDER BY seq ASC"
        return [self._row(r) for r in self._conn.execute(sql, params)]

    def get(self, call_id: str) -> JournalRow | None:
        r = self._conn.execute(
            "SELECT * FROM journal WHERE call_id = ?", (call_id,)).fetchone()
        return self._row(r) if r else None

    def unfinished(self) -> list[JournalRow]:
        """Rows still 'intended' - crash artefacts. Surface these to the user."""
        return self.rows(status=Status.INTENDED)

    @staticmethod
    def _row(r: sqlite3.Row) -> JournalRow:
        return JournalRow(
            call_id=r["call_id"], plan_id=r["plan_id"], seq=r["seq"],
            ts_intent=r["ts_intent"], ts_outcome=r["ts_outcome"], tool=r["tool"],
            args=json.loads(r["args_json"]),
            decision=Decision(r["decision"]), risk=Risk(r["risk"]),
            rev=Rev(r["rev"]), status=r["status"],
            mutates=bool(r["mutates"]),
            inverse=(ToolCall.model_validate_json(r["inverse_json"])
                     if r["inverse_json"] else None),
            result_hash=r["result_hash"],
            provenance=Provenance(r["provenance"]),
            error=r["error"],
        )

    # ----------------------------------------------------------------- admin

    def journal_mode(self) -> str:
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ActionJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
