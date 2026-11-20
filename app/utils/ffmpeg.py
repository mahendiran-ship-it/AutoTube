"""FFmpeg / FFprobe helpers. Never uses a shell: every command is an argv list."""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.errors import ConfigurationError, NonRetryableError
from app.core.logger import redact


class FFmpegError(NonRetryableError):
    pass


def find_binary(name: str) -> str | None:
    env = os.environ.get(f"{name.upper()}_PATH")
    if env and Path(env).exists():
        return env
    return shutil.which(name)


def require_ffmpeg() -> tuple[str, str]:
    ff, fp = find_binary("ffmpeg"), find_binary("ffprobe")
    if not ff or not fp:
        raise ConfigurationError(
            "FFmpeg/ffprobe not found. Install FFmpeg and make sure it is on PATH "
            "(Windows: `winget install Gyan.FFmpeg`; Ubuntu: `sudo apt install ffmpeg`).")
    return ff, fp


def run(args: list[str], timeout: float = 600, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    ff, _ = require_ffmpeg()
    cmd = [ff, "-hide_banner", "-nostdin", "-y", *map(str, args)]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s") from e
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(f"ffmpeg failed (code {cp.returncode}): {redact(tail)}")
    return cp


def run_capture_stderr(args: list[str], timeout: float = 300) -> str:
    """Run an analysis command (null output) and return stderr (where filters report)."""
    ff, _ = require_ffmpeg()
    cp = subprocess.run([ff, "-hide_banner", "-nostdin", *map(str, args)], capture_output=True, text=True, timeout=timeout)
    return cp.stderr or ""


def probe(path: str | Path) -> dict:
    _, fp = require_ffmpeg()
    p = Path(path)
    if not p.is_file():
        raise FFmpegError(f"file not found: {p}")
    cp = subprocess.run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise FFmpegError(f"ffprobe failed (corrupted/unsupported?): {redact((cp.stderr or '').strip()[-300:])}")
    data = json.loads(cp.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    dur = float(fmt.get("duration") or (v or a or {}).get("duration") or 0)
    fps = 0.0
    if v and v.get("avg_frame_rate") and "/" in v["avg_frame_rate"]:
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    return {
        "duration": dur, "size_bytes": int(fmt.get("size") or p.stat().st_size),
        "format": fmt.get("format_name"),
        "has_video": v is not None, "has_audio": a is not None,
        "width": int(v["width"]) if v else None, "height": int(v["height"]) if v else None,
        "fps": fps, "video_codec": v.get("codec_name") if v else None,
        "audio_codec": a.get("codec_name") if a else None,
        "pix_fmt": v.get("pix_fmt") if v else None,
        "sample_rate": int(a["sample_rate"]) if a and a.get("sample_rate") else None,
    }


def duration_of(path: str | Path) -> float:
    return float(probe(path)["duration"])


@functools.lru_cache(maxsize=64)
def _filter_help(name: str) -> str:
    ff, _ = require_ffmpeg()
    cp = subprocess.run([ff, "-hide_banner", "-h", f"filter={name}"], capture_output=True, text=True)
    return (cp.stdout or "") + (cp.stderr or "")


def filter_exists(name: str) -> bool:
    return "Unknown filter" not in _filter_help(name) and bool(_filter_help(name).strip())


def filter_has_option(name: str, option: str) -> bool:
    return re.search(rf"\b{re.escape(option)}\b", _filter_help(name)) is not None


def volumedetect(path: str | Path) -> dict:
    err = run_capture_stderr(["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
    mx = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)

    def f(m):
        if not m:
            return None
        return float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    return {"mean_db": f(mean), "max_db": f(mx)}


def silencedetect(path: str | Path, noise_db: float = -45, min_duration: float = 1.0) -> list[dict]:
    err = run_capture_stderr(["-i", str(path), "-vn", "-af",
                              f"silencedetect=noise={noise_db}dB:d={min_duration}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = re.findall(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", err)
    out = []
    for i, s in enumerate(starts):
        if i < len(ends):
            out.append({"start": s, "end": float(ends[i][0]), "duration": float(ends[i][1])})
        else:  # silence runs to the end of file
            out.append({"start": s, "end": None, "duration": None})
    return out


def blackdetect(path: str | Path, min_duration: float = 0.4, pic_th: float = 0.98) -> list[dict]:
    err = run_capture_stderr(["-i", str(path), "-an", "-vf",
                              f"blackdetect=d={min_duration}:pic_th={pic_th}", "-f", "null", "-"])
    out = []
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", err):
        out.append({"start": float(m.group(1)), "end": float(m.group(2)), "duration": float(m.group(3))})
    return out


def extract_frame(video: str | Path, t: float, out_path: str | Path) -> Path:
    run(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out_path)])
    return Path(out_path)


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class RawVideoWriter:
    """Pipe raw RGB frames (numpy uint8, HxWx3) into ffmpeg → H.264 mp4 (no audio)."""

    def __init__(self, out_path: str | Path, width: int, height: int, fps: int, crf: int = 18,
                 preset: str = "veryfast"):
        ff, _ = require_ffmpeg()
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.width, self.height = width, height
        self._err = tempfile.TemporaryFile()
        cmd = [ff, "-hide_banner", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an",
               "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(self.out_path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._err)
        self.frames = 0

    def write(self, frame) -> None:
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            raise FFmpegError(f"frame size {frame.shape[1]}x{frame.shape[0]} != {self.width}x{self.height}")
        try:
            self.proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as e:
            raise FFmpegError(f"ffmpeg pipe closed early: {self._stderr_tail()}") from e
        self.frames += 1

    def _stderr_tail(self) -> str:
        self._err.seek(0)
        return redact("\n".join(self._err.read().decode("utf-8", "replace").strip().splitlines()[-8:]))

    def close(self) -> Path:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        rc = self.proc.wait(timeout=300)
        if rc != 0:
            tail = self._stderr_tail()
            self._err.close()
            raise FFmpegError(f"ffmpeg encode failed (code {rc}): {tail}")
        self._err.close()
        return self.out_path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            try:
                self.proc.kill()
            except OSError:
                pass
            self._err.close()
            return False
        self.close()
        return False
