"""Narration engine: sentence-level TTS through adapters (Piper default), pronunciation fixes, pauses,
speed/pitch via FFmpeg, validation, and exact per-sentence timing (used for subtitle alignment).

Adapters: piper (local, free), windows_sapi (Windows built-in), espeak (espeak-ng), test_tone
(placeholder tone for smoke tests ONLY — videos using it can never pass QC / be uploaded).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from app.core.errors import ConfigurationError, NonRetryableError, ProviderUnavailable
from app.core.logger import get_logger
from app.utils import ffmpeg as ff
from app.utils.text import split_sentences

log = get_logger("voice")
SR = 24000       # internal narration sample rate (mono, 16-bit)


def apply_pronunciations(text: str, table: dict[str, str]) -> str:
    """Replace words for the TTS engine only (captions keep the original spelling)."""
    for word, say in sorted((table or {}).items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![\w])" + re.escape(word) + r"(?![\w])", say, text)
    return text


class TTSAdapter(ABC):
    name = "tts"
    placeholder = False

    @abstractmethod
    def available(self) -> tuple[bool, str]: ...

    @abstractmethod
    def synth(self, text: str, out_wav: Path) -> None:
        """Write a WAV file or raise ProviderUnavailable."""


def _run(cmd: list[str], input_text: str | None = None, timeout: float = 180) -> None:
    try:
        cp = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProviderUnavailable(f"{Path(cmd[0]).name}: {e}") from e
    if cp.returncode != 0:
        raise ProviderUnavailable(f"{Path(cmd[0]).name} failed: {(cp.stderr or '').strip()[-300:]}")


class PiperTTS(TTSAdapter):
    name = "piper"

    def __init__(self, cfg):
        self.cfg = cfg
        self.model = self._resolve(cfg.get("voice.piper.model", ""))
        self.exe = cfg.get("voice.piper.executable", "piper")

    def _resolve(self, p: str) -> Path:
        path = Path(p) if p else Path("")
        return path if path.is_absolute() else self.cfg.root / path

    def _command(self) -> list[str] | None:
        exe = shutil.which(self.exe) or (self.exe if Path(self.exe).is_file() else None)
        if exe:
            return [exe]
        try:   # the `piper-tts` pip package exposes `python -m piper`
            import piper  # noqa: F401
            return [sys.executable, "-m", "piper"]
        except ImportError:
            return None

    def available(self):
        if not self._command():
            return False, "Piper not installed (pip install piper-tts) — see README 'Voice setup'"
        if not self.model.is_file():
            return False, f"Piper voice model missing: {self.model}. Run: python main.py setup-voice"
        return True, "ok"

    def synth(self, text, out_wav):
        ok, why = self.available()
        if not ok:
            raise ProviderUnavailable(why)
        _run([*self._command(), "-m", str(self.model), "-f", str(out_wav)], input_text=text)
        if not out_wav.is_file():
            raise ProviderUnavailable("Piper produced no output")


class WindowsSapiTTS(TTSAdapter):
    """Uses System.Speech (built into Windows). Text is passed through a file — never interpolated into code."""
    name = "windows_sapi"
    _PS = ("param($Out,$TextFile) Add-Type -AssemblyName System.Speech; "
           "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SetOutputToWaveFile($Out); "
           "$s.Speak([IO.File]::ReadAllText($TextFile)); $s.Dispose()")

    def available(self):
        if os.name != "nt":
            return False, "Windows only"
        return (True, "ok") if shutil.which("powershell") else (False, "powershell not found")

    def synth(self, text, out_wav):
        with tempfile.TemporaryDirectory() as d:
            tf, ps = Path(d) / "t.txt", Path(d) / "s.ps1"
            tf.write_text(text, encoding="utf-8")
            ps.write_text(self._PS, encoding="utf-8")
            _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps), "-Out", str(out_wav),
                  "-TextFile", str(tf)])


class EspeakTTS(TTSAdapter):
    name = "espeak"

    def __init__(self):
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self):
        return (True, "ok") if self.exe else (False, "espeak-ng not installed (robotic voice; emergency fallback only)")

    def synth(self, text, out_wav):
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "t.txt"
            tf.write_text(text, encoding="utf-8")
            _run([self.exe, "-v", "en-gb", "-s", "150", "-w", str(out_wav), "-f", str(tf)])


class TestToneTTS(TTSAdapter):
    """NOT SPEECH. A syllable-rhythm tone burst, so the whole pipeline can be smoke-tested without a voice model."""
    name = "test_tone"
    placeholder = True

    def available(self):
        return True, "placeholder (not speech)"

    def synth(self, text, out_wav):
        words = max(1, len(text.split()))
        dur = words * 0.38
        t = np.linspace(0, dur, int(SR * dur), endpoint=False)
        env = (0.5 + 0.5 * np.sin(2 * np.pi * (words / dur) * 1.6 * t)) ** 1.5
        sig = (np.sin(2 * np.pi * 118 * t) + 0.4 * np.sin(2 * np.pi * 236 * t)) * env * 0.35
        write_wav(out_wav, sig.astype(np.float32), SR)


ADAPTERS = {"piper": lambda cfg: PiperTTS(cfg), "windows_sapi": lambda cfg: WindowsSapiTTS(),
            "espeak": lambda cfg: EspeakTTS(), "test_tone": lambda cfg: TestToneTTS()}


# ---------------- wav helpers ----------------
def write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SR:
            raise ValueError("unexpected wav format")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def trim_silence(x: np.ndarray, thresh: float = 0.004, keep: float = 0.04) -> np.ndarray:
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) == 0:
        return x
    k = int(SR * keep)
    return x[max(0, idx[0] - k): min(len(x), idx[-1] + k)]


class VoiceGenerator:
    def __init__(self, cfg, repo=None, adapters: list[TTSAdapter] | None = None):
        self.cfg, self.repo = cfg, repo
        self._adapters = adapters

    def adapters(self) -> list[TTSAdapter]:
        if self._adapters is not None:
            return self._adapters
        out = []
        for name in self.cfg.get("voice.provider_order") or []:
            if name in ADAPTERS:
                out.append(ADAPTERS[name](self.cfg))
            else:
                log.warning("unknown voice provider '%s' ignored", name)
        return out

    def status(self) -> list[dict]:
        return [{"name": a.name, "available": a.available()[0], "detail": a.available()[1], "placeholder": a.placeholder}
                for a in self.adapters()]

    def pick(self) -> TTSAdapter:
        notes = []
        for a in self.adapters():
            ok, why = a.available()
            if ok:
                return a
            notes.append(f"{a.name}: {why}")
        raise ConfigurationError("No narration voice available. " + " | ".join(notes) +
                                 "  → Install Piper and download a voice (README: 'Voice setup').")

    # ---- processing ---------------------------------------------------
    def _post(self, raw_wav: Path, out_wav: Path) -> None:
        """Normalise to mono/SR, apply speed + pitch (FFmpeg)."""
        speed = float(self.cfg.get("voice.speed", 1.0))
        semis = float(self.cfg.get("voice.pitch_semitones", 0.0))
        filters = []
        if semis:
            f = 2 ** (semis / 12)
            filters += [f"asetrate={int(SR * f)}", f"aresample={SR}", f"atempo={1 / f:.5f}"]
        if abs(speed - 1.0) > 1e-3:
            filters.append(f"atempo={max(0.5, min(2.0, speed)):.4f}")
        cmd = ["-i", str(raw_wav), "-ac", "1", "-ar", str(SR)]
        if filters:
            cmd += ["-af", ",".join(filters)]
        ff.run([*cmd, "-c:a", "pcm_s16le", str(out_wav)])

    def _speak(self, adapter: TTSAdapter, text: str, workdir: Path, key: str) -> np.ndarray:
        raw, proc = workdir / f"{key}_raw.wav", workdir / f"{key}.wav"
        adapter.synth(apply_pronunciations(text, self.cfg.get("voice.pronunciations") or {}), raw)
        self._post(raw, proc)
        audio = trim_silence(read_wav(proc))
        if len(audio) < SR * 0.15:
            raise ProviderUnavailable(f"TTS output for '{text[:30]}…' is empty or too short")
        return audio

    def synthesize(self, scenes: list[dict], out_dir: Path, extra_pron: list[dict] | None = None) -> dict:
        """scenes: [{idx, narration}] → narration.wav + timings. Returns a serialisable result dict."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        adapter = self.pick()
        pron = dict(self.cfg.get("voice.pronunciations") or {})
        for p in extra_pron or []:
            pron.setdefault(p["word"], p["say_as"])
        saved = self.cfg.get("voice.pronunciations")
        self.cfg.set("voice.pronunciations", pron)
        try:
            lead = float(self.cfg.get("video.intro_lead_seconds", 0.15))
            s_pause = float(self.cfg.get("video.sentence_pause_seconds", 0.22))
            sc_pause = float(self.cfg.get("video.scene_pause_seconds", 0.30))
            tail = float(self.cfg.get("video.tail_seconds", 0.6))
            chunks: list[np.ndarray] = [np.zeros(int(SR * lead), dtype=np.float32)]
            cursor = lead
            result_scenes, n = [], 0
            with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
                tmpd = Path(tmp)
                for si, sc in enumerate(scenes):
                    scene_start, sents = cursor, []
                    for sj, sent in enumerate(split_sentences(sc["narration"])):
                        audio = self._speak(adapter, sent, tmpd, f"s{n}")
                        n += 1
                        dur = len(audio) / SR
                        chunks.append(audio)
                        sents.append({"text": sent, "start": round(cursor, 3), "end": round(cursor + dur, 3)})
                        cursor += dur
                        last = sj == len(split_sentences(sc["narration"])) - 1
                        gap = sc_pause if last else s_pause
                        chunks.append(np.zeros(int(SR * gap), dtype=np.float32))
                        cursor += gap
                    result_scenes.append({"idx": sc["idx"], "start": round(scene_start, 3), "end": round(cursor, 3),
                                          "audio_duration": round(cursor - scene_start, 3), "sentences": sents})
            chunks.append(np.zeros(int(SR * tail), dtype=np.float32))
            cursor += tail
            full = np.concatenate(chunks)
            peak = float(np.max(np.abs(full))) or 1.0
            if peak > 0.89:
                full = full * (0.89 / peak)            # never clip
            path = out_dir / "narration.wav"
            write_wav(path, full)
        finally:
            self.cfg.set("voice.pronunciations", saved if saved is not None else {})
        res = {"path": str(path), "duration": round(len(full) / SR, 3), "provider": adapter.name,
               "placeholder": adapter.placeholder, "scenes": result_scenes, "sample_rate": SR}
        res["validation"] = self.validate(path, res["duration"])
        if not res["validation"]["ok"]:
            raise ProviderUnavailable("narration failed validation: " + "; ".join(res["validation"]["problems"]))
        return res

    def validate(self, path: Path, expected: float | None = None) -> dict:
        problems = []
        try:
            info = ff.probe(path)
            vol = ff.volumedetect(path)
        except Exception as e:
            return {"ok": False, "problems": [f"unreadable audio: {e}"]}
        if not info["has_audio"] or info["duration"] < 1:
            problems.append("no/too short audio")
        floor = float(self.cfg.get("voice.min_audible_db", -38))
        if vol["mean_db"] is None or vol["mean_db"] < floor:
            problems.append(f"narration too quiet (mean {vol['mean_db']} dB < {floor})")
        if vol["max_db"] is not None and vol["max_db"] > -0.05:
            problems.append("clipping detected")
        if expected and abs(info["duration"] - expected) > 0.5:
            problems.append(f"duration mismatch ({info['duration']:.2f}s vs {expected:.2f}s)")
        return {"ok": not problems, "problems": problems, "mean_db": vol["mean_db"], "max_db": vol["max_db"],
                "duration": info["duration"]}


def download_piper_voice(cfg, voice: str = "en_GB-alan-medium") -> Path:
    """Fetch a Piper voice with the official `piper.download_voices` helper (needs internet + `pip install piper-tts`)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", voice):
        raise ConfigurationError("invalid voice name")
    dest = cfg.root / "data" / "voices"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        cp = subprocess.run([sys.executable, "-m", "piper.download_voices", voice, "--download-dir", str(dest)],
                            capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise NonRetryableError("voice download timed out") from e
    model = dest / f"{voice}.onnx"
    if cp.returncode != 0 or not model.is_file():
        raise NonRetryableError(
            "Could not download the voice automatically: " + (cp.stderr or cp.stdout or "").strip()[-300:] +
            f"\nManual download: get {voice}.onnx and {voice}.onnx.json from the 'rhasspy/piper-voices' repository on "
            f"Hugging Face and put them in {dest}")
    return model
