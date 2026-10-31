"""Short-term memory: what has been said and seen, in order.

FR-7 asks for conversation state and task-relevant memory. The security-
relevant design decision is the one rule below, and it is easy to get wrong:

    ONLY THE USER'S OWN WORDS ARE TRUSTED.

Exactly two methods create a trusted entry - `user_said` and `remember_fact`
- and both take their content straight from the user.

Memory is the obvious place to launder provenance. A poisoned sentence read
out of a PDF on turn one becomes "earlier context" by turn three, and if
memory replays it as ordinary history the untrusted wrapper is gone and the
injection has been promoted to an instruction - without any component having
a bug. So every remembered item carries the provenance it was created with,
and replay reproduces it. There is deliberately no method that upgrades an
item's provenance after the fact.

Note also that an error string is untrusted: "could not read X: <contents>"
can carry bytes that came from a file. `noted()` is therefore always
untrusted; it takes no argument that could make it otherwise. Those two
methods are the only routes to a trusted entry, by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from uaic.contracts import Provenance
from uaic.core.context import ContextAssembler, truncation_notice

Kind = Literal["user", "tool_result", "note"]

MAX_TURNS = 20          # a bounded window: context cost must not grow with time

# Per-entry cap, so one enormous file cannot crowd out everything else. The
# worst-case window is MAX_TURNS * MAX_CHARS = 80,000 characters, roughly
# 20k tokens, replayed on every planning call. That is the budget being
# protected, and raising this cap raises it twenty-fold per step. It is not
# raised because the planner does not need the whole document - it needs to
# know what it has and what it is missing, which the truncation notice now
# tells it, and doc.extract_text takes a page range for anything further.
MAX_CHARS = 4000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Remembered:
    kind: Kind
    text: str
    provenance: Provenance
    source_ref: str | None = None
    ts: str = ""
    # Characters cut from the end of `text` by MAX_CHARS. Kept as a number,
    # never appended to `text`: see `context()` for where it is announced.
    dropped: int = 0

    @property
    def trusted(self) -> bool:
        return self.provenance is Provenance.TRUSTED

    @property
    def truncated(self) -> bool:
        return self.dropped > 0


class SessionMemory:
    """One conversation. Bounded, ordered, provenance-preserving."""

    def __init__(self, *, max_turns: int = MAX_TURNS,
                 max_chars: int = MAX_CHARS) -> None:
        self._items: list[Remembered] = []
        self._facts: dict[str, str] = {}
        self._fact_dropped: dict[str, int] = {}
        self._max_turns = max_turns
        self._max_chars = max_chars

    # -------------------------------------------------------------- recording

    def user_said(self, text: str) -> SessionMemory:
        """The only trusted input in this class."""
        return self._add("user", text, Provenance.TRUSTED)

    def observed(self, tool: str, payload: Any,
                 source_ref: str | None = None) -> SessionMemory:
        """A tool result. Untrusted, always, whatever the tool was."""
        return self._add("tool_result", f"{tool} -> {payload}",
                         Provenance.UNTRUSTED, source_ref)

    def noted(self, text: str, *, source_ref: str | None = None) -> SessionMemory:
        """A note the runtime writes about itself - a refusal, a failure.

        Always untrusted, and there is no argument to make it otherwise.
        Failure messages quote the thing that failed, and that quotation may
        have come out of a file.

        This originally took a `provenance` keyword so that a note the runtime
        wrote entirely by itself could be trusted. Nothing used it, and a
        reviewer pointed out that it left the class with a third route to a
        trusted entry - so the security property held by convention at every
        call site rather than by construction. The keyword is gone. If a
        trusted system note is ever genuinely needed, it should arrive as its
        own named method, so that the exception is visible in the API.

        `source_ref` says where the note's content came from. It is a label,
        not a trust level: it cannot change the provenance.
        """
        return self._add("note", text, Provenance.UNTRUSTED, source_ref)

    def remember_fact(self, key: str, value: str) -> SessionMemory:
        """A durable preference stated by the USER. Never inferred from a file."""
        if not key or not isinstance(value, str):
            raise ValueError("a remembered fact needs a key and a string value")
        self._facts[key] = value[: self._max_chars]
        self._fact_dropped[key] = len(value) - len(self._facts[key])
        return self

    def _add(self, kind: Kind, text: str, provenance: Provenance,
             source_ref: str | None = None) -> SessionMemory:
        text = str(text)
        kept = text[: self._max_chars]
        self._items.append(Remembered(kind=kind, text=kept,
                                      provenance=provenance,
                                      source_ref=source_ref, ts=_now(),
                                      dropped=len(text) - len(kept)))
        # Oldest first out. The user's original request is usually the thing
        # worth keeping longest, so it is re-stated by the caller if needed;
        # silently pinning it here would make the window size a lie.
        if len(self._items) > self._max_turns:
            self._items = self._items[-self._max_turns:]
        return self

    # ---------------------------------------------------------------- reading

    @property
    def items(self) -> tuple[Remembered, ...]:
        return tuple(self._items)

    @property
    def facts(self) -> dict[str, str]:
        return dict(self._facts)

    def recent(self, n: int = 5) -> tuple[Remembered, ...]:
        return tuple(self._items[-n:])

    def untrusted_sources(self) -> tuple[str, ...]:
        return tuple(i.source_ref for i in self._items
                     if not i.trusted and i.source_ref)

    def last_user_request(self) -> str | None:
        for item in reversed(self._items):
            if item.kind == "user":
                return item.text
        return None

    # ---------------------------------------------------------------- replay

    def context(self, *, system_extra: str = "") -> ContextAssembler:
        """Rebuild the model's context from memory, provenance intact."""
        assembler = ContextAssembler(system_extra)
        if self._facts:
            # Facts came from the user, so they are replayed as a trusted turn.
            # Trusted, so the notice is appended in-band, as for any trusted
            # entry: there is no untrusted content here that could forge it.
            lines = "\n".join(
                f"- {k}: {v}" + (f" [truncated: {truncation_notice(self._fact_dropped[k])}]"
                                 if self._fact_dropped.get(k) else "")
                for k, v in sorted(self._facts.items()))
            assembler.add_user(f"Remembered preferences:\n{lines}")
        for item in self._items:
            if item.trusted:
                assembler.add_user(item.text, item.dropped)
            else:
                # Re-wrapped, with a NEW nonce each time it is replayed. The
                # truncation notice travels as a number and is rendered into
                # the opening tag, outside the content, so a document cannot
                # forge one or hide the real one.
                assembler.add_tool_result(item.text, item.source_ref, item.dropped)
        return assembler
