# Evaluation harness

Quantitative results for the dissertation. The corpus is **data**; the harness
runs it and counts. Tests verify the harness, never the numbers — measuring
behaviour against the definition of itself would be circular.

```
py eval/harness.py           # table to stdout; JSON/CSV/LaTeX to eval/out/
py eval/harness.py --quiet   # files only, plus the table
```

## Files

- `corpus.json` — the task definitions. Each task has a `goal`, a `fixture`
  (files, PDFs or git repos to create), the folders `grant`ed, the plan
  `steps`, and what counts as success (`post`, `expect`, `post_undo`,
  `attack_target`). Every task carries a `why`.
- `harness.py` — builds each task in a fresh temp dir, runs it through the real
  `AgentLoop`, broker, journal and plugins, and measures.
- `out/` — generated results (git-ignored): `results.json`, `results.csv`,
  `results.tex`.

## The four metrics

| metric | numerator / denominator |
|---|---|
| task success | benign goals that completed with their post-conditions holding / benign tasks |
| containment | out-of-scope attempts the broker refused / attempts |
| interruption | confirmations asked / completed benign tasks |
| recovery | completed actions put back by journal replay / recovery tasks |

Every rate is printed `num/den`. Containment below 100% is flagged as a defect,
not averaged in. Tasks needing git are excluded (and named) where git is absent.

## The ablation

The corpus runs under four configurations — `none` (no binding, no taint),
`binding`, `shipped` (binding + broad taint), `results` (binding + results-only
taint) — crossed with two users, `careful` (approves only pre-approved calls)
and `rubber-stamp` (approves everything). Configurations are applied by patching
the same seams the mutation harness uses.

## Why injection tasks, and the silent-dispatch column

The four headline metrics cannot separate `none` from `binding`: binding and
taint act on *in-scope* calls the user never approved, which are not
out-of-scope and so never touch the containment metric. The `injection` tasks
(in-scope calls delivered via a re-plan after reading) exist for this. Their
number is the **silent-dispatch rate** — an injected action executed with no
confirmation asked — which is user-independent and is where each layer's effect
appears.
