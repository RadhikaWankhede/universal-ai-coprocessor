"""Turn what a speech recogniser writes into what a request needs.

Recognisers spell things the way they are SAID: "p d f", "jay peg",
"c drive", "my downloads folder". The planner needs ".pdf", "C:\\" and a real
path. This module rewrites those spoken forms, and nothing else.

It is a pure function over text: no I/O beyond checking that a known folder
exists, no model, no network. Its output goes into the request box for the
user to read and edit, exactly like the raw transcript did - it never
submits anything, so a wrong rewrite is seen before it can matter.

Rules are conservative on purpose. A bare word like "documents" is left
alone ("move all documents" is not a path), and so is "the downloads" (as in
"the downloads photos folder"). Only "my documents", "my documents folder",
"the documents folder" and "documents folder" become C:\\Users\\<you>\\Documents.
"""
from __future__ import annotations

import re
from pathlib import Path

# Folders under the user's home that people name by voice.
KNOWN_FOLDERS = ("Downloads", "Documents", "Desktop", "Pictures", "Music", "Videos")

# Extensions as people say them -> the extension.
SPOKEN_EXT = {
    "jay peg": "jpg", "jaypeg": "jpg", "j peg": "jpg",
    "p d f": "pdf", "p.d.f.": "pdf", "p.d.f": "pdf",
    "j p g": "jpg", "p n g": "png", "g i f": "gif", "m p 3": "mp3", "m p 4": "mp4",
    "c s v": "csv", "t x t": "txt", "d o c x": "docx", "x l s x": "xlsx",
    "p p t x": "pptx", "z i p": "zip",
}

_FOLDER_ALT = "|".join(f.lower() for f in KNOWN_FOLDERS)
_FOLDER = re.compile(
    rf"\b(?:my\s+({_FOLDER_ALT})(?:\s+folder)?|(?:the\s+)?({_FOLDER_ALT})\s+folder)\b",
    re.IGNORECASE)
_DRIVE = re.compile(r"\b([a-z])\s+drive\b", re.IGNORECASE)
_DOT_EXT = re.compile(r"\bdot\s+([a-z0-9]{2,5})\b", re.IGNORECASE)
_BACKSLASH = re.compile(r"\s*\bback\s?slash\b\s*", re.IGNORECASE)


def _spoken_extensions(text: str) -> str:
    # Longest first, so "d o c x" is rewritten before anything shorter inside it.
    for said in sorted(SPOKEN_EXT, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.]){re.escape(said)}(?!\w)", SPOKEN_EXT[said],
                      text, flags=re.IGNORECASE)
    return text


def normalise(text: str, home: Path | None = None) -> str:
    """Rewrite spoken forms. Text with none of them passes through unchanged."""
    text = " ".join(text.split())
    if not text:
        return ""
    home = home or Path.home()

    text = _spoken_extensions(text)
    text = _DOT_EXT.sub(lambda m: "." + m.group(1).lower(), text)
    text = _DRIVE.sub(lambda m: m.group(1).upper() + ":\\", text)
    text = _BACKSLASH.sub(lambda m: "\\", text)

    def folder(m: re.Match) -> str:
        name = (m.group(1) or m.group(2)).lower()
        real = next(f for f in KNOWN_FOLDERS if f.lower() == name)
        path = home / real
        return str(path) if path.is_dir() else m.group(0)

    text = _FOLDER.sub(folder, text)
    # Recognisers end a command with a full stop; the request box doesn't need it.
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    return text
