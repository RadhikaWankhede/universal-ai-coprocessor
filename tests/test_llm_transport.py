"""The HTTP transport. No test here reaches the network."""
from __future__ import annotations

import pytest

from uaic.core.llm import REDACTED, HttpTransport, LLMError, api_key, redact


def test_a_plaintext_url_is_refused_before_anything_is_sent():
    with pytest.raises(LLMError, match="non-HTTPS"):
        HttpTransport()("http://example.invalid/v1", {"a": 1})


def test_an_unreachable_host_is_reported_as_one_error_type():
    with pytest.raises(LLMError, match="could not reach"):
        HttpTransport()("https://host.invalid.example/v1", {"a": 1}, timeout=2)


def test_secrets_are_scrubbed_from_transport_errors():
    transport = HttpTransport(secrets=("SECRETKEY",))
    with pytest.raises(LLMError) as caught:
        transport("https://host.invalid.example/v1?key=SECRETKEY", {}, timeout=2)
    assert "SECRETKEY" not in str(caught.value)


def test_redact_removes_every_known_secret():
    text = redact("key=abc and token=def", "abc", "def")
    assert "abc" not in text and "def" not in text
    assert text.count(REDACTED) == 2


def test_redact_ignores_empty_secrets():
    assert redact("nothing to hide", "") == "nothing to hide"


def test_a_key_is_resolved_at_call_time_from_the_environment(monkeypatch):
    monkeypatch.setenv("UAIC_TEST_KEY", "value")
    assert api_key("UAIC_TEST_KEY") == "value"


def test_an_explicit_key_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("UAIC_TEST_KEY", "from-env")
    assert api_key("UAIC_TEST_KEY", "explicit") == "explicit"


def test_a_missing_key_names_the_variable_the_user_has_to_set(monkeypatch):
    monkeypatch.delenv("UAIC_TEST_KEY", raising=False)
    with pytest.raises(LLMError, match="UAIC_TEST_KEY"):
        api_key("UAIC_TEST_KEY")


# ---------------- retry of transient failures (added when wiring the live API) --

import io  # noqa: E402
import urllib.error  # noqa: E402

from uaic.core import llm  # noqa: E402


def http_error(code: int, body: bytes = b"{}", headers: dict | None = None):
    import email.message
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x.example/v1", code, "err", msg, io.BytesIO(body))


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def opener(events):
    """Returns a urlopen stand-in that yields each event in turn: an exception
    is raised, bytes are returned as a successful JSON body."""
    seq = iter(events)

    def _urlopen(request, timeout=None):
        event = next(seq)
        if isinstance(event, BaseException):
            raise event
        return FakeResponse(event)
    return _urlopen


def transport(monkeypatch, events, **kw):
    slept: list[float] = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", opener(events))
    t = llm.HttpTransport(sleep=slept.append, **kw)
    return t, slept


def test_a_transient_503_is_retried_then_succeeds(monkeypatch):
    t, slept = transport(monkeypatch, [http_error(503), b'{"ok": true}'])
    assert t("https://x.example/v1", {})["ok"] is True
    assert len(slept) == 1                       # waited once, between the two attempts


def test_a_persistent_503_fails_after_the_attempt_budget(monkeypatch):
    t, slept = transport(monkeypatch, [http_error(503)] * 3, max_attempts=3)
    with pytest.raises(LLMError, match="HTTP 503"):
        t("https://x.example/v1", {})
    assert len(slept) == 2                        # slept between 3 attempts, not after the last


def test_a_read_timeout_is_retried(monkeypatch):
    t, slept = transport(monkeypatch, [TimeoutError("read timed out"), b'{"ok": 1}'])
    assert t("https://x.example/v1", {}) == {"ok": 1}
    assert len(slept) == 1


def test_a_400_is_not_retried(monkeypatch):
    t, slept = transport(monkeypatch, [http_error(400, b'{"error": "bad"}')])
    with pytest.raises(LLMError, match="HTTP 400"):
        t("https://x.example/v1", {})
    assert slept == []                            # a client error is final, no waiting


def test_a_non_timeout_connection_error_is_not_retried(monkeypatch):
    t, slept = transport(monkeypatch, [urllib.error.URLError("name resolution failed")])
    with pytest.raises(LLMError, match="could not reach"):
        t("https://x.example/v1", {})
    assert slept == []


def test_the_retry_after_header_overrides_the_backoff(monkeypatch):
    t, slept = transport(monkeypatch,
                         [http_error(429, headers={"Retry-After": "7"}), b'{"ok": 1}'])
    assert t("https://x.example/v1", {}) == {"ok": 1}
    assert slept == [7.0]


def test_the_key_is_scrubbed_from_a_retried_error(monkeypatch):
    t, _slept = transport(monkeypatch, [http_error(503, b'{"k": "SEKRET"}')] * 2,
                          max_attempts=2, secrets=("SEKRET",))
    with pytest.raises(LLMError) as caught:
        t("https://x.example/v1?key=SEKRET", {})
    assert "SEKRET" not in str(caught.value)
