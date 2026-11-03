"""Launch the desktop application:  py -m uaic.desktop [--data-dir DIR]

The live planner reads the Gemini key from the UAIC_GEMINI_API_KEY environment
variable. Without it the window still opens; history and undo still work.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from uaic.desktop.window import MainWindow
from uaic.surface import default_data_dir, open_backend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uaic.desktop",
                                     description="Universal AI Coprocessor")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(),
                        help="where the journal and trash live (default: %(default)s)")
    parser.add_argument("--self-test", type=Path, metavar="REPORT",
                        help="start the backend, write a JSON report to REPORT, exit; "
                             "used to verify a packaged build")
    args = parser.parse_args(argv)

    if args.self_test is not None:
        return self_test(args.data_dir, args.self_test)

    app = QApplication(sys.argv[:1])
    backend = open_backend(args.data_dir)
    window = MainWindow(backend)
    backend.start()
    window.show()
    code = app.exec()
    if backend.alive:
        backend.shutdown()
    return code


def self_test(data_dir: Path, report: Path) -> int:
    """Prove a build is whole: the session opens (journal schema found), the
    manifests load, and the optional speech pieces report their state. It
    plans nothing and runs nothing."""
    import json
    import queue

    from uaic.desktop.voice import TextToSpeech, make_speech_to_text
    from uaic.surface import Failed, Ready, describe_plan, open_backend

    backend = open_backend(data_dir)
    backend.start()
    result: dict = {}
    try:
        event = backend.events.get(timeout=30)
        if isinstance(event, Ready):
            result.update(ok=True, can_plan=event.can_plan,
                          planner_error=event.planner_error,
                          history=len(event.history))
        elif isinstance(event, Failed):
            result.update(ok=False, error=event.message)
    except queue.Empty:
        result.update(ok=False, error="the backend did not start within 30s")
    finally:
        backend.shutdown()

    from uaic.surface import Plan
    try:
        probe = Plan(plan_id="probe", prompt="probe",
                     steps=({"seq": 1, "tool": "fs.list", "args": {}},))
        result["manifests_loaded"] = describe_plan(probe)[0].declared
        stt = make_speech_to_text()
        result["speech_in"] = stt.available()
        result["speech_engine"] = stt.engine
        if stt.engine == "whisper" and result["speech_in"][0]:
            # Load the model and run one second of silence through it: proves
            # the inference and VAD libraries were bundled, not just the files.
            try:
                stt.transcribe_pcm(b"\x00\x00" * 16000)
                result["speech_in_runs"] = True
            except Exception as exc:
                result["speech_in_runs"] = f"{type(exc).__name__}: {exc}"
        result["speech_out"] = TextToSpeech().available()
    except Exception as exc:
        # Reported, not raised: a windowed build would otherwise show a modal
        # error box and hang whoever is waiting for the report.
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    Path(report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("ok") and result.get("manifests_loaded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
