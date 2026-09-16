# Universal AI Coprocessor

Final-year capstone (CSE-AIDS, 2026–27). A permission-governed agent platform
for natural-language desktop automation on Windows.

## What it does

The user types a request in plain English ("organise my Downloads and summarise
the PDFs"). An LLM turns that into an ordered list of proposed tool calls. Every
single call is then checked by a Capability Broker before it can touch the
machine, recorded in an append-only journal before it runs, and can be undone
afterwards.

## The contribution — read this before changing anything

The project's novelty is NOT that an agent can act on a computer. It is:

1. **Reversibility drives confirmation policy.** Actions are classified by
   whether they can be undone (reversible / compensable / irreversible), not by
   how dangerous they sound. Only irreversible actions block for confirmation.
   Reversibility is a property of the *implementation* — routing delete to a
   trash folder makes it reversible, which removes a prompt without reducing
   safety.

2. **The capability freeze includes the TOOL SET, not just paths.** When the
   user approves a plan, `CapabilitySet.tools` is frozen. If a poisoned PDF
   convinces the planner to call `mail.send` and that tool was not in the
   approved plan, the broker denies it — regardless of how persuasive the
   injected text was. This is the structural defence against indirect prompt
   injection.

3. **Four metrics reported together:** task success, attack success,
   false-block rate, confirmations per task.

## Architecture

    uaic/contracts/   ToolCall, ToolResult, Verdict, enums. A LEAF — imports
                      nothing else from uaic. Everything imports it.
    uaic/broker/      The Capability Broker. Pure functions. No I/O, no model.
    uaic/journal/     Action Journal (SQLite, WAL, append-only trigger) + Undo.
    uaic/plugins/     fs, shell_git, documents, browser. Each returns ToolResult.
    uaic/core/        Planner, memory, context assembly. Uses the LLM.
    uaic/runtime/     Wiring and the execution loop.
    uaic/server/      FastAPI + SSE (not built yet).
    tests/            pytest. Table-driven where possible.

The broker pipeline: validate → normalise → scope → classify → gate → journal.
Stages 1–5 are pure, which is why the decision layer is exhaustively testable
without a language model.

## Hard rules

- **Never weaken a security check.** In particular `resolve()` in
  `broker/capability.py` must always resolve before containment is tested.
  Removing `.resolve()` silently lets `..` traversal through.
- **Never edit a test to make it pass.** Report the failure instead.
- `uaic/core/` must never import `uaic/plugins/`. Enforced by
  `tests/test_architecture.py`.
- `uaic/contracts/` imports nothing else from `uaic`.
- The GATE table in `broker/pipeline.py` stays literal data, not if/else logic.
- Escalation rules are registered Python predicates, never strings passed to
  `eval()`. The manifest must never be a code-execution path.
- `mail.send` is ALWAYS a stub that writes to a local file. Never send real
  email — the injection tests would exfiltrate for real.
- Attack success is measured from the journal (was a dangerous call
  *dispatched*), never from model output.
- Run `pytest` before every commit.

## Conventions

- Python 3.13, launched with `py` on this machine. Virtualenv at `.venv`.
- Pydantic v2 for all contracts. `model_config = ConfigDict(frozen=True)` on
  anything that must not change after creation.
- Target platform is Windows: use `pathlib` plus `os.path.normcase`, and watch
  for 8.3 short names, drive letters and alternate data streams.
- SQLite with `PRAGMA journal_mode=WAL`. No ORM — the schema is part of the
  contribution.
- Evaluation results go to flat files and pandas, never into the journal DB.
- No MCP transport yet. Manifests are internal; say so honestly in the report.

## Status

Contracts, Capability Broker, Action Journal, Undo Engine, filesystem plugin
and runtime execution loop all complete. Trash-backed delete implemented, so
deletion sits in the reversible tier. End-to-end demo runs: a five-step plan
where three reversible steps execute with zero prompts, an out-of-scope move
is denied, an unapproved mail.send is denied by the capability freeze, and
undo reverses all three executed steps including the trashed file.
129 tests, ruff clean.

Next: the Agent Core — planner over native LLM tool-calling, generating tool
schemas from the same manifests the broker validates against.