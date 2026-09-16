from __future__ import annotations

from pathlib import Path

import pytest

from uaic.broker import CapabilitySet


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """A real folder tree, so path tests behave identically on Windows and Linux."""
    downloads = tmp_path / "Downloads"
    downloads2 = tmp_path / "Downloads2"       # sibling-prefix trap
    documents = tmp_path / "Documents"
    for d in (downloads, downloads2, documents):
        d.mkdir()
    (downloads / "a.pdf").write_text("a")
    (downloads / "sub").mkdir()
    (downloads / "sub" / "b.pdf").write_text("b")
    (downloads2 / "other.pdf").write_text("o")
    (documents / "secret.docx").write_text("s")
    return {
        "root": tmp_path,
        "downloads": downloads,
        "downloads2": downloads2,
        "documents": documents,
    }


@pytest.fixture
def caps(sandbox) -> CapabilitySet:
    return CapabilitySet(
        plan_id="p1",
        tools=frozenset({"fs.list", "fs.read", "fs.move", "fs.delete",
                         "git.commit", "mail.send"}),
        path_grants=(sandbox["downloads"],),
    )
