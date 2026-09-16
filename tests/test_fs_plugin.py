"""The filesystem plugin, and the trash trick that makes delete reversible."""
from __future__ import annotations

import pytest

from uaic.contracts import Origin, Provenance, ToolCall
from uaic.plugins import FileSystemPlugin


@pytest.fixture
def plugin(tmp_path):
    return FileSystemPlugin(tmp_path / "trash")


def call(tool: str, **args) -> ToolCall:
    return ToolCall(call_id="c1", plan_id="p1", tool=tool, args=args)


def test_list_returns_names(plugin, sandbox):
    r = plugin.execute(call("fs.list", path=str(sandbox["downloads"])))
    assert r.ok
    assert set(r.payload) == {"a.pdf", "sub"}


def test_read_marks_content_untrusted(plugin, sandbox):
    r = plugin.execute(call("fs.read", path=str(sandbox["downloads"] / "a.pdf")))
    assert r.ok
    assert r.payload == "a"
    assert r.provenance is Provenance.UNTRUSTED, (
        "anything read off the disk is untrusted input")
    assert r.result_hash, "a read records a content hash"



def test_read_only_calls_have_no_inverse(plugin, sandbox):
    for tool in ("fs.list", "fs.read"):
        arg = (sandbox["downloads"] if tool == "fs.list"
               else sandbox["downloads"] / "a.pdf")
        r = plugin.execute(call(tool, path=str(arg)))
        assert r.inverse is None


def test_move_inverse_swaps_the_arguments(plugin, sandbox):
    src = sandbox["downloads"] / "a.pdf"
    dst = sandbox["downloads"] / "sub" / "a.pdf"
    r = plugin.execute(call("fs.move", src=str(src), dst=str(dst)))
    assert r.ok
    assert dst.exists() and not src.exists()
    assert r.inverse.tool == "fs.move"
    assert r.inverse.args == {"src": str(dst), "dst": str(src)}
    assert r.inverse.origin is Origin.UNDO


def test_move_refuses_to_clobber(plugin, sandbox):
    src = sandbox["downloads"] / "a.pdf"
    dst = sandbox["downloads2"] / "other.pdf"
    r = plugin.execute(call("fs.move", src=str(src), dst=str(dst)))
    assert not r.ok
    assert "FileExistsError" in r.error
    assert src.exists(), "a refused move must not have moved anything"


def test_copy_inverse_deletes_the_copy(plugin, sandbox):
    src = sandbox["downloads"] / "a.pdf"
    dst = sandbox["downloads"] / "sub" / "copy.pdf"
    r = plugin.execute(call("fs.copy", src=str(src), dst=str(dst)))
    assert r.ok
    assert src.exists() and dst.exists()
    assert r.inverse.tool == "fs.delete"
    assert r.inverse.args == {"path": str(dst)}


# ---------- the point of the whole design ----------

def test_delete_moves_to_trash_rather_than_unlinking(plugin, sandbox, tmp_path):
    target = sandbox["downloads"] / "a.pdf"
    r = plugin.execute(call("fs.delete", path=str(target)))
    assert r.ok
    assert not target.exists(), "the file is gone from where the user could see it"

    trash_id = r.payload["trash_id"]
    holding = tmp_path / "trash" / trash_id
    assert holding.is_dir(), "but it still exists in the session trash"
    assert (holding / "a.pdf").read_text() == "a"
    assert (holding / "origin.txt").read_text() == str(target)


def test_delete_then_restore_round_trips(plugin, sandbox):
    target = sandbox["downloads"] / "a.pdf"
    original = target.read_text()

    deleted = plugin.execute(call("fs.delete", path=str(target)))
    assert not target.exists()

    restored = plugin.execute(deleted.inverse)
    assert restored.ok
    assert target.exists()
    assert target.read_text() == original


def test_delete_of_a_folder_is_also_reversible(plugin, sandbox):
    target = sandbox["downloads"] / "sub"
    deleted = plugin.execute(call("fs.delete", path=str(target)))
    assert deleted.ok
    assert not target.exists()

    plugin.execute(deleted.inverse)
    assert (target / "b.pdf").read_text() == "b"


def test_restore_refuses_to_overwrite(plugin, sandbox):
    target = sandbox["downloads"] / "a.pdf"
    deleted = plugin.execute(call("fs.delete", path=str(target)))
    target.write_text("something new arrived here")

    r = plugin.execute(deleted.inverse)
    assert not r.ok
    assert "already exists" in r.error
    assert target.read_text() == "something new arrived here"


def test_restore_with_an_unknown_id_fails_cleanly(plugin, sandbox):
    r = plugin.execute(call("fs.restore", trash_id="deadbeef",
                            dst=str(sandbox["downloads"] / "x.pdf")))
    assert not r.ok
    assert "nothing in the trash" in r.error


def test_delete_of_a_missing_file_fails_cleanly(plugin, sandbox):
    r = plugin.execute(call("fs.delete", path=str(sandbox["downloads"] / "ghost")))
    assert not r.ok
    assert "FileNotFoundError" in r.error


def test_overwrite_has_no_inverse(plugin, sandbox):
    target = sandbox["downloads"] / "a.pdf"
    r = plugin.execute(call("fs.overwrite", path=str(target), content="new"))
    assert r.ok
    assert target.read_text() == "new"
    assert r.inverse is None, "irreversible by design: the old bytes are gone"


def test_unknown_tool_fails_without_raising(plugin, sandbox):
    r = plugin.execute(call("fs.teleport", path=str(sandbox["downloads"])))
    assert not r.ok
    assert "does not implement" in r.error


def test_a_plugin_never_raises(plugin):
    """Whatever goes wrong, the runtime gets a ToolResult, not an exception."""
    r = plugin.execute(call("fs.read", path="\\\\?\\nonsense\\nope"))
    assert r.ok is False
    assert r.error
