"""Documents plugin: getting text out of files a user already has.

This plugin is the project's main untrusted input, and it is worth being blunt
about that. Every other plugin acts on the machine; this one brings the
outside world INTO the context, and the outside world is where an attacker
writes. A PDF is a particularly good place to hide an instruction: text can be
invisible on screen - white on white, zero-sized, behind an image - and still
extract perfectly. So the extraction is the easy half; the classification is
the point.

Three decisions follow from that:

* Every payload is marked UNTRUSTED, unconditionally. There is no argument,
  no flag and no trusted mode.
* Extraction is read-only. Nothing in this file writes to disk, so nothing
  here needs an inverse and undo skips it.
* `pypdf` is an optional dependency. If it is missing, the tool returns a
  clear error rather than failing at import - a machine without it should
  still run the other 270 tests.
"""
from __future__ import annotations

import time
from pathlib import Path

from uaic.contracts import Provenance, ToolCall, ToolResult

# Read as bytes and decoded leniently: a document that is 99% readable is more
# useful than an exception, and the result is untrusted either way.
PLAIN_SUFFIXES = frozenset({".txt", ".md", ".csv", ".log"})
MAX_CHARS = 200_000     # a bound on how much untrusted text can enter context


class DocumentError(Exception):
    """The document could not be read."""


def _require_pypdf():
    try:
        from pypdf import PdfReader
    except ImportError as exc:                       # pragma: no cover
        raise DocumentError(
            "reading PDFs needs the 'pypdf' package: pip install pypdf") from exc
    return PdfReader


class DocumentsPlugin:
    name = "doc"

    def __init__(self, *, max_chars: int = MAX_CHARS) -> None:
        self._max_chars = max_chars

    # ------------------------------------------------------------------ dispatch

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        handler = {
            "doc.extract_text": self._extract_text,
            "doc.page_count": self._page_count,
        }.get(call.tool)

        if handler is None:
            return self._fail(call, f"doc plugin does not implement {call.tool}",
                              started)
        try:
            payload = handler(call)
        except (DocumentError, KeyError, OSError, ValueError) as exc:
            return self._fail(call, f"{type(exc).__name__}: {exc}", started)
        except Exception as exc:
            # A malformed PDF can raise almost anything from deep inside a
            # parser. A document must never be able to crash the agent.
            return self._fail(call, f"could not read the document: "
                                    f"{type(exc).__name__}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            payload=payload,
            inverse=None,                       # read-only: nothing to reverse
            provenance=Provenance.UNTRUSTED,    # always, with no way to change it
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _fail(call: ToolCall, error: str, started: float) -> ToolResult:
        return ToolResult(call_id=call.call_id, ok=False, error=error,
                          duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ handlers

    @staticmethod
    def _target(call: ToolCall) -> Path:
        path = Path(call.args["path"])
        if not path.is_file():
            raise DocumentError(f"{path} is not a file")
        return path

    def _extract_text(self, call: ToolCall) -> str:
        path = self._target(call)
        suffix = path.suffix.lower()

        if suffix in PLAIN_SUFFIXES:
            text = path.read_bytes().decode("utf-8", errors="replace")
        elif suffix == ".pdf":
            text = self._pdf_text(path, call.args.get("pages"))
        else:
            raise DocumentError(f"no extractor for {suffix or 'a file with no suffix'}")

        if len(text) > self._max_chars:
            # Truncation is announced in the text itself, so a summary built
            # from it cannot silently be a summary of the first half.
            text = text[: self._max_chars] + "\n[truncated by the documents plugin]"
        return text

    def _page_count(self, call: ToolCall) -> int:
        path = self._target(call)
        if path.suffix.lower() != ".pdf":
            raise DocumentError("page_count only applies to PDFs")
        return len(_require_pypdf()(str(path)).pages)

    def _pdf_text(self, path: Path, pages: object) -> str:
        reader = _require_pypdf()(str(path))
        wanted = self._page_numbers(pages, len(reader.pages))
        out: list[str] = []
        for number in wanted:
            page = reader.pages[number - 1]
            # Page markers stay in the text. If an injected instruction is
            # ever found, the journal and the provenance chain can say which
            # page of which file it came from.
            out.append(f"[page {number}]\n{page.extract_text() or ''}")
        return "\n\n".join(out).strip()

    @staticmethod
    def _page_numbers(pages: object, total: int) -> list[int]:
        """`pages` is optional: None means the whole document."""
        if pages is None:
            return list(range(1, total + 1))
        if isinstance(pages, int):
            pages = str(pages)
        if not isinstance(pages, str) or not pages.strip():
            raise DocumentError("pages must be a string like '1' or '2-5'")

        text = pages.strip()
        if "-" in text:
            first, _, last = text.partition("-")
            start, end = int(first), int(last)
        else:
            start = end = int(text)
        if start < 1 or end < start or end > total:
            raise DocumentError(
                f"page range {text!r} is outside this {total}-page document")
        return list(range(start, end + 1))
