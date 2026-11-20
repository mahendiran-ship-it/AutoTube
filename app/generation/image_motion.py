"""Image-to-video motion: sub-pixel Ken Burns zoom/pan + drifting dust particles + vignette (Provider E).

Pure Pillow/numpy → FFmpeg. Works for any still (stock photo, AI image, NASA image).
Depth-based parallax is NOT implemented (needs a depth model); the dust layer gives a cheap sense of depth.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.generation.procedural_visuals import Camera, to_u8, vignette_mask
from app.utils.ffmpeg import RawVideoWriter

MAX_SIDE = 3000


def prepare_world(path: Path | str, w: int, h: int) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    sx = max(w * 1.3 / img.width, h * 1.3 / img.height)       # cover the viewport with 30% headroom for motion
    sx = min(sx, MAX_SIDE / max(img.width, img.height) if max(img.width, img.height) * sx > MAX_SIDE else sx)
    sx = max(sx, max(w / img.width, h / img.height))           # never smaller than the viewport
    return img.resize((max(w, int(img.width * sx)), max(h, int(img.height * sx))), Image.LANCZOS)


def image_to_clip(image_path: Path | str, out_path: Path | str, duration: float, width: int, height: int, fps: int = 30,
                  seed: int = 0, camera: str = "", crf: int = 20, preset: str = "veryfast", particles: int = 70) -> dict:
    world = prepare_world(image_path, width, height)
    rng = np.random.default_rng(seed)
    cam = Camera(camera or ("slow push-in" if seed % 2 == 0 else "slow pan right"), width, height, world.width, world.height)
    n = max(2, int(round(duration * fps)))
    vig = vignette_mask(height, width, 0.38)
    px, py = rng.random(particles) * width, rng.random(particles) * height
    ps, pa = rng.uniform(0.8, 2.6, particles), rng.uniform(0.15, 0.55, particles)
    vx, vy = rng.uniform(-6, 6, particles), rng.uniform(-14, -3, particles)
    with RawVideoWriter(out_path, width, height, fps, crf=crf, preset=preset) as wr:
        for i in range(n):
            u = i / (n - 1)
            frame = cam.view(world, u).convert("RGBA")
            ov = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            t = i / fps
            for k in range(particles):
                x, y = (px[k] + vx[k] * t) % width, (py[k] + vy[k] * t) % height
                tw = 0.6 + 0.4 * math.sin(t * 2 + k)
                r = ps[k] * width / 540
                d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 245, 225, int(255 * pa[k] * tw)))
            ov = ov.filter(ImageFilter.GaussianBlur(0.8))
            frame.alpha_composite(ov)
            wr.write(to_u8(np.asarray(frame.convert("RGB"), np.float32) * vig))
    return {"frames": n, "source_size": [world.width, world.height]}
