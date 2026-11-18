"""Fit voice lines to the on-screen timing and mix dialogue, SFX and background music."""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from .utils import SAMPLE_RATE, change_tempo, load_audio, log, run, save_wav, warn

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")


def fit_timeline(script: dict, clip_len: float, cfg: dict, tmp_dir: Path, max_extend: float = 4.0) -> float:
    """Place each line near its intended time, speeding up lines that overrun (up to max_speedup)
    and pushing later lines if needed. Returns the final timeline length in seconds."""
    gap = float(cfg["voice"]["gap_between_lines"])
    max_speed = float(cfg["voice"]["max_speedup"])
    lines = script["lines"]
    cursor = 0.0
    for i, ln in enumerate(lines):
        start = max(ln["start"], cursor)
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else clip_len + max_extend
        avail = max(0.3, nxt - gap - start)
        dur = float(ln["audio_dur"])
        path = ln["audio"]
        if dur > avail * 1.02:
            factor = min(dur / avail, max_speed)
            if factor > 1.03:
                fast = Path(tmp_dir) / (Path(path).stem + f"_x{factor:.2f}.wav")
                if not fast.exists():
                    change_tempo(path, fast, factor)
                path = str(fast)
                dur = dur / factor
        ln["t0"] = round(start, 3)
        ln["t1"] = round(start + dur, 3)
        ln["final_audio"] = path
        cursor = start + dur + gap
    end = max(clip_len, (lines[-1]["t1"] + 0.7) if lines else clip_len)
    return round(min(end, clip_len + max_extend), 3)


def _find_file(folder: str, name: str) -> Path | None:
    d = Path(folder)
    if not d.exists():
        return None
    name = name.lower().replace(" ", "")
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in AUDIO_EXT and p.stem.lower().replace(" ", "").replace("_", "").startswith(name):
            return p
    return None


def pick_music(cfg: dict, seed: str = "") -> Path | None:
    d = Path(cfg["audio"]["music_dir"])
    if not d.exists():
        return None
    files = [p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT]
    if not files:
        return None
    rnd = random.Random(seed)
    return rnd.choice(sorted(files))


def build_dialogue(script: dict, total: float, cfg: dict, out: Path) -> Path:
    n = int(total * SAMPLE_RATE) + SAMPLE_RATE
    track = np.zeros(n, dtype=np.float32)
    fade = int(0.012 * SAMPLE_RATE)
    for ln in script["lines"]:
        s = load_audio(ln["final_audio"])
        if s.size > 2 * fade:
            s[:fade] *= np.linspace(0, 1, fade)
            s[-fade:] *= np.linspace(1, 0, fade)
        i = int(ln["t0"] * SAMPLE_RATE)
        j = min(n, i + s.size)
        track[i:j] += s[: j - i]
    # normalise speech peak so different engines/voices sit at similar level
    peak = float(np.max(np.abs(track))) if track.size else 0
    if peak > 0:
        track *= 0.85 / peak
    sfx_gain = float(cfg["audio"]["sfx_volume"])
    for fx in script.get("sfx", []):
        f = _find_file(cfg["audio"]["sfx_dir"], str(fx["name"]))
        if not f:
            continue
        s = load_audio(f) * sfx_gain
        i = int(max(0.0, float(fx["at"])) * SAMPLE_RATE)
        if i >= n:
            continue
        j = min(n, i + s.size)
        track[i:j] += s[: j - i]
    save_wav(out, track[: int(total * SAMPLE_RATE)])
    return out


def final_mix(dialogue: Path, game_clip: Path | None, total: float, cfg: dict, out: Path, seed: str = "") -> Path:
    a = cfg["audio"]
    music = pick_music(cfg, seed)
    game_vol = float(a.get("game_audio_volume", 0) or 0)
    inputs = ["-i", str(dialogue)]
    chains = ["[0:a]aformat=sample_rates=48000:channel_layouts=mono,asplit=2[d1][d2]"]
    mix_labels = ["[d1]"]
    idx = 1
    if music:
        log(f"Background music: {music.name}")
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        chains.append(
            f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=mono,volume={a['music_volume']}[m];"
            f"[m][d2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[md]"
        )
        mix_labels.append("[md]")
        idx += 1
    else:
        chains.append("[d2]anullsink")
        warn("No background music found in assets/music — add a few royalty-free tracks for better Shorts.")
    if game_clip is not None and game_vol > 0:
        inputs += ["-i", str(game_clip)]
        chains.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=mono,volume={game_vol}[g]")
        mix_labels.append("[g]")
        idx += 1
    chains.append(
        f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:normalize=0:duration=first,"
        f"loudnorm=I={a['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000[out]"
    )
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(chains),
           "-map", "[out]", "-t", f"{total:.3f}", "-ac", "2", "-ar", "48000", str(out)]
    run(cmd)
    return out
