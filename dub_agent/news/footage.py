"""Footage finder: real clips for each story (streams, VODs, gameplay, trailers, interviews), scanned scene by scene.

- YouTube search per topic query (plus any URLs you pass with --footage, incl. Kick VODs).
- Channels found are added to creators.yaml as status: pending (you approve the ones you keep).
- Long streams are not scanned end to end: only the loudest / most action-packed windows are (saves Gemini quota).
- HD is downloaded only for the parts the script actually uses.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..utils import ensure_dir, log, media_duration, read_json, run, warn, write_json

GAMING_WORDS = ("gta", "game", "gaming", "gameplay", "stream", "bgmi", "pubg", "valorant", "minecraft", "fortnite",
                "free fire", "esports", "playstation", "xbox", "nintendo", "steam", "rockstar", "call of duty", "cod",
                "roblox", "speedrun", "twitch", "kick", "live", "scrims", "lan")
ENT_WORDS = ("trailer", "teaser", "movie", "film", "song", "interview", "box office", "series", "netflix", "prime video",
             "bollywood", "hollywood", "actor", "actress", "premiere", "red carpet", "concert", "music video")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower())).strip()


def search_footage(topic: dict, cfg: dict, n_pick: int, extra_urls: list[str] | None = None) -> list[dict]:
    from .. import discover as D

    n = cfg.get("news", {})
    depth = int(n.get("footage_search_depth", 12))
    queries = list(topic.get("footage_queries") or [])[:5] or [topic.get("title", "")]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda q: D._flat(f"ytsearch{depth}:{q}", depth), queries))
    names = [_norm(x) for x in (topic.get("names") or []) if len(_norm(x)) > 2]
    gaming = topic.get("category") == "gaming"
    min_s, max_s = float(n.get("footage_min_seconds", 20)), float(n.get("footage_max_hours", 6)) * 3600
    seen, cands = set(), []
    for qi, lst in enumerate(results):
        for rank, e in enumerate(lst):
            vid = e.get("id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title = e.get("title") or ""
            t = _norm(title + " " + (e.get("channel") or ""))
            dur = e.get("duration") or 0
            if dur and not (min_s <= dur <= max_s):
                continue
            if (e.get("live_status") or "") in ("is_live", "is_upcoming"):
                continue
            name_hits = sum(1 for nm in names if nm and nm in t)
            if names and name_hits == 0:
                continue
            if gaming and not any(w in t for w in GAMING_WORDS) and name_hits < 2:
                continue            # gaming videos stay gaming
            if not gaming and any(w in t for w in ("gameplay", "walkthrough")):
                continue
            views = int(e.get("view_count") or 0)
            score = name_hits * 3 + (1.5 if rank < 3 else 0) + min(views, 5_000_000) / 1_000_000 - qi * 0.2
            cands.append({"id": vid, "url": e.get("url") if str(e.get("url", "")).startswith("http")
                          else f"https://www.youtube.com/watch?v={vid}", "title": title,
                          "channel": e.get("channel") or e.get("uploader") or "", "channel_id": e.get("channel_id") or "",
                          "channel_url": e.get("channel_url") or e.get("uploader_url") or "", "duration": dur,
                          "views": views, "score": score})
    cands.sort(key=lambda c: c["score"], reverse=True)
    picks = cands[:n_pick]
    for u in extra_urls or []:
        picks.insert(0, {"id": re.sub(r"\W+", "_", u)[-40:], "url": u, "title": u, "channel": "", "channel_id": "",
                         "channel_url": "", "duration": 0, "views": 0, "score": 99})
    D.report_youtube_errors("footage search")
    if n.get("auto_add_creators", True) and picks:
        from ..config import add_pending_creators
        added = add_pending_creators([{"channel": p["channel"], "channel_id": p["channel_id"],
                                       "channel_url": p["channel_url"], "title": p["title"]} for p in picks
                                      if p.get("channel_id") or p.get("channel_url")])
        if added:
            log(f"   added {len(added)} footage channel(s) to creators.yaml as pending")
    log(f"   footage for '{topic.get('title', '')[:50]}': " + "; ".join(f"{p['channel']}: {p['title'][:40]}" for p in picks))
    return picks


# ------------------------------------------------------------------ preview + scene log

_AI_OFF = {"off": False}


def _note_ai_error(e: Exception) -> None:
    m = str(e)
    if ("429" in m or "RESOURCE_EXHAUSTED" in m) and "All Gemini models failed" in m:
        if not _AI_OFF["off"]:
            warn("   Gemini limit reached for video scanning - using the free local scene finder for the rest of this run")
        _AI_OFF["off"] = True


def local_scenes(proxy, media, item: dict, dur: float) -> list[dict]:
    """No-AI scene log: the loudest / busiest moments (or evenly spaced ones), 8 s each."""
    from ..analysis import excitement
    picks = []
    try:
        hot = excitement(proxy, media / f"{item['id']}_hot.json", dur)
        for h in sorted(hot, key=lambda h: -h["score"]):
            t0 = max(0.0, min(dur - 8, h["t"] - 2))
            if all(abs(t0 - p) >= 10 for p in picks):
                picks.append(t0)
            if len(picks) >= 12:
                break
    except Exception as e:  # noqa: BLE001
        warn(f"   local scene finder failed: {str(e)[:80]}")
    if len(picks) < 4:
        start, end = dur * 0.04, dur * 0.96   # skip intros/outros
        picks += [start + (end - start) * k / 8 for k in range(8)]
    title = item.get("title", "")
    return [{"start": round(t, 2), "end": round(min(dur, t + 8), 2), "score": 6, "focus_x": 0.5,
             "category": "moment", "what": f"moment from '{title[:60]}' ({item.get('channel', '')})",
             "auto": True} for t in sorted(set(round(p, 1) for p in picks)) if t + 3 < dur]


def prepare_and_scan(item: dict, cfg: dict, work: Path, topic: dict) -> dict:
    """Small preview download -> scene log (Gemini). Long streams: only the hottest windows are scanned."""
    from .. import brain
    from ..analysis import excitement
    from ..download import chunk_proxy, cut_segment, fetch_proxy, make_proxy

    media = ensure_dir(work / "news_media")
    cache = media / f"{item['id']}_scenes.json"
    if cache.exists():
        cached = read_json(cache)
        if cached.get("scenes"):
            item.update(cached)
            return item
    lo = fetch_proxy(item["url"], media, item["id"])
    if not lo:
        import time
        time.sleep(15)          # short network/DNS drop -> one more try
        lo = fetch_proxy(item["url"], media, item["id"])
    if not lo:
        warn(f"   could not download preview of {item['url']}")
        item["scenes"] = []
        return item
    proxy = make_proxy(lo, media)
    dur = media_duration(proxy)
    item["duration"] = dur
    note = (f"This footage is for a NEWS video about: {topic.get('title')} - {topic.get('angle', '')}. "
            "In 'what', describe who is on screen (streamer face-cam reactions count - they are wanted here), "
            "what happens, and any on-screen text. Score = how useful the scene is for THIS story.")
    max_full = float(cfg.get("news", {}).get("scan_full_if_under_min", 25)) * 60
    scenes = []
    if _AI_OFF["off"]:
        pass                    # Gemini daily limit already hit this run -> local scene finder only
    elif dur <= max_full:
        for path, off, length in chunk_proxy(proxy, media, 20 * 60):
            try:
                scenes += brain.scan_chunk(path, off, length, cfg, [], note)
            except Exception as e:  # noqa: BLE001
                _note_ai_error(e)
                warn(f"   scan failed: {str(e)[:120]}")
    else:
        hot = excitement(proxy, media / f"{item['id']}_hot.json", dur)
        wins = []
        for h in sorted(hot, key=lambda h: -h["score"])[: int(cfg.get("news", {}).get("stream_windows", 6))]:
            s0 = max(0.0, h["t"] - 45)
            if any(abs(s0 - w) < 90 for w in wins):
                continue
            wins.append(s0)
        if not wins:
            wins = [dur * f for f in (0.1, 0.3, 0.5, 0.7)]
        log(f"   long video ({dur / 60:.0f} min): scanning {len(wins)} high-energy windows only")
        for k, s0 in enumerate(sorted(wins)):
            clip = media / f"{item['id']}_win{k}.mp4"
            cut_segment(proxy, clip, s0, min(dur, s0 + 120), proxy_height=None)
            try:
                scenes += brain.scan_chunk(clip, s0, min(120, dur - s0), cfg, [], note)
            except Exception as e:  # noqa: BLE001
                _note_ai_error(e)
                warn(f"   window scan failed: {str(e)[:120]}")
            if _AI_OFF["off"]:
                clip.unlink(missing_ok=True)
                break
            clip.unlink(missing_ok=True)
    if not scenes:
        scenes = local_scenes(proxy, media, item, dur)
        log(f"   AI scan gave nothing - picked {len(scenes)} moment(s) from loudness/motion instead")
    scenes.sort(key=lambda s: s["start"])
    item["scenes"] = scenes
    item["proxy"] = str(proxy)
    write_json(cache, {"scenes": scenes, "duration": dur, "proxy": str(proxy)})
    log(f"   {item['channel'] or item['id']}: {len(scenes)} scene(s) logged")
    return item


# ------------------------------------------------------------------ HD only for what is used

def download_hd(item: dict, ranges: list[tuple[float, float]], cfg: dict, work: Path) -> Path | None:
    """Download HD (default 720p). Short videos: whole file. Long ones: only the needed ranges, joined into one file
    whose timeline matches the original (gaps are skipped, so we return a time map too)."""
    import yt_dlp

    from ..discover import cookie_opts

    media = ensure_dir(work / "news_media")
    h = int(cfg.get("news", {}).get("footage_height", 720))
    full = media / f"{item['id']}_hd.mp4"
    if full.exists():
        return full
    dur = item.get("duration") or 0
    base = {"quiet": True, "noplaylist": True, "merge_output_format": "mp4", "socket_timeout": 30, "retries": 5,
            "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/bv*[height<={h}]+ba/b",
            **cookie_opts()}
    try:
        if dur and dur > 15 * 60 and ranges:
            from yt_dlp.utils import download_range_func
            merged = []
            for a, b in sorted(ranges):
                a, b = max(0.0, a - 2), min(dur, b + 2)
                if merged and a <= merged[-1][1] + 5:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            opts = dict(base, outtmpl=str(media / f"{item['id']}_sec_%(section_start)s.%(ext)s"),
                        download_ranges=download_range_func(None, [tuple(m) for m in merged]),
                        force_keyframes_at_cuts=True)
            with yt_dlp.YoutubeDL(opts) as y:
                y.download([item["url"]])
            item["sections"] = []
            for a, b in merged:
                f = next((p for p in media.glob(f"{item['id']}_sec_{int(a)}*") if p.suffix in (".mp4", ".mkv", ".webm")),
                         None) or next((p for p in media.glob(f"{item['id']}_sec_{a}*")), None)
                if f:
                    item["sections"].append({"start": a, "end": b, "file": str(f)})
            if item["sections"]:
                return Path(item["sections"][0]["file"])
        opts = dict(base, outtmpl=str(media / f"{item['id']}_hd.%(ext)s"))
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([item["url"]])
        if not full.exists():
            alt = next((p for p in media.glob(f"{item['id']}_hd.*")), None)
            if alt and alt.suffix != ".mp4":
                run(["ffmpeg", "-y", "-v", "error", "-i", str(alt), "-c", "copy", str(full)])
            elif alt:
                full = alt
        return full if full.exists() else None
    except Exception as e:  # noqa: BLE001
        warn(f"   HD download failed for {item['url']}: {str(e)[:150]}")
        return None


def locate(item: dict, t: float) -> tuple[str, float] | None:
    """Map an original-video time to (file, local time)."""
    if item.get("sections"):
        for s in item["sections"]:
            if s["start"] <= t < s["end"]:
                return s["file"], t - s["start"]
        best = min(item["sections"], key=lambda s: min(abs(t - s["start"]), abs(t - s["end"])))
        return best["file"], 0.0
    if item.get("hd"):
        return item["hd"], t
    if item.get("proxy"):
        return item["proxy"], t
    return None
