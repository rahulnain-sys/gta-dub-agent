"""Load config.yaml (+ .env) and the creator permission ledger."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "output_dir": "output",
    "work_dir": "work",
    "series": {
        "enabled": False,             # False = new characters & genre invented for every video (dynamic)
        "channel_name": "",
        "main_character": {
            "name": "Bunty",
            "gender": "male",
            "persona": "25-year-old over-confident Delhi boy who thinks he is the city's biggest don, "
                       "always has a jugaad plan, but is scared of his mummy and his EMIs",
            "catchphrase": "Tension mat le, Bunty hai na!",
            "voices": {"gemini": "Puck", "sarvam": "shubh", "elevenlabs": "", "dummy": "m1"},
        },
        "recurring_cast": [
            {"name": "Pinky", "gender": "female", "persona": "Bunty's smart, sarcastic girlfriend",
             "voices": {"gemini": "Leda", "sarvam": "ritu", "dummy": "f1"}},
            {"name": "Golu", "gender": "male", "persona": "Bunty's lazy, always-hungry best friend",
             "voices": {"gemini": "Fenrir", "sarvam": "manan", "dummy": "m2"}},
            {"name": "Mummy", "gender": "female", "persona": "strict Punjabi mom, threatens with chappal",
             "voices": {"gemini": "Gacrux", "sarvam": "shreya", "dummy": "f2"}},
            {"name": "Inspector Pandey", "gender": "male", "persona": "corrupt, lazy cop always chasing Bunty",
             "voices": {"gemini": "Algenib", "sarvam": "manan", "dummy": "m3"}},
        ],
        "narrator_voices": {"gemini": "Sadachbia", "sarvam": "shubh", "dummy": "n1"},
    },
    "episode": {
        "target_minutes": 10,
        "part_seconds": 120,          # episode script is written part by part (keeps timing accurate)
        "width": 1920,
        "height": 1080,
        "captions": True,
        "caption_size": 60,
        "caption_bottom": 80,
        "words_per_chunk": 5,
    },
    "performance": {
        "scan_workers": 2,            # parts of a video watched by Gemini at the same time
        "tts_workers": 4,             # voice lines generated at the same time
        "encoder": "auto",            # auto | libx264 | h264_nvenc | h264_qsv | h264_amf
    },
    "discover": {
        "target_game": "auto",        # auto = GTA V until gta6_launch, then GTA VI | or force "gta5" / "gta6"
        "gta6_launch": "2026-11-19",
        "allow_gta5_after_launch": False,
        "max_age_hours": 24,          # your creators: only uploads from the last N hours
        "per_channel_recent": 10,     # newest uploads checked per channel
        "pick_top": 4,                # videos used per autopilot run
        "min_duration_min": 6,
        "max_duration_min": 240,
        "rank_by": "velocity",        # velocity = views per hour (best for fresh uploads) | views
        "ai_check": True,             # Gemini checks thumbnail + metadata: real gameplay? faceless? not AI/meme?
        "faceless_only": True,
        "min_quality": 5,             # 1-10 story-footage quality from the AI check
        "exclude_terms": [],          # your own extra words to reject (e.g. "minecraft", "fortnite")
        "trend_max_age_hours": 48,
        "trend_search_depth": 30,     # newest results per search
        "trend_max_check": 120,       # max videos inspected in detail per radar run
        "trend_topics": ["gameplay", "funny gameplay", "mission gameplay", "no commentary", "roleplay",
                         "gameplay hindi", "mods gameplay", "online gameplay", "police chase", "free roam"],
        "extra_queries_gta5": ["grand theft auto v gameplay"],
        "extra_queries_gta6": ["grand theft auto vi gameplay"],
    },
    "autopilot": {
        "format": "both",             # episode + shorts each run
        "episode_minutes": 12,
        "shorts": 3,
        "upload": True,
        "max_queue_days": 2,          # don't make new videos if publish slots are already booked this far ahead
    },
    "brain": {
        "model": "auto",              # auto = newest Flash model your key can use
        "fallback_models": [],
        "planner_model": "auto",      # auto = newest Pro model your key can use (falls back to Flash)
        "max_shorts_per_video": 6,
        "chunk_minutes": 20,
        "min_scene_score": 3,
        "min_clip_sec": 20,
        "max_clip_sec": 50,
        "language": "Hinglish (Hindi + English mix, written in Roman script)",
        "humour_style": "desi comedy: dramatic overreactions, bhai-giri, filmy dialogues, relatable Indian situations (mummy, traffic, EMI, Sharma ji ka beta)",
        "max_characters": 4,
        "temperature": 0.95,
    },
    "voice": {
        "engine": "gemini",           # gemini | sarvam | elevenlabs | dummy
        "fallback_engine": "sarvam",  # used if the main engine fails / runs out of quota (needs its key)
        "accent_hint": "natural North-Indian Hinglish accent, casual and conversational like a real person, not a news reader",
        "max_speedup": 1.3,           # how much a line may be sped up to fit its slot
        "gap_between_lines": 0.08,
        "gemini": {
            "models": ["auto"],       # auto = newest Gemini TTS model your key can use
            "male_voices": ["Puck", "Fenrir", "Algenib", "Zubenelgenubi", "Charon", "Sadachbia", "Orus", "Iapetus",
                            "Alnilam", "Rasalgethi", "Schedar", "Achird", "Umbriel", "Enceladus", "Algieba", "Sadaltager"],
            "female_voices": ["Kore", "Leda", "Aoede", "Laomedeia", "Gacrux", "Zephyr", "Sulafat", "Despina",
                              "Achernar", "Vindemiatrix", "Pulcherrima", "Callirhoe", "Autonoe", "Erinome"],
            "narrator_voice": "Sadachbia",
        },
        "sarvam": {
            "model": "bulbul:v3",
            "language_code": "hi-IN",
            "male_voices": ["shubh", "manan"],
            "female_voices": ["ritu", "shreya", "ishita"],
            "narrator_voice": "shubh",
            "pace": 1.05,
        },
        "elevenlabs": {
            "model": "eleven_v3",
            "language_code": "hi",
            # Paste voice IDs from your ElevenLabs Voice Library (Hindi / Indian voices)
            "male_voices": [],
            "female_voices": [],
            "narrator_voice": "",
        },
        "dummy": {"male_voices": ["m1", "m2", "m3"], "female_voices": ["f1", "f2"], "narrator_voice": "n1"},
    },
    "audio": {
        "game_audio_volume": 0.0,     # 0 = fully mute creator's audio (their voice + game music)
        "music_dir": "assets/music",  # drop royalty-free .mp3/.wav here; one is picked at random
        "music_volume": 0.10,
        "sfx_dir": "assets/sfx",      # optional: boing.mp3, airhorn.mp3, bruh.mp3 ... (names the AI can use)
        "sfx_volume": 0.6,
        "loudness_lufs": -14,
    },
    "video": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "layout": "crop",             # crop = cut left/right, keep middle | blurfill = full frame on blurred background
        "crf": 20,
        "preset": "veryfast",
        "blur_facecam": True,         # blur streamer facecam / overlays the AI detects
        "extra_masks": [],            # manual blur boxes [x, y, w, h] normalised 0-1 on the source frame
    },
    "captions": {
        "font": "Arial Black",
        "fonts_dir": "",              # optional folder with .ttf files (e.g. Montserrat/Anton)
        "size": 92,
        "words_per_chunk": 3,
        "position_from_bottom": 560,
        "outline": 7,
        "highlight_colors": ["#FFE600", "#00E5FF", "#FF4FD8", "#7CFF4F", "#FF8A00"],
        "title_seconds": 2.8,
        "title_size": 80,
        "show_credit": True,
    },
    "upload": {
        "client_secret": "client_secret.json",   # OAuth file from Google Cloud (see README)
        "timezone": "Asia/Kolkata",
        "long_slots": ["19:30"],                 # publish times for long videos (local time)
        "short_slots": ["12:30", "16:30", "21:30"],
        "category_id": "20",                     # Gaming
        "language": "hi",
        "made_for_kids": False,
        "set_thumbnail": True,
        "channel_name": "",
        "extra_hashtags": ["#GTA6", "#GTAVI", "#gta6funny", "#hindicomedy", "#shorts"],
        "disclose_ai_voices": True,
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_config(path: str | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if cfg_path.exists():
        user = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _deep_merge(DEFAULTS, user)
    for key in ("output_dir", "work_dir"):
        p = Path(cfg[key])
        cfg[key] = str(p if p.is_absolute() else ROOT / p)
    for key in ("music_dir", "sfx_dir"):
        p = Path(cfg["audio"][key])
        cfg["audio"][key] = str(p if p.is_absolute() else ROOT / p)
    return cfg


# ------------------------------------------------------------- permissions

def _norm(s: str | None) -> str:
    return (s or "").strip().lower().rstrip("/").replace("https://", "").replace("http://", "").replace("www.", "")


def load_creators(path: str | None = None) -> list[dict]:
    p = Path(path) if path else ROOT / "creators.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [c for c in data.get("creators", []) if c]


def find_creator(meta: dict, creators: list[dict]) -> dict | None:
    """Match a downloaded video's channel against the permission ledger."""
    keys = {
        _norm(meta.get("channel_url")),
        _norm(meta.get("uploader_url")),
        _norm(meta.get("uploader_id")),
        _norm(meta.get("channel_id")),
        _norm(meta.get("channel")),
        _norm(meta.get("uploader")),
    }
    keys.discard("")
    for c in creators:
        for field in ("channel_url", "handle", "channel_id", "name"):
            v = _norm(c.get(field))
            if not v:
                continue
            if v in keys or any(v == k or k.endswith("/" + v.lstrip("@")) or k.endswith("/" + v) for k in keys):
                return c
            handle = v.lstrip("@")
            if any(k.endswith("@" + handle) or k == handle for k in keys):
                return c
    return None
