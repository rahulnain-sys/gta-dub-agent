"""Fact research with Gemini + Google Search grounding (free tier): confirmed vs claimed, timeline, sources."""
from __future__ import annotations

import time

from ..utils import log, warn


def research(topic: dict, cfg: dict, headlines: list[dict] | None = None) -> dict:
    from google.genai import types

    from .. import brain
    from .. import models as M

    client = brain._client()
    related = ""
    if headlines:
        words = [w.lower() for w in (topic.get("names") or []) if len(w) > 2]
        hits = [h for h in headlines if any(w in h["title"].lower() for w in words)][:12]
        related = "\n".join(f"- {h['source']}: {h['title']} ({h.get('url', '')})" for h in hits)
    prompt = f"""
Research this story for a Hindi YouTube news video. Search the web NOW for the latest information.
Story: {topic.get('title')} - {topic.get('angle', '')}
Search for: {topic.get('research_query') or topic.get('title')}
Related headlines we saw today:
{related or '(none)'}

Separate CONFIRMED facts (reported by reliable outlets / official statements / on-stream evidence) from CLAIMS
(rumours, allegations, one-sided statements). Never invent. If something is unclear, say so.
Return JSON only (no markdown):
{{"summary": "3-4 sentences", "confirmed": ["fact (outlet)"], "claimed": ["claim (who claims it)"],
  "timeline": [{{"when": "date/time", "what": "event"}}], "numbers": ["key stat"],
  "people": ["name - who they are"], "open_questions": ["..."],
  "sources": [{{"title": "...", "url": "https://..."}}]}}
"""
    last = None
    for mdl in M.candidates(client, "text", [cfg["brain"].get("model", "auto")]):
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=mdl, contents=prompt,
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                                       temperature=0.3))
                data = brain.as_obj(brain._extract_json(resp.text or ""), "confirmed")
                srcs = list(data.get("sources") or [])
                try:
                    gm = resp.candidates[0].grounding_metadata
                    for ch in (gm.grounding_chunks or []):
                        if getattr(ch, "web", None) and ch.web.uri:
                            srcs.append({"title": ch.web.title or ch.web.uri, "url": ch.web.uri})
                except Exception:  # noqa: BLE001
                    pass
                seen, uniq = set(), []
                for s_ in srcs:
                    u = s_.get("url")
                    if u and u not in seen:
                        seen.add(u)
                        uniq.append(s_)
                data["sources"] = uniq[:10]
                log(f"   research: {len(data.get('confirmed', []))} confirmed, {len(data.get('claimed', []))} claims, "
                    f"{len(data['sources'])} sources")
                return data
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                if M.is_gone_error(msg):
                    M.mark_bad(mdl)
                    break
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    break
                if attempt < 2:
                    time.sleep(5)
                    continue
                break
    warn(f"Web research unavailable ({str(last)[:120]}) - script will use today's headlines only.")
    return {"summary": topic.get("angle", ""), "confirmed": [], "claimed": [],
            "timeline": [], "numbers": [], "people": [], "open_questions": [],
            "sources": [{"title": h.get("title", ""), "url": h.get("url", "")} for h in (headlines or [])[:0]],
            "note": "no web research - headlines only"}
