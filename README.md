# Universal AI Coprocessor

Final-year capstone (CSE-AIDS, 2026–27). A permission-governed agent platform
for natural-language desktop automation on Windows.

## What it does

You type a request in plain English ("organise my Downloads and summarise the
PDFs"). An LLM turns it into an ordered list of proposed tool calls. Every
single call is then checked by a **Capability Broker** before it can touch the
machine, recorded in an **append-only journal** before it runs, and can be
**undone** afterwards.

## The contribution

The novelty is not that an agent can act on a computer. It is:

1. **Reversibility drives confirmation policy.** Actions are classified by
   whether they can be undone (reversible / compensable / irreversible), not by
   how dangerous they sound. Only irreversible actions block for confirmation.
   Reversibility is a property of the *implementation*: routing delete to a
   trash folder makes it reversible, removing a prompt without reducing safety.

2. **The capability freeze includes the tool set, not just paths.** When you
   approve a plan, `CapabilitySet.tools` is frozen. If a poisoned PDF later
   convinces the planner to call `mail.send` and that tool was not in the
   approved plan, the broker denies it — however persuasive the injected text.
   This is the structural defence against indirect prompt injection, layered
   with confirmation binding and taint-based gate escalation (see
   `tests/test_injection.py` and `eval/`).

3. **Measured, not asserted.** The evaluation harness reports task success,
   containment, interruption (confirmations per completed task) and recovery,
   across an ablation of the defence layers. Attack success is read from the
   journal — whether a dangerous call was actually *dispatched* — never from
   what the model said.

## Requirements and install

- Windows, Python 3.13 (the project targets 3.12+). It is launched as `py` on
  the development machine.
- A virtualenv at `.venv`.

```
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

No API key is needed to run the tests, the evaluation harness or the scripts.
Only the live planner (Google Gemini, behind the `Planner` protocol) needs one,
via the `UAIC_GEMINI_API_KEY` environment variable.

## Running it

There is no end-user entry point yet: the LLM planner adapter and the HTTP/SSE
server are the next pieces. Today the system is exercised through its test
suite and the evaluation harness.

```
py -m pytest            # the full suite (~400 tests, 1 skipped where symlinks are unavailable)
py -m ruff check .      # lint; must be clean
py eval/harness.py      # run the task corpus and print the metrics table
```

## Layout

```
uaic/contracts/   ToolCall, ToolResult, Verdict, Plan, enums. A leaf: imports
                  nothing else from uaic. Everything imports it.
uaic/broker/      The Capability Broker and the YAML tool manifests. Pure
                  functions: no I/O, no model. validate → normalise → scope →
                  classify → gate → journal.
uaic/journal/     Action Journal (SQLite, WAL, append-only trigger) + Undo.
uaic/plugins/     fs, git, terminal, documents — each returns a ToolResult.
uaic/core/        Planner protocol, Gemini adapter, memory, context assembly.
uaic/runtime/     The execution loop, capability freeze, confirmation binding,
                  taint propagation.
tests/            pytest; table-driven where possible.
eval/             The evaluation harness and task corpus (see eval/README.md).
scripts/          injection_report.py, confirmation_cost.py, and the mutation
                  harness that proves each defence test can fail.
```

## Evaluation

`eval/` holds the quantitative results. `eval/corpus.json` is the task corpus
(data, not tests); `eval/harness.py` runs it through the real loop, broker,
journal and plugins and emits a table plus JSON/CSV/LaTeX. See
`eval/README.md` for the metrics and the ablation.

## Status

Built and tested: contracts, Plan, Capability Broker, capability freeze, Action
Journal, Undo Engine, the fs/git/terminal/documents plugins, the runtime
execution loop, the context trust boundary, manifest-derived tool schemas,
confirmation binding, taint propagation and gate escalation, the Gemini planner
behind the `Planner` protocol, and the evaluation harness. ruff clean.

Next: wiring the live planner end to end, and the FastAPI + SSE server.
