"""A pytest plugin that weakens exactly one defence, chosen by $MUTANT.

Loaded with `-p mutants` by run_mutants.py. With $MUTANT unset it does
nothing. Each entry in MUTANTS is a function that monkeypatches one defence
out of the running process; the docstring says what it removes. The patches
live only in the test process - no file in the repository is modified.
"""
from __future__ import annotations

import os
from pathlib import Path


def escalation_off() -> None:
    """The loop's re-plan check: a revised plan may use tools outside the freeze."""
    import uaic.runtime.loop as loop
    loop.escalation_needed = lambda plan, caps: frozenset()


def freeze_off() -> None:
    """The tool freeze itself: approval grants every declared tool, not the plan's."""
    import uaic.runtime.loop as loop
    from uaic.broker import default_registry
    real = loop.freeze_plan

    def wide(plan, granted, *, max_steps=None):
        caps = real(plan, granted, max_steps=max_steps)
        return caps.model_copy(update={"tools": frozenset(default_registry().names())})
    loop.freeze_plan = wide


def undeclared_off() -> None:
    """The declared-tool check: an unknown tool is looked up as mail.send.
    Also switches the loop check off, so the call reaches the broker."""
    from uaic.broker.manifest import Registry
    real = Registry.lookup
    Registry.lookup = lambda self, tool: real(self, tool) or real(self, "mail.send")
    escalation_off()


def nonce_fixed() -> None:
    """The per-block nonce: every block is delimited with id="0000"."""
    import secrets
    secrets.token_hex = lambda nbytes=None: "0000"


def noted_trusted() -> None:
    """Failure and refusal notes are recorded as TRUSTED."""
    from uaic.contracts import Provenance
    from uaic.core.memory import SessionMemory
    SessionMemory.noted = lambda self, text, *, source_ref=None: self._add(
        "note", text, Provenance.TRUSTED, source_ref)


def observed_trusted() -> None:
    """Tool results are recorded as TRUSTED."""
    from uaic.contracts import Provenance
    from uaic.core.memory import SessionMemory
    SessionMemory.observed = lambda self, tool, payload, source_ref=None: self._add(
        "tool_result", f"{tool} -> {payload}", Provenance.TRUSTED, source_ref)


def scope_off() -> None:
    """Path scope: every path counts as inside the granted folders."""
    from uaic.broker import CapabilitySet
    CapabilitySet.covers_path = lambda self, target: True


def no_resolve() -> None:
    """resolve() before containment: paths are compared lexically, `..` intact."""
    import uaic.broker.pipeline as pipeline
    pipeline.resolve = lambda raw, root=None: Path(str(raw)) if raw else None


def gate_allow() -> None:
    """The GATE table: irreversible/high is allowed without asking."""
    import uaic.broker.pipeline as pipeline
    from uaic.contracts import Decision, Rev, Risk
    pipeline.GATE[(Rev.IRREVERSIBLE, Risk.HIGH)] = Decision.ALLOW


def silent_trunc() -> None:
    """Truncation notices: memory still cuts, but records nothing dropped."""
    from dataclasses import replace

    from uaic.core.memory import SessionMemory
    real = SessionMemory._add

    def add(self, *args, **kwargs):
        real(self, *args, **kwargs)
        self._items[-1] = replace(self._items[-1], dropped=0)
        return self
    SessionMemory._add = add


def prompt_leak() -> None:
    """The re-plan prompt carries the last failure note's text, as it used to."""
    import uaic.runtime.loop as loop
    from uaic.core.memory import SessionMemory
    last: list[str] = [""]
    real_noted = SessionMemory.noted

    def noted(self, text, **kwargs):
        last[0] = text
        return real_noted(self, text, **kwargs)
    SessionMemory.noted = noted
    loop.replan_prompt = lambda failed, attempts: f"{last[0]}. Propose how to finish."


def binding_off() -> None:
    """Confirmation binding, reverted to the old rule: once any plan-level call
    is confirmed, every later plan-level call in the run is covered."""
    from uaic.runtime.binding import ConfirmationLedger

    def confirmed(self, call):
        self.plan_confirmed = True
    ConfirmationLedger.covers = lambda self, call: self.plan_confirmed
    ConfirmationLedger.confirmed = confirmed


def per_call() -> None:
    """Stricter binding: a "yes" covers only the one call shown in that dialog,
    never the rest of the approved plan."""
    from uaic.runtime.binding import ConfirmationLedger

    def confirmed(self, call):
        self._authorised[self._sig(call)] += 1
    ConfirmationLedger.confirmed = confirmed


def ask_every_step() -> None:
    """No binding at all: every plan-level dispatch is asked, retries included."""
    from uaic.runtime.binding import ConfirmationLedger
    ConfirmationLedger.covers = lambda self, call: False


def keyword_filter() -> None:
    """Adds a content filter to the TRUSTED channel: lines naming mail.send are
    stripped from what the user says. The negative control must catch this."""
    from uaic.core.memory import SessionMemory
    real = SessionMemory.user_said
    SessionMemory.user_said = lambda self, text: real(
        self, "\n".join(line for line in text.split("\n") if "mail.send" not in line))


def plugins_missing() -> None:
    """The fs and mail plugins are not loaded."""
    import uaic.runtime.executor as executor
    real = executor.Executor.__init__

    def init(self, journal, plugins, **kwargs):
        real(self, journal, {k: v for k, v in plugins.items() if k not in ("fs", "mail")},
             **kwargs)
    executor.Executor.__init__ = init


def undo_off() -> None:
    """The undo engine reports success and reverses nothing."""
    from uaic.journal.undo import UndoEngine, UndoReport
    UndoEngine.undo_plan = lambda self, plan_id: UndoReport(plan_id=plan_id)


MUTANTS = {fn.__name__.upper(): fn for fn in (
    escalation_off, freeze_off, undeclared_off, nonce_fixed, noted_trusted,
    observed_trusted, scope_off, no_resolve, gate_allow, silent_trunc, prompt_leak,
    binding_off, per_call, ask_every_step, keyword_filter, plugins_missing, undo_off)}


def pytest_configure(config) -> None:
    name = os.environ.get("MUTANT")
    if not name:
        return
    if name not in MUTANTS:
        raise SystemExit(f"unknown mutant {name!r}; known: {', '.join(MUTANTS)}")
    MUTANTS[name]()
