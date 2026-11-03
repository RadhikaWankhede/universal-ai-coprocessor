"""Offline speech in and out. Nothing here touches the network.

Speech-to-text has two engines, chosen by `make_speech_to_text()`:

* Whisper (faster-whisper, CPU, int8) - the default when its model is
  present. Far better with Indian, British and other non-US accents, and
  with mixed Hindi-English. It transcribes after you stop talking, so the
  window runs it on a background thread (`stop_in_background`).
* Vosk - small and instant, US-English only. The fallback.

Both load a model from a local folder downloaded once (see
`scripts/get_whisper_model.py` and `find_model`); neither downloads anything
at run time. Both pass the transcript through `spoken.normalise`, which turns
"p d f" into "pdf" and "my downloads folder" into a real path.

Text-to-speech: pyttsx3, which drives the Windows SAPI5 voices already on the
machine. Everything is optional: if a library or the model is missing,
`available()` says why and the window disables the control.

The rules speech must follow, because a microphone is an input channel:

* What the user says is TRUSTED - it is the user's own words - but it is
  placed in the request box for the user to read and edit. It is never
  submitted automatically: a misheard command must be seen before it is sent.
* Speech NEVER answers an approval or a confirmation. Listening stops when a
  question appears, and nothing here has a reference to the backend.
* Text-to-speech reads the run's result summary, which is produced by the
  runtime. It is never handed file content to read aloud as if it were the
  system speaking.
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from uaic.desktop.spoken import normalise
from uaic.surface import default_data_dir

SAMPLE_RATE = 16000
MODEL_ENV = "UAIC_VOSK_MODEL"
WHISPER_ENV = "UAIC_WHISPER_MODEL"      # a whisper-* model folder
ENGINE_ENV = "UAIC_STT_ENGINE"          # "whisper" or "vosk"; unset = best available
LANGUAGE_ENV = "UAIC_STT_LANGUAGE"      # "en" (default), "auto", "hi", ...

# Model folders by preference: small is the accuracy/latency sweet spot on a CPU.
WHISPER_PREFERENCE = ("small", "distil-large-v3", "medium", "base", "large-v3", "tiny")

# Primes Whisper with the words requests use, so "PDF", "Downloads" and
# "folder" win over sound-alikes. Kept short: a long prompt slows decoding.
HINT_PROMPT = ("Move the PDF and JPG files from my Downloads folder into my Documents "
               "folder. Copy, rename, delete, list, read, Desktop, Pictures, Python, git.")

MIN_AUDIO_SECONDS = 0.3                 # a click of the button, not speech

Deliver = Callable[[str, str | None], None]   # (text, error) from a background stop


def model_search_path() -> list[Path]:
    """Where a Vosk model folder (vosk-model-*) is looked for, in order."""
    roots = [default_data_dir() / "models",
             Path(__file__).resolve().parents[2] / "desktop" / "models"]
    if getattr(sys, "frozen", False):                  # a PyInstaller build
        roots.insert(0, Path(sys.executable).parent / "models")
    return roots


def _is_model(path: Path) -> bool:
    return path.is_dir() and (path / "am").is_dir() and (path / "conf").is_dir()


def find_model(explicit: Path | str | None = None) -> Path | None:
    for given in (explicit, os.environ.get(MODEL_ENV)):
        if given and _is_model(Path(given)):
            return Path(given)
    for root in model_search_path():
        if root.is_dir():
            for candidate in sorted(root.glob("vosk-model-*")):
                if _is_model(candidate):
                    return candidate
    return None


class SpeechToText:
    """Vosk. Push-to-talk: `start()` opens the microphone, `stop()` returns the text."""

    engine = "vosk"

    def __init__(self, model_dir: Path | str | None = None) -> None:
        self._model_dir = model_dir
        self._model = None
        self._stream = None
        self._audio: queue.Queue[bytes | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._pieces: list[str] = []
        self._recognizer = None

    def available(self) -> tuple[bool, str]:
        try:
            import sounddevice  # noqa: F401
            import vosk  # noqa: F401
        except ImportError as exc:
            return False, f"speech input needs vosk and sounddevice ({exc.name} missing)"
        if find_model(self._model_dir) is None:
            where = " or ".join(str(p) for p in model_search_path())
            return False, (f"no Vosk model found; unzip a vosk-model-* folder into {where}, "
                           f"or set {MODEL_ENV}")
        return True, "ready"

    @property
    def listening(self) -> bool:
        return self._stream is not None

    def _load(self):
        if self._model is None:
            import vosk
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(str(find_model(self._model_dir)))
        return self._model

    def start(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd
        import vosk
        self._recognizer = vosk.KaldiRecognizer(self._load(), SAMPLE_RATE)
        self._pieces = []
        self._audio = queue.Queue()
        self._worker = threading.Thread(target=self._feed, name="uaic-stt", daemon=True)
        self._worker.start()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=8000, dtype="int16", channels=1,
            callback=lambda data, frames, time, status: self._audio.put(bytes(data)))
        self._stream.start()

    def stop(self) -> str:
        """Close the microphone and return everything heard, as plain text."""
        if self._stream is None:
            return ""
        stream, self._stream = self._stream, None
        stream.stop()
        stream.close()
        self._audio.put(None)
        if self._worker is not None:
            self._worker.join(5)
        return self._finish()

    def _feed(self) -> None:
        while (chunk := self._audio.get()) is not None:
            if self._recognizer.AcceptWaveform(chunk):
                self._pieces.append(json.loads(self._recognizer.Result()).get("text", ""))

    def _finish(self) -> str:
        self._pieces.append(json.loads(self._recognizer.FinalResult()).get("text", ""))
        return normalise(" ".join(p for p in self._pieces if p))

    def transcribe_wav(self, path: Path | str) -> str:
        """Recognise a 16-bit mono WAV file. Used to verify a model offline."""
        import wave

        import vosk
        with wave.open(str(path), "rb") as wav:
            self._recognizer = vosk.KaldiRecognizer(self._load(), wav.getframerate())
            self._pieces = []
            while chunk := wav.readframes(4000):
                if self._recognizer.AcceptWaveform(chunk):
                    self._pieces.append(json.loads(self._recognizer.Result()).get("text", ""))
        return self._finish()


def _is_whisper_model(path: Path) -> bool:
    return path.is_dir() and (path / "model.bin").is_file() and (path / "config.json").is_file()


def find_whisper_model(explicit: Path | str | None = None) -> Path | None:
    for given in (explicit, os.environ.get(WHISPER_ENV)):
        if given and _is_whisper_model(Path(given)):
            return Path(given)
    for size in WHISPER_PREFERENCE:
        for root in model_search_path():
            candidate = root / f"whisper-{size}"
            if _is_whisper_model(candidate):
                return candidate
    return None


class WhisperSpeechToText:
    """Whisper. Same push-to-talk interface as SpeechToText, plus a
    non-blocking stop, because transcription takes a second or two on a CPU.

    The microphone is recorded into memory while the button is down and
    transcribed once when it comes up: Whisper works on whole utterances,
    which is also what makes it robust to accents.
    """

    engine = "whisper"

    def __init__(self, model_dir: Path | str | None = None, *,
                 language: str | None = None, home: Path | None = None) -> None:
        self._model_dir = model_dir
        self.language = (language if language is not None
                         else os.environ.get(LANGUAGE_ENV, "en")).strip().lower()
        self._home = home
        self._model = None
        self._load_lock = threading.Lock()
        self._stream = None
        self._chunks: list[bytes] = []
        self._chunks_lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError as exc:
            return False, f"speech input needs faster-whisper and sounddevice ({exc.name} missing)"
        if find_whisper_model(self._model_dir) is None:
            where = " or ".join(str(p) for p in model_search_path())
            return False, ("no Whisper model found; run  py scripts/get_whisper_model.py  "
                           f"(looked for a whisper-* folder in {where}, or set {WHISPER_ENV})")
        return True, "ready"

    @property
    def model_path(self) -> Path | None:
        return find_whisper_model(self._model_dir)

    @property
    def listening(self) -> bool:
        return self._stream is not None

    def warm_up(self) -> None:
        """Load the model in the background, so the first Speak is not slow."""
        def load() -> None:
            with contextlib.suppress(Exception):   # reported on first use instead
                self._load()
        threading.Thread(target=load, name="uaic-stt-load", daemon=True).start()

    def _load(self):
        with self._load_lock:
            if self._model is None:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")    # a local folder, never a download
                from faster_whisper import WhisperModel
                path = find_whisper_model(self._model_dir)
                if path is None:
                    raise FileNotFoundError("no Whisper model found")
                self._model = WhisperModel(str(path), device="cpu", compute_type="int8",
                                           cpu_threads=min(8, os.cpu_count() or 4))
        return self._model

    # ------------------------------------------------------- the microphone

    def start(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd
        with self._chunks_lock:
            self._chunks = []

        def keep(data, frames, time, status) -> None:
            with self._chunks_lock:
                self._chunks.append(bytes(data))

        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=4000,
                                   dtype="int16", channels=1, callback=keep)
        stream.start()
        self._stream = stream

    def _close(self) -> bytes:
        """Close the microphone; return what it recorded (16 kHz mono int16)."""
        if self._stream is None:
            return b""
        stream, self._stream = self._stream, None
        stream.stop()
        stream.close()
        with self._chunks_lock:
            audio, self._chunks = b"".join(self._chunks), []
        return audio

    def stop(self) -> str:
        """Close the microphone and return the text. Blocks while transcribing."""
        return self.transcribe_pcm(self._close())

    def stop_in_background(self, deliver: Deliver) -> None:
        """Close the microphone now; call deliver(text, error) from a worker
        thread when the transcript is ready. The caller must hand the result
        back to its own thread - deliver must not touch a UI."""
        audio = self._close()

        def work() -> None:
            try:
                text = self.transcribe_pcm(audio)
            except Exception as exc:
                deliver("", f"{type(exc).__name__}: {exc}")
                return
            deliver(text, None)
        threading.Thread(target=work, name="uaic-stt", daemon=True).start()

    def discard(self) -> None:
        """Close the microphone and throw the recording away."""
        self._close()

    # -------------------------------------------------------- transcription

    def transcribe_pcm(self, pcm: bytes) -> str:
        if len(pcm) < int(SAMPLE_RATE * 2 * MIN_AUDIO_SECONDS):
            return ""
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self._transcribe(audio)

    def transcribe_wav(self, path: Path | str) -> str:
        """Recognise a PCM WAV file (any rate, mono or stereo). For tests and eval.

        Decoded here with the standard library rather than handed to
        faster-whisper as a path, which would pull in PyAV for one file format.
        """
        import wave

        import numpy as np
        with wave.open(str(path), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())
        if width != 2:
            raise ValueError(f"{path}: only 16-bit WAV is supported, got {8 * width}-bit")
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        if rate != SAMPLE_RATE and len(audio):
            n = int(len(audio) * SAMPLE_RATE / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n),
                              np.arange(len(audio)), audio).astype(np.float32)
        if len(audio) < int(SAMPLE_RATE * MIN_AUDIO_SECONDS):
            return ""
        return self._transcribe(audio)

    def _transcribe(self, audio) -> str:
        language = None if self.language in ("", "auto") else self.language
        segments, _ = self._load().transcribe(
            audio, language=language, beam_size=5, vad_filter=True,
            initial_prompt=HINT_PROMPT, condition_on_previous_text=False)
        return normalise(" ".join(s.text.strip() for s in segments), self._home)


def make_speech_to_text() -> SpeechToText | WhisperSpeechToText:
    """Whisper if it is installed and has a model, else Vosk.

    UAIC_STT_ENGINE=vosk or =whisper forces one. If neither is usable, the
    Whisper engine is returned so the Speak button's tooltip says how to fix it.
    """
    choice = os.environ.get(ENGINE_ENV, "").strip().lower()
    if choice == "vosk":
        return SpeechToText()
    whisper = WhisperSpeechToText()
    if choice == "whisper" or whisper.available()[0]:
        return whisper
    vosk = SpeechToText()
    return vosk if vosk.available()[0] else whisper


class TextToSpeech:
    """Speaks on its own thread, because SAPI blocks while it talks."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import pyttsx3  # noqa: F401
        except ImportError:
            return False, "speech output needs pyttsx3"
        return (self._error is None), (self._error or "ready")

    def say(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="uaic-tts", daemon=True)
            self._thread.start()
        self._queue.put(text)

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(3)

    def _run(self) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
        except Exception as exc:
            self._error = f"speech output unavailable: {exc}"
            return
        while (text := self._queue.get()) is not None:
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as exc:
                self._error = f"speech output failed: {exc}"
