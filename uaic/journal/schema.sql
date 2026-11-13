PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS journal (
    call_id      TEXT PRIMARY KEY,
    plan_id      TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    ts_intent    TEXT    NOT NULL,            -- written BEFORE dispatch
    ts_outcome   TEXT,
    tool         TEXT    NOT NULL,
    args_json    TEXT    NOT NULL,            -- secrets redacted before writing
    decision     TEXT    NOT NULL,
    risk         TEXT    NOT NULL,
    rev          TEXT    NOT NULL,
    status       TEXT    NOT NULL,            -- intended|executed|failed|undone|denied|declined
    mutates      INTEGER NOT NULL DEFAULT 1,
    inverse_json TEXT,
    result_hash  TEXT,
    provenance   TEXT    NOT NULL DEFAULT 'untrusted',
    error        TEXT
);

CREATE INDEX IF NOT EXISTS ix_journal_plan ON journal (plan_id, seq);
CREATE INDEX IF NOT EXISTS ix_journal_status ON journal (status);

-- History is immutable. Only the outcome fields may ever change, and only
-- while the row is still open. This is a database-level guarantee, not a
-- convention the application promises to honour.
CREATE TRIGGER IF NOT EXISTS journal_append_only
BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only')
    WHERE NEW.call_id   IS NOT OLD.call_id
       OR NEW.plan_id   IS NOT OLD.plan_id
       OR NEW.seq       IS NOT OLD.seq
       OR NEW.tool      IS NOT OLD.tool
       OR NEW.args_json IS NOT OLD.args_json
       OR NEW.decision  IS NOT OLD.decision
       OR NEW.risk      IS NOT OLD.risk
       OR NEW.rev       IS NOT OLD.rev
       OR NEW.ts_intent IS NOT OLD.ts_intent
       OR NEW.mutates   IS NOT OLD.mutates
       OR OLD.status NOT IN ('intended', 'executed');
END;

-- REPLACE (and INSERT OR REPLACE) resolves a key conflict by DELETING the old
-- row, and SQLite fires no delete trigger for that unless recursive_triggers
-- is on - a per-connection setting nobody else has to use. So the delete and
-- update triggers alone let any connection rewrite a row. This guard refuses
-- the insert itself whenever its key is already present, on every connection.
-- It checks the primary key only: a future UNIQUE constraint on another column
-- would need the same guard.
CREATE TRIGGER IF NOT EXISTS journal_no_overwrite
BEFORE INSERT ON journal
WHEN EXISTS (SELECT 1 FROM journal WHERE call_id = NEW.call_id)
BEGIN
    SELECT RAISE(ABORT, 'journal rows are never overwritten');
END;

-- The folders the user granted each plan, so undo can be judged against the
-- same approval. Written once at the freeze, never changed.
CREATE TABLE IF NOT EXISTS plan_grants (
    plan_id    TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    paths_json TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS plan_grants_no_update
BEFORE UPDATE ON plan_grants
BEGIN
    SELECT RAISE(ABORT, 'plan grants are append-only');
END;

CREATE TRIGGER IF NOT EXISTS plan_grants_no_delete
BEFORE DELETE ON plan_grants
BEGIN
    SELECT RAISE(ABORT, 'plan grants are never deleted');
END;

-- See journal_no_overwrite: without this, REPLACE could widen a plan's grant.
CREATE TRIGGER IF NOT EXISTS plan_grants_no_overwrite
BEFORE INSERT ON plan_grants
WHEN EXISTS (SELECT 1 FROM plan_grants WHERE plan_id = NEW.plan_id)
BEGIN
    SELECT RAISE(ABORT, 'plan grants are never overwritten');
END;

-- Nothing is ever deleted from the journal.
CREATE TRIGGER IF NOT EXISTS journal_no_delete
BEFORE DELETE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal rows are never deleted');
END;
