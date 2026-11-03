"""Filesystem plugin.

The important design decision is in `_delete`: deletion is implemented as a MOVE
INTO A SESSION TRASH DIRECTORY, not as unlink(). That single choice moves the
operation from the irreversible tier to the reversible tier - identical
behaviour from the user's point of view, but it no longer needs a confirmation
prompt, and undo can put the file back exactly.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from uaic.contracts import Origin, Provenance, ToolCall, ToolResult

READ_ONLY = frozenset({"fs.list", "fs.read"})

BATCH_LIMIT = 500                       # files per fs.move_matching call
_BATCH_ID = re.compile(r"[0-9a-f]{12}")
_PATTERN_FORBIDDEN = ("/", "\\", ":", "..", "\x00")


def check_pattern(pattern: object) -> str:
    """A wildcard over FILE NAMES in one folder, never a path.

    Matching is done against bare names from iterdir(), so a separator could
    never match anything anyway; refusing one says so plainly instead of
    silently moving nothing.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern must be a non-empty file-name wildcard, e.g. *.jpg")
    if any(bad in pattern for bad in _PATTERN_FORBIDDEN):
        raise ValueError(f"pattern {pattern!r} must be a file name, not a path")
    return pattern


def matching_files(folder: Path, pattern: str) -> list[Path]:
    """Regular files DIRECTLY inside `folder` whose name matches, case-insensitively.

    Not recursive, and symlinks are skipped: what moves is exactly what the
    folder holds, never something a link points at.
    """
    pat = check_pattern(pattern).lower()
    found = [p for p in folder.iterdir()
             if p.is_file() and not p.is_symlink()
             and fnmatch.fnmatchcase(p.name.lower(), pat)]
    return sorted(found, key=lambda p: p.name.lower())


def _same(a: Path | str, b: Path | str) -> bool:
    return (os.path.normcase(str(Path(a).resolve()))
            == os.path.normcase(str(Path(b).resolve())))


def check_record(record: object, folder: Path) -> list[tuple[Path, Path]]:
    """Validate a batch record before anything is moved back.

    The record lives on disk, so it is treated as input, not as truth: every
    file must come from the record's `to_folder` and go back DIRECTLY into its
    `from_folder`, under the same name, and `from_folder` must be the folder
    the broker scoped. A record edited to send a file anywhere else is
    refused whole, before anything moves.
    """
    if not isinstance(record, dict):
        raise ValueError("batch record is malformed")
    from_folder, to_folder = record.get("from_folder"), record.get("to_folder")
    moves = record.get("moves")
    if (not isinstance(from_folder, str) or not isinstance(to_folder, str)
            or not isinstance(moves, list)):
        raise ValueError("batch record is malformed")
    if not _same(from_folder, folder):
        raise PermissionError(f"batch record belongs to {from_folder}, not {folder}")
    pairs = []
    for m in moves:
        if (not isinstance(m, dict) or not isinstance(m.get("src"), str)
                or not isinstance(m.get("dst"), str)):
            raise ValueError("batch record is malformed")
        back, now = Path(m["src"]), Path(m["dst"])
        if (not _same(back.parent, from_folder) or not _same(now.parent, to_folder)
                or back.name != now.name):
            raise PermissionError(f"batch record entry {m!r} leaves its folders")
        pairs.append((now, back))
    return pairs


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class FileSystemPlugin:
    name = "fs"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = Path(trash_root)
        self.trash_root.mkdir(parents=True, exist_ok=True)
        # Beside the trash, not inside it: a trash id must never name a batch.
        self.batch_root = self.trash_root.with_name(self.trash_root.name + "-batches")

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
            "fs.move_matching": self._move_matching,
            "fs.restore_batch": self._restore_batch,
            "fs.make_folder": self._make_folder,
            "fs.remove_empty_folder": self._remove_empty_folder,
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

    # ------------------------------------------------------- bulk and folders

    @staticmethod
    def _move_all(pairs: list[tuple[Path, Path]]) -> None:
        """Move every (src, dst) or none: a failure part-way puts back what moved."""
        for src, dst in pairs:
            if not src.is_file():
                raise FileNotFoundError(src)
            if dst.exists():
                raise FileExistsError(f"{dst} already exists; nothing was moved")
        done: list[tuple[Path, Path]] = []
        try:
            for src, dst in pairs:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                done.append((src, dst))
        except Exception:
            for src, dst in reversed(done):
                shutil.move(str(dst), str(src))
            raise

    def _save_batch(self, from_folder: Path, to_folder: Path,
                    pairs: list[tuple[Path, Path]]) -> str:
        self.batch_root.mkdir(parents=True, exist_ok=True)
        batch_id = uuid.uuid4().hex[:12]
        record = {"from_folder": str(from_folder), "to_folder": str(to_folder),
                  "moves": [{"src": str(s), "dst": str(d)} for s, d in pairs]}
        (self.batch_root / f"{batch_id}.json").write_text(
            json.dumps(record, indent=1), encoding="utf-8")
        return batch_id

    def _move_matching(self, call: ToolCall):
        folder, dest = Path(call.args["folder"]), Path(call.args["destination"])
        pattern = call.args["pattern"]
        if not folder.is_dir():
            raise NotADirectoryError(f"{folder} is not a folder")
        if dest.exists() and not dest.is_dir():
            raise NotADirectoryError(f"{dest} exists and is not a folder")
        files = matching_files(folder, pattern)
        if len(files) > BATCH_LIMIT:
            raise ValueError(f"{len(files)} files match {pattern!r}; the limit for "
                             f"one step is {BATCH_LIMIT}")
        pairs = [(f, dest / f.name) for f in files]
        clashes = [d.name for _, d in pairs if d.exists()]
        if clashes:
            raise FileExistsError(
                f"{len(clashes)} file(s) already exist in {dest}, e.g. {clashes[0]}; "
                "nothing was moved")
        dest.mkdir(parents=True, exist_ok=True)
        self._move_all(pairs)
        batch_id = self._save_batch(folder, dest, pairs)
        lines = [f"moved {len(pairs)} file(s) matching {pattern} into {dest}"]
        lines += [f.name for f in files]
        return (lines,
                self._inverse(call, "fs.restore_batch", batch_id=batch_id,
                              folder=str(folder)),
                None)

    def _restore_batch(self, call: ToolCall):
        batch_id, folder = call.args["batch_id"], Path(call.args["folder"])
        if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id):
            raise ValueError(f"{batch_id!r} is not a batch id")
        path = self.batch_root / f"{batch_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no batch with id {batch_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        pairs = check_record(record, folder)
        self._move_all(pairs)
        path.unlink()                        # used up: a record cannot be replayed
        reverse = self._save_batch(Path(record["to_folder"]),
                                   Path(record["from_folder"]), pairs)
        return ([f"moved {len(pairs)} file(s) back into {folder}"],
                self._inverse(call, "fs.restore_batch", batch_id=reverse,
                              folder=record["to_folder"]),
                None)

    def _make_folder(self, call: ToolCall):
        target = Path(call.args["path"])
        if target.exists():
            raise FileExistsError(f"{target} already exists")
        top = target
        while not top.parent.exists():
            top = top.parent
        target.mkdir(parents=True)
        # Undo removes exactly the folders this call created, and only if empty.
        return (str(target),
                self._inverse(call, "fs.remove_empty_folder", path=str(target),
                              up_to=str(top)),
                None)

    def _remove_empty_folder(self, call: ToolCall):
        """rmdir only: it cannot delete a file, by construction."""
        target = Path(call.args["path"])
        up_to = Path(call.args.get("up_to") or target)
        if not target.is_dir() or target.is_symlink():
            raise NotADirectoryError(f"{target} is not a folder")
        chain = [target]
        while not _same(chain[-1], up_to):
            parent = chain[-1].parent
            if parent == chain[-1]:
                raise ValueError(f"{up_to} is not {target} or a folder above it")
            chain.append(parent)
        for i, d in enumerate(chain):
            allowed = {chain[i - 1].name} if i else set()
            extra = [p.name for p in d.iterdir() if p.name not in allowed]
            if extra:
                raise OSError(f"{d} is not empty (contains {extra[0]}); nothing removed")
        for d in chain:
            d.rmdir()
        return (str(target),
                self._inverse(call, "fs.make_folder", path=str(target)),
                None)
