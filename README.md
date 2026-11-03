# Universal AI Coprocessor

Final-year capstone (CSE-AIDS, 2026–27). A permission-governed agent platform
for natural-language desktop automation on Windows.

For the full design, the evaluation results, the claims they support and the
limitations, see **[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)**.

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

3. **Measured with five metrics, not four.** The evaluation harness reports
   task success, containment, interruption (confirmations per completed task),
   recovery, and **silent-dispatch rate** — an injected in-scope call that
   executed with no confirmation asked. The fifth is necessary, not
   supplementary: containment and recovery are invariant across every
   configuration in the ablation, so a four-metric table would show
   confirmation binding and taint propagation accomplishing nothing, when they
   in fact take silent dispatch from 2/3 to 0/3. Attack success is read from
   the journal — whether a dangerous call was actually *dispatched* — never
   from what the model said.

## Requirements and install

- Windows, Python 3.13 (the project targets 3.12+). It is launched as `py` on
  the development machine.
- A virtualenv at `.venv`.

```
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt            # core, tests, eval, scripts
py -m pip install -r requirements-desktop.txt    # + the desktop app (PySide6, voice, packaging)
```

No API key is needed to run the tests, the evaluation harness or the scripts.

### The Gemini API key

Only the live planner needs a key. It is read from the **environment variable
`UAIC_GEMINI_API_KEY`**, never from a file in this repository. It never reaches
the journal, and it is scrubbed from every error message. Set it once for your
Windows user account:

```
setx UAIC_GEMINI_API_KEY "your-key-here"
setx UAIC_GEMINI_MODEL "gemini-flash-lite-latest"
```

The second line is optional; that model tier has been the most reliable.
`setx` only affects programs started afterwards: close and reopen the terminal,
and VS Code if you launch from its terminal. Check with `echo %UAIC_GEMINI_API_KEY%`
in a new `cmd` window. The packaged `UAIC.exe` reads the same variable.
Do not put the key in `.env`, in source or in a script: nothing reads those,
and they could end up committed.

## Running it

**Double-click `Run UAIC.bat`** to start the app. It runs from `.venv` (or from
`dist\UAIC\UAIC.exe` if there is no `.venv`), and reads the API key from your
Windows user environment even when Explorer was started before you ran `setx`.
If the install is broken, it shows the error instead of failing silently.

```
py -m uaic.desktop                   # the desktop app (needs requirements-desktop.txt)
py -m uaic.desktop --data-dir D      # keep the journal and trash somewhere else

py -m pytest                         # the full suite; the symlink test skips where
                                     # Windows does not permit symlinks
py -m ruff check .                   # lint; must be clean
py scripts/injection_report.py       # the attack table
py eval/harness.py                   # the ablation
py scripts/mutation/run_mutants.py   # proves every defence test can fail
```

The journal (`journal.db`) and the delete-to-trash folder live in
`%LOCALAPPDATA%\uaic` by default. Without an API key the app still opens:
history and undo work, planning is disabled, and the window says why.

### Using the desktop app

1. Type (or speak) a request and press **Plan** (Ctrl+Enter).
2. The **plan approval screen** shows every step: what it does, its exact
   arguments, its risk, and whether it can be undone. These are read from the
   tool manifests, the same data the broker gates on. Tick the folders you grant
   (none are pre-ticked), then approve or decline. Approval freezes the tools and
   folders for the whole run.
3. Steps that cannot be undone ask again, individually, with the exact
   arguments. Enter, Escape and closing the dialog all mean **no**.
4. The execution view shows each step live, including refusals and their
   reason codes. **Cancel run** stops at the next safe point; a step already
   running finishes, and everything that ran stays undoable.
5. **Undo selected plan** reverses a plan from the journal.

The plan is written before anything runs, so a request about files whose
names aren't known yet ("move all the .jpg files in Downloads to Pictures")
uses `fs.move_matching`. It is one step with a folder, a file-name wildcard
and a destination, so it isn't limited to 25 steps. It moves only files that
sit directly in that folder, moves nothing if any name clashes, and undo moves
back exactly those files. `fs.make_folder` creates folders; undo removes them
again only if they are still empty.

### Voice (optional, offline)

- **Speech in** uses Whisper (faster-whisper, on the CPU). It copes much
  better than Vosk with Indian, British and other non-US accents. Download the
  model once:

  ```
  py scripts/get_whisper_model.py          # small, ~480 MB (recommended)
  py scripts/get_whisper_model.py base     # ~145 MB, faster, less accurate
  ```

  That is the only step that uses the network; the app loads the model from
  `desktop\models\whisper-*` (git-ignored) or `%LOCALAPPDATA%\uaic\models\`.
  Click **Speak**, talk, then click again. Transcription takes 1–3 seconds
  after you stop (the button shows *Transcribing...*), then the words go into
  the request box for you to check. Spoken forms are cleaned up: "p d f" becomes
  `pdf`, and "my downloads folder" becomes `C:\Users\<you>\Downloads`. Speech is
  never submitted on its own and never answers an approval.

  Settings (environment variables):
  - `UAIC_STT_LANGUAGE`: `en` (the default), `auto`, or a language code such as `hi`.
  - `UAIC_STT_ENGINE`: `whisper` or `vosk`.
  - `UAIC_WHISPER_MODEL`: a model folder to use.

  Vosk is the fallback when no Whisper model is installed: unzip a
  `vosk-model-*` folder from https://alphacephei.com/vosk/models into the same
  place, or set `UAIC_VOSK_MODEL`.
- **Measuring accents.** `py scripts/speech_eval.py record --speaker <name>`
  has a person read 12 request-shaped sentences aloud. `py scripts/speech_eval.py
  score` then reports the word error rate and the time per clip for each
  engine, per speaker, in `eval\out\speech_wer.csv`. Recordings are kept in
  `%LOCALAPPDATA%\uaic\speech-samples\`, outside the repository.
- **Speech out** uses the Windows voices through pyttsx3. Tick **Read results
  aloud**.

### Packaging

```
py scripts/build_desktop.py --with-model --verify
```

This builds `dist\UAIC\UAIC.exe` (a one-folder build) and copies the speech
models in `desktop\models\` (Whisper and/or Vosk) next to it. It then runs the exe's `--self-test` to prove the manifests, the
journal schema and the speech pieces made it into the bundle. The API key is
never bundled.

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
                  taint propagation, progress events, cooperative cancellation.
uaic/app/         The session (composition root) and the backend thread that
                  turns the loop's blocking questions into UI events.
uaic/surface.py   THE UI integration surface: the only uaic module a UI may
                  import (enforced by tests/test_surface.py). Documents the protocol.
uaic/desktop/     The PySide6 app: window, approval and confirmation dialogs,
                  offline voice. Imports uaic only through uaic.surface.
tests/            pytest; table-driven where possible.
eval/             The evaluation harness and task corpus (see eval/README.md).
scripts/          injection_report.py, confirmation_cost.py, live_check.py,
                  live_e2e.py, build_desktop.py, and the mutation harness that
                  proves each defence test can fail.
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
behind the `Planner` protocol, the evaluation harness, progress and
cancellation callbacks, the UI integration surface, and the desktop application.
The app (PySide6) covers plan approval, folder granting, step confirmation, a
live execution view, results and undo, offline speech in and out, and
PyInstaller packaging. ruff clean.

The browser plugin is **not implemented**; its adversarial tests are prepared,
so the write-up can report its threat model without the plugin existing.

The desktop app runs in the **same process** as the backend. There is
deliberately no HTTP server, socket or network transport between the UI and the
backend (enforced by tests/test_surface.py) — a network port would be a
way for something other than the user to issue approvals, which this project's
threat model does not survive.
