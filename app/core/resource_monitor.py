"""Hardware / storage awareness. Never assumes a GPU; degrades gracefully without psutil."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


class ResourceMonitor:
    def __init__(self, cfg=None):
        self.cfg = cfg

    # ---- raw measurements --------------------------------------------
    def ram_gb(self) -> tuple[float | None, float | None]:
        """(total, available) in GB."""
        try:
            import psutil
            vm = psutil.virtual_memory()
            return vm.total / 1e9, vm.available / 1e9
        except Exception:
            pass
        try:  # Linux fallback
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0]) * 1024
            return info["MemTotal"] / 1e9, info.get("MemAvailable", info["MemFree"]) / 1e9
        except Exception:
            return None, None

    def disk_free_gb(self, path: str | Path | None = None) -> float:
        p = Path(path) if path else (self.cfg.root if self.cfg else Path("."))
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free / 1e9

    def gpu(self) -> dict | None:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        try:
            cp = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5)
            if cp.returncode == 0 and cp.stdout.strip():
                name, mem = [x.strip() for x in cp.stdout.strip().splitlines()[0].split(",")]
                return {"name": name, "vram_gb": round(float(mem) / 1024, 1)}
        except Exception:
            pass
        return None

    def cpu_count(self) -> int:
        return os.cpu_count() or 1

    # ---- derived ------------------------------------------------------
    def snapshot(self) -> dict:
        total, avail = self.ram_gb()
        return {
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu_count": self.cpu_count(),
            "ram_total_gb": round(total, 1) if total else None,
            "ram_available_gb": round(avail, 1) if avail else None,
            "disk_free_gb": round(self.disk_free_gb(), 1),
            "gpu": self.gpu(),
            "recommended_preset": self.recommend_preset(),
        }

    def recommend_preset(self) -> str:
        total, _ = self.ram_gb()
        cpus = self.cpu_count()
        if total is None:
            return "medium" if cpus >= 4 else "low"
        if total < 4 or cpus < 2:
            return "low"
        if total >= 12 and cpus >= 8:
            return "high"
        return "medium"

    def render_settings(self) -> dict:
        """Resolved video preset dict {width,height,crf,x264_preset,name}."""
        name = self.cfg.get("video.preset", "auto") if self.cfg else "auto"
        if name == "auto":
            name = self.recommend_preset()
        presets = (self.cfg.get("video.presets") if self.cfg else None) or {
            "low": {"width": 720, "height": 1280, "crf": 26, "x264_preset": "veryfast"},
            "medium": {"width": 1080, "height": 1920, "crf": 23, "x264_preset": "veryfast"},
            "high": {"width": 1080, "height": 1920, "crf": 20, "x264_preset": "medium"}}
        out = dict(presets.get(name) or presets["medium"])
        out["name"] = name if name in presets else "medium"
        return out

    def can_load_model(self, size_gb: float, headroom_gb: float = 1.5) -> tuple[bool, str]:
        _, avail = self.ram_gb()
        if avail is None:
            return True, "RAM could not be measured; proceeding cautiously"
        need = size_gb + headroom_gb
        if avail < need:
            return False, f"needs ~{need:.1f} GB free RAM, only {avail:.1f} GB available"
        return True, "ok"

    def check_disk(self, min_free_gb: float | None = None) -> tuple[bool, float]:
        floor = float(min_free_gb if min_free_gb is not None else
                      (self.cfg.get("pipeline.min_free_disk_gb", 3) if self.cfg else 3))
        free = self.disk_free_gb()
        return free >= floor, free

    def can_download(self, size_bytes: int) -> bool:
        floor = float(self.cfg.get("pipeline.min_free_disk_gb", 3)) if self.cfg else 3
        return self.disk_free_gb() - size_bytes / 1e9 >= floor
