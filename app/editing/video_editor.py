"""FFmpeg video editor (reusable for Shorts and long-form): xfade transitions synced to narration boundaries,
cinematic grade, burned subtitles, mixed audio, H.264/AAC MP4 in ONE encode pass (no generational loss)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.core.errors import ConfigurationError
from app.core.logger import get_logger
from app.utils import ffmpeg as ff

log = get_logger("editor")
ALLOWED_TRANSITIONS = {"fade", "dissolve", "fadeblack", "fadewhite", "smoothleft", "smoothright"}


def clip_lengths(scene_ends: list[float], total: float, T: float) -> list[float]:
    """Length each scene clip must have so that cross-fades CENTRED on narration boundaries keep audio sync.

    scene_ends[i] = narration time at which scene i ends (the last must equal ~total)."""
    n = len(scene_ends)
    bounds = [0.0] + [scene_ends[i] for i in range(n - 1)] + [total]
    lens = []
    for i in range(n):
        base = bounds[i + 1] - bounds[i]
        lens.append(base + (T / 2 if i in (0, n - 1) else T) if n > 1 else base)
    if n > 1 and n == 2:      # first and last both get T/2
        lens = [bounds[1] - bounds[0] + T / 2, bounds[2] - bounds[1] + T / 2]
    return [round(max(l, 0.5), 3) for l in lens]


def _escape_fontsdir(p: str) -> str:
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def pick_encoder(cfg) -> tuple[str, list[str]]:
    """libx264 by default; NVENC only if asked for AND present."""
    if cfg.get("video.use_gpu_encoder", False) and shutil.which("nvidia-smi"):
        try:
            enc = subprocess.run([ff.require_ffmpeg()[0], "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
            if "h264_nvenc" in enc:
                return "h264_nvenc", ["-rc", "vbr", "-cq", "23", "-b:v", "0", "-preset", "p4"]
        except Exception:
            pass
        log.warning("GPU encoder requested but h264_nvenc is unavailable — using libx264")
    return "libx264", []


class VideoEditor:
    def __init__(self, cfg, resources):
        self.cfg, self.resources = cfg, resources

    def render_settings(self) -> dict:
        s = self.resources.render_settings()
        s["fps"] = int(self.cfg.get("video.fps", 30))
        return s

    def assemble(self, clips: list[Path | str], scene_ends: list[float], transitions: list[str], audio_path: Path | str,
                 ass_path: Path | str | None, out_path: Path | str, total: float, settings: dict | None = None) -> dict:
        settings = settings or self.render_settings()
        W, H, fps = settings["width"], settings["height"], settings["fps"]
        T = float(self.cfg.get("video.transition_seconds", 0.35))
        n = len(clips)
        if n == 0:
            raise ValueError("no clips to assemble")
        lens = clip_lengths(scene_ends, total, T)
        T = max(0.05, min(T, min(lens) / 3)) if n > 1 else 0.0
        parts, inputs = [], []
        for i, c in enumerate(clips):
            inputs += ["-i", str(c)]
            parts.append(f"[{i}:v]settb=AVTB,fps={fps},setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
        label = "v0"
        cum = lens[0]
        for i in range(1, n):
            tr = transitions[i - 1] if i - 1 < len(transitions) and transitions[i - 1] in ALLOWED_TRANSITIONS else "fade"
            offset = cum - T
            parts.append(f"[{label}][v{i}]xfade=transition={tr}:duration={T:.3f}:offset={offset:.3f}[x{i}]")
            label = f"x{i}"
            cum = offset + lens[i]
        g = self.cfg.get("video.grade", {}) or {}
        chain = [f"eq=contrast={g.get('contrast', 1.04)}:saturation={g.get('saturation', 1.08)}"]
        if g.get("vignette", True):
            chain.append("vignette=PI/5")
        if g.get("grain", 0):
            chain.append(f"noise=alls={int(g['grain'])}:allf=t")
        chain.append("format=yuv420p")
        cwd = None
        if ass_path and self.cfg.get("subtitles.burn_in", True):
            ass = Path(ass_path)
            cwd = ass.parent
            fontdir = Path(self.cfg.get("thumbnail.font_path") or "").parent if self.cfg.get("thumbnail.font_path") else None
            extra = f":fontsdir='{_escape_fontsdir(str(fontdir))}'" if fontdir and fontdir.is_dir() else ""
            chain.append(f"ass={ass.name}{extra}")
        parts.append(f"[{label}]{','.join(chain)}[vout]")
        audio_idx = n
        encoder, enc_args = pick_encoder(self.cfg)
        v_args = ["-c:v", encoder]
        if encoder == "libx264":
            v_args += ["-preset", settings["x264_preset"], "-crf", str(settings["crf"])]
        else:
            v_args += enc_args
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".part.mp4")
        args = [*inputs, "-i", str(Path(audio_path).resolve()), "-filter_complex", ";".join(parts), "-map", "[vout]",
                "-map", f"{audio_idx}:a", *v_args, "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2", "-t", f"{total:.3f}", "-movflags", "+faststart", "-f", "mp4", str(tmp.resolve())]
        # clip paths must be absolute because ffmpeg may run with cwd = the .ass folder
        args = [str(Path(a).resolve()) if (i > 0 and args[i - 1] == "-i" and Path(a).exists()) else a for i, a in enumerate(args)]
        try:
            ff.run(args, timeout=1800, cwd=cwd)
            tmp.replace(out_path)
        finally:
            tmp.unlink(missing_ok=True)
        info = ff.probe(out_path)
        return {"path": str(out_path), "width": info["width"], "height": info["height"], "fps": info["fps"],
                "duration": info["duration"], "size_mb": round(info["size_bytes"] / 1e6, 2), "encoder": encoder,
                "preset": settings.get("name"), "transition_seconds": T}
