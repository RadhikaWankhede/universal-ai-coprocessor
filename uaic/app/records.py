"""Run records: what you asked, and what each step returned. NOT the journal.

The journal is the audit trail. It is append-only by trigger, holds no tool
output (only a hash), and is never cleared - "an action not in the journal did
not occur" depends on that. Reading back what a past run *returned* - a file
listing, a document's text - needs the output itself, so it is kept here, in a
separate database next to the journal:

* records.db can be cleared by the user. Clearing deletes the stored requests
  and outputs for real, and marks a point in time before which the history
  list hides plans. The journal is untouched, so the audit trail survives.
* Outputs are untrusted content (they came from files and programs). They are
  stored and displayed as plain text and never fed back to the planner from
  here - the planner's memory is a separate, provenance-tagged structure.
* Each output is already capped by the runtime (progress.MAX_OUTPUT_CHARS).

SQLite, WAL, no ORM, like the journal. Used on the backend thread only.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS plans (
    plan_id  TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,             -- when the request was made (UTC, ISO 8601)
    request  TEXT NOT NULL,             -- the user's own words
    outcome  TEXT,                      -- completed | declined | refused | ...
    summary  TEXT,                      -- LoopReport.describe()
    preview  TEXT                       -- Plan.preview(), what the user approved
);
CREATE TABLE IF NOT EXISTS outputs (
    call_id  TEXT PRIMARY KEY,
    plan_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    tool     TEXT NOT NULL,
    output   TEXT NOT NULL              -- plain text, untrusted, capped
);
CREATE INDEX IF NOT EXISTS ix_outputs_plan ON outputs (plan_id, seq);
CREATE TABLE IF NOT EXISTS settings (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL
);
"""

CLEARED_AT = "history_cleared_at"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunRecords:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- writes

    def begin(self, plan_id: str, request: str) -> None:
        self._conn.execute("INSERT OR IGNORE INTO plans (plan_id, ts, request) "
                           "VALUES (?, ?, ?)", (plan_id, now(), request))

    def output(self, call_id: str, plan_id: str, seq: int, tool: str, text: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO outputs VALUES (?, ?, ?, ?, ?)",
                           (call_id, plan_id, seq, tool, text))

    def finish(self, plan_id: str, outcome: str, summary: str, preview: str | None) -> None:
        self._conn.execute("UPDATE plans SET outcome = ?, summary = ?, preview = ? "
                           "WHERE plan_id = ?", (outcome, summary, preview, plan_id))

    def clear(self) -> str:
        """Delete every stored request and output; hide older plans from history."""
        stamp = now()
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM outputs")
            self._conn.execute("DELETE FROM plans")
            self._conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                               (CLEARED_AT, stamp))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("VACUUM")         # the deleted text leaves the file too
        return stamp

    # ----------------------------------------------------------------- reads

    def plans(self) -> dict[str, sqlite3.Row]:
        return {r["plan_id"]: r for r in self._conn.execute("SELECT * FROM plans")}

    def plan(self, plan_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM plans WHERE plan_id = ?",
                                  (plan_id,)).fetchone()

    def outputs(self, plan_id: str) -> dict[str, str]:
        return {r["call_id"]: r["output"] for r in self._conn.execute(
            "SELECT call_id, output FROM outputs WHERE plan_id = ?", (plan_id,))}

    def cleared_at(self) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?",
                                 (CLEARED_AT,)).fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        self._conn.close()
