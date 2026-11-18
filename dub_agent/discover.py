"""Discovery - precise and fresh.

Two jobs:
  1. watchlist_candidates(): fresh GTA gameplay uploads from YOUR permitted creators (YouTube + Kick) -> footage.
  2. trend_radar(): today's fastest-growing real GTA gameplay videos on YouTube -> story ideas + outreach list.

Every video goes through the same filter funnel:
  newest-first search -> game filter (GTA V now, GTA VI after launch) -> junk-word filter (news, leaks, trailers,
  vlogs, reactions, memes, AI/cartoon mashups, other games) -> real upload time + age limit -> length/live/category
  -> Gemini looks at the THUMBNAIL + title/description and confirms: real gameplay, faceless, not AI/meme/vlog/news.
Every decision (and the reason) is written to discover_report.csv so you can see why a video was kept or rejected.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .config import ROOT, find_creator, load_creators
from .utils import log, read_json, warn, write_json

PROCESSED_FILE = ROOT / "processed_videos.json"
REPORT_FILE = ROOT / "discover_report.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept": "application/json"}

GAME_ALIASES = {"gta5": ["gta 5", "gta v", "gtav"], "gta6": ["gta 6", "gta vi", "gtavi"]}
GAME_LABEL = {"gta5": "GTA V (GTA 5 / GTA V / GTAV)", "gta6": "GTA VI (GTA 6 / GTA VI / GTAVI)"}
DEFAULT_TOPICS = ["gameplay", "funny gameplay", "mission gameplay", "no commentary", "roleplay", "gameplay hindi",
                  "mods gameplay", "online gameplay", "police chase", "free roam"]

GAME_TERMS = {
    "gta5": ["gta 5", "gta v", "gta online", "grand theft auto 5", "grand theft auto v", "los santos", "gta 5 rp"],
    "gta6": ["gta 6", "gta vi", "grand theft auto 6", "grand theft auto vi"],
}
OTHER_GAMES = ["gta 4", "gta iv", "san andreas", "gta sa", "vice city stories", "gta 3", "gta iii", "liberty city",
               "beamng", "minecraft", "roblox", "free fire", "bgmi", "pubg", "fortnite", "cyberpunk", "red dead",
               "rdr2", "rdr 2", "watch dogs", "mafia definitive", "mafia 2", "mafia 3", "saints row", "indian bike driving", "car parking",
               "farming simulator", "euro truck", "forza", "need for speed"]
NON_GAMEPLAY = ["trailer", "leak", "leaks", "leaked", "leaker", "news", "rumor", "rumour", "theory", "theories",
                "explained", "reaction", "reacts", "react", "unboxing", "downloaded", "download", "codes", "pre order",
                "preorder", "pre-order", "gaming room", "setup", "review", "iceberg", "top 10", "top 5",
                "podcast", "interview", "vs real life", "in real life", "irl", "vlog", "comparison", "release date",
                "countdown", "giveaway", "everything we know", "confirmed", "map size", "ranking", "tier list",
                "tutorial", "how to install", "how to download", "money glitch", "settings"]
AI_MEME = ["meme", "memes", "best moments", "funny fails", "compilation", "ai generated", "ai-generated", "animation",
           "animated", "cartoon", "shinchan", "shin chan", "doraemon", "lightning mcqueen", "skibidi", "cocomelon",
           "tom and jerry", "bluey", "sigma", "#shorts", "shorts", "minions", "spongebob"]


# ------------------------------------------------------------------ text filters

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#]+", " ", (text or "").lower())).strip()


def _term_re(term: str) -> re.Pattern:
    parts = [re.escape(p) for p in _norm(term).split()]
    return re.compile(r"(?<![a-z0-9])" + r"\s*".join(parts) + r"(?![a-z0-9])")


_RE_CACHE: dict[str, re.Pattern] = {}


def _has(text: str, terms: list[str]) -> str | None:
    t = _norm(text)
    for term in terms:
        rx = _RE_CACHE.get(term) or _RE_CACHE.setdefault(term, _term_re(term))
        if rx.search(t):
            return term
    return None


def target_game(cfg: dict) -> str:
    d = cfg["discover"]
    tg = d.get("target_game", "auto")
    if tg in ("gta5", "gta6"):
        return tg
    try:
        launch = dt.date.fromisoformat(str(d.get("gta6_launch", "2026-11-19")))
    except ValueError:
        launch = dt.date(2026, 11, 19)
    return "gta6" if dt.date.today() >= launch else "gta5"


def text_verdict(title: str, desc: str, cfg: dict) -> tuple[bool, str]:
    """Cheap keyword checks on title (+ start of description)."""
    game = target_game(cfg)
    d = cfg["discover"]
    if not _has(title, GAME_TERMS[game]):
        if game == "gta6" or not _has(title + " " + (desc or "")[:200], GAME_TERMS[game]):
            return False, f"not {GAME_LABEL[game].split(' (')[0]}"
    if game == "gta5" and _has(title, GAME_TERMS["gta6"]) and not _has(title, GAME_TERMS["gta5"]):
        return False, "GTA 6 video (GTA V wanted until launch)"
    if game == "gta6" and not d.get("allow_gta5_after_launch", False) and _has(title, GAME_TERMS["gta5"]) \
            and not _has(title, GAME_TERMS["gta6"]):
        return False, "GTA V video (GTA VI wanted)"
    bad = _has(title, OTHER_GAMES)
    if bad:
        return False, f"other game: {bad}"
    bad = _has(title, NON_GAMEPLAY + list(d.get("exclude_terms", [])))
    if bad:
        return False, f"not gameplay: '{bad}'"
    bad = _has(title, AI_MEME)
    if bad:
        return False, f"AI/meme/compilation: '{bad}'"
    return True, "ok"


# ------------------------------------------------------------------ processed log

def load_processed() -> set[str]:
    try:
        return set(read_json(PROCESSED_FILE)) if PROCESSED_FILE.exists() else set()
    except Exception:  # noqa: BLE001
        return set()


def mark_processed(ids: list[str]) -> None:
    done = load_processed() | set(ids)
    write_json(PROCESSED_FILE, sorted(done)[-5000:])


# ------------------------------------------------------------------ yt-dlp helpers

YT_ERRORS: list[str] = []


class _Quiet:
    """Keep yt-dlp's console quiet but remember its errors so we can explain them."""
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        YT_ERRORS.append(str(msg))


def cookie_opts() -> dict:
    """Use your YouTube login so YouTube doesn't treat the agent as a bot."""
    import os

    f = (os.environ.get("DUB_COOKIES_FILE") or "").strip()
    if f and Path(f).exists():
        return {"cookiefile": f}
    b = (os.environ.get("DUB_COOKIES_BROWSER") or "").strip()
    return {"cookiesfrombrowser": (b,)} if b else {}


def using_cookies() -> bool:
    return bool(cookie_opts())


def report_youtube_errors(stage: str) -> None:
    if not YT_ERRORS:
        return
    errs = list(YT_ERRORS)
    YT_ERRORS.clear()
    bot = [e for e in errs if "not a bot" in e.lower() or "sign in" in e.lower() or "429" in e]
    first = (bot or errs)[0]
    first = re.sub(r"^ERROR:\s*", "", first)[:220]
    warn(f"{stage}: YouTube refused {len(errs)} request(s). Example: {first}")
    if bot:
        warn("YouTube thinks this is a bot. Fix: add your YouTube cookies (README: 'YouTube bot check') "
             "- set discover.cookies_file: cookies.txt or discover.cookies_from_browser: firefox in config.yaml.")


def _ydl(extra: dict | None = None):
    import yt_dlp

    opts = {"quiet": True, "skip_download": True, "no_warnings": True, "ignoreerrors": True, "logger": _Quiet(),
            "socket_timeout": 30, "sleep_interval_requests": 0.4}
    opts.update(cookie_opts())
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _flat(url: str, n: int) -> list[dict]:
    try:
        with _ydl({"extract_flat": "in_playlist", "playlistend": n}) as y:
            info = y.extract_info(url, download=False) or {}
        return [e for e in (info.get("entries") or []) if e and e.get("id")]
    except Exception:  # noqa: BLE001
        return []


def _sp(max_age_h: float) -> str:
    """YouTube search filter: sort by upload date, videos only, uploaded today / this week."""
    return "CAISBAgCEAE%3D" if max_age_h <= 24 else "CAISBAgDEAE%3D"


def _search(query: str, n: int, max_age_h: float) -> list[dict]:
    import urllib.parse

    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query) + "&sp=" + _sp(max_age_h))
    found = _flat(url, n)
    if not found:  # fallback: normal (relevance) search; the age filter still applies later
        found = _flat(f"ytsearch{n}:{query}", n)
    return found


def search_queries(cfg: dict) -> list[str]:
    d = cfg["discover"]
    game = target_game(cfg)
    topics = d.get("trend_topics") or DEFAULT_TOPICS
    qs = [f"{alias} {topic}" for topic in topics for alias in GAME_ALIASES[game]]
    qs += list(d.get(f"extra_queries_{game}") or [])
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _full(video_id_or_url: str) -> dict | None:
    url = video_id_or_url if video_id_or_url.startswith("http") else f"https://www.youtube.com/watch?v={video_id_or_url}"
    try:
        with _ydl() as y:
            return y.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None


def _record(full: dict, platform: str = "youtube", creator: str = "") -> dict:
    vid = full.get("id")
    return {
        "platform": platform, "id": vid, "url": full.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
        "title": full.get("title", ""), "description": (full.get("description") or "")[:600],
        "tags": (full.get("tags") or [])[:15], "categories": full.get("categories") or [],
        "channel": full.get("channel") or full.get("uploader") or "", "channel_id": full.get("channel_id") or "",
        "channel_url": full.get("channel_url") or full.get("uploader_url") or "",
        "subscribers": full.get("channel_follower_count") or 0,
        "views": int(full.get("view_count") or 0),
        "timestamp": full.get("timestamp") or full.get("release_timestamp"),
        "duration": full.get("duration") or 0, "live_status": full.get("live_status") or "",
        "thumbnail": full.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
        "creator": creator or full.get("channel") or "",
    }


def _hard_filters(v: dict, cfg: dict, max_age_h: float) -> tuple[bool, str]:
    d = cfg["discover"]
    if v.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return False, f"live status: {v['live_status']}"
    dur_min = (v.get("duration") or 0) / 60
    if not dur_min:
        return False, "unknown length"
    if dur_min < float(d["min_duration_min"]):
        return False, f"too short ({dur_min:.0f} min)"
    if dur_min > float(d["max_duration_min"]):
        return False, f"too long ({dur_min:.0f} min)"
    cats = [c.lower() for c in v.get("categories") or []]
    if cats and "gaming" not in cats:
        return False, f"category: {', '.join(cats)}"
    if not v.get("timestamp"):
        return False, "no upload time"
    age_h = (time.time() - v["timestamp"]) / 3600
    if age_h > max_age_h:
        return False, f"too old ({age_h / 24:.1f} days)" if age_h > 48 else f"too old ({age_h:.0f} h)"
    v["age_hours"] = round(max(age_h, 0.3), 1)
    v["velocity"] = round(v["views"] / max(age_h, 0.5), 1)
    return True, "ok"


# ------------------------------------------------------------------ AI check (thumbnail + metadata)

CLASSIFY_CACHE = ROOT / "work" / "classify_cache.json"


def ai_classify(videos: list[dict], cfg: dict) -> None:
    """Adds v['ai'] = {gameplay, faceless, ai_or_meme, vlog_news, game, quality, reason} using Gemini vision."""
    if not videos or not cfg["discover"].get("ai_check", True):
        return
    try:
        from google.genai import types

        from . import brain
        client = brain._client()
    except BaseException as e:  # noqa: BLE001  (SystemExit when no key)
        warn(f"AI check skipped ({str(e)[:100]}) - using keyword filters only.")
        return
    cache = {}
    if CLASSIFY_CACHE.exists():
        try:
            cache = read_json(CLASSIFY_CACHE)
        except Exception:  # noqa: BLE001
            cache = {}
    todo = [v for v in videos if v["id"] not in cache]
    limit = int(cfg["discover"].get("ai_check_max", 40))
    if len(todo) > limit:
        todo.sort(key=lambda v: v.get("velocity", 0), reverse=True)
        skipped = todo[limit:]
        todo = todo[:limit]
        for v in skipped:
            v["ai"] = {"gameplay": False, "reason": f"not AI-checked (outside top {limit} by views/hour)",
                       "skipped": True}
    game = target_game(cfg)
    gname = "GTA V (GTA 5 / GTA Online)" if game == "gta5" else "GTA VI (GTA 6)"

    def _thumb(url):
        try:
            r = requests.get(url, headers={"User-Agent": UA["User-Agent"]}, timeout=15)
            if r.ok and r.content:
                return r.content
        except Exception:  # noqa: BLE001
            pass
        return None

    batches = [todo[i:i + 8] for i in range(0, len(todo), 8)]
    if batches:
        log(f"AI is checking {len(todo)} video thumbnails ({len(batches)} batches, 3 at a time)...")

    def _one_batch(idx_batch):
        idx, batch = idx_batch
        with ThreadPoolExecutor(max_workers=8) as ex:
            thumbs = list(ex.map(lambda v: _thumb(v.get("thumbnail", "")) if v.get("thumbnail") else None, batch))
        parts = [f"""
You are filtering YouTube/Kick videos for a channel that needs RAW {gname} GAMEPLAY footage (we mute the audio and
dub our own story). For each video, judge from the thumbnail + metadata:
- gameplay: true only if the video is mostly actual in-game footage of {gname} (missions, free roam, driving, chases,
  heists, roleplay, mods gameplay). False for news, leaks, trailers, theories, reactions, unboxing, buying/downloading
  the game, gaming-room/setup videos, IRL vlogs, podcasts, tier lists.
- faceless: true if there is no big real-human face/webcam as the main subject (a small corner facecam is acceptable).
- ai_or_meme: true for AI-generated/cartoon-character mashups, meme compilations, 'best moments #NN' clip
  compilations, Shinchan/Doraemon style edits, reupload compilations.
- vlog_news: true for talking-head, news, commentary-only, IRL content.
- game: "gta5", "gta6" or "other" (what is actually shown).
- quality 1-10: how good this footage would be for building a story (clear characters, action, variety).
Return JSON only: {{"results": [{{"k": 0, "gameplay": true, "faceless": true, "ai_or_meme": false,
 "vlog_news": false, "game": "gta5", "quality": 7, "reason": "short reason"}}]}}
"""]
        for k, (v, th) in enumerate(zip(batch, thumbs)):
            parts.append(f"[{k}] title: {v['title']} | channel: {v.get('channel', '')} | length: "
                         f"{(v.get('duration') or 0) / 60:.0f} min | tags: {', '.join(v.get('tags', [])[:8])} | "
                         f"description: {v.get('description', '')[:250].replace(chr(10), ' ')}")
            if th:
                parts.append(types.Part.from_bytes(data=th, mime_type="image/jpeg"))
        try:
            data = brain._generate(client, cfg, parts)
            for r in data.get("results", []):
                k = int(r.get("k", -1))
                if 0 <= k < len(batch):
                    cache[batch[k]["id"]] = r
            log(f"   AI check batch {idx + 1}/{len(batches)} done")
        except Exception as e:  # noqa: BLE001
            warn(f"AI check batch {idx + 1} failed ({str(e)[:150]}) - those videos use keyword filters only.")

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(_one_batch, list(enumerate(batches))))
    try:
        CLASSIFY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        write_json(CLASSIFY_CACHE, dict(list(cache.items())[-4000:]))
    except Exception:  # noqa: BLE001
        pass
    for v in videos:
        if v["id"] in cache:
            v["ai"] = cache[v["id"]]


def ai_verdict(v: dict, cfg: dict) -> tuple[bool, str]:
    a = v.get("ai")
    if not a:
        return True, "keyword-only (no AI check)"
    if a.get("skipped"):
        return False, a.get("reason", "not AI-checked")
    game = target_game(cfg)
    if not a.get("gameplay"):
        return False, f"AI: not gameplay ({a.get('reason', '')})"
    if a.get("ai_or_meme"):
        return False, f"AI: AI/meme/compilation ({a.get('reason', '')})"
    if a.get("vlog_news"):
        return False, f"AI: vlog/news ({a.get('reason', '')})"
    if cfg["discover"].get("faceless_only", True) and not a.get("faceless", True):
        return False, f"AI: face-cam/creator on screen ({a.get('reason', '')})"
    if a.get("game") and a["game"] != game:
        return False, f"AI: shows {a['game']} not {game}"
    if float(a.get("quality", 5) or 0) < float(cfg["discover"].get("min_quality", 5)):
        return False, f"AI: low story quality {a.get('quality')}"
    return True, f"AI ok (quality {a.get('quality')})"


# ------------------------------------------------------------------ funnel + report

def _funnel(videos: list[dict], cfg: dict, max_age_h: float, label: str) -> list[dict]:
    report = []
    stage1 = []
    for v in videos:
        ok, why = text_verdict(v["title"], v.get("description", ""), cfg)
        if ok:
            ok, why = _hard_filters(v, cfg, max_age_h)
        (stage1 if ok else report).append(v)
        if not ok:
            v["verdict"] = f"REJECT {why}"
    ai_classify(stage1, cfg)
    kept = []
    for v in stage1:
        ok, why = ai_verdict(v, cfg)
        v["verdict"] = ("KEEP " if ok else "REJECT ") + why
        (kept if ok else report).append(v)
    key = "velocity" if cfg["discover"].get("rank_by", "velocity") == "velocity" else "views"
    kept.sort(key=lambda v: v.get(key, 0), reverse=True)
    _write_report(label, kept + report)
    log(f"{label}: {len(videos)} checked -> {len(stage1)} passed keyword/age/length -> {len(kept)} confirmed gameplay")
    return kept


def _write_report(label: str, rows: list[dict]) -> None:
    new = not REPORT_FILE.exists()
    try:
        with open(REPORT_FILE, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["checked_at", "list", "verdict", "views_per_hour", "views", "age_h", "minutes",
                            "channel", "title", "url"])
            now = time.strftime("%Y-%m-%d %H:%M")
            for v in rows:
                w.writerow([now, label, v.get("verdict", ""), v.get("velocity", ""), v.get("views", ""),
                            v.get("age_hours", ""), round((v.get("duration") or 0) / 60, 1), v.get("channel", ""),
                            v.get("title", ""), v.get("url", "")])
    except PermissionError:
        warn("discover_report.csv is open in Excel - close it to update the report.")


def _print(vs: list[dict], n: int = 12) -> None:
    for v in vs[:n]:
        print(f"   {v.get('velocity', 0):>9,.0f} views/h  {v.get('views', 0):>9,}  {v.get('age_hours', '?'):>5}h  "
              f"{(v.get('duration') or 0) / 60:>4.0f}m  {v.get('channel', '')[:20]:<20}  {v['title'][:70]}")


# ------------------------------------------------------------------ 1) your creators -> footage

def _yt_base(creator: dict) -> str | None:
    cid = (creator.get("channel_id") or "").strip()
    if cid:
        return cid.rstrip("/") if cid.startswith("http") else f"https://www.youtube.com/channel/{cid}"
    url = (creator.get("channel_url") or "").strip()
    if url and "youtube.com" in url:
        return url.rstrip("/")
    h = (creator.get("handle") or "").strip()
    return f"https://www.youtube.com/@{h.lstrip('@')}" if h else None


CID_CACHE = ROOT / "work" / "channel_ids.json"
_CID_LOCK = __import__("threading").Lock()


def _channel_id(creator: dict) -> str | None:
    cid = str(creator.get("channel_id") or "").strip()
    m = re.search(r"(UC[0-9A-Za-z_-]{22})", cid)
    if m:
        return m.group(1)
    base = _yt_base(creator)
    if not base:
        return None
    m = re.search(r"(UC[0-9A-Za-z_-]{22})", base)
    if m:
        return m.group(1)
    with _CID_LOCK:
        cache = read_json(CID_CACHE) if CID_CACHE.exists() else {}
    if base in cache:
        return cache[base]
    try:
        with _ydl({"extract_flat": "in_playlist", "playlistend": 1}) as y:
            info = y.extract_info(f"{base}/videos", download=False) or {}
        cid = info.get("channel_id") or ""
    except Exception:  # noqa: BLE001
        cid = ""
    if cid:
        with _CID_LOCK:
            cache = read_json(CID_CACHE) if CID_CACHE.exists() else {}
            cache[base] = cid
            CID_CACHE.parent.mkdir(parents=True, exist_ok=True)
            write_json(CID_CACHE, cache)
    return cid or None


def _rss(cid: str) -> list[dict] | None:
    """Latest ~15 uploads from YouTube's public RSS feed: light, fast, includes upload time + views."""
    import xml.etree.ElementTree as ET

    try:
        r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                         headers={"User-Agent": UA["User-Agent"]}, timeout=20)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
    except Exception:  # noqa: BLE001
        return None
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", default="", namespaces=ns)
        link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "") or ""
        pub = e.findtext("a:published", default="", namespaces=ns)
        stats = e.find("m:group/m:community/m:statistics", ns)
        thumb = e.find("m:group/m:thumbnail", ns)
        try:
            ts = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
        out.append({"id": vid, "title": e.findtext("a:title", default="", namespaces=ns),
                    "is_short": "/shorts/" in link, "timestamp": ts,
                    "views": int(stats.get("views", 0)) if stats is not None else 0,
                    "thumbnail": thumb.get("url") if thumb is not None else ""})
    return out


def _youtube_recent(creator: dict, cfg: dict) -> list[dict]:
    name = creator.get("name") or creator.get("handle") or ""
    d = cfg["discover"]
    window_h = max(float(d["max_age_hours"]), float(d.get("fallback_max_age_hours", 168)))
    cid = _channel_id(creator)
    feed = _rss(cid) if cid else None
    if feed is not None:
        now = time.time()
        picks = []
        for e in feed:
            if e["is_short"] or not e["id"]:
                continue
            if e["timestamp"] and (now - e["timestamp"]) / 3600 > window_h:
                continue
            if not text_verdict(e["title"], "", cfg)[0]:
                continue
            picks.append(e)
        picks = picks[: int(d.get("per_channel_details", 4))]
        fulls = [_full(e["id"]) for e in picks]
        return [_record(f, "youtube", name) for f in fulls if f]
    return _youtube_recent_scrape(creator, cfg)


def _youtube_recent_scrape(creator: dict, cfg: dict) -> list[dict]:
    base = _yt_base(creator)
    if not base:
        return []
    n = int(cfg["discover"]["per_channel_recent"])
    flat = _flat(f"{base}/videos", n) + _flat(f"{base}/streams", max(3, n // 2))
    name = creator.get("name") or creator.get("handle") or ""
    if not flat:
        warn(f"   no videos found for {name} ({base}) - check the channel in creators.yaml")
    pre = [e for e in flat if text_verdict(e.get("title", ""), "", cfg)[0]]
    with ThreadPoolExecutor(max_workers=6) as ex:
        fulls = list(ex.map(lambda e: _full(e["id"]), pre))
    return [_record(f, "youtube", name) for f in fulls if f]


def _kick_recent(creator: dict, cfg: dict) -> list[dict]:
    slug = (creator.get("kick") or "").strip().strip("/").split("/")[-1]
    if not slug:
        return []
    try:
        r = requests.get(f"https://kick.com/api/v2/channels/{slug}/videos", headers=UA, timeout=20)
        r.raise_for_status()
        items = r.json()
        if isinstance(items, dict):
            items = items.get("data") or items.get("videos") or []
    except Exception as ex:  # noqa: BLE001
        warn(f"   Kick list for {slug} unavailable ({str(ex)[:100]})")
        return []
    out = []
    for v in items[: int(cfg["discover"]["per_channel_recent"])]:
        title = v.get("session_title") or v.get("title") or ""
        cats = [c.get("name", "") for c in (v.get("categories") or []) if isinstance(c, dict)]
        uuid = (v.get("video") or {}).get("uuid") or v.get("uuid")
        if not uuid:
            continue
        ts = None
        created = v.get("start_time") or v.get("created_at")
        if created:
            try:
                ts = calendar.timegm(time.strptime(created[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = None
        dur = v.get("duration") or 0
        thumb = v.get("thumbnail")
        thumb = thumb.get("src") if isinstance(thumb, dict) else thumb
        out.append({
            "platform": "kick", "id": f"kick_{uuid}", "url": f"https://kick.com/{slug}/videos/{uuid}",
            "title": title + (" | " + ", ".join(cats) if cats else ""), "description": ", ".join(cats),
            "tags": cats, "categories": [], "channel": slug, "channel_url": f"https://kick.com/{slug}",
            "views": int(v.get("views") or v.get("view_count") or 0), "timestamp": ts,
            "duration": dur / 1000 if dur > 100000 else dur, "live_status": "", "thumbnail": thumb or "",
            "creator": creator.get("name") or slug,
        })
    return out


def watchlist_candidates(cfg: dict) -> list[dict]:
    creators = [c for c in load_creators()
                if c.get("name") != "Example Gamer" and "xxxx" not in str(c.get("channel_id", "")).lower()]
    if not creators:
        warn("creators.yaml has no real creators yet (only the example). Add one with:\n"
             '         python -m dub_agent add-creator "CHANNEL_OR_VIDEO_URL" "how they gave permission"')
        return []
    log(f"Checking {len(creators)} permitted creator(s) for fresh {GAME_LABEL[target_game(cfg)]} gameplay...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        lists = list(ex.map(lambda c: _youtube_recent(c, cfg) + _kick_recent(c, cfg), creators))
    done = load_processed()
    vids = [v for lst in lists for v in lst if v["id"] not in done]
    log(f"   {len(vids)} recent {GAME_LABEL[target_game(cfg)].split(' (')[0]} upload(s) found across your creators")
    d = cfg["discover"]
    kept = _funnel([dict(v) for v in vids], cfg, float(d["max_age_hours"]), "watchlist")
    fb = float(d.get("fallback_max_age_hours", 168))
    if not kept and vids and fb > float(d["max_age_hours"]):
        log(f"   nothing from the last {d['max_age_hours']} h - using the best uploads from the last {fb / 24:.0f} days")
        kept = _funnel([dict(v) for v in vids], cfg, fb, "watchlist_fallback")
    report_youtube_errors("watchlist")
    _print(kept, 10)
    return kept


# ------------------------------------------------------------------ 2) trend radar -> ideas + outreach

def trend_radar(cfg: dict) -> tuple[list[str], list[dict]]:
    d = cfg["discover"]
    game = target_game(cfg)
    queries = search_queries(cfg)
    n = int(d.get("trend_search_depth", 30))
    max_age = float(d.get("trend_max_age_hours", 48))
    log(f"Trend radar: {len(queries)} searches for the newest {GAME_LABEL[game]} gameplay...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda q: _search(q, n, max_age), queries))
    log(f"   search returned {sum(len(r) for r in results)} results")
    report_youtube_errors("trend radar search")
    seen, pre = set(), []
    for lst in results:
        for e in lst:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            dur = e.get("duration") or 0
            if dur and dur < float(d["min_duration_min"]) * 60:
                continue
            if text_verdict(e.get("title", ""), "", cfg)[0]:
                pre.append(e)
    pre.sort(key=lambda e: int(e.get("view_count") or 0), reverse=True)
    pre = pre[: int(d.get("trend_max_check", 40))]
    with ThreadPoolExecutor(max_workers=4) as ex:
        fulls = list(ex.map(lambda e: _full(e["id"]), pre))
    vids = [_record(f) for f in fulls if f]
    if pre and not vids:
        warn(f"trend radar: all {len(pre)} detail lookups failed")
    kept = _funnel(vids, cfg, max_age, "trend_radar")
    report_youtube_errors("trend radar")
    _print(kept, 15)
    return [v["title"] for v in kept[:20]], kept


# ------------------------------------------------------------------ outreach list

OUTREACH_TEMPLATE = (
    "Hi {channel}! Loved your {game} gameplay '{video}'. I run a Hindi story-dub channel: we write original stories "
    "and voice-overs on top of gameplay. Could we use some of your gameplay footage? You'd get full credit + link "
    "in every video and description (happy to discuss a revenue share too). Thanks! - {me}"
)


def write_outreach(rows: list[dict], cfg: dict) -> Path:
    creators = load_creators()
    me = cfg["upload"].get("channel_name") or "(your channel)"
    game = "GTA V" if target_game(cfg) == "gta5" else "GTA VI"
    path = ROOT / "outreach.csv"
    existing = set()
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            existing = {r.get("channel_url", "") for r in csv.DictReader(fh)}
    best: dict[str, dict] = {}
    for r in rows:
        key = r.get("channel_url") or r.get("channel")
        if not key or key in existing:
            continue
        if find_creator({"channel_url": r.get("channel_url"), "channel_id": r.get("channel_id"),
                         "channel": r.get("channel")}, creators):
            continue
        if key not in best or r.get("velocity", 0) > best[key].get("velocity", 0):
            best[key] = r
    new = sorted(best.values(), key=lambda r: r.get("velocity", 0), reverse=True)
    write_header = not path.exists()
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "channel", "channel_url", "subscribers", "top_video",
                                               "views", "views_per_hour", "status", "message"])
            if write_header:
                w.writeheader()
            for r in new:
                w.writerow({"date": time.strftime("%Y-%m-%d"), "channel": r.get("channel", ""),
                            "channel_url": r.get("channel_url", ""), "subscribers": r.get("subscribers", ""),
                            "top_video": r.get("title", ""), "views": r.get("views", ""),
                            "views_per_hour": r.get("velocity", ""), "status": "to_contact",
                            "message": OUTREACH_TEMPLATE.format(channel=r.get("channel", ""), game=game,
                                                                video=r.get("title", "")[:60], me=me)})
    except PermissionError:
        warn("outreach.csv is open in Excel - close it to update.")
        return path
    if new:
        log(f"Outreach list: {len(new)} new gameplay channel(s) added to outreach.csv.")
    return path
