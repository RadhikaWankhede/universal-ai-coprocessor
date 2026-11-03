"""Measure speech recognition on REAL voices: record, then score each engine.

    py scripts/speech_eval.py record --speaker shreyansh     # read 12 sentences aloud
    py scripts/speech_eval.py record --speaker friend-uk
    py scripts/speech_eval.py score                          # Whisper vs Vosk, per speaker

Synthetic Windows voices are all US English, so they cannot tell you how an
Indian, British or Nigerian accent fares. This can: each speaker reads the
same request-shaped sentences, and `score` reports the word error rate (WER)
and the time per clip for every engine whose model is installed.

Samples live in %LOCALAPPDATA%\\uaic\\speech-samples\\<speaker>\\NN.wav with the
expected words in NN.txt - outside the repository, because they are
recordings of people. Any 16-bit WAV + .txt pair dropped there is scored too.
Results go to eval/out/speech_wer.csv (a flat file, never the journal).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from uaic.desktop.spoken import normalise  # noqa: E402
from uaic.desktop.voice import SAMPLE_RATE, SpeechToText, WhisperSpeechToText  # noqa: E402
from uaic.surface import default_data_dir  # noqa: E402

SAMPLES = default_data_dir() / "speech-samples"
OUT = ROOT / "eval" / "out" / "speech_wer.csv"

SENTENCES = (
    "move all the pdf files in my downloads folder into my documents folder",
    "list the files on my desktop",
    "copy the report to my pictures folder",
    "what version of python is installed",
    "delete the old log file from my downloads folder",
    "move all the jpg photos from my downloads folder into my pictures folder",
    "create a folder called projects in my documents folder",
    "read the notes file and tell me what it says",
    "is git installed on this computer",
    "rename the invoice file to invoice march",
    "how many pages does the report have",
    "show the git status of my project",
)


# ------------------------------------------------------------------ record

def record(speaker: str) -> int:
    import sounddevice as sd

    folder = SAMPLES / re.sub(r"[^\w-]", "_", speaker)
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Recording into {folder}\n"
          "For each sentence: press Enter, read it aloud naturally, press Enter again.\n"
          "Type s then Enter to skip one, q then Enter to stop.\n")
    for n, sentence in enumerate(SENTENCES, 1):
        print(f"[{n}/{len(SENTENCES)}]  {sentence}")
        if input("  Enter to start > ").strip().lower() in ("q", "s"):
            continue
        chunks: list[bytes] = []
        keep = chunks.append                    # bound now, not looked up per loop
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=4000,
                               callback=lambda d, f, t, s, keep=keep: keep(bytes(d))):
            input("  recording... Enter to stop > ")
        audio = b"".join(chunks)
        with wave.open(str(folder / f"{n:02d}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio)
        (folder / f"{n:02d}.txt").write_text(sentence, encoding="utf-8")
        print(f"  saved {len(audio) / (2 * SAMPLE_RATE):.1f}s")
    return 0


# ------------------------------------------------------------------- score

def words(text: str, home: Path) -> list[str]:
    """Compare like with like: both sides get the same spoken-form cleanup,
    then case and punctuation are ignored."""
    return re.findall(r"[a-z0-9:\\._-]+", normalise(text, home).lower().replace(",", " "))


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """(word edits, reference length) - Levenshtein over words."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1], len(ref)


def engines() -> list[tuple[str, object]]:
    found = []
    for name, stt in (("whisper", WhisperSpeechToText()), ("vosk", SpeechToText())):
        ok, why = stt.available()
        if ok:
            found.append((name, stt))
        else:
            print(f"(skipping {name}: {why})")
    return found


def score() -> int:
    clips = sorted(p for p in SAMPLES.glob("*/*.wav") if p.with_suffix(".txt").is_file())
    if not clips:
        print(f"no recordings in {SAMPLES}; run:  py scripts/speech_eval.py record "
              "--speaker <name>")
        return 1
    home = Path.home()
    rows, totals = [], {}
    for name, stt in engines():
        for clip in clips:
            ref = clip.with_suffix(".txt").read_text(encoding="utf-8").strip()
            started = time.perf_counter()
            try:
                heard = stt.transcribe_wav(clip)
            except Exception as exc:
                heard = f"<error {type(exc).__name__}>"
            seconds = time.perf_counter() - started
            edits, length = wer(words(ref, home), words(heard, home))
            speaker = clip.parent.name
            rows.append({"engine": name, "speaker": speaker, "clip": clip.name,
                         "expected": ref, "heard": heard, "edits": edits,
                         "words": length, "seconds": round(seconds, 2)})
            t = totals.setdefault((name, speaker), [0, 0, 0.0, 0])
            t[0] += edits
            t[1] += length
            t[2] += seconds
            t[3] += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'engine':8} {'speaker':20} {'clips':>5} {'WER':>7} {'s/clip':>7}")
    for (name, speaker), (edits, length, seconds, n) in sorted(totals.items()):
        print(f"{name:8} {speaker:20} {n:5d} {edits / max(length, 1):7.1%} "
              f"{seconds / n:7.2f}")
    print(f"\nper-clip results: {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="read the sentences aloud and save them")
    r.add_argument("--speaker", required=True, help="a label, e.g. your name or accent")
    sub.add_parser("score", help="transcribe every recording with every engine")
    args = ap.parse_args()
    return record(args.speaker) if args.cmd == "record" else score()


if __name__ == "__main__":
    raise SystemExit(main())
