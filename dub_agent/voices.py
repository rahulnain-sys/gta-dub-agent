"""Voice casting + text-to-speech engines (Gemini, Sarvam, ElevenLabs, dummy for testing)."""
from __future__ import annotations

import base64
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests

from .utils import (SAMPLE_RATE, ensure_dir, env, load_audio, log, save_pcm16_as_wav, save_wav,
                    text_hash, trim_silence, warn)


class QuotaError(RuntimeError):
    pass


# --------------------------------------------------------------------- casting

# Voice catalog: gender + character tags used to match an AI-written "voice_type" to a real voice.
VOICE_CATALOG = {
    "gemini": {
        "Puck": ("male", "upbeat young funny cheerful playful excited"),
        "Fenrir": ("male", "excitable energetic loud hyper panicked young"),
        "Algenib": ("male", "gravelly deep rough gangster tough don villain old"),
        "Charon": ("male", "deep calm informative serious narrator mature"),
        "Orus": ("male", "firm strong authoritative boss police commanding"),
        "Zubenelgenubi": ("male", "casual relaxed chill friend lazy"),
        "Sadachbia": ("male", "lively commentator narrator energetic"),
        "Iapetus": ("male", "clear neutral gentleman polite"),
        "Alnilam": ("male", "firm serious strict mature"),
        "Rasalgethi": ("male", "informative mature posh gentleman classy"),
        "Schedar": ("male", "even calm smooth gentleman"),
        "Achird": ("male", "friendly warm young nice"),
        "Umbriel": ("male", "easy-going relaxed smooth"),
        "Enceladus": ("male", "breathy soft nervous scared"),
        "Algieba": ("male", "smooth suave charming romantic classy"),
        "Sadaltager": ("male", "knowledgeable mature old wise uncle"),
        "Kore": ("female", "firm confident strong boss"),
        "Leda": ("female", "youthful young sweet girl cute"),
        "Aoede": ("female", "breezy light cheerful"),
        "Laomedeia": ("female", "upbeat energetic excited funny"),
        "Gacrux": ("female", "mature old strict mother aunty"),
        "Zephyr": ("female", "bright cheerful young"),
        "Sulafat": ("female", "warm caring kind mother"),
        "Despina": ("female", "smooth classy elegant romantic"),
        "Achernar": ("female", "soft gentle shy"),
        "Vindemiatrix": ("female", "gentle calm soft"),
        "Pulcherrima": ("female", "forward bold sassy confident"),
        "Callirhoe": ("female", "easy-going relaxed casual"),
        "Autonoe": ("female", "bright young"),
        "Erinome": ("female", "clear neutral"),
    },
}


def _match_voice(engine: str, gender: str, voice_type: str, pool: list[str], used: set[str]) -> str | None:
    cat = VOICE_CATALOG.get(engine)
    if not cat or not voice_type:
        return None
    words = set(re.findall(r"[a-z]+", voice_type.lower()))
    best, best_score = None, 0.0
    for name in pool:
        g, tags = cat.get(name, (None, ""))
        if g and g != gender:
            continue
        score = len(words & set(tags.split())) - (0.8 if name in used else 0)
        if score > best_score:
            best, best_score = name, score
    return best

def series_casting(cfg: dict, engine: str) -> dict:
    """Pinned voices for the channel's main character, recurring cast and narrator (same voice in every video)."""
    from .utils import slugify

    s = cfg.get("series", {})
    out = {}
    if not s.get("enabled", False):
        return out
    mc = s.get("main_character", {})
    if mc.get("name") and (mc.get("voices") or {}).get(engine):
        out[slugify(mc["name"], 20).replace("-", "_")] = mc["voices"][engine]
    for c in s.get("recurring_cast", []) or []:
        if c.get("name") and (c.get("voices") or {}).get(engine):
            out[slugify(c["name"], 20).replace("-", "_")] = c["voices"][engine]
    if (s.get("narrator_voices") or {}).get(engine):
        out["narrator"] = s["narrator_voices"][engine]
    return out


def cast_voices(characters: list[dict], engine: str, cfg: dict, fixed: dict | None = None) -> dict:
    """Assign a distinct voice to each character, matching gender. `fixed` pins voices (series cast etc.)."""
    ecfg = cfg["voice"].get(engine, {})
    pools = {
        "male": list(ecfg.get("male_voices") or []),
        "female": list(ecfg.get("female_voices") or []),
    }
    reserved = set(series_casting(cfg, engine).values()) | set((fixed or {}).values())
    used: set[str] = set(reserved)
    casting: dict[str, str] = {}
    for c in characters:
        cid = c["id"]
        if fixed and cid in fixed:
            casting[cid] = fixed[cid]
            used.add(fixed[cid])
            continue
        if cid == "narrator" and ecfg.get("narrator_voice"):
            casting[cid] = ecfg["narrator_voice"]
            used.add(casting[cid])
            continue
        pool = pools.get(c.get("gender", "male")) or pools["male"] or pools["female"] or ["default"]
        matched = _match_voice(engine, c.get("gender", "male"), c.get("voice_type", "") + " " + c.get("persona", ""),
                               pool, used)
        if matched:
            casting[cid] = matched
            used.add(matched)
            continue
        free = [v for v in pool if v not in used]
        if not free:  # ran out: reuse a non-series voice before reusing a series one
            free = [v for v in pool if v not in reserved] or pool
        choice = free[0] if free != pool else pool[len(casting) % len(pool)]
        casting[cid] = choice
        used.add(choice)
    return casting


# --------------------------------------------------------------------- engines

class BaseEngine:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.vcfg = cfg["voice"]

    def synth(self, text: str, voice: str, style: str, persona: str, out: Path) -> None:
        raise NotImplementedError


class GeminiEngine(BaseEngine):
    """Gemini TTS: takes natural-language acting directions -> expressive, non-robotic delivery."""
    name = "gemini"

    def __init__(self, cfg):
        super().__init__(cfg)
        from google import genai
        key = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY missing")
        self.client = genai.Client(api_key=key)
        from . import models as M
        self.models = M.candidates(self.client, "tts", list(self.vcfg["gemini"].get("models") or ["auto"]))

    def synth(self, text, voice, style, persona, out):
        from google.genai import types

        direction = (
            f"You are a voice actor in a funny Indian comedy dub. Character: {persona or 'a desi guy'}. "
            f"Speak in {self.vcfg['accent_hint']}. Acting direction: {style}. "
            f"Sound like a real person with natural pauses, emotion and timing - never robotic or announcer-like. "
            f"Say exactly this line and nothing else:\n{text}"
        )
        last = None
        for model in list(self.models):
            for attempt in range(3):
                try:
                    resp = self.client.models.generate_content(
                        model=model,
                        contents=direction,
                        config=types.GenerateContentConfig(
                            response_modalities=["AUDIO"],
                            speech_config=types.SpeechConfig(
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                                )
                            ),
                        ),
                    )
                    part = resp.candidates[0].content.parts[0]
                    data = part.inline_data.data
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    mime = (part.inline_data.mime_type or "").lower()
                    rate = 24000
                    if "rate=" in mime:
                        try:
                            rate = int(mime.split("rate=")[1].split(";")[0])
                        except ValueError:
                            pass
                    save_pcm16_as_wav(out, data, sr=rate)
                    if model != self.models[0]:
                        self.models.remove(model)
                        self.models.insert(0, model)  # stick with the model that works
                    return
                except Exception as e:  # noqa: BLE001
                    last = e
                    msg = str(e)
                    from . import models as M
                    if M.is_gone_error(msg):
                        M.mark_bad(model)
                        break  # retired / not available to this key -> next model
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        if "per day" in msg.lower() or "PerDay" in msg:
                            raise QuotaError(f"Gemini TTS daily quota used up: {msg[:160]}")
                        wait = 20 * (attempt + 1)
                        warn(f"Gemini TTS rate-limited, waiting {wait}s...")
                        time.sleep(wait)
                        continue
                    if "503" in msg or "UNAVAILABLE" in msg or "500" in msg:
                        time.sleep(5 * (attempt + 1))
                        continue
                    break
        if last and ("429" in str(last) or "RESOURCE_EXHAUSTED" in str(last)):
            raise QuotaError(str(last)[:200])
        raise RuntimeError(f"Gemini TTS failed: {last}")


class SarvamEngine(BaseEngine):
    """Sarvam Bulbul: Indian-built TTS that handles Hinglish code-mixing natively."""
    name = "sarvam"
    URL = "https://api.sarvam.ai/text-to-speech"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.key = env("SARVAM_API_KEY")
        if not self.key:
            raise RuntimeError("SARVAM_API_KEY missing")
        self.scfg = self.vcfg["sarvam"]

    def synth(self, text, voice, style, persona, out):
        pace = float(self.scfg.get("pace", 1.0))
        s = (style or "").lower()
        if any(w in s for w in ("fast", "panic", "excited", "hurry", "angry", "shout")):
            pace = min(pace + 0.15, 1.6)
        elif any(w in s for w in ("slow", "lazy", "sad", "crying", "sleepy")):
            pace = max(pace - 0.12, 0.7)
        body = {
            "text": text,
            "target_language_code": self.scfg.get("language_code", "hi-IN"),
            "speaker": voice,
            "model": self.scfg.get("model", "bulbul:v3"),
            "pace": round(pace, 2),
            "speech_sample_rate": 24000,
        }
        for attempt in range(4):
            r = requests.post(self.URL, json=body, headers={"api-subscription-key": self.key}, timeout=60)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (402, 403):
                raise QuotaError(f"Sarvam: {r.status_code} {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"Sarvam TTS error {r.status_code}: {r.text[:300]}")
            audios = r.json().get("audios") or []
            if not audios:
                raise RuntimeError("Sarvam returned no audio")
            raw = b"".join(base64.b64decode(a) for a in audios[:1])
            Path(out).write_bytes(raw)  # Sarvam returns WAV bytes
            return
        raise QuotaError("Sarvam rate limit")


class ElevenLabsEngine(BaseEngine):
    """ElevenLabs v3: premium expressive voices; acting via audio tags like [laughs] [whispers]."""
    name = "elevenlabs"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.key = env("ELEVENLABS_API_KEY")
        if not self.key:
            raise RuntimeError("ELEVENLABS_API_KEY missing")
        self.ecfg = self.vcfg["elevenlabs"]

    def synth(self, text, voice, style, persona, out):
        if not voice or voice == "default":
            raise RuntimeError("Set ElevenLabs voice IDs in config.yaml (voice.elevenlabs.male_voices/female_voices)")
        tag = _style_to_tag(style)
        body = {"text": f"{tag} {text}".strip(), "model_id": self.ecfg.get("model", "eleven_v3")}
        if self.ecfg.get("language_code"):
            body["language_code"] = self.ecfg["language_code"]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
        r = requests.post(url, json=body, headers={"xi-api-key": self.key}, timeout=90)
        if r.status_code in (401, 402, 429):
            raise QuotaError(f"ElevenLabs: {r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:300]}")
        mp3 = Path(out).with_suffix(".mp3")
        mp3.write_bytes(r.content)
        save_wav(out, load_audio(mp3))
        mp3.unlink(missing_ok=True)


def _style_to_tag(style: str) -> str:
    s = (style or "").lower()
    for words, tag in (
        (("laugh",), "[laughs]"), (("whisper",), "[whispers]"), (("cry", "sob"), "[crying]"),
        (("shout", "angry", "yell"), "[shouting]"), (("panic", "scared"), "[panicked]"),
        (("excited", "happy"), "[excited]"), (("sarcas", "smug"), "[sarcastic]"), (("sad",), "[sad]"),
    ):
        if any(w in s for w in words):
            return tag
    return ""


class EdgeEngine(BaseEngine):
    """Microsoft Edge neural voices (Indian English + Hindi): free, no API key, no daily limit.
    Less expressive than Gemini, so it is the safety net that guarantees every video gets a voiceover.
    Voice spec: "<voice>|<pitch>" e.g. "en-IN-PrabhatNeural|-8Hz" (pitch shifts give each character its own sound)."""
    name = "edge"

    def __init__(self, cfg):
        super().__init__(cfg)
        try:
            import edge_tts  # noqa: F401
        except ImportError as e:
            raise RuntimeError("edge-tts not installed (pip install -r requirements.txt)") from e

    def synth(self, text, voice, style, persona, out):
        import asyncio

        import edge_tts

        name, _, pitch = (voice or "en-IN-PrabhatNeural").partition("|")
        pitch = pitch or "+0Hz"
        st = (style or "").lower()
        rate = 6
        if any(w in st for w in ("fast", "panic", "excited", "hurry", "angry", "shout", "hyper")):
            rate = 18
        elif any(w in st for w in ("slow", "lazy", "sad", "crying", "sleepy", "calm", "menacing", "deep")):
            rate = -8
        mp3 = Path(out).with_suffix(".edge.mp3")
        last = None
        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, name, rate=f"{rate:+d}%", pitch=pitch)
                asyncio.run(comm.save(str(mp3)))
                save_wav(out, load_audio(mp3))
                mp3.unlink(missing_ok=True)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Edge TTS failed: {last}")


class DummyEngine(BaseEngine):
    """Offline test engine: beeps with a length matching the text (no API needed)."""
    name = "dummy"

    def synth(self, text, voice, style, persona, out):
        words = max(1, len(text.split()))
        dur = 0.35 + words / 2.6
        t = np.arange(int(dur * SAMPLE_RATE)) / SAMPLE_RATE
        base = 180 + (abs(hash(voice)) % 6) * 45
        sig = 0.25 * np.sin(2 * np.pi * base * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3.2 * t) ** 2)
        env_ = np.minimum(1, np.minimum(t / 0.03, (dur - t) / 0.05))
        save_wav(out, (sig * env_).astype(np.float32))


ENGINES = {"gemini": GeminiEngine, "sarvam": SarvamEngine, "elevenlabs": ElevenLabsEngine, "edge": EdgeEngine,
           "dummy": DummyEngine}

ENGINE_KEYS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "sarvam": ("SARVAM_API_KEY",),
               "elevenlabs": ("ELEVENLABS_API_KEY",)}


def engine_ready(name: str) -> bool:
    keys = ENGINE_KEYS.get(name)
    return True if not keys else any(env(k) for k in keys)


def make_engine(name: str, cfg: dict) -> BaseEngine:
    if name not in ENGINES:
        raise SystemExit(f"Unknown voice engine '{name}'. Choose: {', '.join(ENGINES)}")
    return ENGINES[name](cfg)


# --------------------------------------------------------------------- synth all lines

def synth_lines(script: dict, engine_name: str, cfg: dict, cache_dir: Path, casting: dict | None = None) -> dict:
    """Generate audio for every line. Cached by (engine, voice, text, style) so edits only redo changed lines.
    Adds `audio` (wav path) and `audio_dur` to each line, and returns the casting used."""
    v = cfg["voice"]
    chain = [engine_name] + list(v.get("fallback_engines") or [])
    if v.get("fallback_engine") and v["fallback_engine"] not in chain:
        chain.insert(1, v["fallback_engine"])
    if engine_name != "dummy" and "edge" not in chain:
        chain.append("edge")          # free safety net: a video never ends up without a voiceover
    seen, errors = set(), []
    for i, name in enumerate(chain):
        if name in seen:
            continue
        seen.add(name)
        if not engine_ready(name):
            if i == 0:
                warn(f"Voice engine '{name}' has no API key in .env - skipping it.")
            continue
        try:
            return _synth_all(script, name, cfg, cache_dir, casting if i == 0 else None)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:150]}")
            nxt = next((n for n in chain[i + 1:] if n not in seen and engine_ready(n)), None)
            warn(f"Voice engine '{name}' failed ({str(e)[:150]}).")
            if nxt:
                warn(f"Re-voicing the whole video with '{nxt}' so every character keeps one voice.")
    raise RuntimeError("No voice engine worked: " + " | ".join(errors))


def _synth_all(script: dict, engine_name: str, cfg: dict, cache_dir: Path, casting: dict | None) -> dict:
    cache = ensure_dir(cache_dir)
    personas = {c["id"]: c.get("persona", "") for c in script["characters"]}
    engine = make_engine(engine_name, cfg)
    pinned = series_casting(cfg, engine_name)
    pinned.update(casting or {})
    casting = cast_voices(script["characters"], engine_name, cfg, pinned)
    jobs, queued = [], set()
    for i, ln in enumerate(script["lines"], 1):
        voice = casting.get(ln["speaker"], "default")
        key = text_hash(engine.name, voice, ln["text"], ln.get("style", ""))
        out = cache / f"{engine.name}_{key}.wav"
        ln["_wav"], ln["voice"] = str(out), voice
        if not out.exists() and str(out) not in queued:   # identical lines are voiced once and reused
            queued.add(str(out))
            jobs.append((i, ln, voice, out))
    workers = max(1, int(cfg.get("performance", {}).get("tts_workers", 4)))
    if jobs:
        log(f"  generating {len(jobs)} voice line(s) with {engine.name} ({workers} at a time)...")

        def _one(job):
            i, ln, voice, out = job
            tmp = out.with_suffix(".part.wav")
            engine.synth(ln["text"], voice, ln.get("style", ""), personas.get(ln["speaker"], ""), tmp)
            tmp.replace(out)
            return i

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, j) for j in jobs]
            done = 0
            for f in as_completed(futs):
                f.result()  # re-raise errors (quota etc.)
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    log(f"    {done}/{len(jobs)} lines voiced")
    for ln in script["lines"]:
        out = Path(ln.pop("_wav"))
        voice = ln["voice"]
        clean = out.with_name(out.stem + "_trim.wav")
        if not clean.exists():
            save_wav(clean, trim_silence(load_audio(out)))
        samples = load_audio(clean)
        ln["audio"] = str(clean)
        ln["audio_dur"] = round(samples.size / SAMPLE_RATE, 3)
        ln["voice"] = voice
    script["voice_engine"] = engine.name
    script["casting"] = casting
    return casting
