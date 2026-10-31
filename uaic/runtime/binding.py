"""Confirmation binding: a "yes" authorises the calls the user was shown.

Before this module, a plan-level confirmation authorised the TOOL for the rest
of the plan. An injected `git.commit` to a different repository, proposed by a
re-plan after a document was read, rode the user's earlier "yes" without a
question. A confirmation now authorises specific calls:

* A plan-level "yes" authorises exactly the calls in the APPROVED plan, which
  the user saw with their arguments in the plan preview. That keeps the
  reversibility ladder's middle tier - compensable steps ask once per plan -
  and it is safe because an approved plan is fixed before any content is
  read, so an injection can never be one of its calls.
* Any other call - in practice, anything a re-plan introduces - needs its
  own question, and a "yes" to it authorises that call alone.
* An authorisation is spent when its call EXECUTES SUCCESSFULLY. A failure
  spends nothing, so a retry of the same failed call is never re-asked.
  Running a call twice is two actions, so a second identical call after a
  success must ask again.

Step-level confirmation (irreversible steps) is untouched: it is asked on
every dispatch, retries included. A failed irreversible call may already have
had an effect, so re-running it is a new decision. That is the stricter of the
two readings, chosen deliberately.

"Materially different" is defined by `signature()`.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from uaic.broker import Registry, canonical, resolve
from uaic.contracts import ToolCall

Signature = tuple


def signature(call: ToolCall, registry: Registry, session_root: Path | None = None) -> Signature:
    """What a call DOES, as opposed to how it was labelled.

    Two calls are the same, for confirmation purposes, iff their signatures
    are equal. In the signature:

    * the tool name, exactly;
    * every path-typed argument as the broker sees it - resolved by the
      broker's own `resolve()` and compared in the broker's `canonical()`
      form - so case, separators, `.` and `..` and symlinks cannot make one
      target look like two, or two targets look like one. A path that will
      not resolve is kept raw and so matches nothing that does;
    * every other argument by exact value. A commit message, a mail body or a
      recipient that differs by one character is a different call. No
      whitespace or case normalisation: what the user read is what they
      approved, and the stricter option is chosen where it is unclear;
    * an argument left out compared as its manifest default, because omitting
      it and passing the default do the same thing.

    Deliberately NOT in the signature: call_id, seq, plan_id and origin. They
    are bookkeeping, and a retry changes call_id while a re-plan changes seq.
    """
    spec = registry.lookup(call.tool)
    args = dict(call.args)
    declared = spec.args if spec else {}
    for name, arg in declared.items():
        args.setdefault(name, arg.default)

    parts = []
    for name in sorted(args):
        value = args[name]
        if name in declared and declared[name].type == "path":
            resolved = resolve(value, session_root)
            key = ("path", canonical(resolved)) if resolved is not None else ("raw", repr(value))
        else:
            key = ("value", json.dumps(value, sort_keys=True, default=repr))
        parts.append((name, key))
    return (call.tool, tuple(parts))


class ConfirmationLedger:
    """Which calls the user has authorised in one plan run, and how many times."""

    def __init__(self, approved: Iterable[ToolCall], registry: Registry,
                 session_root: Path | None = None) -> None:
        self._registry = registry
        self._session_root = session_root
        self._shown = Counter(self._sig(c) for c in approved)
        self._authorised: Counter = Counter()
        self.plan_confirmed = False

    def _sig(self, call: ToolCall) -> Signature:
        return signature(call, self._registry, self._session_root)

    def approved(self, call: ToolCall) -> bool:
        """True if `call` is one of the calls of the approved plan."""
        return self._sig(call) in self._shown

    def covers(self, call: ToolCall) -> bool:
        """True if a plan-level question about `call` was already answered yes."""
        return self._authorised[self._sig(call)] > 0

    def confirmed(self, call: ToolCall) -> None:
        """The user said yes to a plan-level question about `call`."""
        sig = self._sig(call)
        if not self.plan_confirmed and sig in self._shown:
            # The first yes to a call from the approved plan confirms the plan:
            # every call the user was shown in its preview, as many times as shown.
            self.plan_confirmed = True
            self._authorised.update(self._shown)
        else:
            self._authorised[sig] += 1

    def executed(self, call: ToolCall) -> None:
        """`call` ran successfully: spend one authorisation, if it had one."""
        sig = self._sig(call)
        if self._authorised[sig] > 0:
            self._authorised[sig] -= 1
