"""Upload finished videos to YOUR YouTube channel and schedule them at peak times (YouTube Data API v3)."""
from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

from .config import ROOT
from .utils import log, read_json, warn, write_json

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]
TOKEN_FILE = ROOT / "token.json"
UPLOAD_LOG = ROOT / "upload_log.json"


def _tz(cfg: dict):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(cfg["upload"].get("timezone", "Asia/Kolkata"))
    except Exception:  # noqa: BLE001
        return dt.timezone(dt.timedelta(hours=5, minutes=30))


def get_service(cfg: dict, interactive: bool = True):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise SystemExit("YouTube upload libraries missing. Run: pip install -r requirements.txt") from e
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("YouTube login expired. Run once by hand:  python -m dub_agent auth")
        secret = Path(cfg["upload"]["client_secret"])
        secret = secret if secret.is_absolute() else ROOT / secret
        if not secret.exists():
            raise SystemExit(f"{secret.name} not found. Follow README 'Connect your YouTube channel'.")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ------------------------------------------------------------------ scheduling

def _log() -> list[dict]:
    try:
        return read_json(UPLOAD_LOG) if UPLOAD_LOG.exists() else []
    except Exception:  # noqa: BLE001
        return []


def next_slot(kind: str, cfg: dict, after: dt.datetime | None = None) -> dt.datetime:
    """Next free publish time for 'episode' (long) or 'short' from the configured daily slots."""
    tz = _tz(cfg)
    slots = cfg["upload"]["long_slots"] if kind == "episode" else cfg["upload"]["short_slots"]
    taken = {e["publish_at"] for e in _log() if e.get("kind") == kind}
    now = after or (dt.datetime.now(tz) + dt.timedelta(minutes=45))
    for day in range(0, 60):
        d = (now + dt.timedelta(days=day)).date()
        for hhmm in sorted(slots):
            h, m = [int(x) for x in hhmm.split(":")]
            t = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if t < now:
                continue
            iso = t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if iso not in taken:
                return t
    return now + dt.timedelta(days=1)


def queue_days(kind: str, cfg: dict) -> float:
    """How many days ahead the next free slot is (used to avoid making videos faster than we publish)."""
    tz = _tz(cfg)
    return (next_slot(kind, cfg) - dt.datetime.now(tz)).total_seconds() / 86400


# ------------------------------------------------------------------ upload

def _clean_title(t: str) -> str:
    t = re.sub(r"[<>]", "", t or "").strip()
    return t[:100] or "GTA story"


def _clean_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = re.sub(r"[<>#,]", "", str(t)).strip()
        if not t or t.lower() in (x.lower() for x in out):
            continue
        if total + len(t) + 1 > 450:
            break
        out.append(t)
        total += len(t) + 1
    return out


def pending_creators_for(moment: dict) -> list[str]:
    """Names of creators in this video whose status (checked NOW in creators.yaml) is not approved."""
    from .config import creator_status, find_creator, load_creators

    current = load_creators()
    out = []
    for c in moment.get("creators") or ([moment["creator"]] if moment.get("creator") else []):
        if not (c.get("channel_id") or c.get("channel_url") or c.get("name")):
            continue
        now = find_creator({"channel_id": c.get("channel_id"), "channel_url": c.get("channel_url"),
                            "channel": c.get("name")}, current)
        if creator_status(now) != "approved":
            out.append(c.get("name") or c.get("channel_url") or "?")
    return out


def upload_dir(vdir: Path, cfg: dict, publish_at: dt.datetime | None = None, interactive: bool = True) -> dict | None:
    """Upload the finished video in `vdir` as a private video scheduled to go public at `publish_at`."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    vdir = Path(vdir)
    done_file = vdir / "uploaded.json"
    if done_file.exists():
        log(f"Already uploaded: {vdir.name}")
        return read_json(done_file)
    moment = read_json(vdir / "moment.json")
    meta = read_json(vdir / "upload.json")
    waiting = pending_creators_for(moment)
    if waiting:
        names = ", ".join(waiting)
        (vdir / "AWAITING_PERMISSION.txt").write_text(
            f"Not uploaded yet: waiting for permission from {names}.\n"
            f'After they say yes:  python -m dub_agent approve "{waiting[0]}" "WhatsApp <date> - OK"\n'
            "The next autopilot run (or: python -m dub_agent upload <this folder>) uploads it.\n", encoding="utf-8")
        log(f"Holding {vdir.name}: waiting for permission from {names}")
        return None
    (vdir / "AWAITING_PERMISSION.txt").unlink(missing_ok=True)
    kind = "episode" if moment.get("format") == "episode" else "short"
    video = next((p for p in vdir.glob("*.mp4") if not p.name.startswith("_")), None)
    if not video:
        warn(f"No video in {vdir}")
        return None
    publish_at = publish_at or next_slot(kind, cfg)
    publish_iso = publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    up = cfg["upload"]
    body = {
        "snippet": {
            "title": _clean_title(meta["title"]),
            "description": re.sub(r"[<>]", "", meta.get("description", ""))[:4900],
            "tags": _clean_tags(meta.get("tags", [])),
            "categoryId": str(up.get("category_id", "20")),
            "defaultLanguage": up.get("language", "hi"),
            "defaultAudioLanguage": up.get("language", "hi"),
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_iso,
            "selfDeclaredMadeForKids": bool(up.get("made_for_kids", False)),
            "containsSyntheticMedia": bool(up.get("disclose_ai_voices", True)),
        },
    }
    yt = get_service(cfg, interactive)
    log(f"Uploading {video.name} ({kind}) - goes public {publish_at.strftime('%a %d %b %H:%M')}...")
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True))
    resp, retries = None, 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                print(f"\r   {int(status.progress() * 100)}%", end="", flush=True)
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 6:
                retries += 1
                time.sleep(2 ** retries)
                continue
            if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
                warn("YouTube daily upload quota reached - the agent will retry this video next run.")
                return None
            raise
    print()
    vid = resp["id"]
    thumb = vdir / "thumbnail.jpg"
    if up.get("set_thumbnail", True) and thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
        except Exception as e:  # noqa: BLE001
            warn(f"Thumbnail not set ({str(e)[:120]}). Verify your channel (phone) to allow custom thumbnails.")
    record = {"video_id": vid, "url": f"https://youtu.be/{vid}", "kind": kind, "title": body["snippet"]["title"],
              "publish_at": publish_iso, "folder": str(vdir), "uploaded": dt.datetime.now().isoformat(timespec="seconds")}
    write_json(done_file, record)
    hist = _log()
    hist.append(record)
    write_json(UPLOAD_LOG, hist)
    log(f"Scheduled: {record['url']}  ({publish_at.strftime('%a %d %b %H:%M')})")
    return record
