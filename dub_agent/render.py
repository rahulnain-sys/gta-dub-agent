"""Turn the horizontal gameplay into a finished vertical Short (crop/blur-fill, blur facecam, captions, audio)."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

from .utils import log, run, video_encoder_args, video_size, warn


def _even(x: float) -> int:
    return int(round(x / 2.0)) * 2


def _filter_path(p: Path, cwd: Path) -> str:
    """Path usable inside an ffmpeg filter argument (Windows-safe)."""
    try:
        rel = os.path.relpath(p, cwd)
        s = rel.replace("\\", "/")
    except ValueError:  # different drive on Windows
        s = str(p).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def render_short(src: Path, start: float, total: float, audio: Path, ass: Path, out: Path, cfg: dict,
                 focus_x: float = 0.5, masks: list | None = None, segments: list | None = None) -> Path:
    """segments (optional): [{start, end, focus_x, facecam}] in clip-relative seconds - lets each
    stitched scene have its own crop position and facecam blur."""
    v = cfg["video"]
    W, H = int(v["width"]), int(v["height"])
    sw, sh = video_size(src)
    chain = []
    label = "0:v"

    # 1) blur facecam / overlays on the source frame
    timed = [(m, None) for m in (masks or [])]
    if cfg["video"].get("blur_facecam", True):
        for sg in (segments or []):
            if sg.get("facecam"):
                timed.append((sg["facecam"], (float(sg["start"]), float(sg["end"]))))
    boxes = []
    for m, when in timed:
        try:
            x, y, w, h = [float(t) for t in m]
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        px, py = _even(max(0, x) * sw), _even(max(0, y) * sh)
        pw, ph = _even(min(w, 1 - x) * sw), _even(min(h, 1 - y) * sh)
        if pw >= 8 and ph >= 8:
            boxes.append((px, py, pw, ph, when))
    for i, (px, py, pw, ph, when) in enumerate(boxes):
        en = f":enable='between(t,{when[0]:.2f},{when[1]:.2f})'" if when else ""
        chain.append(f"[{label}]split=2[mb{i}][mt{i}];[mt{i}]crop={pw}:{ph}:{px}:{py},boxblur=24:6[mbl{i}];"
                     f"[mb{i}][mbl{i}]overlay={px}:{py}{en}[mk{i}]")
        label = f"mk{i}"

    # 2) layout
    layout = v.get("layout", "crop")
    if layout == "landscape":
        chain.append(f"[{label}]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[lay]")
    elif sw / sh <= W / H + 0.01:  # already vertical-ish: just cover
        chain.append(f"[{label}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[lay]")
    elif layout == "blurfill":
        chain.append(
            f"[{label}]split=2[bga][fga];"
            f"[bga]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=28:3,eq=brightness=-0.07[bg];"
            f"[fga]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[lay]"
        )
    else:  # crop: cut left & right, keep the middle where the action is
        cw = _even(sh * W / H)

        def _x(fx):
            fx = min(max(float(fx if fx is not None else 0.5), 0.0), 1.0)
            return int(min(max(fx * sw - cw / 2, 0), sw - cw))

        segs = [sg for sg in (segments or []) if sg.get("focus_x") is not None]
        if len(segs) > 1:
            expr = str(_x(segs[-1]["focus_x"]))
            for sg in reversed(segs[:-1]):
                expr = f"if(lt(t,{float(sg['end']):.2f}),{_x(sg['focus_x'])},{expr})"
            chain.append(f"[{label}]crop={cw}:{sh}:x='{expr}':y=0,scale={W}:{H},setsar=1[lay]")
        else:
            fx = segs[0]["focus_x"] if segs else focus_x
            chain.append(f"[{label}]crop={cw}:{sh}:{_x(fx)}:0,scale={W}:{H},setsar=1[lay]")

    # 3) fps, hold last frame if the dub runs past the source, captions
    ass_arg = _filter_path(ass, out.parent)
    fonts_dir = cfg["captions"].get("fonts_dir") or ""
    ass_filter = f"ass='{ass_arg}'"
    if fonts_dir and Path(fonts_dir).exists():
        ass_filter = f"ass='{ass_arg}':fontsdir='{_filter_path(Path(fonts_dir).resolve(), out.parent)}'"
    chain.append(f"[lay]fps={v['fps']},tpad=stop_mode=clone:stop_duration={total:.2f},{ass_filter}[vout]")

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-i", str(src.resolve()),
        "-i", str(audio.resolve()),
        "-filter_complex", ";".join(chain),
        "-map", "[vout]", "-map", "1:a",
        "-t", f"{total:.3f}",
        *video_encoder_args(cfg),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        out.name,
    ]
    log(f"Rendering {out.name} ({total:.1f}s)...")
    run(cmd, cwd=out.parent)
    return out


# ------------------------------------------------------------------ thumbnail

def _font(cfg: dict, size: int):
    from PIL import ImageFont

    candidates = []
    fd = cfg["captions"].get("fonts_dir")
    if fd and Path(fd).exists():
        candidates += sorted(Path(fd).glob("*.ttf")) + sorted(Path(fd).glob("*.otf"))
    candidates += [Path(p) for p in (
        "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    )]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_thumbnail(video: Path, at: float, text: str, out: Path, cfg: dict) -> Path:
    frame = out.with_name("_frame.jpg")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1", str(frame)])
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        warn("Pillow not installed; thumbnail without text.")
        frame.replace(out)
        return out
    img = Image.open(frame).convert("RGB")
    landscape = img.width > img.height
    if landscape:
        img = img.resize((1280, 720))
    draw = ImageDraw.Draw(img)
    text = (text or "").strip()
    if text:
        size = 110 if landscape else 120
        font = _font(cfg, size)
        lines = textwrap.wrap(text.upper(), width=14 if landscape else 12)[:3 if landscape else 4]
        y = int(img.height * (0.08 if landscape else 0.16))
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=10)
            w = bbox[2] - bbox[0]
            draw.text(((img.width - w) / 2, y), line, font=font, fill=(255, 230, 0),
                      stroke_width=10, stroke_fill=(0, 0, 0))
            y += (bbox[3] - bbox[1]) + 22
    img.save(out, quality=92)
    frame.unlink(missing_ok=True)
    return out
