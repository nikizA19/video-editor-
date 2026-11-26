"""Turn the user's instructions into a concrete edit plan.

Two planners:
* Auto (free, offline): keyword rules + heuristics that copy the usual
  talking-head style (jump cuts, alternating punch-ins, a whoosh now and then).
* Claude (optional): if ANTHROPIC_API_KEY is set, Claude reads the transcript,
  the instructions and frames from the reference videos and decides the edit.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .analysis import Segment

Zoom = Literal["none", "punch_in", "slow_zoom_in", "slow_zoom_out"]
Transition = Literal["cut", "fade", "whoosh_slide", "flash"]
Sfx = Literal["none", "whoosh", "pop", "click", "boom", "ding"]


class SegmentEdit(BaseModel):
    index: int
    keep: bool
    zoom: Zoom
    transition_in: Transition  # how we enter this segment from the previous one
    sfx: Sfx


class TextPopup(BaseModel):
    segment_index: int
    text: str


class EditPlan(BaseModel):
    summary: str
    segments: list[SegmentEdit]
    captions: bool
    caption_style: Literal["bold_yellow", "clean_white", "boxed"]
    caption_uppercase: bool
    words_per_caption: int
    text_popups: list[TextPopup]
    color_grade: Literal["none", "warm", "cool", "punchy", "cinematic"]
    output_format: Literal["original", "vertical_9_16", "horizontal_16_9", "square_1_1"]
    music_volume: float  # 0..1, only used if music was uploaded


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def pause_preset_from(instructions: str, chosen: str) -> str:
    t = instructions.lower()
    if _has(t, "ne mahai pauz", "не махай пауз", "keep pauses", "bez rqzane", "без рязане"):
        return "off"
    if _has(t, "agresiv", "агресив", "tight", "brzo", "бързо", "dinamich", "динамич", "fast pace"):
        return "tight"
    return chosen or "normal"


# --------------------------------------------------------------- auto planner

def auto_plan(segments: list[Segment], instructions: str, options: dict,
              retakes: set[int], references: list[dict]) -> EditPlan:
    t = instructions.lower()
    no_zoom = _has(t, "bez zoom", "без zoom", "без зуум", "no zoom", "bez zuum")
    much_zoom = _has(t, "mnogo zoom", "много zoom", "повече zoom", "poveche zoom", "more zoom")
    no_sfx = _has(t, "bez zvuk", "без звук", "no sound effect", "no sfx", "bez efekti", "без ефекти")
    no_trans = _has(t, "bez prehod", "без преход", "no transition", "bez transition")
    no_caps = _has(t, "bez nadpis", "без надпис", "no caption", "bez subtit", "без субтит")
    if not options.get("captions", True) or not any(s.words for s in segments):
        no_caps = True

    # Pace from the reference: fast-cut references get more punch-ins.
    shots = [r["avg_shot_seconds"] for r in references if r.get("avg_shot_seconds")]
    zoom_every = 2
    if much_zoom or (shots and min(shots) < 2.5):
        zoom_every = 1

    edits = []
    since_sfx = 99.0
    kept_i = 0
    for seg in segments:
        keep = seg.index not in retakes
        zoom: Zoom = "none"
        trans: Transition = "cut"
        sfx: Sfx = "none"
        if keep:
            if not no_zoom:
                if kept_i % 2 == 1:
                    zoom = "punch_in"  # alternate framing on every jump cut
                elif seg.duration > 3.5 and (zoom_every == 1 or kept_i % 6 == 4):
                    zoom = "slow_zoom_in"
            if kept_i > 0 and seg.pause_before > 1.2 and since_sfx > 8 and not no_trans:
                trans = "whoosh_slide"
                if not no_sfx:
                    sfx = "whoosh"
                since_sfx = 0
            since_sfx += seg.duration
            kept_i += 1
        edits.append(SegmentEdit(index=seg.index, keep=keep, zoom=zoom,
                                 transition_in=trans, sfx=sfx))

    popups = []
    m = re.search(r"(?:title|заглавие|zaglavie)\s*[:=]\s*[\"„“]?([^\"\n“”,.;!]+)", instructions, re.I)
    if m and segments:
        popups.append(TextPopup(segment_index=next((e.index for e in edits if e.keep), 0),
                                text=m.group(1).strip()[:60]))

    grade = "punchy"
    if _has(t, "topl", "топл", "warm"):
        grade = "warm"
    elif _has(t, "studen", "студен", "cool"):
        grade = "cool"
    elif _has(t, "kino", "кино", "cinematic", "filmov", "филмов"):
        grade = "cinematic"
    elif _has(t, "bez cvet", "без цвят", "без цветове", "no color", "no grade"):
        grade = "none"

    fmt = options.get("output_format") or "original"
    if _has(t, "tiktok", "reels", "shorts", "9:16", "vertikal", "вертикал"):
        fmt = "vertical_9_16"

    dropped = len(retakes)
    zooms = sum(1 for e in edits if e.keep and e.zoom != "none")
    sfxs = sum(1 for e in edits if e.keep and e.sfx != "none")
    summary = (
        f"Авто монтаж: {sum(e.keep for e in edits)} кадъра, махнати {dropped} повторени дубли, "
        f"{zooms} zoom-а, {sfxs} звукови ефекта"
        + ("" if no_caps else ", надписи") + "."
    )
    return EditPlan(
        summary=summary,
        segments=edits,
        captions=not no_caps,
        caption_style=options.get("caption_style") or "bold_yellow",
        caption_uppercase=_has(t, "glavni bukvi", "главни букви", "uppercase", "caps"),
        words_per_caption=3,
        text_popups=popups,
        color_grade=grade,
        output_format=fmt,
        music_volume=0.15,
    )


# ------------------------------------------------------------- claude planner

SYSTEM = """You are the editor inside a simple AI video studio. The footage has already
been split into short speech segments (silences removed). Decide how to edit it.

House style (unless the user asks otherwise): fast talking-head edit. Drop failed takes,
repeated sentences, false starts and off-topic rambling (keep=false). Alternate framing
with punch-in zooms on cuts so jump cuts feel intentional, use slow zooms on emotional or
important lines. Use transitions sparingly (topic changes), mostly plain cuts. Add a sound
effect only where it helps: whoosh with whoosh_slide transitions, pop for text popups or
lists, ding for a key point, boom for a big reveal. Never more than one SFX per ~6 seconds.
Text popups: 1-4 words, punchy keywords or numbers the speaker emphasises.
Return exactly one SegmentEdit per input segment, same index order.
Write `summary` in the same language the user wrote in (often Bulgarian in Latin letters:
answer in Bulgarian Cyrillic then), 1-3 short sentences describing what you did."""


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def claude_plan(segments: list[Segment], instructions: str, options: dict,
                references: list[dict], previous: EditPlan | None) -> EditPlan:
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "user_instructions": instructions,
        "studio_options": options,
        "segments": [s.to_dict() for s in segments],
        "references": [{k: v for k, v in r.items() if k != "frames"} for r in references],
    }
    if previous is not None:
        payload["previous_plan_user_wants_changed"] = previous.model_dump()
    content: list[dict] = []
    for ref in references:
        for frame in ref.get("frames", [])[:4]:
            p = Path(frame)
            if p.exists() and p.stat().st_size < 4_000_000:
                mt = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": mt,
                    "data": base64.b64encode(p.read_bytes()).decode()}})
    if content:
        content.insert(0, {"type": "text", "text": "Frames from the user's style reference videos:"})
    content.append({"type": "text", "text": json.dumps(payload, ensure_ascii=False)})

    response = client.messages.parse(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=EditPlan,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError("Claude не върна план")
    plan: EditPlan = response.parsed_output
    # Make sure every segment has exactly one edit.
    by_index = {e.index: e for e in plan.segments}
    plan.segments = [
        by_index.get(s.index) or SegmentEdit(index=s.index, keep=True, zoom="none",
                                             transition_in="cut", sfx="none")
        for s in segments
    ]
    if not any(e.keep for e in plan.segments):
        for e in plan.segments:
            e.keep = True
    plan.words_per_caption = min(max(plan.words_per_caption, 1), 6)
    plan.music_volume = min(max(plan.music_volume, 0.0), 1.0)
    return plan
