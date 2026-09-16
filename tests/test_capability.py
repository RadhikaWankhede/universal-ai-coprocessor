"""Scope resolution: the most attackable code in the project."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from uaic.broker import is_inside, resolve


def test_plain_file_is_inside(sandbox):
    t = resolve(sandbox["downloads"] / "a.pdf")
    assert is_inside(t, sandbox["downloads"])


def test_nested_file_is_inside(sandbox):
    t = resolve(sandbox["downloads"] / "sub" / "b.pdf")
    assert is_inside(t, sandbox["downloads"])


def test_sibling_folder_is_outside(sandbox):
    t = resolve(sandbox["documents"] / "secret.docx")
    assert not is_inside(t, sandbox["downloads"])


def test_traversal_escape_is_blocked(sandbox):
    sneaky = sandbox["downloads"] / ".." / "Documents" / "secret.docx"
    t = resolve(sneaky)
    assert t == sandbox["documents"].resolve() / "secret.docx"
    assert not is_inside(t, sandbox["downloads"])


def test_sibling_prefix_is_not_containment(sandbox):
    """Downloads2 starts with the same letters as Downloads. It is not inside it."""
    t = resolve(sandbox["downloads2"] / "other.pdf")
    assert not is_inside(t, sandbox["downloads"])


def test_deep_traversal_is_blocked(sandbox):
    sneaky = sandbox["downloads"] / "sub" / ".." / ".." / "Documents" / "secret.docx"
    assert not is_inside(resolve(sneaky), sandbox["downloads"])


@pytest.mark.parametrize("bad", [None, 123, "", "   ", [], {}])
def test_junk_input_returns_none(bad):
    assert resolve(bad) is None


def test_relative_path_without_root_is_refused():
    assert resolve("a.pdf") is None


def test_relative_path_with_root_resolves(sandbox):
    t = resolve("a.pdf", session_root=sandbox["downloads"])
    assert t == (sandbox["downloads"] / "a.pdf").resolve()


def test_symlink_out_of_grant_is_blocked(sandbox):
    link = sandbox["downloads"] / "escape"
    try:
        link.symlink_to(sandbox["documents"], target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    t = resolve(link / "secret.docx")
    assert not is_inside(t, sandbox["downloads"]), (
        "a symlink inside the grant must not be a way out of it")


def test_case_insensitive_match_on_windows(sandbox):
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    t = resolve(str(sandbox["downloads"] / "a.pdf").upper())
    assert is_inside(t, sandbox["downloads"])


def test_alternate_data_stream_is_refused(sandbox):
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    assert resolve(str(sandbox["downloads"] / "a.pdf") + ":hidden") is None


def test_capability_set_is_frozen(caps):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        caps.plan_id = "other"
