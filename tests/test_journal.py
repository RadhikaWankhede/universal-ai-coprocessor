"""The journal is append-only, and that is enforced by the database."""
from __future__ import annotations

import sqlite3

import pytest

from uaic.contracts import (
    Decision,
    Provenance,
    Rev,
    Risk,
    ToolCall,
    ToolResult,
    Verdict,
)
from uaic.journal import ActionJournal, JournalError, Status, redact


@pytest.fixture
def journal(tmp_path):
    with ActionJournal(tmp_path / "journal.db") as j:
        yield j


def mk_call(seq: int = 1, tool: str = "fs.move", **args) -> ToolCall:
    return ToolCall(call_id=f"c{seq}", plan_id="p1", tool=tool,
                    args=args or {"src": "a", "dst": "b"}, seq=seq)


def mk_verdict(rev: Rev = Rev.REVERSIBLE) -> Verdict:
    return Verdict(decision=Decision.ALLOW, risk=Risk.LOW, rev=rev)


# ---------- schema and durability ----------

def test_wal_is_enabled_on_a_real_file(journal):
    assert journal.journal_mode().lower() == "wal"


def test_intent_is_visible_before_any_outcome(journal):
    journal.write_intent(mk_call(), mk_verdict())
    row = journal.get("c1")
    assert row.status == Status.INTENDED
    assert row.ts_outcome is None


def test_intent_survives_a_crash(tmp_path):
    """Write the intent, drop the connection without an outcome, reopen."""
    db = tmp_path / "j.db"
    j1 = ActionJournal(db)
    j1.write_intent(mk_call(), mk_verdict())
    j1.close()                                  # simulates the process dying

    j2 = ActionJournal(db)
    pending = j2.unfinished()
    assert [r.call_id for r in pending] == ["c1"]
    j2.close()


def test_duplicate_call_id_is_refused(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(JournalError, match="already journalled"):
        journal.write_intent(mk_call(), mk_verdict())


def test_outcome_on_unknown_call_is_refused(journal):
    with pytest.raises(JournalError, match="no journal row"):
        journal.write_outcome(ToolResult(call_id="nope", ok=True))


# ---------- append-only enforcement ----------

def test_the_tool_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET tool = 'mail.send' WHERE call_id = 'c1'")


def test_the_arguments_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET args_json = '{}' WHERE call_id = 'c1'")


def test_the_decision_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET decision = 'deny' WHERE call_id = 'c1'")


def test_the_risk_tier_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET risk = 'critical' WHERE call_id = 'c1'")


def test_the_reversibility_class_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET rev = 'irreversible' WHERE call_id = 'c1'")


def test_the_intent_timestamp_cannot_be_rewritten(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute(
            "UPDATE journal SET ts_intent = '1999-01-01' WHERE call_id = 'c1'")


def test_rows_are_never_deleted(journal):
    journal.write_intent(mk_call(), mk_verdict())
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        journal._conn.execute("DELETE FROM journal WHERE call_id = 'c1'")


def test_a_closed_row_cannot_be_reopened(journal):
    journal.write_intent(mk_call(), mk_verdict())
    journal.write_outcome(ToolResult(call_id="c1", ok=True))
    journal.mark_undone("c1")
    with pytest.raises(JournalError, match="append-only"):
        journal.mark_undone("c1")


def test_outcome_fields_may_be_written_once(journal):
    journal.write_intent(mk_call(), mk_verdict())
    inverse = mk_call(seq=1, tool="fs.move", src="b", dst="a")
    journal.write_outcome(
        ToolResult(call_id="c1", ok=True, inverse=inverse,
                   provenance=Provenance.TRUSTED),
        result_hash="abc123")
    row = journal.get("c1")
    assert row.status == Status.EXECUTED
    assert row.result_hash == "abc123"
    assert row.inverse.args == {"src": "b", "dst": "a"}
    assert row.provenance is Provenance.TRUSTED


def test_failure_is_recorded_with_its_error(journal):
    journal.write_intent(mk_call(), mk_verdict())
    journal.write_outcome(ToolResult(call_id="c1", ok=False,
                                     error="PermissionError: denied"))
    row = journal.get("c1")
    assert row.status == Status.FAILED
    assert "PermissionError" in row.error


# ---------- redaction ----------

def test_secrets_are_redacted_before_writing(journal):
    call = ToolCall(call_id="c9", plan_id="p1", tool="fs.move", seq=9,
                    args={"src": "a", "dst": "b", "api_key": "sk-live-123"})
    journal.write_intent(call, mk_verdict())
    stored = journal.get("c9").args
    assert stored["api_key"] == "***redacted***"
    assert "sk-live-123" not in str(stored)


def test_redact_is_case_insensitive():
    assert redact({"API_KEY": "x"})["API_KEY"] == "***redacted***"
    assert redact({"path": "x"})["path"] == "x"


# ---------- reads ----------

def test_rows_are_ordered_by_sequence(journal):
    for i in (3, 1, 2):
        journal.write_intent(mk_call(seq=i), mk_verdict())
    assert [r.seq for r in journal.rows("p1")] == [1, 2, 3]
    assert [r.seq for r in journal.rows("p1", newest_first=True)] == [3, 2, 1]


def test_rows_can_be_filtered_by_plan(journal):
    journal.write_intent(mk_call(seq=1), mk_verdict())
    other = ToolCall(call_id="x1", plan_id="p2", tool="fs.list",
                     args={"path": "."}, seq=1)
    journal.write_intent(other, mk_verdict())
    assert [r.call_id for r in journal.rows("p2")] == ["x1"]
