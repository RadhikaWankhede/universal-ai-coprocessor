"""A real planner: Google Gemini behind the `Planner` protocol.

Everything the rest of the system sees is unchanged. This class returns a
`Plan`, exactly as `ScriptedPlanner` does, and has no more authority than it:
a Plan is a proposal, and the broker decides what may run.

Two boundary rules are enforced HERE rather than left to the broker:

* A proposed tool that is not in the manifest registry is rejected outright.
  The broker would refuse it later with BRK.UNDECLARED_TOOL, but a hallucinated
  capability is a planning failure we want to see at the boundary, where it can
  be re-prompted, not a security event.
* Arguments are passed through untouched. Validating them is the broker's job,
  and doing it twice in two places is how the two copies drift apart.

The model is asked for function calls only, at temperature 0, with the same
schemas the broker validates against.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from uaic.broker import Registry, default_registry, tool_schemas
from uaic.contracts import Plan, PlanStep
from uaic.core.context import ContextAssembler
from uaic.core.llm import (
    DEFAULT_TIMEOUT,
    HttpTransport,
    PlannerError,
    Transport,
    api_key,
    redact,
)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# A stable alias, not a pinned version: gemini-2.0-flash was retired out from
# under us (404), so pin to the moving "latest" alias and let UAIC_GEMINI_MODEL
# override it when a specific or less-loaded model is wanted.
DEFAULT_MODEL = "gemini-flash-latest"
ENV_KEY = "UAIC_GEMINI_API_KEY"
ENV_MODEL = "UAIC_GEMINI_MODEL"

MAX_STEPS = 25          # a plan longer than this is a runaway, not a plan


def function_declarations(registry: Registry) -> list[dict[str, Any]]:
    """Convert the broker's tool schemas into this provider's wire format.

    Generated from the manifests, so the tools the model is offered and the
    tools the broker will accept cannot drift apart.
    """
    declarations: list[dict[str, Any]] = []
    for schema in tool_schemas(registry):
        properties = schema["input_schema"]["properties"]
        declaration: dict[str, Any] = {
            "name": schema["name"],
            "description": schema["description"],
        }
        if properties:
            # This provider rejects `additionalProperties` and `default`, so the
            # schema is narrowed rather than passed through verbatim.
            declaration["parameters"] = {
                "type": "object",
                "properties": {
                    name: {k: v for k, v in prop.items()
                           if k in ("type", "description")}
                    for name, prop in properties.items()
                },
                "required": schema["input_schema"]["required"],
            }
        declarations.append(declaration)
    return declarations


def _requested_paths(registry: Registry, steps: tuple[PlanStep, ...]) -> tuple[str, ...]:
    """The folders this plan will need, for the user to approve or narrow.

    This is a REQUEST, not a grant. The user approves the plan, and approval is
    what mints the capability set - so the planner asking for a folder has no
    force of its own. A step targeting a file asks for that file's folder,
    because a plan that moves one file still needs its directory.
    """
    wanted: list[str] = []
    for step in steps:
        spec = registry.lookup(step.tool)
        if spec is None:
            continue
        for arg in spec.scope_args:
            raw = step.args.get(arg)
            if not isinstance(raw, str) or not raw:
                continue
            target = Path(raw)
            folder = target if target.is_dir() else target.parent
            text = str(folder)
            if text not in wanted:
                wanted.append(text)
    return tuple(wanted)


class GeminiPlanner:
    """Implements the Planner protocol: propose(prompt, plan_id) -> Plan."""

    def __init__(self, *, api_key_value: str | None = None,
                 model: str | None = None,
                 registry: Registry | None = None,
                 transport: Transport | None = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 max_steps: int = MAX_STEPS) -> None:
        self._given_key = api_key_value
        self._model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self._registry = registry or default_registry()
        self._transport = transport
        self._timeout = timeout
        self._max_steps = max_steps

    # ------------------------------------------------------------------- public

    def propose(self, prompt: str, plan_id: str,
                context: ContextAssembler | None = None) -> Plan:
        """Ask the model for a plan.

        `context` carries any untrusted content already read - file text, page
        text - each block wrapped by the context assembler. Passing it here is
        what makes re-planning after a tool result possible.
        """
        key = api_key(ENV_KEY, self._given_key)
        assembler = context or ContextAssembler()
        if context is None:
            assembler.add_user(prompt)

        payload = self._payload(assembler)
        url = f"{API_BASE}/{self._model}:generateContent?key={key}"
        transport = self._transport or HttpTransport(secrets=(key,))

        try:
            response = transport(url, payload, timeout=self._timeout)
        except Exception as exc:
            # Anything at all from the transport becomes one error type, with
            # the key scrubbed, so no caller has to know how it failed.
            raise PlannerError(redact(f"planning request failed: {exc}", key)) from None

        steps, notes = self._parse(response, key)
        return Plan(plan_id=plan_id, prompt=prompt, steps=steps, summary=notes,
                    requested_paths=_requested_paths(self._registry, steps))

    # ---------------------------------------------------------------- internals

    def _payload(self, assembler: ContextAssembler) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system = ""
        for message in assembler.messages():
            if message["role"] == "system":
                system = message["content"]
                continue
            # A tool result is sent as a user turn: its text is already inside
            # an <untrusted> block, and it must never occupy a system position.
            contents.append({"role": "user",
                             "parts": [{"text": message["content"]}]})
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": function_declarations(self._registry)}],
            # ANY: answer with function calls, not prose. A planner that
            # replies in sentences has produced nothing this system can check.
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": 0},
        }

    def _parse(self, response: dict[str, Any],
               key: str) -> tuple[tuple[PlanStep, ...], str]:
        if not isinstance(response, dict):
            raise PlannerError("provider returned something that is not an object")

        blocked = (response.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise PlannerError(f"the provider blocked this request: {blocked}")

        candidates = response.get("candidates") or []
        if not candidates:
            raise PlannerError(redact(
                f"the model returned no candidates: {response}", key))

        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        steps: list[PlanStep] = []
        notes: list[str] = []

        for part in parts:
            if "text" in part and part["text"].strip():
                notes.append(part["text"].strip())
            fn = part.get("functionCall")
            if not fn:
                continue
            name = fn.get("name") or ""
            if self._registry.lookup(name) is None:
                raise PlannerError(
                    f"the model proposed {name!r}, which is not a declared tool")
            args = fn.get("args") or {}
            if not isinstance(args, dict):
                raise PlannerError(f"{name}: arguments were not an object")
            steps.append(PlanStep(seq=len(steps) + 1, tool=name, args=args,
                                  rationale=""))

        if not steps:
            said = " ".join(notes)[:300]
            raise PlannerError(
                "the model proposed no actions"
                + (f"; it said: {said}" if said else ""))
        if len(steps) > self._max_steps:
            raise PlannerError(
                f"the model proposed {len(steps)} steps, over the limit of "
                f"{self._max_steps}")

        return tuple(steps), " ".join(notes)[:500]
