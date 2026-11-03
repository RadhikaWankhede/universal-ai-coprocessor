"""Package the desktop app with PyInstaller (milestone 9).

    py scripts/build_desktop.py                 # dist/UAIC/UAIC.exe
    py scripts/build_desktop.py --with-model    # also copy desktop/models/ next to it
    py scripts/build_desktop.py --verify        # build, then run the exe's --self-test

A one-folder build, not one-file: a one-file exe unpacks itself to a temp
folder on every start, which is slow and makes antivirus suspicious.

The tool manifests (YAML) and the journal schema (SQL) are data files loaded
from next to their modules, so they are collected explicitly - a build
without them would have no tools and no journal. Speech models (whisper-*,
vosk-model-*) are large and user-chosen, so they are only copied with
--with-model; otherwise the app looks for them in %LOCALAPPDATA%\\uaic\\models
or a `models` folder next to the exe.

The API key is never bundled. The packaged app reads UAIC_GEMINI_API_KEY from
the environment exactly as the source version does.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# PyInstaller's --collect-data resolves `uaic` with THIS process's imports, not
# with --paths; without this the build silently ships no manifests.
sys.path.insert(0, str(ROOT))
NAME = "UAIC"
LAUNCHER = ROOT / "scripts" / "uaic_desktop.py"


def build() -> Path:
    import PyInstaller.__main__ as pyinstaller

    pyinstaller.run([
        str(LAUNCHER),
        "--name", NAME,
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT),
        "--collect-data", "uaic",              # manifests/*.yaml, journal/schema.sql
        "--collect-submodules", "uaic",
        "--collect-binaries", "vosk",          # libvosk.dll and friends
        "--collect-all", "faster_whisper",     # its Silero VAD model is a data file
        "--collect-binaries", "ctranslate2",   # the Whisper inference DLLs
        "--collect-binaries", "onnxruntime",   # runs the VAD
        "--collect-submodules", "tokenizers",
        "--collect-submodules", "pyttsx3",     # its drivers are imported by name
        "--hidden-import", "pyttsx3.drivers.sapi5",
        "--hidden-import", "comtypes.client",
        # Nothing in the app uses these; leaving them out keeps the build smaller
        # and keeps Qt's network-server modules out of the bundle entirely.
        "--exclude-module", "PySide6.QtNetwork",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebSockets",
        "--exclude-module", "PySide6.QtHttpServer",
        "--exclude-module", "tkinter",
    ])
    return ROOT / "dist" / NAME


def copy_model(dist: Path) -> None:
    src = ROOT / "desktop" / "models"
    models = (sorted(src.glob("whisper-*")) + sorted(src.glob("vosk-model-*"))
              if src.is_dir() else [])
    if not models:
        print(f"no whisper-* or vosk-model-* folder in {src}; skipping")
        return
    for model in models:
        target = dist / "models" / model.name
        if not target.exists():
            shutil.copytree(model, target)
            print(f"copied {model.name}")


def verify(dist: Path) -> int:
    exe = dist / f"{NAME}.exe"
    with tempfile.TemporaryDirectory(prefix="uaic-build-check-") as tmp:
        report = Path(tmp) / "report.json"
        proc = subprocess.run([str(exe), "--data-dir", str(Path(tmp) / "data"),
                               "--self-test", str(report)], timeout=120, check=False)
        if not report.exists():
            print(f"self-test wrote no report (exit {proc.returncode})")
            return 1
        result = json.loads(report.read_text(encoding="utf-8"))
    print(json.dumps(result, indent=2))
    ok = proc.returncode == 0 and result.get("ok") and result.get("manifests_loaded")
    print("BUILD OK" if ok else "BUILD BROKEN")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-model", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    dist = build()
    if args.with_model:
        copy_model(dist)
    print(f"built {dist / (NAME + '.exe')}")
    return verify(dist) if args.verify else 0


if __name__ == "__main__":
    raise SystemExit(main())
