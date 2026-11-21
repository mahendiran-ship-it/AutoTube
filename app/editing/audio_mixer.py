"""Sound engine: generated SFX + ambient bed, optional licensed music, ducking, loudness, limiting.

* SFX/ambient are synthesised here with numpy (original, no third-party rights).
* Music is used ONLY from assets/music/library.yaml entries that declare a license and whose file exists.
* No music available? The mix still works (narration + ambient + sfx).
"""
from __future__ import annotations

import random
import wave
from pathlib import Path

import numpy as np
import yaml

from app.core.logger import get_logger
from app.utils import ffmpeg as ff

log = get_logger("audio")
SR = 48000
GEN_LICENSE = {"provider": "autotube-generated", "license_name": "Original generated audio (no third-party rights)",
               "license_url": "", "author": "AutoTube AI", "attribution_required": 0}


def write_stereo(path: Path, x: np.ndarray) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


def _t(d: float) -> np.ndarray:
    return np.linspace(0, d, int(SR * d), endpoint=False, dtype=np.float32)


def _lowpass(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    kern = np.ones(k, np.float32) / k
    return np.convolve(x, kern, mode="same")


def synth_sfx(name: str, seed: int = 0) -> np.ndarray:
    return np.nan_to_num(_synth_sfx(name, seed), nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _synth_sfx(name: str, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    if name == "low_boom":
        t = _t(1.8)
        f = 70 * np.exp(-t * 1.6) + 28
        x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.1)
        return (x + 0.12 * _lowpass(rng.normal(0, 1, len(t)).astype(np.float32), 60) * np.exp(-t * 6)) * 0.9
    if name == "whoosh":
        t = _t(1.1)
        n = rng.normal(0, 1, len(t)).astype(np.float32)
        env = np.clip(np.sin(np.pi * t / t[-1]), 0, None) ** 2
        sweep = _lowpass(n, 40) * (0.5 + 0.5 * np.sin(2 * np.pi * (1.5 + 3 * t / t[-1]) * t))
        return sweep * env * 1.6
    if name == "shimmer":
        t = _t(2.2)
        x = sum(np.sin(2 * np.pi * f * t + p) * a for f, a, p in
                ((1320, 0.5, 0.0), (1760, 0.4, 1.3), (2217, 0.3, 2.1), (2960, 0.2, 0.7)))
        env = np.clip(np.sin(np.pi * t / t[-1]), 0, None) ** 2.5
        trem = 0.6 + 0.4 * np.sin(2 * np.pi * 5.5 * t)
        return (x * env * trem * 0.18).astype(np.float32)
    if name == "riser":
        t = _t(2.4)
        n = _lowpass(rng.normal(0, 1, len(t)).astype(np.float32), 12)
        f = 90 + 700 * (t / t[-1]) ** 2.2
        tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
        env = (t / t[-1]) ** 1.8
        return (0.5 * tone + 0.35 * n) * env * 0.7
    if name == "heartbeat":
        t = _t(1.4)
        x = np.zeros_like(t)
        for start, amp in ((0.0, 1.0), (0.28, 0.7)):
            m = t >= start
            tt = t[m] - start
            x[m] += np.sin(2 * np.pi * 52 * tt) * np.exp(-tt * 14) * amp
        return x * 0.95
    return np.zeros(int(SR * 0.1), np.float32)


def synth_ambient(duration: float, seed: int = 0) -> np.ndarray:
    """Slowly evolving space drone: detuned low sines + filtered noise, fade in/out."""
    rng = np.random.default_rng(seed)
    t = _t(duration)
    drone = sum(a * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28) + 0.4 * np.sin(2 * np.pi * lf * t))
                for f, a, lf in ((55, 0.5, 0.05), (55.4, 0.4, 0.07), (82.5, 0.25, 0.04), (110.7, 0.15, 0.09)))
    noise = _lowpass(rng.normal(0, 1, len(t)).astype(np.float32), 90) * 6
    x = (drone + 0.5 * noise) * (0.75 + 0.25 * np.sin(2 * np.pi * 0.08 * t))
    fade = np.minimum(1, np.minimum(t / 1.5, (duration - t) / 2.0))
    return np.nan_to_num(x * fade * 0.5).astype(np.float32)


class MusicLibrary:
    def __init__(self, cfg, repo=None):
        self.cfg, self.repo = cfg, repo
        self.dir = cfg.path("assets_dir", "music", create=False)

    def tracks(self) -> list[dict]:
        lib = self.dir / "library.yaml"
        if not lib.is_file():
            return []
        try:
            data = yaml.safe_load(lib.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.warning("music library.yaml invalid: %s", e)
            return []
        out = []
        for t in data.get("tracks", []) or []:
            f = self.dir / str(t.get("file", ""))
            if not t.get("license"):
                log.warning("music track %s ignored: no license declared", t.get("file"))
                continue
            if not f.is_file() or f.resolve().parent != self.dir.resolve():
                continue
            out.append({**t, "path": str(f)})
        return out

    def pick(self, seed: int = 0) -> dict | None:
        tr = self.tracks()
        if not tr:
            return None
        usage = self.repo.music_usage() if self.repo else {}
        tr.sort(key=lambda t: usage.get(t["path"], 0))
        least = [t for t in tr if usage.get(t["path"], 0) == usage.get(tr[0]["path"], 0)]
        return random.Random(seed).choice(least)


class AudioMixer:
    def __init__(self, cfg, repo=None):
        self.cfg, self.repo = cfg, repo
        self.library = MusicLibrary(cfg, repo)

    def build_fx(self, total: float, scene_timings: list[dict], scenes: list[dict], out_path: Path, seed: int = 0) -> list[dict]:
        """Write fx.wav (ambient + sfx) and return the list of placed effects."""
        n = int(SR * total)
        buf = np.zeros(n, np.float32)
        placed = []
        if self.cfg.get("audio.ambient.enabled", True):
            amb = synth_ambient(total, seed) * float(self.cfg.get("audio.ambient.volume", 0.12)) * 2.0
            buf += amb[:n]
            placed.append({"kind": "ambient", "at": 0.0})
        if self.cfg.get("audio.sfx.enabled", True):
            vol = float(self.cfg.get("audio.sfx.volume", 0.35))
            for sc, tm in zip(scenes, scene_timings):
                name = sc.get("sfx") or "none"
                if name == "none":
                    continue
                s = synth_sfx(name, seed + sc["idx"]) * vol
                at = max(0.0, tm["start"] - 0.12)
                i0 = int(at * SR)
                seg = s[: max(0, n - i0)]
                buf[i0:i0 + len(seg)] += seg
                placed.append({"kind": "sfx", "name": name, "at": round(at, 2), "scene": sc["idx"]})
        write_stereo(out_path, np.clip(buf, -1, 1))
        return placed

    def mix(self, narration_path: Path, total: float, scene_timings: list[dict], scenes: list[dict], out_dir: Path,
            seed: int = 0) -> dict:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fx_path, mix_path = out_dir / "fx.wav", out_dir / "mix.wav"
        placed = self.build_fx(total, scene_timings, scenes, fx_path, seed)
        music = self.library.pick(seed) if self.cfg.get("audio.music.enabled", True) else None
        duck = self.cfg.get("audio.ducking", {}) or {}
        mv = float(self.cfg.get("audio.music.volume", 0.22))
        fi, fo = float(self.cfg.get("audio.music.fade_in", 1.0)), float(self.cfg.get("audio.music.fade_out", 2.0))
        lufs, tp = float(self.cfg.get("audio.target_lufs", -14)), float(self.cfg.get("audio.true_peak_db", -1.5))
        T = f"{total:.3f}"
        parts = [f"[0:a]aresample={SR},aformat=channel_layouts=stereo,apad,atrim=0:{T},asetpts=PTS-STARTPTS,asplit=2[nar][sc]",
                 f"[1:a]aresample={SR},aformat=channel_layouts=stereo,atrim=0:{T}[fx]"]
        inputs = ["-i", str(narration_path), "-i", str(fx_path)]
        mix_in, n_in = "[nar][fx]", 2
        if music:
            inputs += ["-stream_loop", "-1", "-i", music["path"]]
            parts.append(f"[2:a]aresample={SR},aformat=channel_layouts=stereo,atrim=0:{T},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d={fi},afade=t=out:st={max(0, total - fo):.2f}:d={fo},volume={mv}[m0]")
            parts.append(f"[m0][sc]sidechaincompress=threshold={duck.get('threshold', 0.04)}:ratio={duck.get('ratio', 8)}:"
                         f"attack={duck.get('attack_ms', 20)}:release={duck.get('release_ms', 450)}[md]")
            mix_in, n_in = "[nar][md][fx]", 3
        else:
            parts.append("[sc]anullsink")
        norm = "normalize=0:" if ff.filter_has_option("amix", "normalize") else ""
        tail = (f"amix=inputs={n_in}:{norm}duration=first:dropout_transition=0,loudnorm=I={lufs}:TP={tp}:LRA=11,"
                f"aresample={SR},alimiter=limit=0.89:level=disabled")
        if not norm:
            tail = f"amix=inputs={n_in}:duration=first:dropout_transition=0,volume={n_in},loudnorm=I={lufs}:TP={tp}:LRA=11,aresample={SR},alimiter=limit=0.89:level=disabled"
        parts.append(f"{mix_in}{tail}[out]")
        ff.run([*inputs, "-filter_complex", ";".join(parts), "-map", "[out]", "-t", T, "-ar", str(SR), "-ac", "2",
                "-c:a", "pcm_s16le", str(mix_path)])
        vol = ff.volumedetect(mix_path)
        credits = []
        if music and music.get("attribution_required"):
            credits.append(music.get("attribution_text") or f"Music: {music.get('title')} by {music.get('author')} ({music['license']})")
        return {"path": str(mix_path), "fx_path": str(fx_path), "effects": placed, "music": music,
                "credits": credits, "mean_db": vol["mean_db"], "max_db": vol["max_db"], "duration": ff.duration_of(mix_path)}

    def record_assets(self, video_id: int, result: dict) -> None:
        if not self.repo:
            return
        self.repo.add_asset(video_id=video_id, kind="audio", method="ambient", provider="generated",
                            path=result["fx_path"], license_id=self.repo.add_license(**GEN_LICENSE, notes="ambient + sfx"),
                            meta_json={"effects": result["effects"]})
        m = result.get("music")
        if m:
            lic = self.repo.add_license(provider="music_library", license_name=m["license"], license_url=m.get("license_url", ""),
                                        author=m.get("author", ""), attribution_text=m.get("attribution_text", ""),
                                        attribution_required=1 if m.get("attribution_required") else 0,
                                        source_url=m.get("source_url", ""))
            self.repo.add_asset(video_id=video_id, kind="audio", method="music", provider="library", path=m["path"],
                                source_url=m.get("source_url"), license_id=lic, meta_json={"title": m.get("title")})
