"""Speech input: the Whisper engine, engine choice, and spoken-form cleanup.

No microphone and no Qt. The engine is driven with a fake model and a fake
audio stream; one test uses the real Whisper model on synthetic speech and
is skipped when the model (or a Windows voice) is not installed.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

from uaic.desktop import voice
from uaic.desktop.spoken import normalise
from uaic.desktop.voice import (
    SAMPLE_RATE,
    SpeechToText,
    WhisperSpeechToText,
    find_whisper_model,
    make_speech_to_text,
)


@pytest.fixture
def home(tmp_path):
    for name in ("Downloads", "Documents", "Desktop", "Pictures"):
        (tmp_path / name).mkdir()
    return tmp_path


# ---------------------------------------------------------- spoken forms

@pytest.mark.parametrize(("said", "expected"), [
    ("move all the p d f files", "move all the pdf files"),
    ("Move all the P.D.F. files", "Move all the pdf files"),
    ("copy the jay peg photos", "copy the jpg photos"),
    ("every j p g and p n g file", "every jpg and png file"),
    ("files ending in dot csv", "files ending in .csv"),
    ("list the c drive", "list the C:\\"),
    ("the d o c x files", "the docx files"),
    ("Move the report.", "Move the report"),
])
def test_spoken_forms_become_written_forms(home, said, expected):
    assert normalise(said, home) == expected


def test_named_home_folders_become_real_paths(home):
    out = normalise("move the pdfs in my downloads folder into the documents folder", home)
    assert out == f"move the pdfs in {home / 'Downloads'} into {home / 'Documents'}"
    assert normalise("list my desktop", home) == f"list {home / 'Desktop'}"
    assert normalise("pictures folder", home) == str(home / "Pictures")


@pytest.mark.parametrize("said", [
    "move all documents to the archive",           # a noun, not a folder
    "rename the downloads photos folder",          # "the downloads" + another name
    "what version of python is installed",
    "",
])
def test_ordinary_words_are_left_alone(home, said):
    assert normalise(said, home) == said


def test_a_folder_that_does_not_exist_is_not_invented(tmp_path):
    assert normalise("open my music folder", tmp_path) == "open my music folder"


def test_whitespace_is_tidied(home):
    assert normalise("  move   the\nfile  ", home) == "move the file"


# ---------------------------------------------------------- engine choice

def make_model_dir(root: Path, size: str) -> Path:
    d = root / f"whisper-{size}"
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"")
    (d / "config.json").write_text("{}", encoding="utf-8")
    return d


def test_the_small_model_is_preferred(tmp_path, monkeypatch):
    monkeypatch.delenv(voice.WHISPER_ENV, raising=False)
    monkeypatch.setattr(voice, "model_search_path", lambda: [tmp_path])
    make_model_dir(tmp_path, "tiny")
    small = make_model_dir(tmp_path, "small")
    assert find_whisper_model() == small


def test_an_explicit_model_folder_wins(tmp_path, monkeypatch):
    chosen = make_model_dir(tmp_path / "x", "base")
    monkeypatch.setattr(voice, "model_search_path", lambda: [])
    monkeypatch.setenv(voice.WHISPER_ENV, str(chosen))
    assert find_whisper_model() == chosen


def test_a_folder_without_model_bin_is_not_a_model(tmp_path, monkeypatch):
    monkeypatch.delenv(voice.WHISPER_ENV, raising=False)
    monkeypatch.setattr(voice, "model_search_path", lambda: [tmp_path])
    (tmp_path / "whisper-small").mkdir()
    assert find_whisper_model() is None


def test_without_a_model_the_reason_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.delenv(voice.WHISPER_ENV, raising=False)
    monkeypatch.setattr(voice, "model_search_path", lambda: [tmp_path])
    ok, why = WhisperSpeechToText().available()
    assert not ok
    assert "get_whisper_model.py" in why or "missing" in why


@pytest.mark.parametrize(("env", "engine"), [("vosk", "vosk"), ("whisper", "whisper")])
def test_the_engine_can_be_forced(monkeypatch, env, engine):
    monkeypatch.setenv(voice.ENGINE_ENV, env)
    assert make_speech_to_text().engine == engine


def test_whisper_is_chosen_when_usable_else_vosk(monkeypatch):
    monkeypatch.delenv(voice.ENGINE_ENV, raising=False)
    monkeypatch.setattr(WhisperSpeechToText, "available", lambda self: (True, "ready"))
    assert make_speech_to_text().engine == "whisper"
    monkeypatch.setattr(WhisperSpeechToText, "available", lambda self: (False, "no"))
    monkeypatch.setattr(SpeechToText, "available", lambda self: (True, "ready"))
    assert make_speech_to_text().engine == "vosk"
    monkeypatch.setattr(SpeechToText, "available", lambda self: (False, "no"))
    assert make_speech_to_text().engine == "whisper"   # its tooltip explains the fix


# ---------------------------------------------------- the engine, faked

class Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    def __init__(self, *texts: str, fail: bool = False) -> None:
        self.texts, self.fail, self.calls = texts, fail, []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        if self.fail:
            raise RuntimeError("decoder broke")
        return iter(Segment(t) for t in self.texts), None


class FakeStream:
    def __init__(self) -> None:
        self.stopped = self.closed = False

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def engine_with(model: FakeModel, home: Path, **kw) -> WhisperSpeechToText:
    stt = WhisperSpeechToText(home=home, **kw)
    stt._model = model
    return stt


def recording(stt: WhisperSpeechToText, seconds: float = 1.0) -> FakeStream:
    stream = FakeStream()
    stt._stream = stream
    stt._chunks = [b"\x01\x00" * int(SAMPLE_RATE * seconds)]
    return stream


def test_stop_closes_the_mic_and_returns_cleaned_text(home):
    model = FakeModel(" Move all the P D F files", " in my downloads folder.")
    stt = engine_with(model, home)
    stream = recording(stt)

    text = stt.stop()

    assert stream.stopped and stream.closed and not stt.listening
    assert text == f"Move all the pdf files in {home / 'Downloads'}"
    _, kw = model.calls[0]
    assert kw["language"] == "en" and kw["vad_filter"] is True
    assert "Downloads" in kw["initial_prompt"]


def test_a_click_without_speech_is_not_sent_to_the_model(home):
    model = FakeModel("hallucinated text")
    stt = engine_with(model, home)
    recording(stt, seconds=0.1)
    assert stt.stop() == ""
    assert model.calls == []


def test_auto_language_lets_whisper_detect_it(home):
    model = FakeModel("namaste")
    stt = engine_with(model, home, language="auto")
    recording(stt)
    stt.stop()
    assert model.calls[0][1]["language"] is None


def test_language_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(voice.LANGUAGE_ENV, "HI")
    assert WhisperSpeechToText().language == "hi"


def test_stop_in_background_closes_the_mic_at_once_and_delivers_later(home):
    gate = threading.Event()

    class SlowModel(FakeModel):
        def transcribe(self, audio, **kw):
            gate.wait(5)
            return super().transcribe(audio, **kw)

    stt = engine_with(SlowModel("copy the report"), home)
    stream = recording(stt)
    got: list[tuple[str, str | None]] = []
    done = threading.Event()

    stt.stop_in_background(lambda text, error: (got.append((text, error)), done.set()))

    assert stream.closed and not stt.listening      # the mic is off before any decoding
    assert got == []
    gate.set()
    assert done.wait(5)
    assert got == [("copy the report", None)]


def test_a_transcription_error_is_delivered_not_raised(home):
    stt = engine_with(FakeModel(fail=True), home)
    recording(stt)
    got = []
    done = threading.Event()
    stt.stop_in_background(lambda text, error: (got.append((text, error)), done.set()))
    assert done.wait(5)
    assert got[0][0] == "" and "decoder broke" in got[0][1]


def test_discard_throws_the_recording_away(home):
    model = FakeModel("secret")
    stt = engine_with(model, home)
    stream = recording(stt)
    stt.discard()
    assert stream.closed and stt._chunks == [] and model.calls == []


def test_the_model_is_loaded_from_a_folder_offline(tmp_path, monkeypatch):
    faster_whisper = pytest.importorskip("faster_whisper")
    model_dir = make_model_dir(tmp_path, "small")
    seen = {}

    class FakeWhisperModel:
        def __init__(self, path, **kw):
            seen["path"], seen["kw"] = path, kw

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    WhisperSpeechToText(model_dir=model_dir)._load()

    assert seen["path"] == str(model_dir)               # a folder, never a model name
    assert seen["kw"]["device"] == "cpu"
    assert os.environ["HF_HUB_OFFLINE"] == "1"


# ------------------------------------------------- the real model, if present

def synthesize(text: str) -> Path:
    pyttsx3 = pytest.importorskip("pyttsx3")
    path = Path(tempfile.gettempdir()) / "uaic_test_speech.wav"
    engine = pyttsx3.init()
    engine.save_to_file(text, str(path))
    engine.runAndWait()
    engine.stop()
    if not path.is_file() or path.stat().st_size < 1000:
        pytest.skip("no Windows voice could synthesize speech")
    return path


def test_the_real_model_understands_a_request(home):
    stt = WhisperSpeechToText(home=home)
    ok, why = stt.available()
    if not ok:
        pytest.skip(why)
    wav = synthesize("move all the pdf files in my downloads folder into my documents folder")

    text = stt.transcribe_wav(wav).lower()

    assert "pdf" in text
    assert str(home / "Downloads").lower() in text
    assert str(home / "Documents").lower() in text
