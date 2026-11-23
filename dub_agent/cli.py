"""Command line:  python -m dub_agent <command> ...

  run URL            watch the whole video, then make vertical Shorts (--format shorts), a long 16:9 story
                     episode (--format episode) or both; every story stars your channel's main character
  rerender DIR       re-make one short after you edit its script.json (only changed lines are re-voiced)
  voices             audition every voice of an engine with a sample line
  check              verify ffmpeg, API keys and creators.yaml
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .config import ROOT, load_config, load_creators
from .utils import ensure_dir, env, log, need_binary, parse_time


def _moments(text: str | None):
    if not text:
        return None
    out = []
    for part in text.split(","):
        a, b = part.strip().split("-")
        out.append((parse_time(a), parse_time(b)))
    return out


def cmd_run(a, cfg):
    from .pipeline import run_sources

    if a.layout:
        cfg["video"]["layout"] = a.layout
    if a.game_audio is not None:
        cfg["audio"]["game_audio_volume"] = a.game_audio
    run_sources(
        a.source, cfg, n_shorts=a.shorts, start=parse_time(a.start), end=parse_time(a.end),
        engine=a.engine, style_note=a.style or "", allow_unlisted=a.allow_unlisted,
        manual_moments=_moments(a.moments), creator_handle=a.creator, script_file=a.script,
        fmt=a.format, episode_minutes=a.minutes,
    )


def cmd_make(a, cfg):
    from .news.make import run

    n = cfg["news"]
    run(cfg, n_shorts=a.shorts if a.shorts is not None else int(n["shorts_per_day"]),
        n_long=1 if (a.long or (n.get("long_enabled") and not a.no_long)) else 0, minutes=a.minutes or float(n["long_minutes"]),
        focus=a.focus or n.get("focus", "auto"), topic=a.topic, footage_urls=a.footage, engine=a.engine)


def cmd_rerender(a, cfg):
    from .pipeline import produce

    d = Path(a.short_dir)
    if (d / "meta.json").exists() or (d / "topic.json").exists():
        from .news.make import make_one
        from .utils import read_json
        fmt = "long" if d.name.startswith("long") else "short"
        make_one(read_json(d / "topic.json"), fmt, d, cfg, [], float(cfg["news"]["long_minutes"]), a.engine, None)
        return

    if a.layout:
        cfg["video"]["layout"] = a.layout
    produce(Path(a.short_dir), cfg, a.engine)


def cmd_voices(a, cfg):
    from .utils import load_audio, save_wav
    from .voices import make_engine, trim_silence

    eng_name = a.engine or cfg["voice"]["engine"]
    eng = make_engine(eng_name, cfg)
    ecfg = cfg["voice"].get(eng_name, {})
    voices = list(ecfg.get("male_voices") or []) + list(ecfg.get("female_voices") or [])
    if not voices:
        sys.exit(f"No voices configured for {eng_name} in config.yaml")
    out_dir = ensure_dir(Path(cfg["output_dir"]) / f"voice_samples_{eng_name}")
    text = a.text or "Arre bhai, ye gaadi meri hai! Police aa gayi, ab mummy ko kya bolunga?"
    for v in voices:
        out = out_dir / f"{v}.wav"
        log(f"Voice {v}...")
        try:
            eng.synth(text, v, "panicked but funny, fast", "a desi guy in trouble", out)
            save_wav(out, trim_silence(load_audio(out)))
        except Exception as e:  # noqa: BLE001
            log(f"  failed: {str(e)[:150]}")
    log(f"Listen to the samples in: {out_dir}")


def cmd_discover(a, cfg):
    from . import discover

    print(f"Target game right now: {discover.GAME_LABEL[discover.target_game(cfg)]}\n")
    titles, rows = discover.trend_radar(cfg)
    discover.write_outreach(rows, cfg)
    print()
    cands = discover.watchlist_candidates(cfg)
    print(f"\nFresh gameplay from your creators: {len(cands)} (top {cfg['discover']['pick_top']} would be used)")
    print("Every decision with its reason: discover_report.csv")


def cmd_autopilot(a, cfg):
    from . import autopilot

    autopilot.run(cfg, dry_run=a.dry_run, upload=False if a.no_upload else None)


def cmd_auth(a, cfg):
    from .uploader import get_service

    yt = get_service(cfg)
    me = yt.channels().list(part="snippet", mine=True).execute()
    items = me.get("items") or []
    log(f"Connected to YouTube channel: {items[0]['snippet']['title'] if items else '(unknown)'}")


def cmd_upload(a, cfg):
    import datetime as dt

    from .uploader import _tz, upload_dir

    at = None
    if a.at:
        at = dt.datetime.strptime(a.at, "%Y-%m-%d %H:%M").replace(tzinfo=_tz(cfg))
    for d in a.dirs:
        upload_dir(Path(d), cfg, at)


def cmd_schedule(a, cfg):
    from .autopilot import install_windows_task

    install_windows_task(a.every)


def _channel_from_url(url: str) -> dict | None:
    import yt_dlp

    class _Q:
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): pass

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
                               "playlistend": 1, "logger": _Q()}) as y:
            info = y.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None
    if not info:
        return None
    cid = info.get("channel_id") or (info.get("id") if str(info.get("id", "")).startswith("UC") else "")
    if not cid:
        return None
    handle = info.get("uploader_id") if str(info.get("uploader_id", "")).startswith("@") else ""
    return {"name": info.get("channel") or info.get("uploader") or info.get("title") or cid, "handle": handle,
            "channel_id": cid, "channel_url": info.get("channel_url") or f"https://www.youtube.com/channel/{cid}"}


def _search_channels(name: str) -> list[dict]:
    import yt_dlp

    out, seen = [], set()
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
                               "playlistend": 15, "no_warnings": True}) as y:
            res = y.extract_info(f"ytsearch15:{name} gta", download=False) or {}
        for e in res.get("entries") or []:
            cid = (e or {}).get("channel_id")
            if cid and cid not in seen:
                seen.add(cid)
                out.append({"name": e.get("channel") or e.get("uploader") or cid, "channel_id": cid,
                            "channel_url": e.get("channel_url") or f"https://www.youtube.com/channel/{cid}",
                            "example": e.get("title", "")})
    except Exception:  # noqa: BLE001
        pass
    return out[:8]


def cmd_add_creator(a, cfg):
    """Add a creator who gave permission. Accepts a video URL, channel URL, @handle or just the channel name."""
    import yaml

    from .config import ROOT

    raw = a.url.strip()
    entry = None
    if "kick.com" in raw:
        slug = raw.rstrip("/").split("kick.com/")[-1].split("/")[0]
        entry = {"name": a.name or slug, "kick": slug}
    else:
        tries = []
        if raw.startswith("http"):
            tries.append(raw.replace(" ", ""))
        elif raw.startswith("@"):
            tries.append("https://www.youtube.com/" + raw.replace(" ", ""))
        elif raw.startswith("UC") and " " not in raw:
            tries.append(f"https://www.youtube.com/channel/{raw}")
        for t in tries:
            entry = _channel_from_url(t)
            if entry:
                break
        if not entry:
            name = re.sub(r"https?://(www\.)?youtube\.com/", "", raw).lstrip("@").strip() or raw
            log(f"Couldn't open that link directly - searching YouTube for channel '{name}'...")
            found = _search_channels(name)
            if not found:
                sys.exit("No matching channel found. Open one of their videos and copy the URL from the address bar.")
            for i, c in enumerate(found, 1):
                print(f"  {i}. {c['name']}   (e.g. \"{c['example'][:60]}\")   {c['channel_url']}")
            pick = input("Which number is the right channel? (Enter to cancel): ").strip()
            if not pick.isdigit() or not (1 <= int(pick) <= len(found)):
                sys.exit("Cancelled.")
            entry = {k: v for k, v in found[int(pick) - 1].items() if k != "example"}
        if a.name:
            entry["name"] = a.name
    entry["permission"] = a.permission
    entry["status"] = "approved"
    path = ROOT / "creators.yaml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
    lst = [c for c in (data.get("creators") or []) if c and c.get("name") != "Example Gamer"]
    key = entry.get("channel_id") or entry.get("kick")
    lst = [c for c in lst if (c.get("channel_id") or c.get("kick")) != key]
    lst.append({k: v for k, v in entry.items() if v})
    from .config import save_creators
    save_creators(lst)
    log(f"Added to creators.yaml: {entry.get('name')}  ({entry.get('channel_url') or entry.get('kick')})")


def cmd_models(a, cfg):
    from . import brain
    from . import models as M

    client = brain._client()
    names = M.available(client)
    print("Gemini models your key can use:")
    for n in sorted(names, key=M._version, reverse=True):
        print("  ", n)


def cmd_approve(a, cfg):
    from .config import set_creator_status

    c = set_creator_status(a.channel, "approved", a.note)
    if not c:
        sys.exit(f"No creator matching '{a.channel}' in creators.yaml. Run: python -m dub_agent pending")
    log(f"Approved: {c.get('name')} ({c.get('channel_url', '')}). Their held videos upload on the next autopilot run.")
    if a.upload_now:
        from .config import ROOT
        from .uploader import upload_dir
        for d in sorted((ROOT / "output").glob("*/*")):
            if (d / "AWAITING_PERMISSION.txt").exists():
                upload_dir(d, cfg)


def cmd_reject(a, cfg):
    from .config import set_creator_status

    c = set_creator_status(a.channel, "rejected", a.note or "said no")
    if not c:
        sys.exit(f"No creator matching '{a.channel}'.")
    log(f"Marked rejected: {c.get('name')}. Their footage won't be used or uploaded.")


def cmd_pending(a, cfg):
    from .config import ROOT, creator_status, load_creators

    pend = [c for c in load_creators() if creator_status(c) == "pending"]
    held = {}
    for d in sorted((ROOT / "output").glob("*/*")):
        f = d / "AWAITING_PERMISSION.txt"
        if f.exists():
            for c in pend:
                if c.get("name") and c["name"] in f.read_text(encoding="utf-8"):
                    held.setdefault(c["name"], []).append(str(d))
    if not pend:
        print("No pending creators.")
        return
    me = cfg["upload"].get("channel_name") or "(your channel)"
    print(f"{len(pend)} creator(s) waiting for your WhatsApp OK:\n")
    for c in pend:
        n = len(held.get(c.get("name"), []))
        print(f"- {c.get('name')}   {c.get('channel_url', '')}   videos waiting: {n}")
        print(f"    found via: {c.get('found_video', '')}")
        print(f'    message: Hi {c.get("name")}! Loved your GTA gameplay. I run a Hindi story-dub channel ({me}) - '
              "can I use some of your gameplay footage with full credit + link in every video?")
        print(f'    approve: python -m dub_agent approve "{c.get("name")}" "WhatsApp <date> - OK"\n')


def cmd_check(a, cfg):
    ok = True
    import shutil as _sh
    from . import discover as _d
    log(f"target game: {_d.GAME_LABEL[_d.target_game(cfg)]}")
    log(f"deno (needed by yt-dlp for YouTube): {'OK' if _sh.which('deno') else 'MISSING - winget install DenoLand.Deno --source winget'}")
    import os as _os
    cf = _os.environ.get("DUB_COOKIES_FILE")
    if cf and Path(cf).exists():
        log(f"cookies.txt: {'OK (YouTube cookies found)' if _d.clean_cookies(cf) else 'INVALID (see warning above)'}")
    else:
        log("cookies.txt: not found (recommended - see README 'YouTube bot check')")
    for b in ("ffmpeg", "ffprobe"):
        try:
            need_binary(b)
            log(f"{b}: OK")
        except SystemExit:
            log(f"{b}: MISSING")
            ok = False
    for mod in ("yt_dlp", "google.genai", "yaml", "numpy", "requests", "PIL", "googleapiclient", "google_auth_oauthlib"):
        try:
            __import__(mod)
            log(f"python {mod}: OK")
        except ImportError:
            log(f"python {mod}: MISSING (pip install -r requirements.txt)")
            ok = False
    for k in ("GEMINI_API_KEY", "SARVAM_API_KEY", "ELEVENLABS_API_KEY"):
        log(f"{k}: {'set' if env(k) else 'not set'}")
    if env("GEMINI_API_KEY"):
        try:
            from . import brain
            brain._client()  # logs the models it will use
        except BaseException as e:  # noqa: BLE001
            log(f"Gemini: could not connect ({str(e)[:120]})")
            ok = False
    cr = load_creators()
    log(f"creators.yaml: {len(cr)} creator(s) with permission")
    for c in cr:
        log(f"   - {c.get('name')} {c.get('handle', '')} :: {c.get('permission', '(no note!)')}")
    music = [m for m in Path(cfg["audio"]["music_dir"]).glob("*.*") if m.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")] if Path(cfg["audio"]["music_dir"]).exists() else []
    log(f"background music tracks: {len(music)}")
    log("All good!" if ok else "Fix the MISSING items above.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="dub_agent", description="GTA story-dub agent: discover -> scan -> write -> voice -> render -> schedule")
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="make an episode and/or shorts from video URL(s) you choose")
    r.add_argument("source", nargs="+", help="one or more YouTube/Kick URLs (scenes from all are mixed)")
    r.add_argument("--format", choices=["shorts", "episode", "both"], default="shorts",
                   help="shorts = vertical Shorts (default) | episode = one long 16:9 story video | both")
    r.add_argument("--minutes", type=float, help="target length of the episode in minutes (default 10)")
    r.add_argument("--shorts", type=int, help="maximum shorts to make; the AI only makes the good ones (default 6)")
    r.add_argument("--start", help="optional: only scan from this time (by default the WHOLE video is scanned)")
    r.add_argument("--end", help="optional: ... until this time")
    r.add_argument("--engine", choices=["gemini", "sarvam", "elevenlabs", "dummy"])
    r.add_argument("--style", help="extra comedy direction, e.g. 'Bunty is a Delhi auto-wala'")
    r.add_argument("--layout", choices=["crop", "blurfill"])
    r.add_argument("--moments", help="skip AI picking: '75-110,300-340' (seconds or mm:ss)")
    r.add_argument("--script", help="use your own script.json instead of AI writing (needs --moments)")
    r.add_argument("--creator", help="creator handle for local files, e.g. @gamerbhai (must be in creators.yaml)")
    r.add_argument("--game-audio", type=float, help="0-1 volume of original audio (default 0 = muted)")
    r.add_argument("--allow-unlisted", action="store_true", help="skip creators.yaml check (only for your own footage)")
    r.set_defaults(func=cmd_run)

    mk = sub.add_parser("make", help="ONE command: today's hottest topics -> finished long video + Shorts")
    mk.add_argument("--topic", help="choose the long video's topic yourself")
    mk.add_argument("--shorts", type=int, help="how many Shorts (default 4)")
    mk.add_argument("--no-long", action="store_true", help="only Shorts")
    mk.add_argument("--long", action="store_true", help="also make the long video (off by default)")
    mk.add_argument("--minutes", type=float, help="long video length (default 10)")
    mk.add_argument("--focus", choices=["auto", "gaming", "entertainment"])
    mk.add_argument("--footage", nargs="*", help="extra footage URLs for the long video (YouTube/Kick VODs)")
    mk.add_argument("--engine", choices=["gemini", "sarvam", "elevenlabs", "edge", "dummy"])
    mk.set_defaults(func=cmd_make)

    rr = sub.add_parser("rerender", help="re-make a short after editing its script.json")
    rr.add_argument("short_dir")
    rr.add_argument("--engine", choices=["gemini", "sarvam", "elevenlabs", "edge", "dummy"])
    rr.add_argument("--layout", choices=["crop", "blurfill"])
    rr.set_defaults(func=cmd_rerender)

    v = sub.add_parser("voices", help="audition voices")
    v.add_argument("--engine", choices=["gemini", "sarvam", "elevenlabs", "dummy"])
    v.add_argument("--text")
    v.set_defaults(func=cmd_voices)

    d = sub.add_parser("discover", help="show trending GTA videos + fresh uploads from your creators")
    d.set_defaults(func=cmd_discover)

    ap = sub.add_parser("autopilot", help="find -> make episode + shorts -> schedule upload (fully automatic)")
    ap.add_argument("--dry-run", action="store_true", help="only show what would be picked")
    ap.add_argument("--no-upload", action="store_true", help="make the videos but don't upload")
    ap.set_defaults(func=cmd_autopilot)

    au = sub.add_parser("auth", help="connect your YouTube channel (one time)")
    au.set_defaults(func=cmd_auth)

    up = sub.add_parser("upload", help="upload finished video folder(s) to YouTube, scheduled")
    up.add_argument("dirs", nargs="+")
    up.add_argument("--at", help='publish time "YYYY-MM-DD HH:MM" (default: next free slot)')
    up.set_defaults(func=cmd_upload)

    sc = sub.add_parser("schedule", help="run autopilot automatically every N hours (Windows Task Scheduler)")
    sc.add_argument("--every", type=int, default=2)
    sc.set_defaults(func=cmd_schedule)

    apv = sub.add_parser("approve", help="mark a creator approved (after their WhatsApp OK) - held videos then upload")
    apv.add_argument("channel", help="channel name, @handle, channel ID or URL")
    apv.add_argument("note", nargs="?", default="", help='how they agreed, e.g. "WhatsApp 19 Nov - OK"')
    apv.add_argument("--upload-now", action="store_true", help="upload their held videos immediately")
    apv.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="mark a creator who said no (their footage is never used)")
    rj.add_argument("channel")
    rj.add_argument("note", nargs="?", default="")
    rj.set_defaults(func=cmd_reject)

    pe = sub.add_parser("pending", help="list creators waiting for permission + their held videos")
    pe.set_defaults(func=cmd_pending)

    mo = sub.add_parser("models", help="list the Gemini models your API key can use")
    mo.set_defaults(func=cmd_models)

    ac = sub.add_parser("add-creator", help="add a creator who gave permission (YouTube or Kick URL)")
    ac.add_argument("url", help="a video URL, channel URL, @handle, or just the channel name")
    ac.add_argument("permission", help='how they gave permission, e.g. "WhatsApp 16 Nov - OK, credit + link"')
    ac.add_argument("--name")
    ac.set_defaults(func=cmd_add_creator)

    c = sub.add_parser("check", help="check setup")
    c.set_defaults(func=cmd_check)

    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd not in ("check", "auth", "discover", "schedule", "add-creator", "models", "approve", "reject", "pending"):
        need_binary("ffmpeg")
        need_binary("ffprobe")
    a.func(a, cfg)


if __name__ == "__main__":
    main()
