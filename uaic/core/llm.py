"""Talking to a hosted model, without the rest of the system knowing how.

Three deliberate choices:

* The transport is a callable, not a vendor SDK. Tests inject a fake one, so
  the entire planner is tested with no network and no API key - which is why
  this project's test suite is reproducible by an examiner.
* Only the standard library is used. A planner is one HTTPS POST; adding a
  dependency to make that shorter would be a poor trade.
* The API key lives in the URL query string, because that is what this
  provider requires. Every message that can carry the URL - a transport
  failure, or a dump of a malformed response - is therefore passed through
  `redact` before it is raised, or the key ends up in logs and in the journal.
  Messages built only from model-supplied text are not redacted, and do not
  need to be: the key never enters the request body, so nothing the model
  returns can echo it. The distinction is worth stating precisely, because a
  comment that claims more than the code does is the kind of thing a reviewer
  is right to catch.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

DEFAULT_TIMEOUT = 60
REDACTED = "***redacted***"

# Transient server-side conditions worth retrying. A 4xx other than 429 is the
# caller's fault (bad request, bad key, unknown model) and is never retried.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0      # seconds: 2, then 4; capped, and overridden by Retry-After


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

    def __init__(self, *, secrets: tuple[str, ...] = (),
                 max_attempts: int = MAX_ATTEMPTS, backoff: float = BACKOFF_BASE,
                 sleep=time.sleep) -> None:
        # Values that must never appear in an exception raised from here.
        self._secrets = secrets
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff
        self._sleep = sleep          # injectable so a test need not wait

    def __call__(self, url: str, payload: dict[str, Any], *,
                 timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        # Checked before the request is built, not after: an API key in a
        # query string must never be handed to a plaintext scheme.
        if not url.startswith("https://"):
            raise LLMError("refusing to send a request over a non-HTTPS URL")
        body = json.dumps(payload).encode("utf-8")

        last: LLMError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._once(url, body, timeout)
            except _Retryable as exc:
                # A transient server condition. Back off and try again, unless
                # this was the last attempt, in which case surface it.
                last = exc.as_llm_error()
                if attempt == self._max_attempts:
                    break
                self._sleep(exc.retry_after or self._backoff * attempt)
        raise last

    def _once(self, url: str, body: bytes, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            message = redact(f"provider returned HTTP {exc.code}: {detail}",
                             *self._secrets)
            if exc.code in RETRY_STATUSES:
                raise _Retryable(message, _retry_after(exc)) from None
            raise LLMError(message) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            message = redact(f"could not reach the provider: {exc}", *self._secrets)
            # A read/connect TIMEOUT is transient and worth retrying. A plain
            # unreachable host (DNS failure, refused) is not: retrying it just
            # stalls, so it is surfaced on the first attempt as before.
            if _is_timeout(exc):
                raise _Retryable(message, None) from None
            raise LLMError(message) from None
        except json.JSONDecodeError as exc:
            raise LLMError(f"provider returned malformed JSON: {exc}") from None


class _Retryable(Exception):
    """Internal: a transport failure worth retrying. Never escapes this module."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def as_llm_error(self) -> LLMError:
        return LLMError(str(self))


def _is_timeout(exc: BaseException) -> bool:
    """A socket timeout, however urllib wrapped it this time."""
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError))


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """The provider's Retry-After header in seconds, if it sent an integer one."""
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value and value.isdigit():
        return min(float(value), 30.0)      # honour it, but never stall for minutes
    return None
