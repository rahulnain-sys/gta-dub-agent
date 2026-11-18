"""Cheap local 'excitement' signals so the AI knows where to look hardest.

- Loudness spikes  -> streamer shouting/laughing, explosions, crashes
- Cut/motion density -> fast action, chases, stunts
These are hints only; the AI still watches every chunk.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .utils import log, read_json, write_json


def _loudness_per_sec(video: Path) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(video), "-vn",
        "-af", "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", out):
        v = m.group(1)
        vals.append(-90.0 if v == "-inf" else max(-90.0, float(v)))
    return np.array(vals, dtype=np.float32)


def _scene_cuts(video: Path) -> list[float]:
    cmd = ["ffmpeg", "-v", "info", "-i", str(video), "-an",
           "-vf", "select='gt(scene,0.28)',showinfo", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    return [float(m.group(1)) for m in re.finditer(r"pts_time:([\d.]+)", err)]


def _z(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    sd = float(x.std()) or 1.0
    return (x - float(np.median(x))) / sd


def excitement(video: Path, cache: Path, duration: float, window: float = 5.0) -> list[dict]:
    """Return hotspot windows sorted by time: [{t, score, reason}]"""
    if cache.exists():
        return read_json(cache)
    log("Measuring loudness & action density across the whole video (local, free)...")
    loud = _loudness_per_sec(video)
    cuts = _scene_cuts(video)
    n = int(duration // window) + 1
    lw = np.full(n, -90.0, dtype=np.float32)
    for i in range(n):
        seg = loud[int(i * window): int((i + 1) * window)]
        if seg.size:
            lw[i] = float(np.percentile(seg, 90))
    cw = np.zeros(n, dtype=np.float32)
    for t in cuts:
        idx = int(t // window)
        if 0 <= idx < n:
            cw[idx] += 1
    # loudness jump vs. the previous 30 s matters more than absolute loudness
    base = np.array([np.median(lw[max(0, i - 6): i]) if i else lw[0] for i in range(n)], dtype=np.float32)
    jump = lw - base
    score = 0.55 * _z(jump) + 0.25 * _z(lw) + 0.35 * _z(cw)
    spots = []
    for i in range(n):
        lo, hi = max(0, i - 3), min(n, i + 4)
        if score[i] < 1.0 or score[i] < score[lo:hi].max():
            continue  # keep local peaks only
        reasons = []
        if _z(jump)[i] > 1.0 or _z(lw)[i] > 1.3:
            reasons.append("loud reaction/impact")
        if _z(cw)[i] > 1.0:
            reasons.append("fast action/cuts")
        spots.append({"t": round(i * window, 1), "score": round(float(score[i]), 2),
                      "reason": ", ".join(reasons) or "energy spike"})
    write_json(cache, spots)
    log(f"Found {len(spots)} high-energy hotspots.")
    return spots
