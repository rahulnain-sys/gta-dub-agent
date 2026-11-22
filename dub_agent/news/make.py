"""ONE command: today's hottest topics -> research -> footage -> script -> narration -> finished videos (+ upload text).

python -m dub_agent make                       1 long video + 3 Shorts on today's hottest topics
python -m dub_agent make --topic "Jonathan vs GodLike"   choose the long video's topic yourself
python -m dub_agent make --shorts 5 --no-long  only Shorts
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..utils import ensure_dir, log, read_json, warn, write_json
from . import footage as F
from .editor import produce_news
from .research import research
from .topics import gather_headlines, pick_topics
from .writer import WPS, write_script


def _creators(items: list[dict]) -> list[dict]:
    out, seen = [], set()
    for it in items:
        key = it.get("channel_id") or it.get("channel_url") or it.get("channel")
        if key and key not in seen and it.get("scenes"):
            seen.add(key)
            out.append({"name": it.get("channel", ""), "channel_id": it.get("channel_id", ""),
                        "channel_url": it.get("channel_url", ""), "video_url": it.get("url", "")})
    return out


def make_one(topic: dict, fmt: str, tdir: Path, cfg: dict, headlines: list[dict], minutes: float,
             engine: str | None, footage_urls: list[str] | None) -> Path:
    work = Path(cfg["work_dir"])
    write_json(tdir / "topic.json", topic)
    log(f"\n=== {('LONG' if fmt == 'long' else 'SHORT')}: {topic.get('title')}  [{topic.get('category')}]")
    # 1) research
    facts = read_json(tdir / "facts.json") if (tdir / "facts.json").exists() else research(topic, cfg, headlines)
    write_json(tdir / "facts.json", facts)
    # 2) footage + scene logs
    items = read_json(tdir / "footage.json") if (tdir / "footage.json").exists() else []
    if items and not any(it.get("scenes") for it in items):
        items = [F.prepare_and_scan(it, cfg, work, topic) for it in items]   # earlier run logged nothing -> redo
    if not items:
        n_pick = int(cfg.get("news", {}).get("videos_per_long" if fmt == "long" else "videos_per_short",
                                             3 if fmt == "long" else 2))
        items = F.search_footage(topic, cfg, n_pick, footage_urls)
        with ThreadPoolExecutor(max_workers=2) as ex:
            items = list(ex.map(lambda it: F.prepare_and_scan(it, cfg, work, topic), items))
        items = [it for it in items if it.get("scenes")]
        write_json(tdir / "footage.json", items)
    # 3) script
    if (tdir / "script.json").exists():
        script = read_json(tdir / "script.json")
    else:
        script = write_script(topic, facts, items, fmt, cfg, minutes)
        write_json(tdir / "script.json", script)
    # 4) HD only for the scenes the script uses
    from .editor import _scene_lookup
    refs = _scene_lookup(items)
    need: dict[int, list] = {}
    for b in script["beats"]:
        r = refs.get(str(b.get("scene")))
        if r:
            est = max(3.0, len(b.get("narration", "").split()) / WPS) + 8
            need.setdefault(r[0], []).append((r[1]["start"], r[1]["start"] + est + float(b.get("raw_seconds") or 0)))
    for vi, it in enumerate(items):
        if vi in need and not it.get("hd") and not it.get("sections"):
            p = F.download_hd(it, need[vi], cfg, work)
            if p and not it.get("sections"):
                it["hd"] = str(p)
    write_json(tdir / "footage.json", items)
    write_json(tdir / "meta.json", {"mode": "news", "format": fmt, "creators": _creators(items),
                                    "made": dt.datetime.now().isoformat(timespec="seconds")})
    # 5) narration + edit + render
    return produce_news(tdir, cfg, engine)


def run(cfg: dict, n_shorts: int = 3, n_long: int = 1, minutes: float = 10, focus: str = "auto",
        topic: str | None = None, footage_urls: list[str] | None = None, engine: str | None = None) -> list[Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    batch = ensure_dir(Path(cfg["output_dir"]) / f"{stamp}_daily")
    headlines = gather_headlines(cfg)
    write_json(batch / "headlines.json", headlines)
    log("Choosing today's topics...")
    plan = pick_topics(headlines, cfg, n_shorts, n_long, focus, topic)
    write_json(batch / "plan.json", plan)
    for t in plan["long"]:
        log(f"   LONG : {t.get('title')}  - {t.get('why_now', '')[:90]}")
    for t in plan["shorts"]:
        log(f"   SHORT: {t.get('title')}  - {t.get('why_now', '')[:90]}")
    results = []
    jobs = [("long", t, batch / f"long_{i + 1:02d}") for i, t in enumerate(plan["long"])] + \
           [("short", t, batch / f"short_{i + 1:02d}") for i, t in enumerate(plan["shorts"])]
    for fmt, t, d in jobs:
        ensure_dir(d)
        try:
            results.append(make_one(t, fmt, d, cfg, headlines, minutes, engine, footage_urls if fmt == "long" else None))
            (d / "FAILED.txt").unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            (d / "FAILED.txt").write_text(f"Not finished: {e}\nRetry this one:  python -m dub_agent rerender \"{d}\"\n",
                                          encoding="utf-8")
            warn(f"{d.name} FAILED: {str(e)[:200]}")
    log(f"\nDone. {len(results)} video(s) in {batch}")
    for r in results:
        log(f"   OK      {r.parent.name}/{r.name}")
    for f in sorted(batch.glob("*/FAILED.txt")):
        log(f"   FAILED  {f.parent.name}  (see FAILED.txt)")
    log("Watch them, then upload by hand using each folder's upload.txt (title, description, tags, IG caption).")
    return results
