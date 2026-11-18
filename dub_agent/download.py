"""Get the source video: YouTube/any yt-dlp URL, or a local file."""
from __future__ import annotations

from pathlib import Path

from .utils import ensure_dir, log, media_duration, run, write_json, read_json


def is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def fetch(source: str, work_dir: str | Path, start: float | None = None, end: float | None = None,
          precheck=None) -> tuple[Path, dict]:
    """Return (video_path, metadata). Metadata has title/channel info for crediting."""
    work = ensure_dir(work_dir)
    if not is_url(source):
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        meta = {"title": path.stem, "webpage_url": "", "channel": "", "local_file": str(path)}
        if start is not None or end is not None:
            path = _cut_local(path, work, start, end)
            meta["section_offset"] = start or 0.0
        return path, meta

    import yt_dlp  # imported lazily so local-only use doesn't need it

    from .discover import cookie_opts

    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True, "ignore_no_formats_error": True,
                           **cookie_opts()}) as ydl:
        info = ydl.extract_info(source, download=False)
    meta = meta_from_info(info, source, start)
    if precheck:
        precheck(meta)  # permission check BEFORE downloading anything
    return _download_hd(source, info, meta, work, start, end)


def probe(source: str) -> dict:
    import yt_dlp

    from .discover import cookie_opts

    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True, "ignore_no_formats_error": True,
                           **cookie_opts()}) as ydl:
        info = ydl.extract_info(source, download=False)
    return meta_from_info(info, source)


def meta_from_info(info: dict, source: str, start: float | None = None) -> dict:
    return {
        "id": info.get("id", "video"),
        "title": info.get("title", ""),
        "webpage_url": info.get("webpage_url", source),
        "channel": info.get("channel") or info.get("uploader") or info.get("channel_id") or "",
        "channel_url": info.get("channel_url", ""),
        "channel_id": info.get("channel_id", ""),
        "uploader": info.get("uploader", ""),
        "uploader_id": info.get("uploader_id", ""),
        "uploader_url": info.get("uploader_url", ""),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "timestamp": info.get("timestamp") or info.get("release_timestamp"),
        "section_offset": start or 0.0,
    }


def _download_hd(source: str, info: dict, meta: dict, work: Path, start, end) -> tuple[Path, dict]:
    import yt_dlp

    vid = meta["id"]
    suffix = ""
    if start is not None or end is not None:
        suffix = f"_{int(start or 0)}-{int(end or 0)}"
    target = work / f"src_{vid}{suffix}.mp4"
    meta_path = work / f"src_{vid}{suffix}.json"
    if target.exists() and target.stat().st_size > 0:
        log(f"Using cached download: {target.name}")
        if meta_path.exists():
            meta = read_json(meta_path)
        return target, meta

    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": str(work / f"src_{vid}{suffix}.%(ext)s"),
        # <=1080p H.264-friendly mp4; falls back to best available
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "socket_timeout": 30, "retries": 5, "fragment_retries": 5,
    }
    from .discover import cookie_opts
    opts.update(cookie_opts())
    if start is not None or end is not None:
        from yt_dlp.utils import download_range_func

        opts["download_ranges"] = download_range_func(None, [(start or 0, end or (info.get("duration") or 10**7))])
        opts["force_keyframes_at_cuts"] = True

    log(f"Downloading: {meta['title']}  [{meta['channel']}]")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source])

    if not target.exists():
        # yt-dlp may pick a different container; find whatever it produced
        found = sorted(work.glob(f"src_{vid}{suffix}.*"), key=lambda p: p.stat().st_size, reverse=True)
        found = [f for f in found if f.suffix.lower() in (".mp4", ".mkv", ".webm")]
        if not found:
            raise RuntimeError("Download finished but no video file was found.")
        if found[0].suffix.lower() != ".mp4":
            run(["ffmpeg", "-y", "-v", "error", "-i", str(found[0]), "-c", "copy", str(target)])
        else:
            target = found[0]
    write_json(meta_path, meta)
    return target, meta


def _cut_local(path: Path, work: Path, start: float | None, end: float | None) -> Path:
    out = work / f"{path.stem}_{int(start or 0)}-{int(end or 0)}.mp4"
    if out.exists():
        return out
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end:
        cmd += ["-t", f"{(end - (start or 0)):.3f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(out)]
    run(cmd)
    return out


def fetch_proxy(source: str, work_dir: Path, vid: str) -> Path | None:
    """Download a small 360p copy straight from YouTube/Kick (seconds, no 1080p decode) for the AI to watch."""
    if not is_url(source):
        return None
    import yt_dlp

    raw = Path(work_dir) / f"src_{vid}_lo.mp4"
    if not raw.exists():
        opts = {"quiet": True, "noplaylist": True, "outtmpl": str(Path(work_dir) / f"src_{vid}_lo.%(ext)s"),
                "format": "bv*[height<=360]+ba/b[height<=360]/bv*[height<=480]+ba/worst",
                "merge_output_format": "mp4", "socket_timeout": 30, "retries": 5, "fragment_retries": 5}
        from .discover import cookie_opts
        opts.update(cookie_opts())
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([source])
        except Exception as e:  # noqa: BLE001
            log(f"Low-res download failed ({str(e)[:120]}); will make the preview from the HD file.")
            return None
        if not raw.exists():
            found = [p for p in Path(work_dir).glob(f"src_{vid}_lo.*") if p.suffix in (".mp4", ".mkv", ".webm")]
            if not found:
                return None
            raw = found[0]
    return raw


def make_proxy(src: Path, work_dir: Path, height: int = 360) -> Path:
    """Small low-fps copy for uploading to the AI (faster, cheaper)."""
    out = Path(work_dir) / f"{src.stem}_proxy{height}.mp4"
    if out.exists():
        return out
    log("Making a small preview copy for the AI to watch...")
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale=-2:{height},fps=10", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
        "-force_key_frames", "expr:gte(t,n_forced*30)",
        "-c:a", "aac", "-ac", "1", "-b:a", "48k", str(out),
    ])
    return out


def cut_segment(src: Path, out: Path, start: float, end: float, proxy_height: int | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}"]
    if proxy_height:
        cmd += ["-vf", f"scale=-2:{proxy_height}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                "-c:a", "aac", "-ac", "1", "-b:a", "64k"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac"]
    cmd.append(str(out))
    run(cmd)
    return out


def duration_of(path: Path) -> float:
    return media_duration(path)


def chunk_proxy(proxy: Path, work_dir: Path, chunk_sec: float) -> list[tuple[Path, float, float]]:
    """Split the preview copy into chunks the AI can watch. Returns [(path, start, length)]."""
    total = media_duration(proxy)
    if total <= chunk_sec * 1.15:
        return [(proxy, 0.0, total)]
    folder = Path(work_dir) / f"{proxy.stem}_chunks_{int(chunk_sec)}"
    lst = folder / "list.csv"
    if not lst.exists():
        folder.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(proxy), "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(int(chunk_sec)), "-reset_timestamps", "1",
             "-segment_list", str(lst), "-segment_list_type", "csv", str(folder / "chunk_%03d.mp4")])
    out = []
    for line in lst.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        start, end = float(parts[1]), float(parts[2])
        out.append((folder / parts[0], start, end - start))
    return out


def assemble(src, segments: list[dict], out: Path, fps: int = 30, cfg: dict | None = None) -> Path:
    """Cut several scenes (possibly from different videos: segment['src'] indexes `src` when it is a list)
    and join them into one clip with the same size/fps/audio format."""
    from concurrent.futures import ThreadPoolExecutor

    from .utils import has_audio, video_encoder_args, video_size

    srcs = list(src) if isinstance(src, (list, tuple)) else [src]
    used = sorted({int(sg.get("src", 0)) for sg in segments})
    sizes = {i: video_size(srcs[i]) for i in used}
    w, h = max(sizes.values(), key=lambda wh: wh[0] * wh[1])
    w, h = w - w % 2, h - h % 2
    venc = video_encoder_args(cfg, 16) if cfg else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                                                       "-pix_fmt", "yuv420p"]
    audio_ok = {i: has_audio(srcs[i]) for i in used}

    def _cut(item):
        i, sg = item
        s_path = srcs[int(sg.get("src", 0))]
        audio = audio_ok[int(sg.get("src", 0))]
        part = out.with_name(f"_part{i:03d}.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{sg['start']:.3f}", "-i", str(s_path)]
        if not audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-t", f"{sg['end'] - sg['start']:.3f}",
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}",
                *venc, "-c:a", "aac", "-ar", "48000", "-ac", "2"]
        if not audio:
            cmd += ["-map", "0:v", "-map", "1:a", "-shortest"]
        cmd.append(str(part))
        run(cmd)
        return part

    with ThreadPoolExecutor(max_workers=3) as ex:
        parts = list(ex.map(_cut, list(enumerate(segments))))
    lst = out.with_name("_concat.txt")
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", out.name],
        cwd=out.parent)
    for p in parts:
        p.unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    return out
