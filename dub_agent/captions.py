"""Word-by-word karaoke captions (ASS), hook title and creator credit."""
from __future__ import annotations

from pathlib import Path


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ovr_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(script: dict, total: float, cfg: dict, out: Path, credit: str = "") -> Path:
    c = cfg["captions"]
    v = cfg["video"]
    W, H = v["width"], v["height"]
    font = c["font"]
    colors = c["highlight_colors"]
    speakers = [ch["id"] for ch in script["characters"]]
    spk_color = {s: colors[i % len(colors)] for i, s in enumerate(speakers)}

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{c['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,{c['outline']},3,2,70,70,{c['position_from_bottom']},1
Style: Title,{font},{c['title_size']},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00101010,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{c.get('title_margin', 300)},1
Style: Credit,{font},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,8,60,60,{c.get('credit_margin', 150)},1
Style: Name,{font},44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,70,70,{c['position_from_bottom'] + c['size'] + 30},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    wpc = max(1, int(c["words_per_chunk"]))
    lines = script["lines"]
    show_lines = c.get("enabled", True)
    for li, ln in enumerate(lines if show_lines else []):
        words = [w for w in ln["text"].split() if w]
        if not words:
            continue
        t0, t1 = float(ln["t0"]), float(ln["t1"])
        hard_end = float(lines[li + 1]["t0"]) - 0.02 if li + 1 < len(lines) else total
        weights = [len(w) + 1.5 for w in words]
        tot = sum(weights)
        starts, acc = [], t0
        for wgt in weights:
            starts.append(acc)
            acc += (t1 - t0) * wgt / tot
        hl = _ovr_color(spk_color.get(ln["speaker"], colors[0]))
        for ci in range(0, len(words), wpc):
            chunk = words[ci: ci + wpc]
            for k in range(len(chunk)):
                wi = ci + k
                ws = starts[wi]
                if wi + 1 < len(words):
                    we = starts[wi + 1]
                else:
                    we = min(t1 + 0.25, hard_end)
                parts = []
                for j, w in enumerate(chunk):
                    w = _esc(w)
                    if j == k:
                        parts.append(f"{{\\c{hl}\\fscx112\\fscy112}}{w}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        parts.append(w)
                pop = "{\\fad(0,0)}" if k else "{\\t(0,80,\\fscx106\\fscy106)\\t(80,160,\\fscx100\\fscy100)}"
                events.append(f"Dialogue: 1,{_ts(ws)},{_ts(max(we, ws + 0.05))},Cap,,0,0,0,,{pop}{' '.join(parts)}")

    hook = (script.get("hook_text") or "").strip()
    if hook:
        events.append(f"Dialogue: 2,{_ts(0)},{_ts(min(float(c['title_seconds']), total))},Title,,0,0,0,,"
                      f"{{\\fad(120,200)}}{_esc(hook)}")
    if credit and c.get("show_credit", True):
        events.append(f"Dialogue: 0,{_ts(0)},{_ts(total)},Credit,,0,0,0,,{_esc(credit)}")

    out.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out
