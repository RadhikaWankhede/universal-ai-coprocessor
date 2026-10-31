# Mutation harness for the injection suite

A test that passes is only evidence if it would fail without the defence it
claims to measure. This harness checks that. For each test in
`tests/test_injection.py`, plus the loop's prompt-field test, it removes the
defence the test targets and expects the test to go **red**.

## Running it

From the repository root, with the project's virtualenv active:

    py scripts/mutation/run_mutants.py           # the full matrix
    py scripts/mutation/run_mutants.py --show    # also print the failing assertion

Each run is a fresh `pytest` process with one weakening applied in memory by
the plugin in `mutants.py`. No file in the repository is changed. The script
exits 0 only if every mapped run went red **and** every test in the injection
suite has an entry in `MATRIX`. A new injection test that nobody has mapped
is reported as `UNMAPPED`, which counts as a failure.

Use `--show` to check that a red happened for the right reason. A mutant
that crashes the test for an unrelated reason would also show as red; the
printed assertion line is how you tell the difference.

To run one weakening by hand:

    set MUTANT=FREEZE_OFF
    set PYTHONPATH=scripts\mutation
    py -m pytest -p mutants tests/test_injection.py -k broker-freeze

## The weakenings

| Mutant             | Defence removed |
|--------------------|-----------------|
| `ESCALATION_OFF`   | The loop's re-plan check: a revised plan may use tools outside the freeze. |
| `FREEZE_OFF`       | The tool freeze: approval grants every declared tool, not just the plan's. |
| `UNDECLARED_OFF`   | The broker's declared-tool check: an unknown tool is looked up as `mail.send`. Also turns the loop check off, so the call reaches the broker. |
| `NONCE_FIXED`      | The per-block nonce: every untrusted block uses `id="0000"`, which content can forge. |
| `NOTED_TRUSTED`    | Failure and refusal notes are stored as trusted. |
| `OBSERVED_TRUSTED` | Tool results are stored as trusted. |
| `SCOPE_OFF`        | Path scope: every path counts as inside the granted folders. |
| `NO_RESOLVE`       | `resolve()` before containment: paths are compared as written, `..` included. |
| `GATE_ALLOW`       | The GATE table: irreversible/high actions run without asking. |
| `SILENT_TRUNC`     | Truncation notices: memory still cuts long entries but records nothing as dropped. |
| `PROMPT_LEAK`      | The re-plan `prompt` carries the last failure note's text, as it did before the fix. |
| `BINDING_OFF`      | Confirmation binding, reverted: one plan-level "yes" covers every later plan-level call in the run. |
| `PER_CALL`         | Stricter binding: a "yes" covers only the call shown in that dialog, not the rest of the approved plan. |
| `ASK_EVERY_STEP`   | No binding at all: every plan-level dispatch is asked, retries included. |
| `KEYWORD_FILTER`   | Adds a content filter to the **trusted** channel. The negative control must catch it, which proves the defences are provenance, not keywords. |
| `PLUGINS_MISSING`  | The fs and mail plugins are not loaded. The "legitimately granted" controls must catch it, which proves every refusal was the broker's decision. |
| `UNDO_OFF`         | The undo engine reports success and reverses nothing. |

## Reading the residual tests

Tests named `test_residual_*` record attacks that are **not** structurally
defeated. Their mutant is the one that would *close* the gap, or break the
remedy. For example, `UNDO_OFF` makes the frozen-reversible-tool test go red,
because the injected delete then cannot be reversed.

The binding mutants run against `tests/test_confirmation_binding.py` as well
as the injection suite. Between them they pin the binding from both sides:
`BINDING_OFF` and `PER_CALL` must not *loosen* or *tighten* it, and
`ASK_EVERY_STEP` must not break the rule that a retry is not re-asked.

`scripts/confirmation_cost.py` reuses `BINDING_OFF` and `PER_CALL` to measure
the questions asked per completed task under each rule.

`test_residual_poisoned_memory_*` has no mutant. Its only defence is a
human reading the plan. The `attentive` and `rubber-stamp` variants are
themselves the with-and-without comparison.
