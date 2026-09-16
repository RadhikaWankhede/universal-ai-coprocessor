from pathlib import Path

UNDOABLE = {"move", "copy", "delete"}


def inside_folder(target, allowed_folders):
    t = Path(target).expanduser().resolve()
    for folder in allowed_folders:
        f = Path(folder).expanduser().resolve()
        try:
            t.relative_to(f)
            return True
        except ValueError:
            continue
    return False


def decide(action, target, allowed_actions, allowed_folders):
    if action not in allowed_actions:
        return "NO  - action not approved"
    if not inside_folder(target, allowed_folders):
        return "NO  - outside allowed folder"
    if action in UNDOABLE:
        return "YES"
    return "ASK - cannot be undone"


if __name__ == "__main__":
    allowed_actions = {"move", "delete", "email", "rename"}
    allowed_folders = [r"C:\Users\Radhika Wankhede\Downloads"]

    tests = [
        ("move",   r"C:\Users\Radhika Wankhede\Downloads\a.pdf"),
        ("delete", r"C:\Users\Radhika Wankhede\Downloads\b.pdf"),
        ("email",  r"C:\Users\Radhika Wankhede\Downloads\c.pdf"),
        ("move",   r"C:\Users\Radhika Wankhede\Documents\secret.docx"),
        ("move",   r"C:\Users\Radhika Wankhede\Downloads\..\Documents\secret.docx"),
        ("move",   r"C:\Users\Radhika Wankhede\Downloads2\other.pdf"),
        ("rename", r"C:\Users\Radhika Wankhede\Downloads\a.pdf"),
    ]

    for a, t in tests:
        print(f"{a:8} {t:62} -> {decide(a, t, allowed_actions, allowed_folders)}")