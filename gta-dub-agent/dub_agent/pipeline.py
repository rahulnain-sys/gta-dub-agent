"""End-to-end: URL -> full-video scene log -> planned Shorts and/or long story episodes -> dub -> render."""
from __future__ import annotations

import copy
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
from pathlib import Path

from . import audio as A
from . import brain
from .captions import build_ass
from .config import ROOT, find_creator, load_creators
from .download import assemble, chunk_proxy, cut_segment, fetch, fetch_proxy, is_url, make_proxy, probe
from .render import make_thumbnail, render_short
from .utils import ensure_dir, has_audio, log, media_duration, read_json, run, slugify, warn, write_json
from .voices import synth_lines


# ------------------------------------------------------------------ permission

def check_permission(meta: dict, allow_unlisted: bool) -> dict:
    if meta.get("local_file"):
        if not allow_unlisted:
            raise SystemExit(
                "Local file given. Re-run with --creator \"@handle\" (must be in creators.yaml) "
                "or --allow-unlisted if this is your own footage."
            )
        return {"name": "", "handle": "", "channel_url": ""}
    creator = find_creator(meta, load_creators())
    if creator:
        log(f"Permission found for creator: {creator.get('name') or creator.get('handle')} "
            f"({creator.get('permission', 'no note')})")
        return creator
    if allow_unlisted:
        warn("Channel is not in creators.yaml — continuing because --allow-unlisted was given. "
             "Make sure you really have permission!")
        return {"name": meta.get("channel", ""), "handle": "", "channel_url": meta.get("channel_url", "")}
    raise SystemExit(
        f"\nNo permission record for channel '{meta.get('channel') or meta.get('uploader') or '?'}'\n"
        f"   channel_id: {meta.get('channel_id')}\n   channel_url: {meta.get('channel_url')}\n"
        "Nothing was downloaded. If this creator gave you permission, add the channel_id above to their\n"
        "entry in creators.yaml (with a note of how they gave permission), then run again.\n"
        "(This guard keeps the channel safe from copyright strikes.)"
    )


def credit_text_multi(creators: list[dict]) -> str:
    whos = [c.get("handle") or c.get("name") for c in creators if (c.get("handle") or c.get("name"))]
    return ("Gameplay: " + ", ".join(whos[:3])) if whos else ""


def credit_text(creator: dict) -> str:
    who = creator.get("handle") or creator.get("name") or ""
    return f"Gameplay: {who}" if who else ""


# ------------------------------------------------------------------ main flows

# ------------------------------------------------------------------ series history

HISTORY_FILE = ROOT / "series_log.json"


def load_history() -> list[dict]:
    try:
        return read_json(HISTORY_FILE) if HISTORY_FILE.exists() else []
    except Exception:  # noqa: BLE001
        return []


def add_history(title: str, summary: str, fmt: str) -> None:
    hist = load_history()
    hist.append({"date": dt.date.today().isoformat(), "title": title, "summary": summary, "format": fmt})
    write_json(HISTORY_FILE, hist[-200:])


def series_characters(cfg: dict) -> list[dict]:
    if not cfg["series"].get("enabled", False):
        return []
    s = cfg["series"]
    mc = s["main_character"]
    out = [{"id": brain.main_id(cfg), "name": mc["name"], "gender": mc.get("gender", "male"),
            "persona": mc.get("persona", "")}]
    for c in s.get("recurring_cast", []) or []:
        if c.get("name"):
            out.append({"id": slugify(c["name"], 20).replace("-", "_"), "name": c["name"],
                        "gender": c.get("gender", "male"), "persona": c.get("persona", "")})
    return out


# ------------------------------------------------------------------ getting sources ready (fast path)

def prepare_source(source: str, cfg: dict, work: Path, start=None, end=None, allow_unlisted=False,
                   creator_handle: str | None = None, pool: ThreadPoolExecutor | None = None) -> dict:
    """Permission check -> start the HD download in the background -> get a small preview for the AI quickly."""
    if creator_handle:
        creator = find_creator({"uploader_id": creator_handle, "channel": creator_handle}, load_creators())
        if not creator and not allow_unlisted:
            raise SystemExit(f"{creator_handle} is not in creators.yaml. Add them first (with a permission note).")
        creator = creator or {"handle": creator_handle}
    if is_url(source):
        meta = probe(source)
        if not creator_handle:
            creator = check_permission(meta, allow_unlisted)
        hd_future = (pool or ThreadPoolExecutor(1)).submit(fetch, source, work, start, end)
        lo = None if (start is not None or end is not None) else fetch_proxy(source, work, meta["id"])
        return {"source": source, "meta": meta, "creator": creator, "hd": hd_future, "lo": lo}
    path, meta = fetch(source, work, start, end)
    if not creator_handle:
        creator = check_permission(meta, allow_unlisted)
    done = Future()
    done.set_result((path, meta))
    return {"source": source, "meta": meta, "creator": creator, "hd": done, "lo": None}


def _hd(item: dict) -> Path:
    path, meta = item["hd"].result()
    item["meta"].update({k: v for k, v in meta.items() if v})
    return path


# ------------------------------------------------------------------ main entry

def run_source(source: str, cfg: dict, **kw) -> list[Path]:
    return run_sources([source], cfg, **kw)


def run_sources(sources: list[str], cfg: dict, n_shorts: int | None = None, start=None, end=None,
                engine: str | None = None, style_note: str = "", allow_unlisted: bool = False,
                manual_moments: list | None = None, creator_handle: str | None = None,
                script_file: str | None = None, fmt: str = "shorts", episode_minutes: float | None = None,
                trends: list[str] | None = None, batch_name: str | None = None) -> list[Path]:
    """One or more videos -> scene log -> long episode and/or Shorts (scenes can mix videos)."""
    work = ensure_dir(cfg["work_dir"])
    if trends:
        cfg = dict(cfg)
        cfg["_trends"] = trends
    pool = ThreadPoolExecutor(max_workers=2)
    items = [prepare_source(src, cfg, work, start, end, allow_unlisted, creator_handle, pool) for src in sources]

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    name = batch_name or slugify(items[0]["meta"].get("title", "video"), 30)
    batch = ensure_dir(Path(cfg["output_dir"]) / f"{stamp}_{name}")
    ctx = {"items": items, "batch": batch, "history": load_history(), "style": style_note, "engine": engine,
           "script_file": script_file, "cfg_trends": trends}

    if manual_moments:
        srcs = [_hd(it) for it in items]
        ctx["srcs"] = srcs
        ctx["lens"] = [media_duration(p) for p in srcs]
        plans = [{"type": "single", "concept": "", "segments": [
            {"start": s, "end": e, "focus_x": 0.5, "facecam": None, "what": "", "src": 0}]} for s, e in manual_moments]
        return make_shorts(plans, cfg, ctx)

    scenes = scan_all(items, cfg, work, style_note)
    ctx["srcs"] = [_hd(it) for it in items]           # HD downloads finished in the background meanwhile
    ctx["lens"] = [media_duration(p) for p in ctx["srcs"]]
    write_json(batch / "all_scenes_found.json", scenes)
    write_json(batch / "sources.json", [it["meta"] for it in items])

    results: list[Path] = []
    if fmt in ("episode", "both"):
        results += make_episode(scenes, cfg, ctx, episode_minutes or float(cfg["episode"]["target_minutes"]))
    if fmt in ("shorts", "both"):
        ctx["history"] = load_history()
        max_shorts = n_shorts or int(cfg["brain"]["max_shorts_per_video"])
        log(f"\nShowrunner is planning up to {max_shorts} shorts from {len(scenes)} scenes...")
        title = " | ".join(it["meta"].get("title", "") for it in items)[:300]
        try:
            plans = brain.plan_shorts(scenes, cfg, max_shorts, title, style_note, ctx["history"])
        except Exception as ex:  # noqa: BLE001
            warn(f"Shorts planner failed ({str(ex)[:150]}); using the best scenes instead.")
            plans = []
        if not plans:
            plans = brain.fallback_shorts(scenes, cfg, max_shorts)
        write_json(batch / "plan_shorts.json", plans)
        for i, p in enumerate(plans, 1):
            segs = " + ".join(f"v{sg.get('src', 0) + 1}:{sg['start']:.0f}-{sg['end']:.0f}s" for sg in p["segments"])
            log(f"  Short {i} [{p.get('type', 'single')}]: {p.get('concept', '')}  ({segs})")
        results += make_shorts(plans, cfg, ctx)
    pool.shutdown(wait=False)
    log(f"\nDone. {len(results)} video(s) in: {batch}")
    return results


# ------------------------------------------------------------------ shorts

def _clip_from_segments(srcs: list[Path], segs: list[dict], folder: Path, cfg: dict):
    """Single segment -> use its source directly; several -> stitch them. Returns (clip, start, end, rel)."""
    if len(segs) == 1:
        sg = segs[0]
        return srcs[int(sg.get("src", 0))], sg["start"], sg["end"], [{**sg, "start": 0.0, "end": round(sg["end"] - sg["start"], 3)}]
    log(f"Stitching {len(segs)} scenes together...")
    clip = assemble(srcs, segs, folder / "_assembled.mp4", int(cfg["video"]["fps"]), cfg)
    rel, t = [], 0.0
    for sg in segs:
        d = sg["end"] - sg["start"]
        rel.append({**sg, "src_start": sg["start"], "src_end": sg["end"], "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return clip, 0.0, media_duration(clip), rel


def _clean_segments(segs: list[dict], lens: list[float]) -> list[dict]:
    out = []
    for sg in segs:
        sg = dict(sg)
        i = int(sg.get("src", 0))
        if not (0 <= i < len(lens)):
            continue
        sg["src"] = i
        sg["start"], sg["end"] = max(0.0, float(sg["start"])), min(lens[i], float(sg["end"]))
        if sg["end"] - sg["start"] >= 2:
            out.append(sg)
    return out


def _creators_for(segs: list[dict], ctx: dict) -> list[dict]:
    seen, out = set(), []
    for sg in segs:
        c = ctx["items"][int(sg.get("src", 0))]["creator"] or {}
        key = c.get("handle") or c.get("name") or c.get("channel_url")
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def make_shorts(plans: list[dict], cfg: dict, ctx: dict) -> list[Path]:
    results = []
    for k, plan in enumerate(plans, 1):
        segs = _clean_segments(plan["segments"], ctx["lens"])
        if not segs:
            continue
        sdir = ensure_dir(ctx["batch"] / f"short_{k:02d}")
        log(f"\n=== Short {k}/{len(plans)} ({plan.get('type', 'single')}): {plan.get('concept', '')}")
        clip, c_start, c_end, rel = _clip_from_segments(ctx["srcs"], segs, sdir, cfg)
        if ctx.get("script_file"):
            script = brain.normalise_script(read_json(ctx["script_file"]))
        else:
            preview = cut_segment(clip, sdir / "_ai_preview.mp4", c_start, c_end, proxy_height=480)
            try:
                script = brain.write_script(preview, _with_trends(cfg, ctx), plan.get("concept", ""), ctx["style"],
                                            rel if len(rel) > 1 else None, ctx["history"])
            except Exception as ex:  # noqa: BLE001
                warn(f"Script writing failed for short {k}: {ex}")
                continue
            finally:
                preview.unlink(missing_ok=True)
        moment = _moment(clip, c_start, c_end, rel, "short", plan.get("concept", ""), ctx, segs)
        write_json(sdir / "moment.json", moment)
        write_json(sdir / "script.json", script)
        try:
            results.append(produce(sdir, cfg, ctx["engine"]))
        except Exception as ex:  # noqa: BLE001
            warn(f"Short {k} failed while producing: {ex}")
    return results


def _with_trends(cfg: dict, ctx: dict) -> dict:
    if ctx.get("cfg_trends") and not cfg.get("_trends"):
        cfg = dict(cfg)
        cfg["_trends"] = ctx["cfg_trends"]
    return cfg


def _moment(clip, c_start, c_end, rel, fmt, concept, ctx, segs) -> dict:
    creators = _creators_for(segs, ctx)
    metas = [ctx["items"][i]["meta"] for i in sorted({int(s.get("src", 0)) for s in segs})]
    return {
        "source": str(Path(clip).resolve()), "start": c_start, "end": c_end, "segments": rel,
        "format": fmt, "concept": concept,
        "video_title": metas[0].get("title", "") if metas else "",
        "video_url": metas[0].get("webpage_url", "") if metas else "",
        "source_videos": [{"title": m.get("title", ""), "url": m.get("webpage_url", "")} for m in metas],
        "creator": creators[0] if creators else {}, "creators": creators,
    }


# ------------------------------------------------------------------ long episode

def _split_parts(rel: list[dict], part_sec: float) -> list[dict]:
    """Group consecutive segments into parts of ~part_sec (split very long segments)."""
    pieces = []
    for sg in rel:
        s0, e0 = sg["start"], sg["end"]
        while e0 - s0 > part_sec * 1.4:
            pieces.append({**sg, "start": s0, "end": s0 + part_sec})
            s0 += part_sec
        pieces.append({**sg, "start": s0, "end": e0})
    parts, cur = [], []
    for pc in pieces:
        if cur and (pc["end"] - cur[0]["start"]) > part_sec:
            parts.append(cur)
            cur = []
        cur.append(pc)
    if cur:
        parts.append(cur)
    out = []
    for grp in parts:
        p0 = grp[0]["start"]
        out.append({"start": p0, "end": grp[-1]["end"], "act": grp[0].get("act", 0),
                    "boundaries": [{**g, "start": round(g["start"] - p0, 2), "end": round(g["end"] - p0, 2)} for g in grp]})
    return out


def make_episode(scenes: list[dict], cfg: dict, ctx: dict, target_min: float) -> list[Path]:
    cfg = _with_trends(cfg, ctx)
    log(f"\nHead writer is planning a ~{target_min:.0f}-minute story episode...")
    title = " | ".join(it["meta"].get("title", "") for it in ctx["items"])[:300]
    try:
        outline = brain.plan_episode(scenes, cfg, target_min, title, ctx["style"], ctx["history"])
    except Exception as ex:  # noqa: BLE001
        warn(f"Episode planner failed ({str(ex)[:150]}); building a best-of episode instead.")
        outline = brain.fallback_episode(scenes, cfg, target_min)
    edir = ensure_dir(ctx["batch"] / "episode")
    write_json(edir / "outline.json", outline)
    log(f"  Episode [{outline.get('genre', '')}]: {outline.get('title', '')}  -  {outline.get('logline', '')}")
    segs = []
    co = outline.get("cold_open_segment")
    if co:
        co_clean = _clean_segments([co], ctx["lens"])
        if co_clean:
            segs.append({**co_clean[0], "act": 0, "cold_open": True})
            log(f"  Cold open hook: v{co_clean[0]['src'] + 1} {co_clean[0]['start']:.0f}-{co_clean[0]['end']:.0f}s")
        else:
            outline["cold_open_segment"] = None
    for ai, act in enumerate(outline["acts"]):
        log(f"  Act {ai + 1}: {act.get('name', '')} - {act.get('beat', '')} ({len(act['segments'])} scenes)")
        for sg in _clean_segments(act["segments"], ctx["lens"]):
            segs.append({**sg, "act": ai})
    if not segs:
        warn("Episode had no usable scenes.")
        return []
    clip, c_start, c_end, rel = _clip_from_segments(ctx["srcs"], segs, edir, cfg)
    log(f"  Episode footage: {(c_end - c_start) / 60:.1f} min")

    parts = _split_parts(rel, float(cfg["episode"]["part_seconds"]))
    known = series_characters(cfg)
    known_ids = {c["id"] for c in known}
    all_lines, all_sfx = [], []
    key = hashlib.sha1(json.dumps([(s.get("src", 0), s["start"], s["end"]) for s in segs]).encode()).hexdigest()[:10]
    cache_dir = ensure_dir(Path(cfg["work_dir"]) / f"episode_{key}")
    for i, part in enumerate(parts):
        cache = cache_dir / f"part_{i:02d}.json"
        if cache.exists():
            data = read_json(cache)
        else:
            log(f"  Writing part {i + 1}/{len(parts)} ({part['start'] / 60:.1f}-{part['end'] / 60:.1f} min)...")
            preview = cut_segment(clip, edir / "_part_preview.mp4", c_start + part["start"], c_start + part["end"],
                                  proxy_height=480)
            beat = outline["acts"][part["act"]].get("beat", "") if part["act"] < len(outline["acts"]) else ""
            try:
                data = brain.write_episode_part(preview, cfg, outline, i, len(parts), beat, part["boundaries"],
                                                known, all_lines, ctx["style"], ctx["history"])
            except Exception as ex:  # noqa: BLE001
                warn(f"  Part {i + 1} writing failed: {str(ex)[:150]} (it will be silent)")
                data = {"lines": [], "characters": []}
            finally:
                preview.unlink(missing_ok=True)
            if data.get("lines"):
                write_json(cache, data)
        for c in data.get("characters") or []:
            cid = str(c.get("id") or c.get("name") or "").strip().lower().replace(" ", "_")
            if cid and cid not in known_ids:
                known.append({**c, "id": cid})
                known_ids.add(cid)
        plen = part["end"] - part["start"]
        for ln in data.get("lines") or []:
            try:
                st, en = float(ln["start"]), float(ln.get("end", float(ln["start"]) + 2))
            except (KeyError, TypeError, ValueError):
                continue
            if st >= plen - 0.3:
                continue
            all_lines.append({**ln, "start": round(part["start"] + st, 2), "end": round(part["start"] + min(en, plen), 2)})
        for fx in data.get("sfx") or []:
            try:
                all_sfx.append({**fx, "at": round(part["start"] + float(fx["at"]), 2)})
            except (KeyError, TypeError, ValueError):
                pass
    if not all_lines:
        warn("No dialogue was written for the episode.")
        return []
    used = {ln.get("speaker", "").strip().lower().replace(" ", "_") for ln in all_lines}
    script = brain.normalise_script({
        "title": outline.get("title", ""), "hook_text": outline.get("thumbnail_text", ""),
        "description": outline.get("description", ""), "hashtags": outline.get("hashtags", []),
        "characters": [c for c in known if c["id"] in used], "lines": all_lines, "sfx": all_sfx,
        "genre": outline.get("genre", ""),
    })
    moment = _moment(clip, c_start, c_end, rel, "episode", outline.get("logline", ""), ctx, segs)
    write_json(edir / "moment.json", moment)
    write_json(edir / "script.json", script)
    try:
        return [produce(edir, cfg, ctx["engine"])]
    except Exception as ex:  # noqa: BLE001
        warn(f"Episode failed while producing: {ex}")
        return []


# ------------------------------------------------------------------ scanning (parallel, all videos)

def scan_all(items: list[dict], cfg: dict, work: Path, style_note: str = "") -> list[dict]:
    """Watch EVERY video completely (parts in parallel) and log every usable scene. Cached per part."""
    from .analysis import excitement

    chunk_sec = float(cfg["brain"]["chunk_minutes"]) * 60
    jobs = []
    for vi, it in enumerate(items):
        base = it["lo"] or _hd(it)          # small download if we have it, else the HD file
        proxy = make_proxy(base, work)
        dur = media_duration(proxy)
        hot = excitement(proxy, work / f"{proxy.stem}_hotspots.json", dur)
        vid = it["meta"].get("id") or Path(base).stem
        for ci, (path, off, length) in enumerate(chunk_proxy(proxy, work, chunk_sec), 1):
            jobs.append({"vi": vi, "ci": ci, "path": path, "off": off, "len": length, "hot": hot,
                         "cache": work / f"scenes_v4_{vid}_{int(chunk_sec)}_{ci:03d}.json"})
    total_min = sum(j["len"] for j in jobs) / 60
    workers = max(1, int(cfg["performance"]["scan_workers"]))
    log(f"AI is watching {len(items)} video(s), {total_min:.0f} min total, in {len(jobs)} part(s) "
        f"({workers} at a time)...")
    errors: list[str] = []

    def _scan(j):
        if j["cache"].exists():
            return j, read_json(j["cache"])
        found = brain.scan_chunk(j["path"], j["off"], j["len"], cfg, j["hot"], style_note)
        if found:
            write_json(j["cache"], found)
        return j, found

    scenes: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_scan, j) for j in jobs]
        for f in as_completed(futs):
            try:
                j, found = f.result()
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
                warn(f"  a part could not be scanned: {str(e)[:250]}")
                continue
            for sc in found:
                sc["src"] = j["vi"]
            log(f"  video {j['vi'] + 1} part {j['ci']}: {len(found)} scene(s)")
            scenes.extend(found)
    if not scenes and errors:
        raise SystemExit("\nThe AI could not watch the video(s). First error was:\n  " + errors[0][:600] +
                         "\nCheck your GEMINI_API_KEY / internet, then run again (finished parts are cached).")
    scenes.sort(key=lambda x: (x.get("src", 0), x["start"]))
    dedup: list[dict] = []
    for sc in scenes:
        if (dedup and sc.get("src") == dedup[-1].get("src") and sc["start"] < dedup[-1]["end"] - 2
                and abs(sc["start"] - dedup[-1]["start"]) < 8):
            if sc.get("score", 0) > dedup[-1].get("score", 0):
                dedup[-1] = sc
            continue
        dedup.append(sc)
    min_score = float(cfg["brain"].get("min_scene_score", 3))
    good = [sc for sc in dedup if sc.get("score", 0) >= min_score] or dedup
    if not good:
        warn("The AI logged no scenes - falling back to fixed 30-second windows.")
        for vi, it in enumerate(items):
            dur = media_duration(it["lo"] or _hd(it))
            t = 5.0
            while t + 20 < dur:
                good.append({"start": t, "end": min(dur, t + 30), "category": "other", "score": 4, "funny": 3,
                             "characters": 1, "standalone": False, "focus_x": 0.5, "facecam": None,
                             "what": "gameplay", "story_use": "", "src": vi})
                t += 30
    log(f"Usable scenes: {len(good)} (of {len(dedup)} logged)")
    return good


def produce(sdir: Path, cfg: dict, engine: str | None = None) -> Path:
    """Voice, mix, caption and render one short from its script.json + moment.json.
    Safe to re-run after editing script.json: unchanged lines reuse cached voices."""
    sdir = Path(sdir)
    moment = read_json(sdir / "moment.json")
    script = brain.normalise_script(read_json(sdir / "script.json"))
    src = Path(moment["source"])
    if not src.exists():
        raise SystemExit(f"Source video missing: {src}")
    if not script["lines"]:
        raise SystemExit(f"No dialogue lines in {sdir / 'script.json'}")
    engine = engine or script.get("voice_engine") or cfg["voice"]["engine"]
    if moment.get("format") == "episode":
        cfg = episode_profile(cfg)
    s, e = float(moment["start"]), float(moment["end"])
    clip_len = e - s
    tmp = ensure_dir(sdir / "_tmp")

    log("Generating voices...")
    fixed = script.get("casting") if script.get("voice_engine") == engine else None
    synth_lines(script, engine, cfg, Path(cfg["work_dir"]) / "tts_cache", fixed)

    total = A.fit_timeline(script, clip_len, cfg, tmp)
    src_left = media_duration(src) - s
    total = min(total, max(clip_len, src_left + 2.0))

    # whoosh between stitched scenes (if assets/sfx has a 'whoosh' file)
    for sg in (moment.get("segments") or [])[1:]:
        script.setdefault("sfx", []).append({"at": max(0.0, float(sg["start"]) - 0.15), "name": "whoosh", "auto": True})
    dialogue = A.build_dialogue(script, total, cfg, tmp / "dialogue.wav")
    script["sfx"] = [fx for fx in script.get("sfx", []) if not fx.get("auto")]
    game_audio = None
    if float(cfg["audio"].get("game_audio_volume", 0) or 0) > 0 and has_audio(src):
        game_audio = tmp / "game.wav"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.3f}", "-i", str(src), "-t", f"{total:.3f}",
             "-vn", "-ac", "1", "-ar", "48000", str(game_audio)])
    mix = A.final_mix(dialogue, game_audio, total, cfg, tmp / "mix.wav", seed=str(sdir))

    creator = moment.get("creator") or {}
    ass = build_ass(script, total, cfg, sdir / "captions.ass",
                    credit_text_multi(moment.get("creators") or ([creator] if creator else [])))

    masks = list(cfg["video"].get("extra_masks") or [])
    segments = moment.get("segments") or [{"start": 0.0, "end": clip_len,
                                           "focus_x": moment.get("focus_x", 0.5), "facecam": moment.get("facecam")}]

    title_slug = slugify(script.get("title", "short"), 36)
    out = sdir / f"{title_slug}.mp4"
    for old in sdir.glob("*.mp4"):
        if not old.name.startswith("_"):
            old.unlink(missing_ok=True)
    render_short(src, s, total, mix, ass, out, cfg, moment.get("focus_x", 0.5), masks, segments)

    peak = script["lines"][-1]["t0"] if script["lines"] else total / 2
    thumb_text = "".join(ch for ch in (script.get("hook_text") or script.get("title", "")) if ord(ch) < 0x2000)
    make_thumbnail(out, min(peak, total - 0.2), thumb_text, sdir / "thumbnail.jpg", cfg)
    write_upload_pack(sdir, script, moment, cfg, total)

    # save the script back with resolved voices/timings so edits keep casting
    keep = {k: v for k, v in script.items()}
    keep["lines"] = [{k: v for k, v in ln.items() if k not in ("audio", "final_audio", "audio_dur")}
                     for ln in script["lines"]]
    write_json(sdir / "script.json", keep)
    add_history(script.get("title", ""), moment.get("concept", "") or script.get("description", ""),
                moment.get("format", "short"))
    log(f"Ready: {out}")
    return out


def episode_profile(cfg: dict) -> dict:
    """Settings for 16:9 long-form episodes."""
    c = copy.deepcopy(cfg)
    ep = c["episode"]
    c["video"].update({"width": int(ep["width"]), "height": int(ep["height"]), "layout": "landscape"})
    c["captions"].update({"size": int(ep["caption_size"]), "position_from_bottom": int(ep["caption_bottom"]),
                          "words_per_chunk": int(ep["words_per_chunk"]), "title_size": 64, "title_seconds": 4,
                          "title_margin": 110, "credit_margin": 36,
                          "enabled": bool(ep.get("captions", True))})
    return c


def write_upload_pack(sdir: Path, script: dict, moment: dict, cfg: dict, total: float) -> None:
    up = cfg["upload"]
    creator = moment.get("creator") or {}
    tags = []
    for t in list(script.get("hashtags") or []) + list(up.get("extra_hashtags") or []):
        t = "#" + str(t).lstrip("#").replace(" ", "")
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    creators = moment.get("creators") or ([creator] if creator else [])
    credit_rows = []
    for c in creators:
        w = c.get("name") or c.get("handle") or ""
        if w:
            link = c.get("channel_url") or ""
            credit_rows.append(f"🎮 Gameplay: {w} {('- ' + link) if link else ''} (used with permission)")
    credit_line = "\n".join(credit_rows)
    who = ", ".join(c.get("name") or c.get("handle") or "" for c in creators)
    ai_line = "🎙️ Funny voices are AI-generated. Comedy dub / parody - not affiliated with Rockstar Games." \
        if up.get("disclose_ai_voices", True) else ""
    title = (script.get("title") or "GTA 6 but desi 😂").strip()[:95]
    desc = "\n".join(x for x in [script.get("description", ""), "", credit_line, ai_line, "", " ".join(tags)] if x is not None)
    ig = "\n".join(x for x in [title, "", credit_line, ai_line, "", " ".join(tags[:12])] if x is not None)
    kind = "YOUTUBE (LONG VIDEO)" if moment.get("format") == "episode" else "YOUTUBE SHORTS"
    txt = f"""=== {kind} ===
TITLE:
{title}

DESCRIPTION:
{desc}

TAGS (paste in Tags box):
{', '.join(t.lstrip('#') for t in tags)}

=== INSTAGRAM REELS / FACEBOOK ===
{ig}

=== CHECKLIST BEFORE POSTING ===
[ ] Watched the whole video (voices, timing, captions OK)
[ ] Creator credited / tagged ({who or 'n/a'})
[ ] No story spoilers or cutscenes, no graphic violence in first 7 sec
[ ] Video length: {total:.1f}s
"""
    (sdir / "upload.txt").write_text(txt, encoding="utf-8")
    write_json(sdir / "upload.json", {"title": title, "description": desc, "tags": tags, "duration": total})
