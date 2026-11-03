"""Download a Whisper model ONCE, for offline speech input.

    py scripts/get_whisper_model.py            # small (default, ~480 MB)
    py scripts/get_whisper_model.py base       # faster, less accurate (~145 MB)
    py scripts/get_whisper_model.py --to DIR   # somewhere else

This is the only step that uses the network. The app loads the model from
the folder written here and never downloads anything itself (voice.py passes
a local directory and sets HF_HUB_OFFLINE). The default folder is
desktop/models/, which is git-ignored and is where `build_desktop.py
--with-model` picks models up from.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("size", nargs="?", default="small", choices=SIZES)
    ap.add_argument("--to", type=Path, default=ROOT / "desktop" / "models")
    args = ap.parse_args()

    from huggingface_hub import snapshot_download

    target = args.to / f"whisper-{args.size}"
    print(f"downloading Systran/faster-whisper-{args.size} into {target} ...")
    snapshot_download(repo_id=f"Systran/faster-whisper-{args.size}",
                      local_dir=str(target),
                      allow_patterns=["config.json", "model.bin", "tokenizer.json",
                                      "vocabulary.*", "preprocessor_config.json"])
    if not (target / "model.bin").is_file():
        print("download finished but model.bin is missing", file=sys.stderr)
        return 1
    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    print(f"ok: {target} ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
