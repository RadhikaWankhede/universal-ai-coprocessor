import pytest
from broker import decide

ALLOWED_ACTIONS = {"move", "delete", "email", "rename"}
DOWNLOADS = r"C:\Users\Radhika Wankhede\Downloads"
DOCUMENTS = r"C:\Users\Radhika Wankhede\Documents"
ALLOWED_FOLDERS = [DOWNLOADS]

CASES = [
    ("plain move is allowed",     "move",   DOWNLOADS + r"\a.pdf",                     "YES"),
    ("delete goes to trash",      "delete", DOWNLOADS + r"\b.pdf",                     "YES"),
    ("email cannot be undone",    "email",  DOWNLOADS + r"\c.pdf",                     "ASK"),
    ("outside granted folder",    "move",   DOCUMENTS + r"\secret.docx",               "NO"),
    ("traversal escape blocked",  "move",   DOWNLOADS + r"\..\Documents\secret.docx",  "NO"),
    ("sibling prefix blocked",    "move",   DOWNLOADS + r"2\other.pdf",                "NO"),
    ("undeclared action blocked", "fly",    DOWNLOADS + r"\a.pdf",                     "NO"),
]


@pytest.mark.parametrize(
    "action, target, expected",
    [(c[1], c[2], c[3]) for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_decide(action, target, expected):
    result = decide(action, target, ALLOWED_ACTIONS, ALLOWED_FOLDERS)
    assert result.startswith(expected), f"got {result!r}"