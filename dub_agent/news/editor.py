"""Audio-led editor: narration first (few TTS calls, pauses squeezed out), then the footage is cut to the voice.

Long video: 1920x1080, chapter cards, captions, source credits.  Short: 1080x1920, title bar, big word captions.
Raw beats play the original clip audio (max 10 s) with no narration.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

from .. import audio as A
from ..captions import _esc, _ts, build_ass
from ..render import _filter_path, make_thumbnail
from ..utils import (SAMPLE_RATE, ensure_dir, load_audio, log, media_duration, read_json, run, save_wav,
                     slugify, video_encoder_args, video_size, warn, write_json)
from ..voices import synth_lines
from .footage import locate

FPS = 30


def _profile(cfg: dict, fmt: str) -> dict:
    import copy

    c = copy.deepcopy(cfg)
    n = c.get("news", {})
    if fmt == "long":
        c["video"].update({"width": 1920, "height": 1080})
        c["captions"].update({"size": int(n.get("long_caption_size", 58)), "position_from_bottom": 70,
                              "words_per_chunk": 5, "title_size": 70, "title_margin": 120, "credit_margin": 30,
                              "enabled": bool(n.get("long_captions", True)), "show_credit": True})
    else:
        c["video"].update({"width": 1080, "height": 1920})
        c["captions"].update({"size": int(n.get("short_caption_size", 96)), "position_from_bottom": 620,
                              "words_per_chunk": 2, "title_size": 64, "title_margin": 210, "credit_margin": 120,
                              "enabled": True, "show_credit": True})
    return c


def _squeeze_pauses(src: Path, out: Path, cfg: dict) -> None:
    """Shorten every pause longer than ~0.35 s to 0.15 s and apply the narration speed."""
    speed = float(cfg.get("news", {}).get("speech_speed", 1.06))
    flt = ("silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration=0.35:"
           "stop_threshold=-45dB:stop_silence=0.15")
    if abs(speed - 1.0) > 0.01:
        flt += f",atempo={speed:.3f}"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", flt, "-ar", str(SAMPLE_RATE), "-ac", "1", str(out)])
    except Exception:  # noqa: BLE001
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", str(SAMPLE_RATE), "-ac", "1", str(out)])


def _layout_filter(fmt: str, sw: int, sh: int, focus_x: float, cfg: dict) -> str:
    W, H = (1920, 1080) if fmt == "long" else (1080, 1920)
    mode = cfg.get("news", {}).get("short_layout", "crop") if fmt == "short" else "fit"
    if fmt == "short" and mode == "crop" and sw / sh > 0.7:
        cw = int(round(sh * W / H / 2)) * 2
        x = int(min(max(float(focus_x if focus_x is not None else 0.5) * sw - cw / 2, 0), max(0, sw - cw)))
        return f"crop={cw}:{sh}:{x}:0,scale={W}:{H},setsar=1"
    # fit with blurred background (handles vertical clips in long videos and wide clips in blurfill shorts)
    return (f"split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,"
            f"eq=brightness=-0.08[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def _card_frame(out: Path, fmt: str, footage: list[dict]) -> Path:
    """Background for text cards: a blurred frame from the footage, or a dark gradient."""
    W, H = (1920, 1080) if fmt == "long" else (1080, 1920)
    for it in footage:
        src = it.get("hd") or it.get("proxy")
        if src and Path(src).exists():
            try:
                t = max(1.0, media_duration(src) * 0.3)
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.1f}", "-i", str(src), "-frames:v", "1",
                     "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,"
                            f"eq=brightness=-0.25", str(out)])
                return out
            except Exception:  # noqa: BLE001
                continue
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x101030:c1=0x401020:d=1",
         "-frames:v", "1", str(out)])
    return out


def _scene_lookup(footage: list[dict]) -> dict:
    refs = {}
    for vi, it in enumerate(footage):
        for sj, sc in enumerate(sorted(sorted(it.get("scenes") or [], key=lambda s: -float(s.get("score", 0)))[:40],
                                       key=lambda s: s["start"])):
            refs[f"v{vi + 1}#{sj + 1}"] = (vi, sc)
    return refs


def produce_news(vdir: Path, cfg: dict, engine: str | None = None) -> Path:
    vdir = Path(vdir)
    meta = read_json(vdir / "meta.json")
    fmt = meta["format"]
    script = read_json(vdir / "script.json")
    footage = read_json(vdir / "footage.json") if (vdir / "footage.json").exists() else []
    facts = read_json(vdir / "facts.json") if (vdir / "facts.json").exists() else {}
    pc = _profile(cfg, fmt)
    W, H = pc["video"]["width"], pc["video"]["height"]
    tmp = ensure_dir(Path(cfg.get("work_dir", "work")) / "news_tmp" / f"{vdir.parent.name}_{vdir.name}")
    beats = script["beats"]
    if not beats:
        raise RuntimeError("script has no beats")

    # ---------------- 1) narration: consecutive narrated beats -> chunks -> few TTS calls
    max_chars = int(cfg.get("news", {}).get("tts_chunk_chars", 900 if fmt == "long" else 2000))
    pieces, cur = [], []
    for i, b in enumerate(beats):
        if b.get("raw"):
            if cur:
                pieces.append({"kind": "narr", "beats": cur})
                cur = []
            pieces.append({"kind": "raw", "beats": [i]})
            continue
        if cur and sum(len(beats[j]["narration"]) for j in cur) + len(b["narration"]) > max_chars:
            pieces.append({"kind": "narr", "beats": cur})
            cur = []
        cur.append(i)
    if cur:
        pieces.append({"kind": "narr", "beats": cur})
    n = cfg.get("news", {})
    lines = [{"speaker": "narrator", "start": 0, "end": 0,
              "text": " ".join((beats[j].get("speak") or beats[j]["narration"]) for j in p["beats"]),
              "style": n.get("narrator_style", "energetic Hindi YouTube storyteller, fast continuous pace, "
                                               "excited and clear, no long pauses")}
             for p in pieces if p["kind"] == "narr"]
    vcfg = dict(cfg)
    vcfg["voice"] = dict(cfg["voice"])
    nv = dict(n.get("narrator_voices") or {})
    if any(_DEVANAGARI.search(ln["text"]) for ln in lines):
        # Hindi script -> Hindi voices read it naturally (English voices mangle "unhone" -> "unhon")
        nv["edge"] = nv.get("edge_hindi") or "hi-IN-MadhurNeural|+0Hz"
    for eng, v in nv.items():
        vcfg["voice"][eng] = dict(cfg["voice"].get(eng, {}), narrator_voice=v)
    tts_script = {"characters": [{"id": "narrator", "name": "Narrator", "gender": n.get("narrator_gender", "male"),
                                  "persona": "energetic Hindi YouTube news storyteller",
                                  "voice_type": "lively energetic commentator narrator"}], "lines": lines}
    if lines:
        log(f"Narration: {len(lines)} voice take(s) for {sum(1 for b in beats if not b.get('raw'))} beats")
        engine = engine or n.get("voice_engine") or cfg["voice"]["engine"]
        synth_lines(tts_script, engine, vcfg, Path(cfg["work_dir"]) / "tts_cache",
                    {"narrator": nv[engine]} if nv.get(engine) else None)

    # ---------------- 2) timeline (beats get times from their share of words in the take)
    refs = _scene_lookup(footage)
    t = 0.0
    li = 0
    timeline = []    # (beat_index, start, end)
    narr_audio = []  # (wav, start)
    raw_audio = []   # (vi, scene, start, dur)
    for p in pieces:
        if p["kind"] == "narr":
            ln = lines[li]
            li += 1
            sq = tmp / f"narr_{li:02d}.wav"
            _squeeze_pauses(Path(ln["audio"]), sq, cfg)
            dur = media_duration(sq)
            narr_audio.append((sq, t))
            words = [max(1, len(beats[j]["narration"].split())) for j in p["beats"]]
            tot = sum(words)
            acc = t
            for j, w in zip(p["beats"], words):
                d = dur * w / tot
                timeline.append((j, acc, acc + d))
                acc += d
            t += dur + 0.12
        else:
            j = p["beats"][0]
            d = float(beats[j].get("raw_seconds") or 6)
            ref = refs.get(str(beats[j].get("scene")))
            if ref:
                d = min(d, max(2.0, ref[1]["end"] - ref[1]["start"]), 10.0)
                raw_audio.append((ref[0], ref[1], t, d))
            timeline.append((j, t, t + d))
            t += d + 0.1
    total = t + (0.6 if fmt == "short" else 1.5)

    # ---------------- 3) shots: cut footage to the voice (new shot every few seconds)
    max_shot = float(n.get("max_shot_seconds", 4.5 if fmt == "short" else 6.0))
    used: dict = {}
    vid_scenes = {vi: [sc for (v, sc) in refs.values() if v == vi] for vi in range(len(footage))}
    card_bg = None
    shots = []

    def _covered(vi, sc):
        it = footage[vi]
        if it.get("sections"):
            return any(s["start"] <= sc["start"] < s["end"] for s in it["sections"])
        return bool(it.get("hd") or it.get("proxy"))

    spare = [r for r in refs.values() if _covered(*r)]
    spare.sort(key=lambda r: -float(r[1].get("score", 5) or 5))
    spare_i = 0
    for j, s0, e0 in timeline:
        b = beats[j]
        ref = refs.get(str(b.get("scene")))
        if not ref and not b.get("raw") and spare:
            ref = spare[spare_i % len(spare)]   # no matching scene -> best unused footage, never a blank card
            spare_i += 1
        span = e0 - s0
        k = 0
        pos = s0
        while pos < e0 - 0.05:
            d = min(max_shot, e0 - pos)
            if e0 - (pos + d) < 1.0:
                d = e0 - pos
            src = None
            if ref:
                vi, sc = ref
                pool = sorted(vid_scenes.get(vi) or [sc], key=lambda x: x["start"])
                pick = sc if k == 0 else pool[(pool.index(sc) + k) % len(pool)] if sc in pool else sc
                key = (vi, round(pick["start"], 1))
                off = used.get(key, 0.0)
                st = pick["start"] + off
                if b.get("raw"):
                    st = pick["start"]
                used[key] = off + d
                loc = locate(footage[vi], st)
                if loc and Path(loc[0]).exists():
                    src = (loc[0], loc[1], pick.get("focus_x", 0.5))
            shots.append({"beat": j, "start": pos, "dur": d, "src": src})
            pos += d
            k += 1
        if span <= 0:
            continue
    if not footage or not any(s["src"] for s in shots):
        warn("No usable footage - building the video from text cards.")
    W_, H_ = W, H
    seg_files = []
    venc = video_encoder_args(cfg, 20)
    for si, sh in enumerate(shots):
        out = tmp / f"shot_{si:04d}.mp4"
        if sh["src"]:
            f, lt, fx = sh["src"]
            try:
                fd = media_duration(f)
                lt = max(0.0, min(lt, fd - sh["dur"] - 0.05))
                sw, shh = video_size(f)
                flt = _layout_filter(fmt, sw, shh, fx, cfg)
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{lt:.3f}", "-i", str(f), "-t", f"{sh['dur']:.3f}", "-an",
                     "-filter_complex", f"[0:v]{flt},fps={FPS},format=yuv420p[v]", "-map", "[v]", *venc, str(out)])
                seg_files.append(out)
                continue
            except Exception as e:  # noqa: BLE001
                warn(f"   shot {si} failed ({str(e)[:80]}) - using a card")
        if card_bg is None:
            card_bg = _card_frame(tmp / "card_bg.png", fmt, footage)
        run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(card_bg), "-t", f"{sh['dur']:.3f}",
             "-vf", f"scale={W_}:{H_},zoompan=z='min(zoom+0.0008,1.08)':d=1:s={W_}x{H_}:fps={FPS},format=yuv420p",
             *venc, str(out)])
        seg_files.append(out)
    # tail
    tail = tmp / "shot_tail.mp4"
    last = seg_files[-1]
    run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.2", "-i", str(last), "-frames:v", "1", str(tmp / "tail.png")])
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(tmp / "tail.png"), "-t", f"{max(0.3, total - t):.2f}",
         "-vf", f"fps={FPS},format=yuv420p", *venc, str(tail)])
    seg_files.append(tail)
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in seg_files), encoding="utf-8")
    video = tmp / "video.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", video.name], cwd=tmp)

    # ---------------- 4) audio: narration + raw clip sound + ducked music, loudness normalised
    track = np.zeros(int((total + 1) * SAMPLE_RATE), dtype=np.float32)
    for wav, st in narr_audio:
        s = load_audio(wav)
        i = int(st * SAMPLE_RATE)
        track[i:i + len(s)] += s[: len(track) - i]
    peak = float(np.max(np.abs(track))) or 1.0
    track *= 0.85 / peak
    for vi, sc, st, d in raw_audio:
        loc = locate(footage[vi], sc["start"])
        if not loc:
            continue
        rw = tmp / f"raw_{int(st * 10)}.wav"
        try:
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{loc[1]:.3f}", "-i", str(loc[0]), "-t", f"{d:.3f}", "-vn",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), str(rw)])
            s = load_audio(rw)
            pk = float(np.max(np.abs(s))) or 1.0
            s = s * (0.8 / pk)
            fade = int(0.08 * SAMPLE_RATE)
            if len(s) > 2 * fade:
                s[:fade] *= np.linspace(0, 1, fade)
                s[-fade:] *= np.linspace(1, 0, fade)
            i = int(st * SAMPLE_RATE)
            track[i:i + len(s)] += s[: len(track) - i]
        except Exception as e:  # noqa: BLE001
            warn(f"   raw clip audio failed: {str(e)[:80]}")
    dialogue = tmp / "dialogue.wav"
    save_wav(dialogue, track[: int(total * SAMPLE_RATE)])
    mix = A.final_mix(dialogue, None, total, pc, tmp / "mix.wav", seed=str(vdir))

    # ---------------- 5) captions, title bar, cards, chapter titles, credits
    cap_lines = [{"speaker": "narrator", "text": beats[j]["narration"], "t0": s0, "t1": e0}
                 for j, s0, e0 in timeline if beats[j]["narration"]]
    creators = meta.get("creators") or []
    credit = ("Clips: " + ", ".join(c.get("name", "") for c in creators[:3] if c.get("name"))) if creators else ""
    ass = build_ass({"characters": [{"id": "narrator"}], "lines": cap_lines,
                     "hook_text": "".join(ch for ch in script.get("title", "") if ord(ch) < 0x2000).strip() if fmt == "short" else ""},
                    total, pc, vdir / "captions.ass", credit)
    extra_style = (f"Style: Card,{pc['captions']['font']},{90 if fmt == 'long' else 84},&H0000E6FF,&H00FFFFFF,"
                   f"&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,3,14,0,5,80,80,0,1")
    chap_style = (f"Style: Chapter,{pc['captions']['font']},{76 if fmt == 'long' else 64},&H00FFFFFF,&H00FFFFFF,"
                  f"&H001A1AE6,&H001A1AE6,-1,0,0,0,100,100,1,0,3,20,0,8,80,80,{140 if fmt == 'long' else 360},1")
    txt = ass.read_text(encoding="utf-8").replace("\n[Events]", f"\n{extra_style}\n{chap_style}\n\n[Events]")
    ev = []
    last_ch = None
    for j, s0, e0 in timeline:
        b = beats[j]
        if b.get("card"):
            ev.append(f"Dialogue: 3,{_ts(s0)},{_ts(min(e0, s0 + 3.5))},Card,,0,0,0,,{{\\fad(120,150)}}{_esc(str(b['card']).upper())}")
        if fmt == "long" and b.get("chapter") and b["chapter"] != last_ch:
            last_ch = b["chapter"]
            ev.append(f"Dialogue: 4,{_ts(s0)},{_ts(s0 + 3.2)},Chapter,,0,0,0,,{{\\fad(150,200)}}{_esc(b['chapter'])}")
    if fmt == "short":   # keep the title bar on screen for the whole Short
        txt = re.sub(r"(Dialogue: 2,[^,]*,)([^,]*)(,Title,)", lambda m: m.group(1) + _ts(total) + m.group(3), txt)
    ass.write_text(txt + "\n".join(ev) + "\n", encoding="utf-8")

    # ---------------- 6) final render
    title_slug = slugify(script.get("title", "video"), 40)
    out = vdir / f"{title_slug}.mp4"
    for old in vdir.glob("*.mp4"):
        old.unlink(missing_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video.resolve()), "-i", str(mix.resolve()),
         "-vf", f"ass='{_filter_path(ass, vdir)}'", "-map", "0:v", "-map", "1:a", "-t", f"{total:.3f}",
         *video_encoder_args(cfg), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name], cwd=vdir)
    hook_t = timeline[0][1] + 0.5 if timeline else 0.5
    thumb_text = "".join(ch for ch in (script.get("thumbnail_text") or script.get("title", "")) if ord(ch) < 0x2000)
    make_thumbnail(out, min(hook_t + 2, total - 0.3), thumb_text, vdir / "thumbnail.jpg", pc)
    write_pack(vdir, script, facts, meta, timeline, total, fmt, cfg)
    log(f"Ready: {out}  ({total:.0f}s)")
    return out


def write_pack(vdir: Path, script: dict, facts: dict, meta: dict, timeline, total: float, fmt: str, cfg: dict) -> None:
    beats = script["beats"]
    tags = []
    for t in list(script.get("hashtags") or []) + list(cfg.get("news", {}).get("extra_hashtags", [])):
        t = "#" + str(t).lstrip("#").replace(" ", "")
        if fmt == "long" and t.lower() == "#shorts":
            continue
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    chapters = ""
    if fmt == "long":
        rows, last = [], None
        for j, s0, _ in timeline:
            ch = beats[j].get("chapter")
            if ch and ch != last:
                last = ch
                m, s = divmod(int(s0), 60)
                rows.append(f"{m}:{s:02d} {ch}")
        if rows and not rows[0].startswith("0:00"):
            rows[0] = "0:00 " + rows[0].split(" ", 1)[1]
        chapters = "\n".join(rows)
    srcs = "\n".join(f"- {s.get('title', '')[:90]}: {s.get('url', '')}" for s in (facts.get("sources") or [])[:8])
    credits = "\n".join(f"- {c.get('name', '')}: {c.get('video_url') or c.get('channel_url', '')}"
                        for c in meta.get("creators") or [])
    confirmed = "\n".join(f"✔ {x}" for x in (facts.get("confirmed") or [])[:4])
    claimed = "\n".join(f"? {x}" for x in (facts.get("claimed") or [])[:3])
    parts = [script.get("description", ""), ""]
    if confirmed or claimed:
        parts += ["Kya confirm hai / kya sirf dawa hai:", confirmed, claimed, ""]
    if chapters:
        parts += ["Chapters:", chapters, ""]
    if srcs:
        parts += ["Sources:", srcs, ""]
    if credits:
        parts += ["Footage credits:", credits, ""]
    parts += ["Voice-over is AI-generated. Facts checked against the sources above.", "", " ".join(tags)]
    desc = "\n".join(parts)
    title = script.get("title", "")[:95]
    ig = "\n".join([title, "", script.get("description", ""), "",
                    ("Credits: " + ", ".join(c.get("name", "") for c in meta.get("creators") or [])) if meta.get("creators") else "",
                    "", " ".join(tags[:15])])
    (vdir / "upload.txt").write_text(
        f"=== YOUTUBE ({'LONG VIDEO' if fmt == 'long' else 'SHORT'}) ===\nTITLE:\n{title}\n\nDESCRIPTION:\n{desc}\n\n"
        f"TAGS:\n{', '.join(t.lstrip('#') for t in tags)}\n\n=== INSTAGRAM REELS ===\n{ig}\n\n"
        f"=== CHECK BEFORE POSTING ===\n[ ] Watched the whole video  [ ] Facts match the sources  "
        f"[ ] Footage channels approved in creators.yaml\nLength: {total:.0f}s\n", encoding="utf-8")
    write_json(vdir / "upload.json", {"title": title, "description": desc, "tags": tags, "duration": total})
    write_json(vdir / "moment.json", {"format": "episode" if fmt == "long" else "short",
                                      "creators": meta.get("creators") or [], "concept": script.get("description", "")})
