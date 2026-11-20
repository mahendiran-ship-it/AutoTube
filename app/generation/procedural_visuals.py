"""Procedural astronomy visuals (pure numpy + Pillow, CPU only). The always-available fallback.

These are ARTISTIC ILLUSTRATIONS, not simulations or real imagery; assets are recorded as method=procedural.
Kinds: starfield, nebula, planet, black_hole, galaxy, orbit, timeline.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.utils.ffmpeg import RawVideoWriter
from app.utils.fonts import load_font

KINDS = ["starfield", "nebula", "planet", "black_hole", "galaxy", "orbit", "timeline"]


def seed_from(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


# ---------------- helpers ----------------
def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def fbm(h: int, w: int, rng, octaves: int = 5, base: int = 3, persistence: float = 0.55, aniso: float = 1.0) -> np.ndarray:
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        n = base * (2 ** o)
        gy, gx = max(2, int(n * h / w * aniso)), max(2, n)
        g = Image.fromarray(rng.random((gy, gx)).astype(np.float32), mode="F").resize((w, h), Image.BICUBIC)
        out += amp * np.asarray(g)
        total += amp
        amp *= persistence
    out /= total
    lo, hi = np.percentile(out, 1), np.percentile(out, 99)
    return np.clip((out - lo) / (hi - lo + 1e-9), 0, 1)


def palette(v: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    pos = [s[0] for s in stops]
    return np.stack([np.interp(v, pos, [s[1][c] for s in stops]) for c in range(3)], axis=-1).astype(np.float32)


def star_layer(h: int, w: int, rng, n: int, bright: float = 1.0) -> np.ndarray:
    """float32 HxWx3 in 0..1: stars of varied colour temperature; a few large 'hero' stars with halos."""
    canvas = np.zeros((h, w, 3), np.float32)
    xs, ys = rng.integers(0, w, n), rng.integers(0, h, n)
    b = 0.25 + 0.75 * rng.random(n) ** 3.5
    temp = rng.random(n)
    col = np.stack([0.75 + 0.25 * temp, 0.82 + 0.1 * (1 - np.abs(temp - 0.5)), 1.0 - 0.3 * temp], -1)
    np.add.at(canvas, (ys, xs), col * b[:, None] * bright)
    img = Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8))
    glow = np.asarray(img.filter(ImageFilter.GaussianBlur(1.5)), np.float32) / 255
    out = canvas + glow * 2.4
    hero = rng.random(n) < 0.025
    if hero.any():
        hc = np.zeros_like(canvas)
        np.add.at(hc, (ys[hero], xs[hero]), col[hero] * 1.0)
        himg = Image.fromarray((np.clip(hc, 0, 1) * 255).astype(np.uint8))
        out += np.asarray(himg.filter(ImageFilter.GaussianBlur(2.6)), np.float32) / 255 * 9.0
        out += np.asarray(himg.filter(ImageFilter.GaussianBlur(7)), np.float32) / 255 * 10.0
    return np.clip(out, 0, 1)


def bloom(img: np.ndarray, strength: float = 0.5, radius: int = 6, scale: int = 4) -> np.ndarray:
    h, w = img.shape[:2]
    small = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize((max(2, w // scale), max(2, h // scale)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius / scale * 2))
    big = np.asarray(small.resize((w, h), Image.BILINEAR), np.float32)
    return np.clip(img + strength * big, 0, 255)


def radial_glow(size: int, color: tuple[int, int, int], power: float = 7.0, peak: float = 0.5) -> Image.Image:
    """RGBA soft round glow sprite."""
    g = np.mgrid[-1:1:complex(size), -1:1:complex(size)].astype(np.float32)
    a = np.exp(-(g[0] ** 2 + g[1] ** 2) * power) * peak * 255
    rgb = np.broadcast_to(np.array(color, np.float32), (size, size, 3))
    return Image.fromarray(np.dstack([rgb, a]).astype(np.uint8), "RGBA")


def to_u8(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 255).astype(np.uint8)


def vignette_mask(h: int, w: int, strength: float = 0.45) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.hypot((xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2))
    return (1 - strength * smoothstep(0.55, 1.35, d))[..., None]


class Camera:
    """Smooth zoom/pan over a larger 'world' image. Parsed from the scene's camera text."""

    def __init__(self, text: str, w: int, h: int, world_w: int, world_h: int):
        t = (text or "").lower()
        self.w, self.h, self.ww, self.wh = w, h, world_w, world_h
        z0, z1 = 1.0, 1.16
        if "pull" in t or "back" in t or "out" in t:
            z0, z1 = 1.2, 1.0
        self.z0, self.z1 = z0, z1
        self.dx = -1 if "left" in t else (1 if "right" in t else 0)
        self.dy = -1 if "up" in t else (1 if "down" in t else 0)
        self.drift = 0.04 if (self.dx or self.dy) else 0.012

    def view(self, world: Image.Image, u: float) -> Image.Image:
        e = u * u * (3 - 2 * u)
        zoom = self.z0 + (self.z1 - self.z0) * e
        # window size in world pixels (the world is larger than the viewport by sx)
        sx = min(self.ww / self.w, self.wh / self.h)
        win_w, win_h = self.w * sx / zoom, self.h * sx / zoom
        cx = self.ww / 2 + (e - 0.5) * self.dx * self.drift * self.ww
        cy = self.wh / 2 + (e - 0.5) * self.dy * self.drift * self.wh
        cx = min(max(cx, win_w / 2), self.ww - win_w / 2)
        cy = min(max(cy, win_h / 2), self.wh - win_h / 2)
        x0, y0 = cx - win_w / 2, cy - win_h / 2
        return world.transform((self.w, self.h), Image.AFFINE, (win_w / self.w, 0, x0, 0, win_h / self.h, y0), resample=Image.BILINEAR)


# ---------------- renderers: each yields uint8 HxWx3 frames ----------------
class BaseScene:
    def __init__(self, width: int, height: int, fps: int, duration: float, seed: int, camera: str = "", text: str = ""):
        self.w, self.h, self.fps, self.seed, self.camera_text, self.text = width, height, fps, seed, camera, text
        self.n = max(2, int(round(duration * fps)))
        self.rng = np.random.default_rng(seed)

    def frames(self):
        raise NotImplementedError


class StarfieldScene(BaseScene):
    """Three star layers with different parallax speeds + faint dust."""

    def frames(self):
        w, h = self.w, self.h
        W, H = int(w * 1.4), int(h * 1.4)
        layers = []
        for n, br in ((int(W * H / 1500), 0.7), (int(W * H / 3200), 0.9), (int(W * H / 9000), 1.0)):
            layers.append(Image.fromarray(to_u8(star_layer(H, W, self.rng, n, br) * 255)))
        yy, xx = np.mgrid[0:H // 4, 0:W // 4].astype(np.float32)
        ang = float(self.rng.uniform(-0.9, 0.9))
        dist = (xx - W / 8) * math.sin(ang) + (yy - H / 8) * math.cos(ang)
        band = np.exp(-(dist / (W / 14)) ** 2)
        clouds = fbm(H // 4, W // 4, self.rng, 6, 3)
        v = np.clip(0.14 + 0.42 * band * (0.3 + 0.9 * clouds) + 0.10 * clouds, 0, 1)
        dust = palette(v, [(0, (2, 3, 10)), (0.3, (9, 12, 32)), (0.5, (28, 30, 62)), (0.7, (62, 56, 92)), (1, (110, 94, 112))])
        dust = np.asarray(Image.fromarray(to_u8(dust)).resize((W, H), Image.BICUBIC), np.float32)
        cams = [Camera(self.camera_text, w, h, W, H) for _ in layers]
        for c, (z0, z1) in zip(cams, ((1.0, 1.05), (1.0, 1.14), (1.0, 1.32))):
            c.z0, c.z1 = (z0, z1) if c.z0 <= c.z1 else (z1, z0)
        dust_img = Image.fromarray(to_u8(dust))
        vig = vignette_mask(h, w)
        for i in range(self.n):
            u = i / (self.n - 1)
            base = np.asarray(cams[0].view(dust_img, u), np.float32)
            for k, (layer, cam) in enumerate(zip(layers, cams)):
                tw = 0.85 + 0.15 * math.sin(i * 0.15 + k)
                base += np.asarray(cam.view(layer, u), np.float32) * tw
            yield to_u8(base * vig)


class NebulaScene(StarfieldScene):
    def frames(self):
        w, h = self.w, self.h
        W, H = int(w * 1.35), int(h * 1.35)
        rng = self.rng
        hue = rng.random()
        schemes = [[(0, (3, 4, 14)), (0.35, (30, 18, 80)), (0.6, (150, 40, 120)), (0.82, (255, 120, 90)), (1, (255, 205, 160))],
                   [(0, (2, 6, 16)), (0.35, (8, 40, 90)), (0.6, (20, 130, 160)), (0.82, (110, 220, 200)), (1, (170, 235, 225))],
                   [(0, (4, 3, 12)), (0.35, (60, 14, 40)), (0.6, (190, 60, 40)), (0.82, (255, 170, 60)), (1, (255, 245, 200))]]
        stops = schemes[int(hue * 3) % 3]
        a, b = fbm(H // 3, W // 3, rng, 6, 2), fbm(H // 3, W // 3, rng, 6, 2)
        shape = fbm(H // 3, W // 3, rng, 3, 1)
        gate = smoothstep(0.25, 0.75, shape)
        worlds = []
        for field in (a, b):
            v = (field ** 1.5) * gate
            v = np.clip(v / (np.percentile(v, 99.0) + 1e-6) * 0.92, 0, 1)
            worlds.append(np.asarray(Image.fromarray(to_u8(palette(v, stops))).resize((W, H), Image.BICUBIC), np.float32))
        stars = np.asarray(Image.fromarray(to_u8(star_layer(H, W, rng, int(W * H / 2400)) * 255)), np.float32)
        cam = Camera(self.camera_text, w, h, W, H)
        vig = vignette_mask(h, w)
        for i in range(self.n):
            u = i / (self.n - 1)
            m = 0.5 - 0.5 * math.cos(u * math.pi)
            world = Image.fromarray(to_u8(worlds[0] * (1 - m) + worlds[1] * m + stars))
            yield to_u8(bloom(np.asarray(cam.view(world, u), np.float32), 0.25, 8) * vig)


class PlanetScene(BaseScene):
    def style(self) -> dict:
        t = self.text.lower()
        if any(k in t for k in ("mars", "rust", "red planet")):
            return {"name": "rust", "stops": [(0, (70, 30, 20)), (0.5, (170, 85, 50)), (1, (225, 150, 100))], "bands": False,
                    "atmo": (255, 170, 120)}
        if any(k in t for k in ("jupiter", "saturn", "gas giant", "bands")):
            return {"name": "gas", "stops": [(0, (110, 70, 45)), (0.4, (190, 150, 105)), (0.7, (230, 205, 165)), (1, (245, 232, 205))],
                    "bands": True, "atmo": (255, 220, 170), "rings": "saturn" in t or "ring" in t}
        if any(k in t for k in ("neptune", "uranus", "ice", "icy", "europa", "enceladus", "frozen")):
            return {"name": "ice", "stops": [(0, (30, 60, 120)), (0.5, (90, 150, 215)), (1, (220, 240, 255))], "bands": True,
                    "atmo": (140, 200, 255)}
        if "venus" in t:
            return {"name": "venus", "stops": [(0, (150, 105, 50)), (0.6, (225, 185, 105)), (1, (250, 230, 170))], "bands": True,
                    "atmo": (255, 215, 140)}
        pick = self.rng.integers(0, 3)
        return [self.style_for("rust"), self.style_for("gas"), self.style_for("ice")][int(pick)]

    def style_for(self, name: str) -> dict:
        saved = self.text
        self.text = {"rust": "mars", "gas": "jupiter", "ice": "neptune"}[name]
        s = self.style()
        self.text = saved
        return s

    def frames(self):
        w, h, rng = self.w, self.h, self.rng
        st = self.style()
        Wt, Ht = 1024, 512
        noise = fbm(Ht, Wt, rng, 6, 4)
        if st["bands"]:
            lat = np.linspace(0, 1, Ht, dtype=np.float32)[:, None]
            bands = 0.5 + 0.5 * np.sin(lat * 26 + noise * 3.2)
            v = np.clip(0.55 * bands + 0.45 * noise, 0, 1)
        else:
            v = noise
        tex = palette(v, st["stops"])
        R = 0.34 * w
        cx, cy = w * 0.5, h * 0.44
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        X, Y = (xx - cx) / R, (yy - cy) / R
        d2 = X * X + Y * Y
        inside = d2 < 1.0
        nz = np.sqrt(np.clip(1 - d2, 0, 1))
        lat = np.arcsin(np.clip(-Y, -1, 1))
        lon = np.arctan2(X, nz)
        vi = np.clip(((0.5 - lat / math.pi) * Ht).astype(np.int32), 0, Ht - 1)
        ui = (((lon / (2 * math.pi)) + 0.5) * Wt).astype(np.int32)
        L = np.array([-0.62, -0.38, 0.69], np.float32)
        L /= np.linalg.norm(L)
        diff = np.clip(X * L[0] + Y * L[1] + nz * L[2], 0, 1)
        shade = (0.035 + 1.15 * smoothstep(0.0, 0.8, diff))[..., None]
        fres = ((1 - nz) ** 3.0)[..., None] * np.array(st["atmo"], np.float32) / 255
        d = np.sqrt(d2)
        lit_side = np.clip(0.5 - 0.5 * (X * L[0] + Y * L[1]) / (d + 1e-6), 0, 1)
        halo = (np.exp(-(d - 1) / 0.06) * (d >= 1) * 0.55 * (0.35 + 0.65 * lit_side))[..., None] * (np.array(st["atmo"], np.float32) / 255)
        stars = np.asarray(Image.fromarray(to_u8(star_layer(int(h * 1.3), int(w * 1.3), rng, int(w * h / 2000)) * 255)), np.float32)
        star_img = Image.fromarray(to_u8(stars))
        cam = Camera(self.camera_text, w, h, int(w * 1.3), int(h * 1.3))
        ring = self._rings(X, Y, R, nz, inside) if st.get("rings") else None
        vig = vignette_mask(h, w, 0.5)
        for i in range(self.n):
            u = i / (self.n - 1)
            shift = int(i * Wt * 0.012 / self.fps * 30)       # slow rotation
            surf = tex[vi, (ui + shift) % Wt] * shade
            surf = surf + fres * (0.25 + 1.4 * smoothstep(-0.2, 0.7, diff))[..., None] * 255 * 0.55
            bg = np.asarray(cam.view(star_img, u), np.float32)
            frame = np.where(inside[..., None], surf, bg + halo * 255 * 0.8)
            if ring is not None:
                rb, rf = ring
                frame = np.where((rb[..., 3:4] > 0) & ~inside[..., None], frame * (1 - rb[..., 3:4]) + rb[..., :3] * rb[..., 3:4], frame)
                frame = frame * (1 - rf[..., 3:4]) + rf[..., :3] * rf[..., 3:4]
            yield to_u8(bloom(frame, 0.18, 8) * vig)

    def _rings(self, X, Y, R, nz, inside):
        tilt = 0.30
        rr = np.hypot(X, Y / tilt)
        prof = smoothstep(1.35, 1.42, rr) * (1 - smoothstep(2.15, 2.25, rr))
        rng = np.random.default_rng(self.seed + 7)
        grooves = 0.55 + 0.45 * np.interp(np.clip((rr - 1.3) / 1.0, 0, 1) * 255, np.arange(256), rng.random(256).astype(np.float32))
        gap = 1 - 0.85 * np.exp(-((rr - 1.86) / 0.025) ** 2)
        alpha = (prof * grooves * gap * 0.85).astype(np.float32)
        col = np.array([215, 195, 160], np.float32)
        light = (0.45 + 0.55 * np.clip(0.6 - 0.4 * X / (rr + 1e-6), 0, 1))
        rgb = col[None, None, :] * light[..., None]
        front = (Y > 0)
        back = np.concatenate([rgb, (alpha * (~front))[..., None]], -1)
        fro = np.concatenate([rgb, (alpha * front)[..., None]], -1)
        return back, fro


class BlackHoleScene(BaseScene):
    """Artistic gravitational-lensing visualisation: shadow, photon ring, lensed secondary image,
    Doppler-beamed turbulent accretion disk (differential rotation), lensed star background."""

    def frames(self):
        w, h, rng = self.w, self.h, self.rng
        s = 0.115 * w
        cx, cy = w * 0.5, h * 0.42
        sin_e = 0.2
        r_in, r_out = 1.7, 5.2
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        X, Yup = (xx - cx) / s, -(yy - cy) / s
        b = np.hypot(X, Yup) + 1e-6
        # --- lensed background stars (static) ---
        k = np.clip(1 - 0.85 / (b * b + 0.25), 0.05, 1.0)
        sx = np.clip((cx + X * k * s).astype(np.int32), 0, w - 1)
        sy = np.clip((cy - Yup * k * s).astype(np.int32), 0, h - 1)
        stars = star_layer(h, w, rng, int(w * h / 1800))
        bg = stars[sy, sx] * 255 * smoothstep(0.97, 1.06, b)[..., None]
        dust = palette(fbm(h // 4, w // 4, rng, 4, 2), [(0, (2, 3, 8)), (1, (14, 16, 34))])
        bg += np.asarray(Image.fromarray(to_u8(dust)).resize((w, h), Image.BICUBIC), np.float32) * smoothstep(0.97, 1.06, b)[..., None]
        # --- disk texture (r_bins x phi_bins) ---
        RB, PB = 320, 1024
        tex = fbm(RB, PB, rng, 6, 4, 0.6, aniso=0.25)
        tex = 0.35 + 1.3 * tex
        rgrid = np.linspace(r_in, r_out, RB, dtype=np.float32)
        omega = 1.1 * rgrid ** -1.5
        # --- direct (flat) disk image ---
        Yd = Yup / sin_e
        r1 = np.hypot(X, Yd)
        phi1 = np.arctan2(Yd, X)
        m1 = (r1 >= r_in) & (r1 <= r_out) & ((Yup < 0) | (b > 1.02))
        # --- lensed secondary arc (over/under the shadow) ---
        r2 = r_in + (b - 1.0) * 1.9
        sinth = Yup / b
        g = np.clip(sinth, 0, 1) ** 1.1 + 0.35 * np.clip(-sinth, 0, 1) ** 1.1
        m2 = (b >= 1.0) & (b <= 2.05)
        phi2 = np.arctan2(Yup, X) + math.pi

        def prep(m, r, phi):
            ri = np.clip(((r[m] - r_in) / (r_out - r_in) * (RB - 1)).astype(np.int32), 0, RB - 1)
            pi0 = (((phi[m] + math.pi) / (2 * math.pi)) * PB).astype(np.int32)
            return m, ri, pi0

        m1_, ri1, pi1 = prep(m1, r1, phi1)
        r2c = np.clip(r2, r_in, r_out)
        m2_, ri2, pi2 = prep(m2, r2c, phi2)
        emis1 = np.where(m1, (r_in / np.maximum(r1, r_in)) ** 2.0 * smoothstep(r_in, r_in + 0.25, r1) * (1 - smoothstep(r_out - 1.0, r_out, r1)), 0)[m1]
        emis2 = np.where(m2, np.exp(-(b - 1.0) / 0.2) * g * 2.2 * smoothstep(0.99, 1.04, b), 0)[m2]
        dop1 = np.clip(1 - 0.6 * (X / np.maximum(r1, 1e-3)), 0.15, 1.9)[m1] ** 2.2
        dop2 = np.clip(1 - 0.6 * (X / b), 0.15, 1.9)[m2] ** 2.2
        ring = np.exp(-((b - 1.03) / 0.016) ** 2) * 1.1
        lut = palette(np.linspace(0, 1, 256), [(0, (0, 0, 0)), (0.18, (90, 14, 4)), (0.4, (225, 80, 12)), (0.65, (255, 175, 70)),
                                              (0.88, (255, 235, 190)), (1, (255, 255, 255))])
        cam = Camera(self.camera_text or "slow push-in", w, h, w, h)
        cam.z0, cam.z1 = (cam.z0, cam.z1) if cam.z0 <= cam.z1 else (1.15, 1.0)
        vig = vignette_mask(h, w, 0.5)
        for i in range(self.n):
            u = i / (self.n - 1)
            t = i / self.fps
            shift = (omega * t * PB / (2 * math.pi)).astype(np.int32)
            B = np.zeros((h, w), np.float32)
            B[m1_] += tex[ri1, (pi1 + shift[ri1]) % PB] * emis1 * dop1
            B[m2_] += tex[ri2, (pi2 + shift[ri2]) % PB] * emis2 * dop2
            B += ring * (0.55 + 0.45 * dop2_full(X, b))
            tm = 1 - np.exp(-B * 1.35)
            disk = lut[np.clip((tm * 255).astype(np.int32), 0, 255)]
            frame = bloom(bg + disk, 0.55, 12)
            img = Image.fromarray(to_u8(frame * vig))
            yield np.asarray(cam.view(img, u), np.uint8)


def dop2_full(X, b):
    return np.clip(1 - 0.6 * (X / b), 0.15, 1.9) ** 2.2 / 3.0


class GalaxyScene(BaseScene):
    def frames(self):
        w, h, rng = self.w, self.h, self.rng
        N = 80000
        arms = 2
        nb = int(N * 0.22)
        r_arm = rng.random(N - nb) ** 0.62
        arm_i = rng.integers(0, arms, N - nb)
        theta = arm_i * (2 * math.pi / arms) + 5.2 * np.log(r_arm + 0.08) + rng.normal(0, 0.28 / (0.35 + r_arm), N - nb)
        r_b = np.abs(rng.normal(0, 0.1, nb))
        th_b = rng.random(nb) * 2 * math.pi
        r = np.concatenate([r_arm, r_b]).astype(np.float32)
        th = np.concatenate([theta, th_b]).astype(np.float32)
        bulge = np.concatenate([np.zeros(N - nb), np.ones(nb)]).astype(bool)
        mix = np.clip(r / 0.9, 0, 1)[:, None]
        core = np.array([255, 214, 150], np.float32)
        blue = np.array([160, 195, 255], np.float32)
        col = core * (1 - mix) + blue * mix
        hii = (rng.random(N) < 0.025) & ~bulge
        col[hii] = np.array([255, 120, 170], np.float32)
        wgt = np.where(bulge, 0.55, 0.34) * (0.5 + rng.random(N)) * (1 - 0.4 * mix[:, 0])
        omega = (0.9 / (r + 0.12)).astype(np.float32)
        S = 0.64 * w
        cx, cy = w * 0.5, h * 0.44
        tilt0, tilt1 = 0.62, 0.50
        roll = 0.45
        bgstars = np.asarray(Image.fromarray(to_u8(star_layer(h, w, rng, int(w * h / 2200)) * 255)), np.float32)
        cam = Camera(self.camera_text, w, h, w, h)
        cam.z0, cam.z1 = (1.0, 1.12) if cam.z0 <= cam.z1 else (1.12, 1.0)
        vig = vignette_mask(h, w, 0.5)
        for i in range(self.n):
            u = i / (self.n - 1)
            ang = th + omega * (i / self.fps) * 0.6
            tilt = tilt0 + (tilt1 - tilt0) * u
            x, y = r * np.cos(ang), r * np.sin(ang) * tilt
            xr, yr = x * math.cos(roll) - y * math.sin(roll), x * math.sin(roll) + y * math.cos(roll)
            px, py = (cx + xr * S).astype(np.int32), (cy + yr * S).astype(np.int32)
            ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            idx = py[ok] * w + px[ok]
            acc = np.stack([np.bincount(idx, weights=(col[ok, c] * wgt[ok]), minlength=w * h) for c in range(3)], -1).reshape(h, w, 3)
            img = 255 * (1 - np.exp(-acc / 255 * 2.2))
            frame = bloom(img, 0.9, 10) + bgstars
            pil = Image.fromarray(to_u8(frame * vig))
            yield np.asarray(cam.view(pil, u), np.uint8)


class OrbitScene(BaseScene):
    def frames(self):
        w, h, rng = self.w, self.h, self.rng
        W, H = int(w * 1.25), int(h * 1.25)
        star_world = Image.fromarray(to_u8(star_layer(H, W, rng, int(W * H / 2400)) * 255))
        cam = Camera(self.camera_text, w, h, W, H)
        cx, cy = w * 0.5, h * 0.44
        tilt = 0.58
        bodies = [{"a": 0.31 * w, "speed": 1.0, "r": 0.034 * w, "col": (150, 190, 255), "phase": rng.random() * 6.28},
                  {"a": 0.46 * w, "speed": 0.62, "r": 0.024 * w, "col": (230, 200, 160), "phase": rng.random() * 6.28}]

        def sphere(radius, col, light=(-0.6, -0.4)):
            n = int(radius * 2 + 6)
            yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
            X, Y = (xx - n / 2) / radius, (yy - n / 2) / radius
            d2 = X * X + Y * Y
            nz = np.sqrt(np.clip(1 - d2, 0, 1))
            dif = np.clip(X * light[0] + Y * light[1] + nz * 0.69, 0, 1)
            rgb = np.array(col, np.float32)[None, None, :] * (0.12 + 1.0 * dif)[..., None]
            a = smoothstep(1.0, 0.92, np.sqrt(d2))
            return Image.fromarray(np.dstack([to_u8(rgb), to_u8(a * 255)]), "RGBA")

        central = sphere(0.125 * w, (255, 214, 120), (-0.1, -0.1))
        glow_big = radial_glow(int(0.8 * w), (255, 190, 90))
        spr = [sphere(b["r"], b["col"]) for b in bodies]
        vig = vignette_mask(h, w, 0.5)
        trail_len = 60
        for i in range(self.n):
            u = i / (self.n - 1)
            t = i / self.fps
            frame = cam.view(star_world, u).convert("RGBA")
            ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            for b in bodies:
                pts = [(cx + b["a"] * math.cos(q), cy + b["a"] * tilt * math.sin(q)) for q in np.linspace(0, 2 * math.pi, 120)]
                d.line(pts + [pts[0]], fill=(130, 165, 230, 110), width=max(2, w // 260))
            ov.alpha_composite(glow_big, (int(cx - glow_big.size[0] / 2), int(cy - glow_big.size[1] / 2)))
            order = []
            for b, sp in zip(bodies, spr):
                ang = b["phase"] + t * b["speed"] * 0.9
                for j in range(trail_len):
                    a2 = ang - j * 0.018 * b["speed"]
                    px, py = cx + b["a"] * math.cos(a2), cy + b["a"] * tilt * math.sin(a2)
                    al = int(150 * (1 - j / trail_len) ** 2)
                    rr = max(1.0, b["r"] * 0.35 * (1 - j / trail_len))
                    d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=(*b["col"], al))
                order.append((math.sin(ang), ang, b, sp))
            for sn, ang, b, sp in sorted(order, key=lambda o: o[0]):
                if sn < 0:
                    ov.alpha_composite(sp, (int(cx + b["a"] * math.cos(ang) - sp.size[0] / 2), int(cy + b["a"] * tilt * math.sin(ang) - sp.size[1] / 2)))
            ov.alpha_composite(central, (int(cx - central.size[0] / 2), int(cy - central.size[1] / 2)))
            for sn, ang, b, sp in sorted(order, key=lambda o: o[0]):
                if sn >= 0:
                    ov.alpha_composite(sp, (int(cx + b["a"] * math.cos(ang) - sp.size[0] / 2), int(cy + b["a"] * tilt * math.sin(ang) - sp.size[1] / 2)))
            frame.alpha_composite(ov)
            yield to_u8(bloom(np.asarray(frame.convert("RGB"), np.float32), 0.2, 8) * vig)


class TimelineScene(BaseScene):
    """Labelled timeline; labels come ONLY from time spans actually written in the narration."""
    _SPAN = re.compile(r"(\d[\d,.]*\s*(?:billion|million|thousand)?\s*years?(?:\s+ago)?|today|now|the big bang)", re.I)

    @classmethod
    def labels_from(cls, text: str) -> list[str]:
        out = []
        for m in cls._SPAN.findall(text or ""):
            m = re.sub(r"\s+", " ", m.strip())
            if m.lower() not in [x.lower() for x in out]:
                out.append(m)
        return out[:5]

    def frames(self):
        w, h, rng = self.w, self.h, self.rng
        labels = self.labels_from(self.text)
        if len(labels) < 2:
            labels = labels + ["Then", "Now"][len(labels):]
        W, H = int(w * 1.25), int(h * 1.25)
        stars = Image.fromarray(to_u8(star_layer(H, W, rng, int(W * H / 2600)) * 255))
        cam = Camera(self.camera_text, w, h, W, H)
        font = load_font(None, max(20, int(w * 0.066)))
        y = int(h * 0.5)
        x0, x1 = int(w * 0.1), int(w * 0.9)
        xs = [x0 + (x1 - x0) * k / (len(labels) - 1) for k in range(len(labels))]
        vig = vignette_mask(h, w, 0.5)
        for i in range(self.n):
            u = i / (self.n - 1)
            frame = cam.view(stars, u).convert("RGBA")
            ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            px = x0 + (x1 - x0) * min(1, u * 1.05)
            d.line([(x0, y), (x1, y)], fill=(110, 140, 210, 190), width=max(3, w // 170))
            d.line([(x0, y), (px, y)], fill=(255, 190, 80, 255), width=max(5, w // 100))
            for k, (lx, lab) in enumerate(zip(xs, labels)):
                reached = px >= lx - 2
                d.ellipse([lx - 14, y - 14, lx + 14, y + 14], fill=(255, 190, 80, 255) if reached else (90, 120, 190, 200))
                if reached:
                    fade = min(1.0, (px - lx) / (w * 0.06) + 0.25)
                    tb = d.textbbox((0, 0), lab, font=font)
                    tw = tb[2] - tb[0]
                    ty = y + (int(h * 0.05) if k % 2 == 0 else -int(h * 0.05) - (tb[3] - tb[1]))
                    tx = min(max(lx - tw / 2, w * 0.03), w * 0.97 - tw)
                    d.text((tx, ty), lab, font=font, fill=(255, 255, 255, int(255 * fade)), stroke_width=2, stroke_fill=(0, 0, 0, int(255 * fade)))
            for r_, a_ in ((26, 40), (16, 90), (9, 255)):
                d.ellipse([px - r_, y - r_, px + r_, y + r_], fill=(255, 215, 120, a_))
            frame.alpha_composite(ov)
            yield to_u8(np.asarray(frame.convert("RGB"), np.float32) * vig)


RENDERERS = {"starfield": StarfieldScene, "nebula": NebulaScene, "planet": PlanetScene, "black_hole": BlackHoleScene,
             "galaxy": GalaxyScene, "orbit": OrbitScene, "timeline": TimelineScene}


def render_procedural(kind: str, out_path: Path | str, duration: float, width: int, height: int, fps: int = 30,
                      seed: int = 0, camera: str = "", text: str = "", crf: int = 18, preset: str = "veryfast") -> dict:
    cls = RENDERERS.get(kind) or StarfieldScene
    scene = cls(width, height, fps, duration, seed, camera, text)
    with RawVideoWriter(out_path, width, height, fps, crf=crf, preset=preset) as wr:
        for frame in scene.frames():
            wr.write(frame)
    return {"kind": kind if kind in RENDERERS else "starfield", "frames": scene.n, "width": width, "height": height,
            "fps": fps, "seed": seed}
