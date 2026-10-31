"""Scope resolution: the most attackable code in the project."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from uaic.broker import CapabilitySet, is_inside, resolve


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


# ---------- Windows-specific escapes ----------

def test_directory_junction_out_of_grant_is_blocked(sandbox):
    """Junctions need no admin rights, unlike symlinks - so this runs everywhere
    on Windows and covers the same escape the symlink test cannot."""
    if os.name != "nt":
        pytest.skip("Windows-only: directory junctions")
    link = sandbox["downloads"] / "escape"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(sandbox["documents"])],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not create junction: {proc.stderr.strip()}")
    t = resolve(link / "secret.docx")
    assert t is not None
    assert not is_inside(t, sandbox["downloads"]), (
        "a junction inside the grant must not be a way out of it")


def test_mixed_separators_still_resolve(sandbox):
    if os.name != "nt":
        pytest.skip("Windows-only: backslash and forward slash are both separators")
    sneaky = f"{sandbox['downloads']}/../Documents\\secret.docx"
    assert not is_inside(resolve(sneaky), sandbox["downloads"])


def test_short_name_form_is_handled(sandbox):
    """8.3 short names such as PROGRA~1 must resolve to the long form."""
    if os.name != "nt":
        pytest.skip("Windows-only: 8.3 short names")
    long_dir = sandbox["downloads"] / "a folder with spaces"
    long_dir.mkdir()
    (long_dir / "f.txt").write_text("x")
    t = resolve(long_dir / "f.txt")
    assert is_inside(t, sandbox["downloads"])


# ---------- more containment edge cases ----------

def test_the_grant_root_itself_is_inside(sandbox):
    """Listing the granted folder must be permitted - the spike depends on it."""
    assert is_inside(resolve(sandbox["downloads"]), sandbox["downloads"])


def test_redundant_separators_and_dots_are_normalised(sandbox):
    messy = Path(str(sandbox["downloads"])) / "." / "sub" / "." / "b.pdf"
    assert is_inside(resolve(messy), sandbox["downloads"])


def test_excess_parent_segments_do_not_wrap_around(sandbox):
    sneaky = sandbox["downloads"]
    for _ in range(40):
        sneaky = sneaky / ".."
    sneaky = sneaky / "etc" / "passwd"
    assert not is_inside(resolve(sneaky), sandbox["downloads"])


def test_file_merely_starting_with_the_grant_name_is_outside(sandbox):
    """Downloads.bak is not inside Downloads."""
    impostor = sandbox["root"] / "Downloads.bak"
    impostor.mkdir()
    (impostor / "x.pdf").write_text("x")
    assert not is_inside(resolve(impostor / "x.pdf"), sandbox["downloads"])


def test_nul_byte_is_refused(sandbox):
    assert resolve(str(sandbox["downloads"]) + "\x00evil") is None


def test_multiple_grants_are_all_considered(sandbox):
    from uaic.broker import CapabilitySet
    caps = CapabilitySet(
        plan_id="p1", tools=frozenset({"fs.read"}),
        path_grants=(sandbox["downloads"], sandbox["documents"]),
    )
    assert caps.covers_path(resolve(sandbox["downloads"] / "a.pdf"))
    assert caps.covers_path(resolve(sandbox["documents"] / "secret.docx"))
    assert not caps.covers_path(resolve(sandbox["downloads2"] / "other.pdf"))


def test_no_grants_means_nothing_is_covered(sandbox):
    from uaic.broker import CapabilitySet
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"fs.read"}), path_grants=())
    assert not caps.covers_path(resolve(sandbox["downloads"] / "a.pdf"))


def test_a_capability_set_refuses_to_be_built_with_an_unknown_field():
    """Found in review: without extra="forbid", `roots=` instead of
    `path_grants=` silently produced a capability set granting nothing. It
    failed closed, but it surfaced as puzzling out-of-scope denials rather
    than as the typo it was."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CapabilitySet(plan_id="p1", tools=frozenset({"fs.list"}),
                      roots=(Path("/tmp"),))


def test_a_correctly_spelled_grant_still_works():
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"fs.list"}),
                         path_grants=(Path("/tmp"),))
    assert len(caps.path_grants) == 1
