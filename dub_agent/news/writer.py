"""Narrator script writer: one storyteller voice, beats tied to real scenes, hook first, facts checked."""
from __future__ import annotations

from .topics import STYLE

WPS = 2.6  # Hinglish narration words per second (fast YouTube storyteller pace)


def scene_catalog(footage: list[dict], max_per_video: int = 40) -> str:
    rows = []
    for vi, it in enumerate(footage):
        if not it.get("scenes"):
            continue
        rows.append(f"VIDEO v{vi + 1}: \"{it.get('title', '')[:80]}\" by {it.get('channel', '?')}")
        best = sorted(it["scenes"], key=lambda s: -float(s.get("score", 0)))[:max_per_video]
        for sj, sc in enumerate(sorted(best, key=lambda s: s["start"])):
            sc["_ref"] = f"v{vi + 1}#{sj + 1}"
            rows.append(f"  {sc['_ref']} [{sc['start']:.0f}-{sc['end']:.0f}s, {sc['end'] - sc['start']:.0f}s] "
                        f"{sc.get('category', '')} use={sc.get('score')}: {sc.get('what', '')}")
    return "\n".join(rows) or "(no footage scenes available - use cards)"


def write_script(topic: dict, facts: dict, footage: list[dict], fmt: str, cfg: dict, minutes: float = 10,
                 extra: str = "") -> dict:
    from .. import brain

    n = cfg.get("news", {})
    client = brain._client()
    if fmt == "short":
        secs = float(n.get("short_seconds", 50))
        length = (f"A YouTube SHORT of {secs - 8:.0f}-{secs + 8:.0f} seconds = {int((secs - 8) * WPS)}-{int((secs + 8) * WPS)} "
                  "spoken words in total (NOT less - a 25-second Short is too thin). Story arc in 5-8 beats:\n"
                  "  1) HOOK (max 2 s, max 8 words) naming the person/thing + the shock\n"
                  "  2) SETUP: what was expected / what they said before\n"
                  "  3) PROOF: what actually happened, with the number/time/quote (show it on screen)\n"
                  "  4) TWIST: the detail nobody expected\n"
                  "  5) QUESTION to the viewer for comments.\n"
                  "Beat 1's scene must be the strongest visual (the creator's face/stream or the in-game moment), "
                  "never a logo, a dark frame or a generic trailer shot. Use \"card\" for time jumps and numbers "
                  "the viewer should see (e.g. \"17 HOURS LATER\", \"₹25 LAKH/MONTH?\").")
    else:
        length = (f"A LONG video of about {minutes:.0f} minutes = about {int(minutes * 60 * WPS)} spoken words in total, "
                  "in 4-7 chapters. The first 30-50 seconds are a COLD OPEN that teases the most shocking part and "
                  "promises the payoff ('...par asli kahani isse kahin badi hai'). Then the story in order "
                  "(background -> what happened -> reactions -> what's confirmed vs only claimed -> what next). "
                  "End with a question for comments and a subscribe line.")
    gaming_rule = ("This is a GAMING video: every visual must be gaming footage (streams, gameplay, esports, official "
                   "game trailers). No movie/celebrity footage." if topic.get("category") == "gaming" else
                   "This is an ENTERTAINMENT video (movies/series/stars/music).")
    facts_txt = "\n".join([f"SUMMARY: {facts.get('summary', '')}",
                           "CONFIRMED: " + "; ".join(facts.get("confirmed", [])[:15]),
                           "CLAIMED (not confirmed): " + "; ".join(facts.get("claimed", [])[:10]),
                           "TIMELINE: " + "; ".join(f"{t.get('when', '')}: {t.get('what', '')}"
                                                     for t in facts.get("timeline", [])[:15]),
                           "NUMBERS: " + "; ".join(facts.get("numbers", [])[:8]),
                           "PEOPLE: " + "; ".join(facts.get("people", [])[:10])])
    prompt = (extra + "\n" if extra else "") + f"""
You are the head writer + video editor of a top Hindi/Hinglish YouTube channel (gaming + entertainment news).
Write the narration for ONE video and choose the footage for every beat.
{STYLE}
TOPIC: {topic.get('title')}
ANGLE: {topic.get('angle', '')}
HOOK IDEA: {topic.get('hook', '')}
{length}
{gaming_rule}

VERIFIED RESEARCH (use only these facts; label claims as claims - "dawa kiya gaya", "abhi confirm nahi hai"):
{facts_txt}

FOOTAGE SCENES you can show (pick the scene that matches what the narration says at that moment):
{scene_catalog(footage)}

WRITING RULES:
- Voice: one energetic storyteller, natural spoken Hinglish (Roman script, no Devanagari), like a friend telling a
  crazy story - short sentences, rhythm, suspense, no filler, no robotic news-reader tone, no long pauses.
- "speak": the SAME words as "narration", written in Devanagari exactly as a Hindi speaker says them, so the
  voice engine pronounces them right. English words go phonetically into Devanagari (gaming -> गेमिंग,
  GTA 6 -> जीटीए सिक्स, streamer -> स्ट्रीमर), numbers as spoken words (52 -> बावन, 1 crore -> एक करोड़),
  names phonetically (IShowSpeed -> आईशोस्पीड). "narration" stays Roman (it is the on-screen caption).
- Every beat = 1-3 sentences (8-35 words) + the scene shown during it. Match words to visuals ("dekho yahan...").
- "raw": true on a beat = play that scene's ORIGINAL audio (a streamer's reaction/quote) for max 10 seconds with NO
  narration (narration ""). Use 0-3 raw beats in a long video, 0-1 in a Short, only when the original sound is the
  point. Give "raw_seconds" (3-10).
- "card": optional on-screen text for the beat (a number, a name, "CONFIRMED", "SIRF DAWA", a date) - max 5 words.
- If no scene fits, set "scene": null and give a "card" text.
- Facts only from the research. No slurs, no insults, neutral on controversies.

Return JSON only:
{{"title": "...", "thumbnail_text": "2-4 words",
  "description": "the FULL story retold in 3-5 short English paragraphs with the real names, numbers, times and quotes (this is what search reads), then one question line for comments",
  "tags": ["12-15 specific search phrases people type, e.g. 'IShowSpeed GTA 6', 'Speed name in GTA 6', 'GTA 6 marathon stream' - names + game + event, no generic words"],
  "hashtags": ["3-5 hashtags"], "beats": [{{"chapter": "Chapter name (long only)", "narration": "...", "speak": "देवनागरी में वही लाइन",
  "scene": "v1#3", "raw": false, "raw_seconds": 0, "card": null}}]}}
"""
    data = brain.as_obj(brain._generate(client, cfg, [prompt], thinking=cfg["brain"].get("creative_thinking", "medium")), "beats")
    beats = []
    for b in data.get("beats") or []:
        narr = str(b.get("narration") or "").strip()
        raw = bool(b.get("raw"))
        if not narr and not raw:
            continue
        beats.append({"chapter": b.get("chapter") or "", "narration": narr,
                      "speak": str(b.get("speak") or "").strip(), "scene": b.get("scene"),
                      "raw": raw, "raw_seconds": min(10.0, max(2.0, float(b.get("raw_seconds") or 6))) if raw else 0,
                      "card": b.get("card")})
    data["beats"] = beats
    data.setdefault("title", topic.get("title", ""))
    data.setdefault("thumbnail_text", "")
    data.setdefault("description", topic.get("angle", ""))
    data.setdefault("hashtags", [])
    data["tags"] = [str(t).strip().lstrip("#") for t in (data.get("tags") or []) if str(t).strip()][:15]
    data["category"] = topic.get("category", "gaming")
    return data
