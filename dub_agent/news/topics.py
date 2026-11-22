"""Hot-topic finder: what is the internet talking about RIGHT NOW in gaming + entertainment (India + global).

Sources (all free, no keys): publisher RSS feeds, Google News RSS searches, Reddit 'hot', and the YouTube trend radar.
Gemini then picks today's long-video topic and Shorts topics, with channel-style angles.
"""
from __future__ import annotations

import calendar
import email.utils
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

from ..utils import log, warn

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"}

DEFAULT_FEEDS = {
    "gaming_global": ["https://feeds.ign.com/ign/all", "https://www.gamespot.com/feeds/mashup/",
                      "https://www.pcgamer.com/rss/", "https://www.dexerto.com/feed/",
                      "https://www.eurogamer.net/feed", "https://kotaku.com/rss"],
    "gaming_india": ["https://www.talkesport.com/feed/", "https://sportsdigest.in/feed/",
                     "https://www.insidesport.in/feed/", "https://afkgaming.com/rss"],
    "entertainment_global": ["https://variety.com/feed/", "https://www.hollywoodreporter.com/feed/",
                             "https://deadline.com/feed/"],
    "entertainment_india": ["https://www.koimoi.com/feed/", "https://www.bollywoodhungama.com/rss/news.xml",
                            "https://www.filmibeat.com/rss/feeds/bollywood-fb.xml"],
}
DEFAULT_GNEWS = ["GTA 6", "BGMI esports", "Indian gaming YouTuber", "IShowSpeed", "Kai Cenat", "new game release",
                 "Bollywood box office", "upcoming movie trailer", "Netflix India series", "Hollywood trailer"]
DEFAULT_REDDIT = ["GTA6", "gaming", "IndianGaming", "BGMI", "LivestreamFail", "bollywood", "movies", "television"]


def _ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return email.utils.parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def parse_feed(xml_bytes: bytes, source: str, group: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    strip = lambda t: re.sub(r"<[^>]+>", " ", t or "").strip()  # noqa: E731
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names):
            for name in names:
                for c in it:
                    if c.tag.split("}")[-1] == name:
                        return c
            return None
        t_el = get("title")
        title = (t_el.text if t_el is not None else "") or ""
        link_el = get("link")
        link = (link_el.text or link_el.get("href") or "") if link_el is not None else ""
        date_el = get("pubDate", "published", "updated")
        desc_el = get("description", "summary")
        src_el = get("source")
        out.append({"title": strip(title), "url": link.strip(), "published": _ts(date_el.text if date_el is not None else None),
                    "summary": strip(desc_el.text if desc_el is not None else "")[:300],
                    "source": (src_el.text if src_el is not None and src_el.text else source), "group": group})
    return out


def _get(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        return r.content if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def _feed(job):
    url, group = job
    data = _get(url)
    return parse_feed(data, urllib.parse.urlparse(url).netloc.replace("www.", ""), group) if data else []


def _gnews(q: str) -> list[dict]:
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:2d") + "&hl=en-IN&gl=IN&ceid=IN:en")
    data = _get(url)
    return parse_feed(data, "Google News", f"gnews:{q}") if data else []


def _reddit(sub: str) -> list[dict]:
    try:
        r = requests.get(f"https://www.reddit.com/r/{sub}/hot.json?limit=25",
                         headers={"User-Agent": "story-agent/1.0 (research)"}, timeout=15)
        if not r.ok:
            return []
        items = r.json().get("data", {}).get("children", [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for c in items:
        d = c.get("data", {})
        if d.get("stickied"):
            continue
        out.append({"title": d.get("title", ""), "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "published": d.get("created_utc"), "summary": (d.get("selftext") or "")[:200],
                    "source": f"r/{sub}", "group": f"reddit:{sub}", "score": d.get("score", 0),
                    "comments": d.get("num_comments", 0)})
    return out


def gather_headlines(cfg: dict) -> list[dict]:
    n = cfg.get("news", {})
    feeds = n.get("feeds") or DEFAULT_FEEDS
    jobs = [(u, g) for g, urls in feeds.items() for u in urls]
    max_age = float(n.get("headline_max_age_hours", 48)) * 3600
    log(f"Hot-topic finder: {len(jobs)} news feeds, {len(n.get('google_news') or DEFAULT_GNEWS)} Google News searches, "
        f"{len(n.get('reddit') or DEFAULT_REDDIT)} subreddits...")
    with ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(_feed, jobs))
        res += list(ex.map(_gnews, n.get("google_news") or DEFAULT_GNEWS))
        res += list(ex.map(_reddit, n.get("reddit") or DEFAULT_REDDIT))
    now = time.time()
    seen, items = set(), []
    for lst in res:
        for it in lst:
            key = re.sub(r"\W+", " ", it["title"].lower()).strip()[:80]
            if not key or key in seen:
                continue
            if it.get("published") and now - it["published"] > max_age:
                continue
            seen.add(key)
            items.append(it)
    ok_sources = sum(1 for r in res if r)
    log(f"   {len(items)} fresh headlines from {ok_sources} sources")
    if not items:
        warn("No headlines could be fetched (no internet, or all feeds blocked).")
    return items


# ------------------------------------------------------------------ Gemini picks the topics

STYLE = """
CHANNEL STYLE (copy this format - it is proven):
- SHORTS (35-55 s): retell ONE viral moment / discovery / reveal as a mini story. Typical subjects: a famous streamer's
  wild moment in the hottest game (IShowSpeed, Kai Cenat, xQc, Indian streamers), a secret/easter egg players just
  found, a shocking update, a trailer reveal, a star's viral moment. Title = curiosity gap, e.g.
  "IShowSpeed Said No GTA 6 Marathon. 17 Hours Later... 🎮", "Players Already Found Bigfoot in GTA 6 🎮",
  "Kai Cenat Escaped an Arrest GTA 6 Never Planned 🎮". End gaming titles with 🎮, entertainment titles with 🎬.
- LONG VIDEO (8-12 min): a documentary-style deep dive on a personality, controversy or big story, with a
  fact-check framing. Title formats: "<Name>: The Truth (<3-4 keywords>)",
  "<A> vs <B>: The Full Timeline (What's Confirmed, What's Only Claimed)", "The Real Story Behind <X>".
  Indian angle preferred for long videos (Indian gaming community, Bollywood, Indian creators), global for Shorts.
- Neutral and factual on controversies: separate confirmed facts from claims. Never invent facts.
"""


def pick_topics(headlines: list[dict], cfg: dict, n_shorts: int, n_long: int, focus: str = "auto",
                manual_topic: str | None = None) -> dict:
    from .. import brain

    client = brain._client()
    lines = []
    for i, h in enumerate(sorted(headlines, key=lambda h: -(h.get("score") or 0) - (h.get("published") or 0) / 1e9)[:220]):
        age = f"{(time.time() - h['published']) / 3600:.0f}h ago" if h.get("published") else ""
        pop = f" ({h['score']} upvotes)" if h.get("score") else ""
        lines.append(f"[{i}] {h['source']} {age}{pop}: {h['title']}")
    focus_rule = {"gaming": "ONLY gaming topics.", "entertainment": "ONLY entertainment topics (movies, series, actors, "
                  "music artists, celebrities).", "auto": "Mix: mostly gaming, plus the biggest entertainment stories."}
    manual = f"\nThe user asked for this topic for the long video: {manual_topic}\n" if manual_topic else ""
    prompt = f"""
You run content strategy for a fast-growing Hindi/Hinglish YouTube channel about gaming + entertainment news.
From today's headlines, pick the topics with the HIGHEST viewer interest right now (trending, controversial,
surprising, big names). Pick {n_long} long-video topic(s) and {n_shorts} Shorts topics, all different.
Focus: {focus_rule.get(focus, focus_rule['auto'])}{manual}
{STYLE}
For each topic give:
- category: "gaming" or "entertainment" (a gaming video must stay 100% gaming - no movie/celebrity footage in it)
- title: in the channel style (English or Hinglish, as in the examples)
- angle: one line on the story we tell
- hook: the first spoken line (Hinglish), must stop the scroll
- names: key people/games/movies (used to search footage)
- footage_queries: 3-5 YouTube search queries that will find REAL footage of this exact moment/story
  (stream clips, gameplay, official trailers, interviews, news clips). Specific: names + event + game.
- research_query: what to search on the web to verify facts
- why_now: why this is hot today (cite headline numbers like [12])
Return JSON only: {{"long": [{{...}}], "shorts": [{{...}}]}}

TODAY'S HEADLINES:
{chr(10).join(lines) if lines else "(none fetched - use what you know is trending, and say so in why_now)"}
"""
    data = brain._generate(client, cfg, [prompt], want="pro", thinking=cfg["brain"].get("creative_thinking", "medium"))
    data.setdefault("long", [])
    data.setdefault("shorts", [])
    for t in data["long"] + data["shorts"]:
        t["category"] = "entertainment" if str(t.get("category", "")).lower().startswith("ent") else "gaming"
        t.setdefault("footage_queries", [t.get("title", "")])
        t.setdefault("names", [])
    data["long"] = data["long"][:n_long]
    data["shorts"] = data["shorts"][:n_shorts]
    return data


def save(obj, path) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
