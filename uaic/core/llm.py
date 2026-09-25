"""Talking to a hosted model, without the rest of the system knowing how.

Three deliberate choices:

* The transport is a callable, not a vendor SDK. Tests inject a fake one, so
  the entire planner is tested with no network and no API key - which is why
  this project's test suite is reproducible by an examiner.
* Only the standard library is used. A planner is one HTTPS POST; adding a
  dependency to make that shorter would be a poor trade.
* The API key lives in the URL query string, because that is what this
  provider requires. Every error message is therefore passed through `redact`
  before it is raised, or the key ends up in logs and in the journal.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

DEFAULT_TIMEOUT = 60
REDACTED = "***redacted***"


class LLMError(Exception):
    """The model could not be reached, or refused to answer."""


class PlannerError(LLMError):
    """The model answered, but not with a usable plan."""


class Transport(Protocol):
    def __call__(self, url: str, payload: dict[str, Any], *,
                 timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]: ...


def redact(text: str, *secrets: str) -> str:
    """Remove every known secret from a string bound for a log or an exception."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def api_key(env_var: str, given: str | None = None) -> str:
    """Resolve a key at call time, never at import time.

    Import-time resolution means the whole package fails to import on a machine
    without a key - including for the 200-odd tests that do not need one.
    """
    key = given or os.environ.get(env_var, "")
    if not key.strip():
        raise LLMError(
            f"no API key: set the {env_var} environment variable, "
            f"or pass api_key= explicitly")
    return key.strip()


class HttpTransport:
    """A JSON POST over HTTPS. The only place in the project that opens a socket."""

    def __init__(self, *, secrets: tuple[str, ...] = ()) -> None:
        # Values that must never appear in an exception raised from here.
        self._secrets = secrets

    def __call__(self, url: str, payload: dict[str, Any], *,
                 timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        # Checked before the request is built, not after: an API key in a
        # query string must never be handed to a plaintext scheme.
        if not url.startswith("https://"):
            raise LLMError("refusing to send a request over a non-HTTPS URL")
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(
                redact(f"provider returned HTTP {exc.code}: {detail}",
                       *self._secrets)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(redact(f"could not reach the provider: {exc}",
                                  *self._secrets)) from None
        except json.JSONDecodeError as exc:
            raise LLMError(f"provider returned malformed JSON: {exc}") from None
