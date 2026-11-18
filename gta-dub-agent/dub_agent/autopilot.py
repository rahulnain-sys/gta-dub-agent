"""Fully automatic run: find fresh GTA uploads from your creators -> pick the hottest -> make a long
episode + Shorts -> schedule them on your YouTube channel. Meant to run every 1-2 hours (Task Scheduler)."""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

from .config import ROOT
from .utils import ensure_dir, log, warn

LOCK = ROOT / "work" / "autopilot.lock"


def _lock() -> bool:
    ensure_dir(LOCK.parent)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 6 * 3600:
        warn("Another autopilot run is still working (work/autopilot.lock). Skipping this run.")
        return False
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _unlock() -> None:
    LOCK.unlink(missing_ok=True)


def _log_run(text: str) -> None:
    out = ensure_dir(ROOT / "output")
    with open(out / "autopilot_log.txt", "a", encoding="utf-8") as fh:
        fh.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {text}\n")


def run(cfg: dict, dry_run: bool = False, upload: bool | None = None) -> None:
    from . import discover, uploader
    from .pipeline import run_sources

    if not _lock():
        return
    try:
        ap = cfg["autopilot"]
        do_upload = ap.get("upload", True) if upload is None else upload
        fmt = ap.get("format", "both")
        if do_upload and not dry_run:
            max_q = float(ap.get("max_queue_days", 2))
            kind = "episode" if fmt in ("episode", "both") else "short"
            q = uploader.queue_days(kind, cfg)
            if q > max_q:
                log(f"Upload queue is full for the next {q:.1f} days - no new videos needed yet.")
                _log_run(f"skipped: queue {q:.1f} days")
                return
            _retry_pending(cfg)

        titles, rows = discover.trend_radar(cfg)
        discover.write_outreach(rows, cfg)
        cands = discover.watchlist_candidates(cfg)
        if not cands:
            log("No fresh GTA uploads from your creators right now. Will check again next run.")
            _log_run("no fresh videos")
            return
        picks = cands[: int(cfg["discover"]["pick_top"])]
        log(f"Picked {len(picks)} video(s):")
        for p in picks:
            log(f"   - [{p['platform']}] {p['creator']}: {p['title'][:80]} ({p['views']:,} views)")
        if dry_run:
            return
        results = run_sources([p["url"] for p in picks], cfg, fmt=fmt, n_shorts=int(ap.get("shorts", 3)),
                              episode_minutes=float(ap.get("episode_minutes", 12)), trends=titles,
                              batch_name="autopilot")
        discover.mark_processed([p["id"] for p in picks])
        _log_run(f"made {len(results)} video(s) from: " + "; ".join(p["title"][:50] for p in picks))
        if do_upload:
            for vpath in sorted(results, key=lambda p: 0 if p.parent.name == "episode" else 1):
                try:
                    rec = uploader.upload_dir(vpath.parent, cfg, interactive=False)
                    if rec:
                        _log_run(f"scheduled {rec['kind']} {rec['url']} at {rec['publish_at']}")
                except SystemExit as e:
                    warn(str(e))
                    break
                except Exception as e:  # noqa: BLE001
                    warn(f"Upload failed for {vpath.parent.name}: {str(e)[:200]} (will retry next run)")
    finally:
        _unlock()


def _retry_pending(cfg: dict) -> None:
    """Upload anything finished earlier that didn't get uploaded (quota, network...)."""
    from . import uploader

    out = ROOT / "output"
    if not out.exists():
        return
    for d in sorted(out.glob("*_autopilot/*")):
        if d.is_dir() and (d / "upload.json").exists() and not (d / "uploaded.json").exists():
            try:
                uploader.upload_dir(d, cfg, interactive=False)
            except Exception as e:  # noqa: BLE001
                warn(f"Retry upload failed for {d}: {str(e)[:150]}")
                return


def install_windows_task(every_hours: int = 2) -> None:
    """Register a Windows Task Scheduler job that runs the autopilot every N hours."""
    import subprocess

    bat = ROOT / "run_autopilot.bat"
    cmd = ["schtasks", "/create", "/tn", "GTA Dub Autopilot", "/tr", f'"{bat}"', "/sc", "hourly",
           "/mo", str(every_hours), "/f"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
