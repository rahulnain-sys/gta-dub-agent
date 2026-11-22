"""The writers' room: Gemini watches the gameplay, logs every usable scene, plans shorts/episodes
and writes the Hinglish dub - always starring the channel's fixed main character."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .utils import env, log, slugify, warn

SAFETY_RULES = """
CONTENT RULES (must follow):
- Comedy must be light, PG-13. No slurs, no caste/religion/region jokes, no sexual content, no jokes about real
  people, politicians or celebrities, no self-harm.
- Never use names of real people or of characters from other franchises (no Shinchan, Franklin, Chop etc.).
  Use our series characters (below) or invent desi names / roles ("Taxi wala", "Police uncle").
- Keep violence cartoonish; never describe gore. No graphic violence in the first 7 seconds of a short.
- The original audio (streamer voice, someone else's voiceover/story, music) is IGNORED and replaced by our story.
  Judge only what is VISIBLE on screen.
"""

CATEGORIES = ("home, walking, driving, meeting_friends, group_talk, shopping, party, romance_drama, funny, fail, "
              "action, chase, stunt, crash, fight, npc_reaction, weird_physics, travel, other")


# ------------------------------------------------------------------ series bible

def main_id(cfg: dict) -> str:
    if not cfg["series"].get("enabled", False):
        return "hero"
    return slugify(cfg["series"]["main_character"]["name"], 20).replace("-", "_") or "hero"


def hero_name(cfg: dict) -> str:
    return cfg["series"]["main_character"]["name"] if cfg["series"].get("enabled", False) else "the main character"


GENRES = ("gangster / crime drama, desi comedy, classy gentleman (suave, sophisticated), heist thriller, "
          "romance drama, family comedy, action commentary, horror-comedy, rags-to-riches")


def _trends(cfg: dict) -> str:
    t = cfg.get("_trends") or []
    if not t:
        return ""
    return ("WHAT'S TRENDING RIGHT NOW on GTA YouTube (titles of today's most-viewed videos) - use them for fresh "
            "angles, topics and hook ideas; never copy titles:\n" + "\n".join(f"  - {x}" for x in t[:15]) + "\n")


def series_bible(cfg: dict, history: list[dict] | None = None) -> str:
    s = cfg["series"]
    if not s.get("enabled", False):
        return f"""
CHARACTERS & GENRE (dynamic - invent fresh for THIS footage):
- Look at the footage and choose the genre/tone that fits it best: {GENRES}.
- Invent a strong MAIN CHARACTER (id "hero") = the character the player controls / the camera follows. Give him/her a
  memorable desi name and a vivid personality that matches the genre (e.g. a feared don "Raghu Bhai", a clumsy
  show-off "Pappu", a polished gentleman "Mr. Malhotra").
- Invent supporting characters for other people on screen, each with a distinct personality.
- For EVERY character give "voice_type": a short description of how they sound (e.g. "deep gruff gangster",
  "young excited boy", "posh calm gentleman", "old strict woman", "nervous squeaky", "sweet young woman").
{_trends(cfg)}"""
    mc = s["main_character"]
    cast = "\n".join(
        f"  - id \"{slugify(c['name'], 20).replace('-', '_')}\": {c['name']} ({c.get('gender', 'male')}) - {c.get('persona', '')}"
        for c in s.get("recurring_cast", []) if c.get("name")
    ) or "  (none yet)"
    prev = ""
    if history:
        prev = "Previous videos on the channel (for continuity / running jokes - optional):\n" + "\n".join(
            f"  - {h.get('title', '')}: {h.get('summary', '')}" for h in history[-6:])
    channel = s.get("channel_name") or "our channel"
    return _trends(cfg) + f"""
SERIES BIBLE ({channel}) - every video belongs to one continuing series:
- MAIN CHARACTER: {mc['name']} (id "{main_id(cfg)}", {mc.get('gender', 'male')}) - {mc.get('persona', '')}
  {mc['name']} is ALWAYS the character the player controls / the camera follows. Every story is about {mc['name']}.
  Keep his personality identical in every video. Catchphrase (use sometimes, not always): "{mc.get('catchphrase', '')}"
- RECURRING CHARACTERS (reuse them whenever someone fitting appears on screen; use these exact ids):
{cast}
- Other people on screen: invent small roles (shopkeeper, police uncle, random aunty) with new ids.
{prev}
"""


# ------------------------------------------------------------------ Gemini plumbing

def _client():
    try:
        from google import genai
    except ImportError as e:  # pragma: no cover
        raise SystemExit("google-genai not installed. Run: pip install -r requirements.txt") from e
    global _LOGGED
    key = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY missing. Put it in the .env file (see README).")
    from google.genai import types as _t
    timeout_s = int(env("GEMINI_TIMEOUT_SECONDS") or 300)
    client = genai.Client(api_key=key, http_options=_t.HttpOptions(timeout=timeout_s * 1000))
    if not _LOGGED:
        _LOGGED = True
        from . import models as M
        M.log_choice(client)
    return client


_LOGGED = False


def _upload(client, path: Path):
    log(f"    uploading {path.name} to Gemini...")
    f = client.files.upload(file=str(path))
    for _ in range(240):
        f = client.files.get(name=f.name)
        state = getattr(f.state, "name", str(f.state))
        if state == "ACTIVE":
            return f
        if state == "FAILED":
            raise RuntimeError("Gemini could not process the video file.")
        time.sleep(2)
    raise TimeoutError("Gemini file processing timed out.")


def _extract_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise


def _generate(client, cfg: dict, contents, low_res: bool = False, model: str | None = None, want: str = "text",
              thinking: str | None = None):
    """Call Gemini with automatic model choice: skips retired models (404) and falls through on daily limits."""
    from google.genai import types

    from . import models as M

    b = cfg["brain"]
    configured = [model] if model else [b.get("model", "auto")]
    if want == "text":
        configured += list(b.get("fallback_models") or [])
    ordered = M.candidates(client, want, configured)
    last_err = None
    errors = []
    think = thinking or b.get("thinking", "low")
    for mdl in ordered:
        lr = low_res
        use_think = think not in (None, "", "off", "default")
        for attempt in range(4):
            try:
                kwargs = dict(response_mime_type="application/json", temperature=b["temperature"])
                if use_think:
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=think)
                if lr:
                    kwargs["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
                resp = client.models.generate_content(model=mdl, contents=contents,
                                                      config=types.GenerateContentConfig(**kwargs))
                return _extract_json(resp.text)
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e)
                low = msg.lower()
                if use_think and "think" in low:
                    use_think = False  # model doesn't support thinking_level
                    continue
                if "timed out" in low or "timeout" in low or "readtimeout" in low:
                    warn(f"{mdl} took too long (timeout) - {'retrying' if attempt == 0 else 'trying the next model'}")
                    if attempt == 0:
                        continue
                    errors.append(f"{mdl}: timeout")
                    break
                if M.is_gone_error(msg) and "media_resolution" not in msg:
                    M.mark_bad(mdl)
                    warn(f"{mdl} is not available to your key - trying the next model")
                    errors.append(f"{mdl}: not available")
                    break
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    if "per day" in msg.lower() or "PerDay" in msg or attempt >= 2:
                        warn(f"{mdl} hit its limit - trying the next model")
                        break
                    wait = 15 * (attempt + 1)
                    warn(f"{mdl} busy, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                if "503" in msg or "UNAVAILABLE" in msg or "500" in msg:
                    time.sleep(8 * (attempt + 1))
                    continue
                if "media_resolution" in msg and lr:
                    lr = False
                    continue
                if isinstance(e, json.JSONDecodeError):
                    warn(f"{mdl} returned invalid JSON, retrying")
                    continue
                warn(f"{mdl} failed: {msg[:200]}")
                errors.append(f"{mdl}: {msg[:120]}")
                break
    raise RuntimeError("All Gemini models failed: " + " | ".join(errors[-6:]) + f" | last error: {str(last_err)[:300]}")


def as_obj(data, key: str) -> dict:
    """Gemini sometimes answers with a bare JSON list instead of {key: [...]}. Normalise to a dict."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict) and key in data[0]:
            return data[0]
        return {key: [x for x in data if isinstance(x, dict)]}
    return {key: []}


def _safe_delete(client, f) -> None:
    try:
        client.files.delete(name=f.name)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 1) scene log for a chunk

def scan_chunk(proxy_chunk: Path, offset: float, chunk_len: float, cfg: dict, hotspots: list[dict],
               style_note: str = "") -> list[dict]:
    """Watch one chunk and log EVERY usable scene (not just funny ones). Times returned are absolute."""
    client = _client()
    f = _upload(client, proxy_chunk)
    hints = "\n".join(f"- {h['t'] - offset:.0f}s: {h['reason']}"
                      for h in hotspots if offset <= h["t"] < offset + chunk_len) or "- (none)"
    prompt = f"""
You are the scene logger for a Hindi/Hinglish GTA story-dub channel. We take gameplay footage, throw away ALL of its
original audio (even if someone already did a voiceover or story on it), and write our OWN story and voices.
So judge ONLY the visuals.

Watch this clip from start to end and log EVERY usable scene, covering the whole clip (scenes may touch each other).
Everyday scenes are VALUABLE - walking around a house/bungalow, getting into a car, driving through the city,
meeting friends, standing and talking, shopping, eating, phone calls - because we build stories out of them.
Also log the exciting ones: stunts, crashes, fails, chases, fights, funny physics, NPCs reacting.
Only skip: black/loading/menu/map screens, title cards, text slides, intro/outro/subscribe animations.

Energy hotspots measured from audio/video (seconds from the start of this clip) - check them, they often contain action:
{hints}

For each scene:
- start, end: seconds from the start of THIS clip (scene length 5-90 s; cut where the action changes)
- category: one of [{CATEGORIES}]
- score 1-10: how USABLE it is for a story (clear visible characters/action = high; empty/blurry = low)
- funny 1-10: potential for a funny short on its own (stunt, fail, crash, awkward moment = high)
- characters: number of clearly visible people
- standalone: true if it could be a short by itself
- focus_x: horizontal centre of the main character/action 0-1 (we crop a narrow vertical strip around it)
- facecam: IMPORTANT - if a streamer's webcam / face video / big logo overlay is visible ANYWHERE in this scene,
  give its box [x, y, w, h] as fractions of the frame (x,y = top-left corner). We remove it. null only if none.
- what: one line describing exactly what is visible (who, where, doing what)
- story_use: one line on how it could fit a story (e.g. "hero leaves home for the big deal")
{style_note}
{SAFETY_RULES}
Return JSON only: {{"scenes": [{{"start": 0, "end": 0, "category": "", "score": 0, "funny": 0, "characters": 0,
 "standalone": false, "focus_x": 0.5, "facecam": null, "what": "", "story_use": ""}}]}}
"""
    try:
        data = _generate(client, cfg, [f, prompt], low_res=True)
    finally:
        _safe_delete(client, f)
    scenes = as_obj(data, "scenes").get("scenes") or []
    out = []
    for sc in scenes:
        try:
            s0, e0 = float(sc["start"]), float(sc["end"])
        except (KeyError, TypeError, ValueError):
            continue
        s0, e0 = max(0.0, s0), min(chunk_len, e0)
        if e0 - s0 < 3:
            continue
        sc["start"], sc["end"] = round(s0 + offset, 2), round(e0 + offset, 2)
        sc["score"] = float(sc.get("score", 5) or 5)
        sc["funny"] = float(sc.get("funny", sc["score"]) or 0)
        out.append(sc)
    return out


def _scene_table(scenes: list[dict]) -> str:
    return "\n".join(
        f"[{i}] v{int(sc.get('src', 0)) + 1} {sc['start']:.0f}-{sc['end']:.0f}s ({sc['end'] - sc['start']:.0f}s) {sc.get('category', '')} "
        f"use={sc.get('score')} funny={sc.get('funny')} ppl={sc.get('characters', '?')} | {sc.get('what', '')}"
        f" | {sc.get('story_use', '')}"
        for i, sc in enumerate(scenes))


def _segments_from_refs(refs, scenes, used: set[int] | None, allow_reuse: bool = False) -> list[dict]:
    segs = []
    for ref in refs or []:
        try:
            idx = int(ref["scene"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= idx < len(scenes)) or (used is not None and idx in used and not allow_reuse):
            continue
        sc = scenes[idx]
        try:
            s0 = float(ref.get("start", sc["start"]))
            e0 = float(ref.get("end", sc["end"]))
        except (TypeError, ValueError):
            s0, e0 = sc["start"], sc["end"]
        s0, e0 = max(sc["start"] - 1, s0), min(sc["end"] + 1, e0)
        if e0 - s0 < 3:
            s0, e0 = sc["start"], sc["end"]
        if used is not None:
            used.add(idx)
        segs.append({"start": round(s0, 2), "end": round(e0, 2), "focus_x": sc.get("focus_x", 0.5),
                     "facecam": sc.get("facecam"), "what": sc.get("what", ""), "category": sc.get("category", ""),
                     "src": int(sc.get("src", 0))})
    return segs


# ------------------------------------------------------------------ 2a) plan shorts

def plan_shorts(scenes: list[dict], cfg: dict, max_shorts: int, video_title: str = "", style_note: str = "",
                history: list | None = None) -> list[dict]:
    client = _client()
    b = cfg["brain"]
    prompt = f"""
You are the showrunner of a Hindi/Hinglish GTA story-dub channel. Below is the scene log of gameplay footage
(it may come from several videos: v1, v2...).
Plan up to {max_shorts} YouTube Shorts. Each Short = one quick mini-story (funny, gangster swag, dramatic -
whatever the footage suits) with a killer hook in the first 2 seconds.
{series_bible(cfg, history)}
Each Short is either:
- "single": one scene (a stunt, a fail, a crash, an awkward meeting, a funny moment), or
- "compilation": 2-4 scenes stitched with ONE story (e.g. "the don's car deal goes wrong: arrives in style ->
  crash -> runs from police"). Ordinary scenes become exciting or funny through the story/dialogue.
Rules: total {b['min_clip_sec']}-{b['max_clip_sec']} s; hook in the first 2 s; strong payoff at the end; never reuse a scene
in two Shorts; you may trim a scene to its best part (sub-range inside it). Prefer scenes with visible characters.
Make as many good Shorts as the material allows (the material is always usable - stories make it funny).
Humour style: {b['humour_style']}. {style_note}
Video: "{video_title}"
SCENE LOG:
{_scene_table(scenes)}

Return JSON only:
{{"shorts": [{{"type": "single|compilation", "concept": "one-line story", "title_idea": "...",
  "scenes": [{{"scene": 0, "start": 0.0, "end": 0.0}}]}}]}}
"""
    data = _generate(client, cfg, [prompt], model=b.get("planner_model"), want="pro",
                     thinking=b.get("creative_thinking", "medium"))
    used: set[int] = set()
    out = []
    for p in data.get("shorts", data if isinstance(data, list) else []):
        segs = _segments_from_refs(p.get("scenes"), scenes, used)
        if segs and sum(s["end"] - s["start"] for s in segs) >= 8:
            p["segments"] = segs
            out.append(p)
    return out[:max_shorts]


def fallback_shorts(scenes: list[dict], cfg: dict, max_shorts: int) -> list[dict]:
    b = cfg["brain"]
    best = sorted(scenes, key=lambda s: (s.get("funny", 0) + s.get("score", 0)), reverse=True)
    out = []
    for sc in best:
        if len(out) >= max_shorts:
            break
        s0, e0 = sc["start"], min(sc["end"], sc["start"] + b["max_clip_sec"])
        if e0 - s0 < 8:
            continue
        out.append({"type": "single", "concept": sc.get("story_use", ""), "title_idea": "",
                    "segments": [{"start": s0, "end": e0, "focus_x": sc.get("focus_x", 0.5),
                                  "facecam": sc.get("facecam"), "what": sc.get("what", ""),
                                  "category": sc.get("category", ""), "src": int(sc.get("src", 0))}]})
    return out


# ------------------------------------------------------------------ 2b) plan a long episode

def plan_episode(scenes: list[dict], cfg: dict, target_min: float, video_title: str = "", style_note: str = "",
                 history: list | None = None) -> dict:
    client = _client()
    b = cfg["brain"]
    total_avail = sum(s["end"] - s["start"] for s in scenes) / 60
    prompt = f"""
You are the head writer of a Hindi/Hinglish GTA story-dub YouTube channel. Below is the scene log of a gameplay video.
Build ONE long-form story episode of about {target_min:.0f} minutes (allowed {target_min * 0.75:.0f}-{target_min * 1.25:.0f} min;
available footage: {total_avail:.0f} min). Scenes may come from different videos (v1, v2...) - stitch them into ONE story.
{series_bible(cfg, history)}
How to build it:
- Pick the GENRE that suits the footage ({GENRES}) and a simple, gripping story
  (e.g. "a small-time driver becomes the city's biggest don in one day", "a gentleman's perfect date goes wrong",
  "the gang's big heist and the one friend who ruins it").
- COLD OPEN (very important): choose the single most dramatic/funny 15-35 s moment (usually from the climax) as a
  flash-forward teaser shown FIRST. The first 30-50 seconds must make viewers stay: tease the conflict, ask a
  question, promise a payoff ("...lekin ye sab shuru kaise hua? Chaliye 24 ghante peeche chalte hain.").
- Split into 3-6 acts: setup -> problem -> rising chaos -> climax -> funny ending.
- Use the scenes as footage for the story. Ordinary scenes (walking at home, driving, meeting friends) are fine -
  dialogue and narration make them part of the story. Keep mostly chronological order but you may reorder.
- You may trim scenes (sub-range). Skip boring/empty parts. Don't use the same scene twice.
Humour style: {b['humour_style']}. {style_note}
Video: "{video_title}"
SCENE LOG:
{_scene_table(scenes)}

Return JSON only:
{{"title": "catchy Hinglish YouTube title (max 80 chars)", "logline": "one-line story", "genre": "...",
  "cold_open": {{"scene": 0, "start": 0.0, "end": 0.0, "teaser_idea": "what the hook narration says"}},
  "thumbnail_text": "2-4 punchy words", "description": "2-3 lines", "hashtags": ["#..."],
  "acts": [{{"name": "Act name", "beat": "what happens in the story in this act",
            "scenes": [{{"scene": 0, "start": 0.0, "end": 0.0}}]}}]}}
"""
    data = _generate(client, cfg, [prompt], model=b.get("planner_model"), want="pro",
                     thinking=b.get("creative_thinking", "medium"))
    used: set[int] = set()
    acts = []
    for a in data.get("acts", []):
        segs = _segments_from_refs(a.get("scenes"), scenes, used)
        if segs:
            acts.append({"name": a.get("name", ""), "beat": a.get("beat", ""), "segments": segs})
    data["acts"] = acts
    if not acts:
        raise RuntimeError("Episode planner returned no usable acts")
    co = data.get("cold_open")
    data["cold_open_segment"] = None
    if isinstance(co, dict):
        seg = _segments_from_refs([co], scenes, None)
        if seg:
            seg[0]["end"] = min(seg[0]["end"], seg[0]["start"] + 35)
            seg[0]["teaser_idea"] = co.get("teaser_idea", "")
            data["cold_open_segment"] = seg[0]
    return data


def fallback_episode(scenes: list[dict], cfg: dict, target_min: float) -> dict:
    """Chronological best-of: highest-usability scenes in time order until the target length."""
    target = target_min * 60
    ranked = sorted(scenes, key=lambda s: s.get("score", 0), reverse=True)
    chosen, tot = [], 0.0
    for sc in ranked:
        if tot >= target:
            break
        chosen.append(sc)
        tot += sc["end"] - sc["start"]
    chosen.sort(key=lambda s: (s.get("src", 0), s["start"]))
    segs = [{"start": s["start"], "end": s["end"], "focus_x": s.get("focus_x", 0.5), "facecam": s.get("facecam"),
             "what": s.get("what", ""), "category": s.get("category", ""), "src": int(s.get("src", 0))} for s in chosen]
    third = max(1, len(segs) // 3)
    acts = [{"name": n, "beat": "", "segments": segs[i * third:(i + 1) * third if i < 2 else None]}
            for i, n in enumerate(["Shuruaat", "Gadbad", "Climax"])]
    return {"title": "GTA mein ek din ki kahani 😂", "logline": "", "cold_open": None,
            "thumbnail_text": "", "description": "", "hashtags": [], "acts": [a for a in acts if a["segments"]]}


# ------------------------------------------------------------------ 3a) write a short

def _boundary_note(boundaries: list | None) -> str:
    if not boundaries or len(boundaries) < 2:
        return ""
    parts = ", ".join(f"scene {i + 1}: {b['start']:.1f}-{b['end']:.1f}s ({b.get('what', '')})" for i, b in enumerate(boundaries))
    return ("This clip is a COMPILATION of several scenes stitched together: " + parts + ". "
            "Write ONE connected story across the scenes and bridge each jump with a line "
            "(e.g. 'Aur phir...', 'Paanch minute baad...'). Keep the best punchline for the last scene.")


def _line_rules(cfg: dict, long_form: bool = False) -> str:
    b = cfg["brain"]
    density = ("Leave natural pauses; it's fine to let action breathe for a few seconds between lines."
               if long_form else "Don't fill every second.")
    return f"""
Rules for the dialogue:
- Natural {b['language']} the way Indians actually talk on YouTube/Instagram (Roman script only, no Devanagari).
- Lines are SHORT (3-14 words). People speak ~2.5 words/second, so (end - start) must fit the words. {density}
- Time every line to on-screen action, in seconds from the start of THIS clip. Lines must not overlap.
- The player character on screen is always our main character. Match who is visibly talking/doing what.
- A "narrator" (desi commentator voice) may link scenes and add jokes; use id "narrator".
- Every line has "style": an acting direction (e.g. "panicked, high pitch", "smug and lazy", "dramatic TV-serial mother").
- Optional "sfx" at key moments, from: boing, airhorn, bruh, vineboom, laugh, crash, slap, wow.
"""


def write_script(clip: Path, cfg: dict, idea: str = "", style_note: str = "", boundaries: list | None = None,
                 history: list | None = None) -> dict:
    client = _client()
    f = _upload(client, clip)
    b = cfg["brain"]
    prompt = f"""
You are a top Indian writer and voice director. This is a short GTA gameplay clip for a YouTube Short.
We MUTE the original audio and dub it with a new story and voices (funny, gangster, classy - whatever suits it).
{series_bible(cfg, history)}
Story idea (optional): {idea}
{_boundary_note(boundaries)}
Humour style: {b['humour_style']}. {style_note}
First line within 1.5 s must hook the viewer (a shocking line, a question, a bold claim); end on a clear payoff/punchline.
{_line_rules(cfg)}
{SAFETY_RULES}
Also write: "title" (catchy Hinglish Shorts title, max 70 chars, max 1 emoji), "hook_text" (2-6 words shown at the
start), "description" (1-2 lines), "hashtags" (5-8).
Return JSON only:
{{"title": "...", "hook_text": "...", "description": "...", "hashtags": ["#..."],
 "characters": [{{"id": "{main_id(cfg)}", "name": "...", "gender": "male|female", "persona": "...", "voice_type": "..."}}],
 "lines": [{{"start": 0.4, "end": 2.6, "speaker": "{main_id(cfg)}", "text": "...", "style": "..."}}],
 "sfx": [{{"at": 5.2, "name": "vineboom"}}]}}
"""
    try:
        data = _generate(client, cfg, [f, prompt], thinking=b.get("creative_thinking", "medium"))
    finally:
        _safe_delete(client, f)
    return normalise_script(data)


# ------------------------------------------------------------------ 3b) write one part of an episode

def write_episode_part(clip: Path, cfg: dict, outline: dict, part_idx: int, n_parts: int, beat: str,
                       boundaries: list, known_chars: list[dict], previous_lines: list[dict],
                       style_note: str = "", history: list | None = None) -> dict:
    client = _client()
    f = _upload(client, clip)
    b = cfg["brain"]
    acts = "\n".join(f"  {i + 1}. {a.get('name', '')}: {a.get('beat', '')}" for i, a in enumerate(outline.get("acts", [])))
    chars = "\n".join(f"  - id \"{c['id']}\": {c.get('name', '')} ({c.get('gender', '')}) {c.get('persona', '')}"
                      f" | voice: {c.get('voice_type', '')}" for c in known_chars) or "  (none yet - invent them now)"
    recap = "\n".join(f"  {ln['speaker']}: {ln['text']}" for ln in previous_lines[-10:]) or "  (this is the beginning)"
    scenes = "; ".join(f"{bd['start']:.0f}-{bd['end']:.0f}s: {bd.get('what', '')}" for bd in boundaries)
    co = outline.get("cold_open_segment")
    opening = ("This is the OPENING. It starts with the COLD OPEN teaser (first "
               f"{(co['end'] - co['start']):.0f} s: {co.get('what', '')}; idea: {co.get('teaser_idea', '')}). "
               "Narrate a gripping hook over it, then transition ('...lekin ye sab shuru kaise hua?') into the story. "
               "The first 30-50 seconds must hook the viewer hard: conflict, question, promise of payoff."
               if co else
               "This is the OPENING: the first 30-50 seconds must hook the viewer hard - tease the conflict, ask a "
               "question, promise a payoff - then set up the story.")
    position = (opening
                if part_idx == 0 else
                "This is the FINAL part: deliver the climax and a satisfying/funny ending, then a short sign-off "
                "line from the main character (ask viewers to subscribe for the next part, in character)." if part_idx == n_parts - 1 else
                "This is a middle part: keep the story moving and keep the jokes coming.")
    prompt = f"""
You are the head writer and voice director of a Hindi/Hinglish GTA story-dub channel. We MUTE the original audio
and dub our own story. You are writing PART {part_idx + 1} of {n_parts} of a long episode.
Genre/tone: {outline.get('genre', 'whatever fits the footage')}.
{series_bible(cfg, history)}
EPISODE: "{outline.get('title', '')}" - {outline.get('logline', '')}
ACTS:
{acts}
THIS PART's story beat: {beat}
Scenes inside this clip (seconds from its start): {scenes}
{position}
Characters already introduced (reuse these exact ids and keep their personalities):
{chars}
Last lines of the previous part (continue naturally from here):
{recap}
Humour style: {b['humour_style']}. {style_note}
{_line_rules(cfg, long_form=True)}
{SAFETY_RULES}
Return JSON only:
{{"characters": [{{"id": "...", "name": "...", "gender": "male|female", "persona": "...", "voice_type": "..."}}],
 "lines": [{{"start": 0.0, "end": 0.0, "speaker": "...", "text": "...", "style": "..."}}],
 "sfx": [{{"at": 0.0, "name": "..."}}]}}
"""
    try:
        data = _generate(client, cfg, [f, prompt], thinking=b.get("creative_thinking", "medium"))
    finally:
        _safe_delete(client, f)
    return data


# ------------------------------------------------------------------ helpers

def normalise_script(data: dict, cfg: dict | None = None) -> dict:
    data = dict(data or {})
    chars = [dict(c) for c in (data.get("characters") or [])]
    ids = set()
    for c in chars:
        c["id"] = str(c.get("id") or c.get("name") or "char").strip().lower().replace(" ", "_")
        c.setdefault("name", c["id"].replace("_", " ").title())
        c["gender"] = "female" if str(c.get("gender", "")).lower().startswith("f") else "male"
        ids.add(c["id"])
    lines = []
    for ln in data.get("lines") or []:
        ln = dict(ln)
        try:
            ln["start"] = float(ln["start"])
            ln["end"] = float(ln.get("end", ln["start"] + 2.0))
        except (KeyError, TypeError, ValueError):
            continue
        text = str(ln.get("text", "")).strip()
        if not text:
            continue
        ln["text"] = text
        spk = str(ln.get("speaker", "narrator")).strip().lower().replace(" ", "_")
        ln["speaker"] = spk
        ln.setdefault("style", "natural, funny")
        if spk not in ids:
            chars.append({"id": spk, "name": spk.replace("_", " ").title(), "gender": "male",
                          "persona": "desi commentator" if spk == "narrator" else ""})
            ids.add(spk)
        lines.append(ln)
    lines.sort(key=lambda x: x["start"])
    data["characters"] = chars
    data["lines"] = lines
    data["sfx"] = [s for s in (data.get("sfx") or []) if isinstance(s, dict) and "at" in s and "name" in s]
    data.setdefault("title", "GTA but desi 😂")
    data.setdefault("hook_text", "")
    data.setdefault("description", "")
    data.setdefault("hashtags", [])
    return data
