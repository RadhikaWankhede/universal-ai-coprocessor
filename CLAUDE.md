# Instructions for Claude Code

Standing rules for every session in this repository. What the project *is*, how
it is laid out, and how to install and run it live in [README.md](README.md) —
read that first; this file does not repeat it.

## Before you commit

- Run the test suite and the linter (commands in README.md). Both must be green
  before any commit. Never commit with failing tests or lint errors.
- **Never edit a test to make it pass.** Report the failure instead.

## Security-critical — never weaken a check

The Capability Broker (`uaic/broker/`), the Action Journal (`uaic/journal/`) and
the runtime loop (`uaic/runtime/`) are the contribution. Treat changes to them
as security changes.

- `resolve()` in `broker/capability.py` must always resolve a path **before**
  containment is tested. Removing `.resolve()` silently lets `..` traversal through.
- The `GATE` table in `broker/pipeline.py` stays literal data, never if/else logic.
- Escalation rules are registered Python predicates, never strings passed to
  `eval()`. A manifest must never become a code-execution path.
- `mail.send` is ALWAYS a stub that writes to a local file. Never send real
  email — the injection tests would exfiltrate for real.
- Every path-typed argument of a tool that touches the filesystem must be listed
  in its manifest's `scope_args`. An unscoped path argument is a way out of the
  granted folders (`fs.remove_empty_folder`'s `up_to` nearly shipped unscoped).
  A scoped argument cannot be optional, because the broker refuses a missing one
  as unresolvable, so make it required.
- Anything a plugin reads back from disk to decide what to do (such as the
  `fs.restore_batch` undo records) is untrusted input. Re-validate it against
  the scoped arguments before acting.
- Attack success is measured from the journal (was a dangerous call
  *dispatched*), never from model output.

## Architectural invariants

- `uaic/core/` must never import `uaic/plugins/` (enforced by
  `tests/test_architecture.py`). A UI or planner must not reach into plugins.
- `uaic/contracts/` imports nothing else from `uaic`; it is a leaf.
- New tools are declared and classified in a `uaic/broker/manifests/*.yaml`
  manifest, not hard-coded. The manifest is the single source of truth.

## Conventions

- Python 3.12 or later (the development machine uses 3.14), launched as `py`; virtualenv at `.venv`.
- Pydantic v2 for all contracts; `model_config = ConfigDict(frozen=True)` on
  anything that must not change after creation.
- Target platform is Windows: use `pathlib` plus `os.path.normcase`, and watch
  for 8.3 short names, drive letters and alternate data streams.
- SQLite with `PRAGMA journal_mode=WAL`; no ORM — the schema is part of the
  contribution.
- Evaluation results go to flat files, never into the journal DB.
