"""Pick Gemini models automatically from what YOUR API key can use (Google renames/retires models often).

text  -> newest "gemini-X-flash" (falls back to flash-lite, then pro)
pro   -> newest "gemini-X-pro" (falls back to text)
tts   -> newest "gemini-X-flash-tts" (falls back to lite-tts / pro-tts / any *tts*)
Any model that answers 404 / "no longer available" is skipped for the rest of the run.
"""
from __future__ import annotations

import re
import threading

from .utils import log, warn

_LOCK = threading.Lock()
_AVAILABLE: list[str] | None = None
_BAD: set[str] = set()
EXCLUDE_TEXT = ("tts", "live", "image", "transcribe", "translate", "embedding", "aqa", "omni", "veo", "imagen",
                "lyria", "robotics", "computer-use", "audio", "native")


def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def available(client) -> list[str]:
    global _AVAILABLE
    with _LOCK:
        if _AVAILABLE is None:
            names = []
            try:
                for m in client.models.list():
                    n = (getattr(m, "name", "") or "").replace("models/", "")
                    if n.startswith("gemini"):
                        names.append(n)
            except Exception as e:  # noqa: BLE001
                warn(f"Could not list Gemini models ({str(e)[:120]}); using built-in names.")
            _AVAILABLE = names
        return list(_AVAILABLE)


def mark_bad(name: str) -> None:
    _BAD.add(name)


def is_gone_error(msg: str) -> bool:
    m = msg.lower()
    return ("404" in m or "not_found" in m or "not found" in m or "no longer available" in m
            or "is not supported" in m or "deprecated" in m)


def _rank(names: list[str], want: str) -> list[str]:
    def ok(n):
        if n in _BAD:
            return False
        if want == "tts":
            return "tts" in n
        if any(x in n for x in EXCLUDE_TEXT):
            return False
        return True

    def score(n):
        stable = 0 if ("preview" in n or "exp" in n or re.search(r"-\d{2}-\d{2}", n)) else 1
        if want == "tts":
            tier = 3 if "flash-tts" in n or "flash-preview-tts" in n else 2 if "pro" in n else 1
        elif want == "pro":
            tier = 3 if n.endswith("-pro") or "-pro-" in n else 2 if "flash" in n and "lite" not in n else 1
        elif want == "lite":
            tier = 3 if "flash-lite" in n else 2 if "flash" in n else 1
        else:  # text
            tier = 3 if "flash" in n and "lite" not in n else 2 if "flash-lite" in n else 1
        latest_alias = 1 if n.endswith("-latest") else 0
        return (tier, _version(n), stable, latest_alias)

    return sorted([n for n in names if ok(n)], key=score, reverse=True)


BUILTIN = {
    "text": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-flash-latest"],
    "pro": ["gemini-3.8-pro", "gemini-3.1-pro-preview", "gemini-pro-latest"],
    "lite": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"],
    "tts": ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview"],
}


def candidates(client, want: str, configured: str | list | None = None) -> list[str]:
    """Ordered list of models to try: explicit config first (unless 'auto'), then the best available."""
    out: list[str] = []
    conf = configured if isinstance(configured, list) else [configured] if configured else []
    for c in conf:
        if c and c != "auto" and c not in _BAD:
            out.append(c)
    names = available(client)
    ranked = _rank(names, want) if names else []
    if want == "pro" and not ranked:
        ranked = _rank(names, "text")
    for n in ranked + [b for b in BUILTIN[want] if b not in _BAD]:
        if n not in out:
            out.append(n)
    return out[:6]


def describe(client) -> str:
    names = available(client)
    picks = {w: (candidates(client, w) or ["?"])[0] for w in ("text", "pro", "tts")}
    return (f"scenes/scripts: {picks['text']} | planner: {picks['pro']} | voices: {picks['tts']}"
            f"  ({len(names)} Gemini models available to your key)")


def log_choice(client) -> None:
    log("Gemini models -> " + describe(client))
