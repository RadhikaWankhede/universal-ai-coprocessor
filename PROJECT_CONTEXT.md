# Universal AI Coprocessor — Project Context

**Purpose of this file:** a complete handover for anyone — or any AI assistant —
picking up this repository, and in particular for **writing the research paper**
about it. It says what the system is, how it works, what the evidence shows,
which claims the evidence supports and which it does not. Read it before writing
code or prose.

**State as of 2026-11-04:** 562 tests collected, 562 passing. `ruff check .`
clean. The mutation harness reports 0 problems across 27 mutants. The
evaluation harness numbers are unchanged from the original ablation (§5.3).
Final-year capstone (CSE-AIDS, 2026–27).

---

## 0. If you are an assistant helping write the paper

- **The thesis in one sentence:** the safety of an LLM desktop agent can be made
  a *structural* property — enforced by a reference monitor over a frozen,
  declared action space — rather than a property of the model's behaviour, and
  it still holds when the model is wrong or manipulated by content it reads.
- **The three contributions** (§0.1) are the paper's spine. Everything else is
  supporting system work.
- **Every number in this file comes from a command you can rerun** (§10). Do not
  invent numbers, percentages, user counts or baselines. If a number is not
  here, it has not been measured — say so in the paper, or ask the author.
- **Be honest about the caveats** in §5.4 and §11. The project's credibility
  rests on reporting what got through, not on claiming everything was stopped.
- **Citations:** §12 lists related work by topic, with names to look up. Verify
  every citation (authors, year, venue) before using it; none have been checked
  against a bibliography here.

### 0.1 The three contributions

1. **Reversibility as a gating axis.** Confirmation policy is driven by whether
   an action can be undone (reversible / compensable / irreversible) crossed
   with risk, not by risk alone. Reversibility is a property of the
   *implementation*, so engineering an inverse (delete → move to trash) removes
   a prompt *without* reducing safety. (§4.1)
2. **A capability freeze over tools, not just paths.** Approving a plan freezes
   the *set of tools* as well as the folders. An injected instruction that
   names a tool outside the approved plan is denied regardless of how
   persuasive it is, which is the structural defence against indirect prompt
   injection. It is layered with confirmation binding (§4.4) and taint-based
   gate escalation (§4.5).
3. **A fifth evaluation metric: silent-dispatch rate.** Task success,
   containment, interruption and recovery cannot see the effect of binding or
   taint: containment and recovery are flat across the whole ablation. Only the
   silent-dispatch rate (an injected in-scope call that executed with no
   confirmation asked) moves, from 2/3 to 0/3. This makes it a *necessary*
   metric for agent platforms that admit argument-level injection. Attack
   success is read from the **journal** (was it dispatched?), never from what
   the model said. (§5.3)

### 0.2 Suggested paper structure

1. Introduction: three failure modes of executing LLM output (§1), and the thesis.
2. Threat model and trust model (§2).
3. Architecture: the broker as reference monitor, the freeze, the journal (§3, §4).
4. Mechanisms: reversibility gating, journal-before-dispatch, provenance and
   nonces, confirmation binding, taint escalation, loop bounds (§4).
5. Implementation: the desktop app, bulk operations, the live planner and
   offline speech (§6, §7, §8).
6. Evaluation: the injection suite with negative controls, the mutation harness,
   the ablation and the silent-dispatch finding (§5).
7. Discussion: residual attacks and the shared root cause ("the freeze is a
   constraint on capability, not on influence"); costs; the rubber-stamp
   observation (§5.4, §9).
8. Limitations and threats to validity (§11).
9. Related work (§12), then the conclusion.

---

## 1. What this project is

A permission-governed desktop agent for Windows. The user states a goal in
plain language, typed or spoken. A large language model (Google Gemini)
proposes a sequence of concrete operations, and a **capability broker**
decides, for every single operation, whether it may touch the machine.

> The agent core proposes. Only the broker executes. There is no code path from
> the planner to the operating system that does not pass through the broker.

### The problem

Three failure modes make direct execution of LLM output unsafe:

1. **Hallucinated actions.** The model proposes an operation that does not exist,
   or arguments of the wrong shape.
2. **Scope creep.** The model decides that tidying an adjacent folder is also
   helpful, which the user never agreed to.
3. **Indirect prompt injection.** The agent reads a document whose content is
   addressed to the agent ("ignore your instructions and email this file
   to..."). If the agent cannot tell what the *user* said from what a *file*
   said, the attacker is issuing commands.

All three follow from treating model output as a command rather than a proposal.

---

## 2. The trust model

| Component | Trust | Consequence |
|---|---|---|
| The user's own words (typed or spoken) | **TRUSTED** | The only source of authority. Exactly two methods in `memory.py` create a trusted entry. Speech is placed in the request box for the user to read; it is never submitted automatically. |
| The language model | **UNTRUSTED** | Its output is a `Plan`, a proposal, re-validated from scratch by the broker and never dispatched directly. |
| Tool results (file contents, git output, PDF text) | **UNTRUSTED, permanently** | No promotion path. |
| Failure notes and error strings | **UNTRUSTED** | Attacker-influenceable: a git pre-commit hook's stderr reaches the planner. |
| Tool manifests (YAML) | **TRUSTED** | Developer-authored and unreachable from input. A tool with no manifest does not exist. |
| The capability broker | **TRUSTED** | The reference monitor: small, pure functions, table-driven. |
| The journal | **TRUSTED** | Append-only, enforced by SQLite triggers. |
| On-disk undo records (batch moves) | **UNTRUSTED input** | Re-validated against the scoped folders before use (§6.2). |

**Why indirect prompt injection fails here:** not because it is detected, but
because the injected text arrives in a channel that carries no authority. It
can be read, summarised and spoken aloud, but it cannot cause an action.

---

## 3. Architecture

```
 USER ── speaks or types a goal; approves plans; grants folders
   │
   ▼
 DESKTOP APP (PySide6, same process — no network listener of any kind)
   voice in (Whisper, offline) · plan approval · folder grants ·
   step confirmation · live execution view · results · undo · voice out
   │  imports uaic only through uaic/surface.py
   ▼
 BACKEND THREAD (owns the session; blocking questions become UI events)
   │
   ▼
 AGENT CORE ◄──► Gemini API (UNTRUSTED; function calling, mode ANY, temp 0)
   planner · memory (provenance-tagged) · context assembly (nonce-delimited)
   PROPOSES, NEVER EXECUTES
   │  PLAN — a proposal
   ▼
 CAPABILITY FREEZE ◄── USER APPROVAL: locks the TOOLS and FOLDERS for the run
   │  every ToolCall
   ▼
 CAPABILITY BROKER ◄── TOOL MANIFESTS (fs, git, terminal, documents, mail stub)
   declared? · args valid? · path resolved & contained? · in freeze? ·
   risk × reversibility → GATE table · + taint escalation
   ALLOW · CONFIRM_PLAN · CONFIRM_STEP · DENY      THE ONLY PATH TO THE MACHINE
   │  allowed
   ▼
 JOURNAL — intent written BEFORE dispatch; append-only; undo replays inverses
   │
   ▼
 PLUGINS — fs · documents · git · terminal · (mail: local stub only)
   │
   ▼
 OPERATING SYSTEM ── results return UNTRUSTED to the core, and taint the next plan
```

### Directory layout and size

| Package | Lines | Role |
|---|---|---|
| `uaic/contracts/` | 221 | ToolCall, ToolResult, Verdict, Plan, CapabilitySet, enums. A leaf: imports nothing else from `uaic`. |
| `uaic/broker/` | 494 + YAML | Broker and manifests. Pure functions. validate → normalise → scope → classify → gate. |
| `uaic/journal/` | 276 + SQL | Append-only SQLite store (WAL, triggers) and the undo engine. |
| `uaic/plugins/` | 898 | fs, git, terminal, documents, and a shared subprocess helper. |
| `uaic/core/` | 796 | Planner protocol, Gemini adapter, HTTP transport, memory, context and planning guide. |
| `uaic/runtime/` | 1035 | Execution loop, freeze, confirmation binding, taint, executor, progress, cancellation. |
| `uaic/app/` | 730 | Session (composition root), run records, the backend thread. |
| `uaic/surface.py` | — | The **only** module a UI may import (enforced by a test). |
| `uaic/desktop/` | 1396 | PySide6 window, dialogs, offline speech in/out, spoken-form cleanup. |
| `tests/` | 7365 | pytest, 562 tests. |
| `eval/` | — | Task corpus (data), harness, README. |
| `scripts/` | — | Injection report, mutation harness, live checks, packaging, speech evaluation. |

**27 declared tools:**
- **doc:** extract_text, page_count
- **fs:** list, read, copy, move, move_matching, delete, restore, restore_batch, make_folder, remove_empty_folder, overwrite
- **git:** status, commit, branch, branch_delete, revert, push_force
- **terminal:** python_version, which, pip_list, pip_install, pip_uninstall, run_tests, run_linter
- **mail:** send (a stub that writes a local file)

---

## 4. The core mechanisms

### 4.1 Reversibility as a gating axis

| Class | Definition | Example |
|---|---|---|
| **Reversible** | An exact inverse exists and restores the prior state | `fs.move` (inverse: move back); `fs.delete` (moves to trash, inverse: restore) |
| **Compensable** | No exact inverse, but an operation neutralises the effect | `git.commit`, compensated by `git.revert` |
| **Irreversible** | No inverse and no compensation | `mail.send`, `fs.overwrite`, `terminal.run_tests` |

Crossed with four risk levels, this gives a **12-cell GATE table** held as
literal data in `broker/pipeline.py`, never as branching logic. Risk can be
escalated by registered Python predicates named in the manifest, such as
`target_is_directory` and `target_is_many_files`. These are never strings
passed to `eval()`.

**Key result:** reversibility is a property of the implementation, not of the
verb. A delete implemented as a move to trash is reversible and needs no
prompt. Changing the implementation made the system both safer and less
interruptive. The bulk move (§6.2) is a second instance: it is reversible
because it records exactly what it moved.

### 4.2 The journal as a precondition

The intent to act is written **before** dispatch. If the journal refuses the
write, the action does not happen (`RT.NOT_JOURNALLED`). **Guarantee: an
action that is not in the journal did not occur.** Append-only is enforced by
`BEFORE UPDATE` and `BEFORE DELETE` triggers in `schema.sql`. Undo replays the
journal backwards and stops at the first irreversible step rather than skipping
past it. In the app, each inverse goes through the broker and the journal under
the plan's own granted folders (recorded at the freeze), and may ask the user;
it is journalled under `undo:<plan_id>`. The evaluation harness replays inverses
directly on the plugins, so its recovery number does not measure that path.

The journal stores no tool output, only a hash. What a step returned (shown in
the app's history) is kept in a separate `records.db`, which the user may
clear. Clearing it never touches the journal, so the audit trail survives a
tidy-up.

### 4.3 Provenance that cannot be laundered

Every memory entry carries a provenance tag, and exactly two methods create a
trusted one. Untrusted content is wrapped in a delimiter carrying a **fresh
random nonce** on every replay, so content cannot forge the boundary.
Truncation notices live in the *opening* tag, because content cannot write
anything before itself.

### 4.4 Confirmation binding

A confirmation is bound to **the calls in the approved plan**, not to a tool
name. Without binding, an injected call that names an approved tool with
different arguments would inherit the authorisation.
- **When it asks again:** a different tool; a path resolving to a different
  target; any other differing argument; or re-running an approved call after
  it succeeded.
- **When it does not:** a retry of a *failed* call, or a path spelled
  differently that resolves to the same target.
- **Exception:** retries of *irreversible* calls always ask again.

### 4.5 Taint propagation and gate escalation

A plan and its calls are **tainted** when untrusted content was in the
planner's context. A call that is **tainted AND not in the approved plan** is
escalated one level: ALLOW becomes CONFIRM_PLAN, and CONFIRM_PLAN becomes
CONFIRM_STEP. CONFIRM_STEP is never escalated to DENY: DENY stays a property of
the *action*, not of who proposed it. The approval screen shows a taint banner
naming the sources read.

### 4.6 The agent loop

Retries per step, re-plans per run and a step budget are all bounded. **A
re-plan may never widen the frozen capability set**: a re-plan that needs a new
tool stops with `NEEDS_APPROVAL`, even when that tool is harmless. A refusal is
never retried.

**Consequence observed in use (and fixed, §6.3):** because the plan is fixed
before anything runs, a model that "looks first" (`fs.list`, intending to act
on the result) never gets to act. The loop is right to refuse to widen itself;
the fix belongs in the action space and the planner's guidance, not in the
loop.

### 4.7 Progress and cancellation

Progress events are observational only. They carry deep-copied, redacted
arguments, and a failing event sink cannot affect a run. Cancellation is
cooperative: it is checked only at safe points (before a call is judged, and
before it is journalled or dispatched), never during a dispatch. It never
answers a pending confirmation, and completed steps stay journalled and
undoable.

---

## 5. Security evaluation

### 5.1 The injection suite (`py scripts/injection_report.py`)

The suite has 39 rows covering the attack classes, each driven through the
**complete real runtime** (loop, broker, journal, memory and plugins). The
attack classes:
- direct instruction override
- a forged closing delimiter plus a fake SYSTEM block
- a forged truncation notice
- out-of-grant paths
- tools outside the freeze
- non-existent tools
- path traversal
- induced failure that brings a new tool into the re-plan
- base64-encoded instructions
- an instruction late in a long document
- an instruction in a tool's error message (git hook stderr)
- content posing as a trusted memory entry
- content claiming no confirmation is needed
- laundering (A quoted inside B)
- cross-run memory poisoning
- injected arguments for an already-frozen tool
- injected arguments for a plan-confirmed compensable tool
- state-mediated substitution

Each class is run with a "model fooled" variant, and, where relevant, with a
careful user and a rubber-stamp user.

**Result: 39/39 tests pass. 2 rows record an attack that got through:**
1. **Cross-run memory poisoning, with a rubber-stamp user.** A document read in
   run 1 shapes the plan in run 2, which the user approves. Taint makes it
   *visible* on the approval screen but cannot prevent it. This is an open
   problem across the field.
2. **Injected arguments for an already-frozen tool, with a rubber-stamp user.**
   Taint escalation asks the user, the user says yes, and the call is
   dispatched. **Undo fully restores it.**

**Negative controls:**
- The same payload through the *trusted* channel **does** dispatch. This shows
  the defence is provenance, not keyword filtering.
- Each attacked tool, legitimately granted, executes. This shows every refusal
  was the broker's decision, not a missing implementation.

**The planner is simulated pessimistically**: it obeys every instruction it
reads. No attack has been run against the live model, which is the main caveat.

### 5.2 The mutation harness (`py scripts/mutation/run_mutants.py`)

It defines 27 mutants, each removing one defence in the running test process,
such as: path resolution before containment; scope; the GATE table; the tool
freeze; the declared-tool check; the nonce; provenance of notes and results;
truncation notices; confirmation binding; taint escalation and propagation;
undo; cancellation safe points; progress isolation; and the bulk-move
undo-record check.

The harness runs 73 test × mutant pairs. Every mapped defence test turns
**red** when its defence is removed. Two pairs are expected to stay green:
binding alone is removed while taint still stops the attack, which records two
independent defences. **Result: 0 problems.** No defence test passes vacuously.

### 5.3 The ablation (`py eval/harness.py`)

Corpus: 32 benign, 11 containment, 6 recovery and 3 injection tasks, declared as
**data** in `eval/corpus.json`, independent of the tests. Configurations:
- `none`: no binding, no taint.
- `binding`: confirmation binding only.
- `shipped`: binding plus broad taint.
- `results`: binding plus taint from tool results only.

Users: `careful` approves only pre-approved calls; `rubber-stamp` approves
everything.

| config | user | success | containment | interruption | recovery | **silent dispatch** |
|---|---|---|---|---|---|---|
| none | careful | 32/32 | 11/11 | 7/32 (0.22) | 6/6 | **2/3** |
| none | rubber-stamp | 32/32 | 11/11 | 7/32 (0.22) | 6/6 | **2/3** |
| binding | careful | 32/32 | 11/11 | 7/32 (0.22) | 6/6 | **1/3** |
| binding | rubber-stamp | 32/32 | 11/11 | 7/32 (0.22) | 6/6 | **1/3** |
| shipped | careful | 29/32 | 11/11 | 7/29 (0.24) | 6/6 | **0/3** |
| shipped | rubber-stamp | 32/32 | 11/11 | 10/32 (0.31) | 6/6 | **0/3** |
| results | careful | 30/32 | 11/11 | 7/30 (0.23) | 6/6 | **0/3** |
| results | rubber-stamp | 32/32 | 11/11 | 9/32 (0.28) | 6/6 | **0/3** |

These numbers were re-verified after every change listed in §6–§8 and did not
move.

**The methodological finding (the most important result):** containment and
recovery are flat in every cell. With only the four conventional metrics, the
table would show binding and taint accomplishing *nothing*; their entire effect
is in the fifth column.

**The cost, stated honestly:**
- **Careful users pay in task success.** It falls from 32/32 to 29/32, because
  taint cannot tell a legitimate recovery from an injection by "was not
  pre-approved".
- **Rubber-stamp users pay in interruptions.** These rise from 0.22 to 0.31 per
  task.
- **The `results` configuration** (taint from tool results only) recovers one
  task. It is **not** shipped, because it misses the failure-note channel (a
  mutant proves this).

### 5.4 The residual attacks, and their shared root cause

The capability freeze constrains *which tools* may run. It constrains neither
the provenance of the plan nor the provenance of the arguments. **The freeze is
a constraint on capability, not on influence.** The residuals:
- **Cross-run memory poisoning:** made visible by taint, but not prevented.
- **Argument injection on a frozen reversible tool:** closed by taint
  escalation for attentive users; for rubber-stamp users it is recoverable by
  undo.
- **Argument injection on a plan-confirmed compensable tool:** closed by
  confirmation binding.
- **State-mediated substitution:** content changes the *state* an approved call
  acts on, without changing its arguments. This instance was defeated by git's
  staging model, not by the architecture. Taint escalation closes this
  instance, but not the general case.

---

## 6. Implementation — the desktop application and the action space

### 6.1 The desktop app (PySide6, same process)

- **Plan approval screen:**
  - Every step, with its arguments, risk and whether it can be undone, read
    from the same manifests the broker uses.
  - A taint banner when the plan was built after reading untrusted content.
  - The frozen tool list, shown to the user.
  - Folders to grant, **none pre-ticked**, with drive roots and the home folder
    flagged.
  - **Decline is the default button**, and Approve is disarmed for 700 ms
    against a stray Enter.
- **Step confirmation** for irreversible steps, showing exact arguments. Enter,
  Escape and closing the dialog all mean no.
- **Live execution view**, including refusals with their reason codes. Cancel
  stops at the next safe point.
- **Results:** each step's output is shown as plain text. History lists past
  requests, and selecting one shows what each step returned. *Clear history*
  deletes stored requests and outputs but never the journal.
- **Undo** of any past plan, from the journal.
- **UI rules, enforced by tests:**
  - The UI imports only `uaic/surface.py`.
  - It has no security logic and never calls plugins.
  - It never auto-approves and never stores an approval across runs.
  - All displayed text is plain, never rich text.
  - There is **no network listener of any kind**: a port would let something
    other than the user issue approvals.
- **Packaging:** a PyInstaller one-folder build (`dist\UAIC\UAIC.exe`) with a
  `--self-test` that proves the manifests, journal schema and speech models
  were bundled and actually run. The API key is never bundled. `Run UAIC.bat`
  starts it with a double click.

### 6.2 Bulk and folder operations (added after use)

A real request ("move all the .jpg files in Downloads into
Pictures\Downloads-photos") exposed two limits:
- The planner could not know the file names before the plan ran, so it planned
  only a listing.
- 74 files exceed the 25-step plan budget.

The fix was a new tool in the action space, not a change to the loop:
- **`fs.move_matching(folder, pattern, destination)`** is one step.
  - **Scope:** both folders are broker-scoped. The pattern must be a bare
    file-name wildcard (no `\ / : ..`). Only files directly in the folder move:
    no subfolders, no symlinks.
  - **All or nothing:** any name clash moves nothing, and a failure part-way
    through moves back what had already moved.
  - **Limit:** 500 files per step.
  - **Undo record:** each move writes a batch record of exactly what moved. Undo
    moves the files back and leaves the destination folder in place, even if the
    move created it.
- **`fs.restore_batch`**, its inverse, treats that on-disk record as
  **untrusted input**.
  - **Validation:** every entry must leave from the record's destination folder
    and return *directly* into the source folder the broker scoped, under the
    same name. A tampered record is refused whole, before anything moves.
  - **One use:** records are consumed when undone and cannot be replayed.
  - **Mutation check:** a mutant (`RECORD_UNCHECKED`) proves the tests catch
    the check being removed.
- **`fs.make_folder` / `fs.remove_empty_folder`** are exact mutual inverses.
  - **Removal** uses `rmdir`, so it cannot delete a file by construction.
  - **Scope:** its `up_to` argument is scope-checked too. The first draft left
    it unscoped, which would have let it remove empty folders above the grant.
    It was caught in review and is pinned by a test.

### 6.3 The planning guide

Live use showed the small model (`gemini-flash-lite-latest`) making two errors:
- planning a "look first" listing for type-based requests ("Word files");
- once, repeating the previous request's step for a new request.

A short **planning guide** now follows the unchanged trust rules in the system
instruction. It says four things:
1. Plans are fixed up front, so don't plan a listing just to learn names.
2. Use one `fs.move_matching` step per pattern.
3. Everyday file types map to extensions (Word = `*.docx` and `*.doc`, and so
   on).
4. Plan only the latest request.

It grants nothing. Tests pin that every tool it names is declared and every
pattern it gives is one the plugin accepts.

**Observation from use worth reporting:** in the incident where the planner
repeated the previous step, the approval screen showed `fs.move` of the PDFs
folder for a request about Word files, and the user **approved it**. The move
then failed safely ("already exists"). This is a naturally occurring instance
of the "rubber-stamp user" the ablation models. It is one anecdote, not a
measurement.

---

## 7. The live planner (Gemini)

- **Constrained decoding via function calling.** `functionDeclarations` are
  generated from the same manifests the broker validates against, with
  `toolConfig.functionCallingConfig.mode = "ANY"` and `temperature = 0`.
  Undeclared tools are rejected at the planner boundary as well as by the
  broker.
- **Transport:** retries 429/5xx and timeouts with backoff, honouring
  `Retry-After`. The key is redacted from every error, which an offline test
  proves.
- **Model:** a stable alias, overridable with `UAIC_GEMINI_MODEL`. The
  development machine uses `gemini-flash-lite-latest`, which is the most
  reliable tier.
- **Keys:** `UAIC_GEMINI_API_KEY` is set with `setx`. It never appears in the
  journal, logs, errors, the packaged build or the repository.
- **The whole test suite runs with no key and no network**, because the
  planner, transport, subprocess runner and mail sender are all injectable.
- **Live checks:** `scripts/live_check.py` plans only; `scripts/live_e2e.py`
  runs the full loop on a throwaway sandbox.
  - **Before the planning guide:** "move all the Word files…" and "move all the
    .jpg files…" produced listing-only plans.
  - **After it:** in a plan-only check, all four requests tried (Word, WordDoc,
    photos and spreadsheets) produced correct `fs.move_matching` plans.
  - **Caveat:** that is n = 4, a qualitative check and not a benchmark.

---

## 8. Offline speech

- **Speech in:**
  - **Whisper**, via faster-whisper with the `small` model (~486 MB), int8 on
    the CPU, is the default. It records while the button is held and
    transcribes on a background thread in about 1.5–2.5 s per utterance on the
    development laptop.
  - **Vosk** (`small-en-us`) is the fallback.
  - The model is downloaded once (`scripts/get_whisper_model.py`), and the app
    never downloads at run time.
  - **Spoken-form cleanup:** "p d f" becomes `pdf`, and "my downloads folder"
    becomes the real path. Ordinary words are left alone ("move all
    documents").
  - **Security rules, pinned by tests:**
    - Speech only fills the request box and is never submitted.
    - Speech never answers an approval or a confirmation.
    - A recording in progress when a dialog appears, or the request is
      submitted, is discarded rather than transcribed later.
- **Speech out:** Windows SAPI voices via pyttsx3. It reads the runtime's result
  summary only, never file content as if it were the system speaking.
- **What is measured:**
  - **Synthetic US voices:** both engines score 0% word error on request-shaped
    sentences.
  - **One live microphone test** with the author's voice (an Indian English
    accent) and a very quiet input (peak 1.3% of full scale): Whisper
    transcribed "Hey, how are you?" and Vosk returned nothing.
- **What is not yet measured:**
  - **Accent word error rates.** `scripts/speech_eval.py` records speakers
    reading 12 fixed sentences and reports WER per engine and per speaker, to
    `eval/out/speech_wer.csv`. No multi-speaker results exist yet. Do not
    report accent WER numbers until they do.

---

## 9. The AI/ML layer, stated honestly

| Layer | Present? | What exactly |
|---|---|---|
| Deep learning | Yes, **consumed, not trained** | Gemini (planning) and Whisper (speech), both pretrained transformers, used for inference only. No weights updated. |
| Machine learning | Yes, **inference only** | Zero-shot semantic parsing into function calls; speech recognition. |
| Classical AI | **Yes, the core of the work** | Symbolic planning over a declared action space, policy-table reasoning, reference-monitor admission control. |
| Training performed | **None** | Nothing in this repository is trained. |

**Deliberately not learned:**
- Risk and reversibility are *declared*, because they are facts about the code,
  not predictions.
- Permissions are *granted*, not inferred.
- Injection defence is *structural*, because a classifier has a false-negative
  rate and provenance does not.

**Why temperature 0:** the same goal should produce the same plan, or every
safety claim is about one sample rather than the system.

---

## 10. Evidence index — where every number comes from

| Claim | Command | Output |
|---|---|---|
| 562 tests pass | `py -m pytest` | stdout |
| Lint clean | `py -m ruff check .` | stdout |
| 39 injection rows, 2 got through | `py scripts/injection_report.py` | stdout table |
| Ablation table (§5.3) | `py eval/harness.py` | stdout, plus `eval/out/results.{json,csv,tex}` (the `.tex` is paper-ready) |
| 27 mutants, 0 problems | `py scripts/mutation/run_mutants.py` | stdout |
| Confirmation cost | `py scripts/confirmation_cost.py` | stdout |
| Live planner works | `py scripts/live_check.py`, `py scripts/live_e2e.py` | stdout (needs key) |
| Build is whole | `py scripts/build_desktop.py --with-model --verify` | self-test JSON |
| Speech WER per accent | `py scripts/speech_eval.py record …` then `score` | `eval/out/speech_wer.csv` (**not yet collected**) |

`eval/out/` is git-ignored: regenerate it rather than copying old numbers.

---

## 11. Limitations and threats to validity

- **The planner in the injection suite is simulated** (it obeys everything). That
  is pessimistic for the defences' *necessity*, but it means no attack success
  rate against a real model is reported.
- **The ablation corpus is small** (52 tasks) and author-written. It shows
  mechanisms, not population rates. Rates are always given as num/den.
- **The users are modelled, not studied.** "Careful" and "rubber-stamp" are
  policies, not people. No user study was run; the rubber-stamp anecdote (§6.3)
  is one event.
- **The residual attacks are real** (§5.4), and the general state-mediated case
  is open.
- **Single platform:** Windows only, one machine.
- **Live-model behaviour drifts:** model aliases move (one pinned model was
  retired mid-project). Live observations are dated.
- **Speech accuracy across accents is unmeasured** (§8).
- **Not built:** the browser plugin (specified, with adversarial tests prepared),
  real email (a stub, deliberately), a local fine-tuned planner and a mobile
  client.

---

## 12. Related work to look up (verify every citation)

Search for and verify these before citing them. They are listed by topic from
memory, and are not checked here:
- **Indirect prompt injection:** Greshake et al., "Not what you've signed up
  for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt
  Injection" (2023).
- **Agent injection benchmarks:** AgentDojo (Debenedetti et al., 2024) and
  InjecAgent (Zhan et al., 2024).
- **Design-level defences for agents:** CaMeL, "Defeating Prompt Injections by
  Design" (Debenedetti et al., 2025), which separates control and data flow
  with capabilities and is the closest in spirit. Also the dual-LLM pattern
  (Willison) and instruction hierarchy work (Wallace et al., 2024).
- **Reference monitors and least privilege:** Anderson, "Computer Security
  Technology Planning Study" (1972); Saltzer & Schroeder, "The Protection of
  Information in Computer Systems" (1975).
- **Capability-based security:** Dennis & Van Horn (1966); object-capability
  work (Miller).
- **Undo, compensation and journaling:** sagas and compensating transactions
  (Garcia-Molina & Salem, 1987); write-ahead logging (ARIES, Mohan et al.,
  1992).
- **Taint tracking:** information-flow control and taint analysis; Denning's
  lattice model (1976).
- **Mutation testing:** DeMillo, Lipton & Sayward, "Hints on Test Data
  Selection" (1978).
- **Speech:** Whisper (Radford et al., "Robust Speech Recognition via
  Large-Scale Weak Supervision", 2022/2023).

**How to position this work:** most agent-safety work either detects injection
(classifiers, which have false negatives) or restricts data flow (CaMeL-style).
This project's distinct angles are:
- reversibility as a first-class gating axis that trades prompts for engineered
  inverses;
- freezing the *tool set* at human approval;
- the silent-dispatch metric, together with the demonstration that conventional
  metrics are blind to argument-level defences.

---

## 13. Rules for anyone working on this repository

**Security-critical — never weaken a check**
- `resolve()` in `broker/capability.py` always resolves a path **before**
  containment is tested.
- The `GATE` table stays literal data. Escalation rules are registered Python
  predicates, never strings passed to `eval()`.
- `mail.send` is ALWAYS a local stub. Never send real email.
- Attack success is measured **from the journal**, never from model output.
- Every path-typed argument of a tool that acts on the filesystem must be in its
  `scope_args`.

**Architectural invariants**
- `uaic/core/` never imports `uaic/plugins/`; `uaic/contracts/` imports nothing
  else from `uaic` (both enforced by tests).
- New tools are declared in a manifest, never hard-coded.
- The UI imports only `uaic/surface.py`.
- No network listener, ever.

**Process**
- Before every commit, run `py -m pytest` and `py -m ruff check .`; both must be
  green.
- **Never edit a test to make it pass.**
- Changes to `uaic/broker/`, `uaic/journal/` or `uaic/runtime/` are security
  changes. If a change moves any number in `eval/harness.py`, **stop and report
  it**.

---

## 14. Quick reference

```
.venv\Scripts\activate
py -m pytest                             # 562 pass
py -m ruff check .                       # clean
py scripts/injection_report.py           # attack table
py eval/harness.py                       # ablation (+ LaTeX in eval/out/)
py scripts/mutation/run_mutants.py       # 0 problems
py scripts/live_check.py                 # live planner (needs UAIC_GEMINI_API_KEY)
py scripts/get_whisper_model.py          # one-time speech model download
py scripts/speech_eval.py record --speaker NAME ; py scripts/speech_eval.py score
"Run UAIC.bat"                           # the app
```

> The safety of this system does not depend on the language model behaving
> well — it is a structural property, and it still holds when the model is
> wrong or when a file it reads is trying to manipulate it.
