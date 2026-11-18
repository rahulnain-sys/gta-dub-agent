"""Shared helpers: logging, ffmpeg/ffprobe wrappers, time parsing, audio I/O."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48000  # internal mixing rate (mono float32)


def log(msg: str) -> None:
    print(f"[dub] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[dub] WARNING: {msg}", flush=True)


def need_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"'{name}' was not found on PATH. Install it first (see README).")
    return path


def run(cmd: list[str], cwd: str | Path | None = None, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a command; raise with stderr tail on failure."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(map(str, cmd[:6]))} ...\n{tail}")
    if not quiet and proc.stdout:
        print(proc.stdout)
    return proc


def ffprobe_json(path: str | Path) -> dict:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(proc.stdout)


def media_duration(path: str | Path) -> float:
    info = ffprobe_json(path)
    return float(info["format"]["duration"])


def video_size(path: str | Path) -> tuple[int, int]:
    info = ffprobe_json(path)
    for s in info["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError(f"No video stream in {path}")


def has_audio(path: str | Path) -> bool:
    info = ffprobe_json(path)
    return any(s.get("codec_type") == "audio" for s in info["streams"])


def parse_time(value: str | float | int | None) -> float | None:
    """Accept seconds or HH:MM:SS(.ms) / MM:SS."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    parts = [float(p) for p in value.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:max_len].strip("-")) or "short"


def text_hash(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- audio I/O

def load_audio(path: str | Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio file to mono float32 at `sr` using ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not decode audio {path}: {proc.stderr.decode(errors='replace')[-400:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def save_wav(path: str | Path, samples: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def save_pcm16_as_wav(path: str | Path, pcm_bytes: bytes, sr: int = 24000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_bytes)


def trim_silence(samples: np.ndarray, sr: int = SAMPLE_RATE, threshold_db: float = -42.0, pad: float = 0.04) -> np.ndarray:
    """Remove leading/trailing silence that TTS engines often add."""
    if samples.size == 0:
        return samples
    thr = 10 ** (threshold_db / 20)
    idx = np.where(np.abs(samples) > thr)[0]
    if idx.size == 0:
        return samples
    p = int(pad * sr)
    return samples[max(0, idx[0] - p): min(samples.size, idx[-1] + p)]


def change_tempo(path_in: str | Path, path_out: str | Path, factor: float) -> None:
    """Speed speech up/down without changing pitch (ffmpeg atempo, chained for >2x)."""
    filters = []
    f = factor
    while f > 2.0:
        filters.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        filters.append("atempo=0.5")
        f /= 0.5
    filters.append(f"atempo={f:.4f}")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path_in), "-filter:a", ",".join(filters), str(path_out)])


def read_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def env(name: str) -> str | None:
    val = os.environ.get(name)
    return val.strip() if val else None


# ---------------------------------------------------------------- video encoder (hardware if available)

_ENCODER_CACHE: dict = {}

ENCODER_ARGS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "{q}", "-b:v", "0"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "{q}", "-look_ahead", "0"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"],
    "libx264": ["-c:v", "libx264", "-preset", "{preset}", "-crf", "{q}"],
}


def _encoder_works(name: str) -> bool:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-frames:v", "3",
         *[a.replace("{q}", "23").replace("{preset}", "veryfast") for a in ENCODER_ARGS[name]], "-f", "null", "-"],
        capture_output=True,
    )
    return proc.returncode == 0


def video_encoder_args(cfg: dict, quality: int | None = None) -> list[str]:
    """Pick NVIDIA/Intel/AMD hardware encoding when available (3-10x faster), else libx264."""
    want = (cfg.get("performance", {}) or {}).get("encoder", "auto")
    if want not in _ENCODER_CACHE:
        chosen = "libx264"
        if want == "auto":
            for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
                if _encoder_works(name):
                    chosen = name
                    break
        elif want in ENCODER_ARGS and (want == "libx264" or _encoder_works(want)):
            chosen = want
        _ENCODER_CACHE[want] = chosen
        log(f"Video encoder: {chosen}")
    name = _ENCODER_CACHE[want]
    q = int(quality if quality is not None else cfg["video"].get("crf", 20))
    preset = cfg["video"].get("preset", "veryfast")
    return [a.replace("{q}", str(q if name == "libx264" else q + 1)).replace("{preset}", preset)
            for a in ENCODER_ARGS[name]] + ["-pix_fmt", "yuv420p"]
