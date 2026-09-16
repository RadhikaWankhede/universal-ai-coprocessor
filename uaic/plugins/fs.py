"""Filesystem plugin.

The important design decision is in `_delete`: deletion is implemented as a MOVE
INTO A SESSION TRASH DIRECTORY, not as unlink(). That single choice moves the
operation from the irreversible tier to the reversible tier - identical
behaviour from the user's point of view, but it no longer needs a confirmation
prompt, and undo can put the file back exactly.
"""
from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from pathlib import Path

from uaic.contracts import Origin, Provenance, ToolCall, ToolResult

READ_ONLY = frozenset({"fs.list", "fs.read"})


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class FileSystemPlugin:
    name = "fs"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = Path(trash_root)
        self.trash_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- dispatch

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        handler = {
            "fs.list": self._list,
            "fs.read": self._read,
            "fs.copy": self._copy,
            "fs.move": self._move,
            "fs.delete": self._delete,
            "fs.restore": self._restore,
            "fs.overwrite": self._overwrite,
        }.get(call.tool)

        if handler is None:
            return self._fail(call, f"fs plugin does not implement {call.tool}", started)

        try:
            payload, inverse, result_hash = handler(call)
        except Exception as exc:
            return self._fail(call, f"{type(exc).__name__}: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            payload=payload,
            inverse=inverse,
            result_hash=result_hash,
            provenance=(Provenance.UNTRUSTED if call.tool in READ_ONLY
                        else Provenance.TRUSTED),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _fail(call: ToolCall, error: str, started: float) -> ToolResult:
        return ToolResult(call_id=call.call_id, ok=False, error=error,
                          duration_ms=int((time.perf_counter() - started) * 1000))

    def _inverse(self, call: ToolCall, tool: str, **args) -> ToolCall:
        return ToolCall(call_id=f"undo-{call.call_id}", plan_id=call.plan_id,
                        tool=tool, args=args, seq=call.seq, origin=Origin.UNDO)

    # -------------------------------------------------------------- handlers

    def _list(self, call: ToolCall):
        target = Path(call.args["path"])
        return sorted(p.name for p in target.iterdir()), None, None

    def _read(self, call: ToolCall):
        data = Path(call.args["path"]).read_bytes()
        return data.decode("utf-8", errors="replace"), None, _hash(data)

    def _copy(self, call: ToolCall):
        src, dst = Path(call.args["src"]), Path(call.args["dst"])
        if dst.exists():
            raise FileExistsError(f"{dst} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # undoing a copy means removing the copy
        return str(dst), self._inverse(call, "fs.delete", path=str(dst)), None

    def _move(self, call: ToolCall):
        src, dst = Path(call.args["src"]), Path(call.args["dst"])
        if dst.exists():
            raise FileExistsError(f"{dst} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        # a move is its own inverse, with the arguments swapped
        return str(dst), self._inverse(call, "fs.move", src=str(dst), dst=str(src)), None

    def _delete(self, call: ToolCall):
        """Move into the session trash. This is what makes delete reversible."""
        target = Path(call.args["path"])
        if not target.exists():
            raise FileNotFoundError(target)
        trash_id = uuid.uuid4().hex[:12]
        holding = self.trash_root / trash_id
        holding.mkdir(parents=True)
        (holding / "origin.txt").write_text(str(target), encoding="utf-8")
        shutil.move(str(target), str(holding / target.name))
        return ({"trash_id": trash_id, "original": str(target)},
                self._inverse(call, "fs.restore", trash_id=trash_id, dst=str(target)),
                None)

    def _restore(self, call: ToolCall):
        trash_id, dst = call.args["trash_id"], Path(call.args["dst"])
        holding = self.trash_root / trash_id
        if not holding.is_dir():
            raise FileNotFoundError(f"nothing in the trash with id {trash_id}")
        items = [p for p in holding.iterdir() if p.name != "origin.txt"]
        if not items:
            raise FileNotFoundError(f"trash entry {trash_id} is empty")
        if dst.exists():
            raise FileExistsError(f"{dst} already exists; refusing to overwrite")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(items[0]), str(dst))
        shutil.rmtree(holding, ignore_errors=True)
        return (str(dst),
                self._inverse(call, "fs.delete", path=str(dst)),
                None)

    def _overwrite(self, call: ToolCall):
        """Declared irreversible in the manifest: the previous content is gone.

        Snapshotting the old bytes into the trash first would move this up the
        ladder to reversible - a worked example of the design principle, left
        as the next improvement rather than claimed as done.
        """
        target = Path(call.args["path"])
        content = call.args["content"]
        target.write_text(content, encoding="utf-8")
        return str(target), None, _hash(content.encode())
