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
