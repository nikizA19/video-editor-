"""Turn the user's instructions into a concrete edit plan.

Two planners:
* Auto (free, offline): the chosen one-click style (styles.py) + keyword rules +
  what the analysis found (pauses, failed takes, strong moments, b-roll names).
* Claude (optional): if ANTHROPIC_API_KEY is set, Claude reads the transcript,
  the instructions, frames from the reference videos and the auto draft, and
  decides the edit.
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
from .styles import INTENSITY, POWER_WORDS, STYLES, Style, get_style

Camera = Literal["none", "punch_in", "push_in", "pull_out", "crash_zoom", "impact",
                 "pan_left", "pan_right", "tilt_up", "tilt_down", "shake", "float"]
Transition = Literal["cut", "fade", "dip_black", "slide", "whip", "zoom", "glitch", "flash"]
Effect = Literal["none", "flash", "glitch", "light_leak", "bw"]
Sfx = Literal["none", "whoosh", "swoosh", "pop", "click", "boom", "ding", "riser", "glitch",
              "shutter", "type"]
PopupStyle = Literal["pop", "slide", "label", "stamp", "typewriter"]
CaptionStyle = Literal["bold_yellow", "clean_white", "boxed"]
Grade = Literal["none", "punchy", "warm", "cool", "cinematic", "bw"]
OutputFormat = Literal["original", "vertical_9_16", "horizontal_16_9", "square_1_1"]
StyleKey = Literal["viral", "cinematic", "documentary", "energetic", "clean", "podcast"]


class SegmentEdit(BaseModel):
    index: int
    keep: bool
    camera: Camera
    transition_in: Transition  # how we enter this segment from the previous one
    effect: Effect
    sfx: Sfx
    broll: str  # file name of a b-roll clip/photo shown over this segment, "" for none


class TextPopup(BaseModel):
    segment_index: int
    text: str
    style: PopupStyle
    sfx: Sfx


class EditPlan(BaseModel):
    summary: str
    style: StyleKey
    segments: list[SegmentEdit]
    captions: bool
    caption_style: CaptionStyle
    caption_uppercase: bool
    caption_pop: bool
    words_per_caption: int
    emphasis_words: list[str]  # caption words drawn in the accent colour
    text_popups: list[TextPopup]
    lower_third_title: str     # name tag, "" for none
    lower_third_subtitle: str
    color_grade: Grade
    film_grain: bool
    letterbox: bool
    progress_bar: bool
    accent_color: str          # "#RRGGBB"
    output_format: OutputFormat
    music_volume: float        # 0..1, only used if music was uploaded


# ------------------------------------------------------------ reading text

NEGATIONS = {"без", "bez", "no", "not", "не", "ne", "махни", "mahni", "remove", "without",
             "спри", "spri", "stop", "никакви", "nikakvi", "никакъв", "nikakuv", "махай", "mahai"}


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _switch(text: str, words: tuple[str, ...], skip_after: tuple[str, ...] = ()) -> bool | None:
    """Does the text turn a feature on (True) or off (False)? None if it is not mentioned.

    "без зърно" / "махни зърното" / "no grain" turn it off. When it is mentioned
    more than once (e.g. an earlier request plus a correction) the last mention wins.
    """
    last = None
    for w in words:
        for m in re.finditer(re.escape(w), text):
            prev = re.findall(r"[\w-]+", text[max(0, m.start() - 24):m.start()])[-2:]
            if skip_after and prev and prev[-1].startswith(skip_after):
                continue
            hit = (m.start(), not any(p in NEGATIONS for p in prev))
            if last is None or hit[0] >= last[0]:
                last = hit
    return None if last is None else last[1]


ZOOM_WORDS = ("zoom", "зуум", "зум", "zuum", "zum", "приближ", "priblij")
MOVE_WORDS = ("движения на камерата", "dvijeniq na kamerata", "camera move", "движение на камерата")
MUCH_ZOOM = ("mnogo zoom", "много zoom", "повече zoom", "poveche zoom", "more zoom", "много зуум",
             "повече зуум", "много зум", "повече зум", "poveche zum", "mnogo zum")
FX_WORDS = ("ефект", "efekt", "effect")
SOUND_PREFIX = ("звуков", "zvukov", "sound", "аудио", "audio")
SFX_WORDS = ("звук", "zvuk", "sfx", "sound effect", "звуци", "zvuci")
TRANS_WORDS = ("преход", "prehod", "transition")
CAPTION_WORDS = ("надпис", "nadpis", "caption", "субтит", "subtit")
BROLL_WORDS = ("b-roll", "broll", "б-рол", "бирол", "кадри от", "kadri ot")
MORE_FX = ("повече ефект", "poveche efekt", "more effect", "по-динамич", "po-dinamich",
           "по-яко", "po-qko", "more hype", "максимум ефект", "maksimum efekt")
LESS_FX = ("по-малко ефект", "po-malko efekt", "less effect", "fewer effect", "по-спокойн",
           "po-spokoin", "по-леко", "po-leko", "по-семпл", "po-sempl", "по-изчистен", "po-izchisten")
CRASH = ("crash zoom", "crash-zoom", "краш зуум", "краш зум", "krash zum", "рязък zoom",
         "рязък зуум", "rqzuk zoom")
SHAKE = ("shake", "трепер", "treper", "разтърс", "razturs", "тресене", "tresene", "друсане")
GLITCH = ("glitch", "глич", "glich")
FLASH = ("flash", "светкав", "svetkav")
WHIP = ("whip", "уип", "бързи преходи", "burzi prehodi")
LEAK = ("light leak", "отблясъ", "otblqs", "otblyas", "светлинни", "svetlinni")
PANS = ("панорам", "panoram", "pan ")
LETTERBOX = ("черни ленти", "cherni lenti", "letterbox", "кино ленти", "kino lenti")
GRAIN = ("зърно", "zurno", "grain", "зърнест", "zurnest")
PROGRESS = ("прогрес", "progres")
UPPER = ("главни букви", "glavni bukvi", "uppercase", "all caps", "caps lock")
LOWER = ("малки букви", "malki bukvi", "lowercase")
MUSIC_WORDS = ("музика", "muzika", "music")

STYLE_NAMES = {
    "viral": ("вирус", "virus", "viral", "hormozi", "хормози", "mrbeast"),
    "cinematic": ("кино", "kino", "cinemat", "кинематограф", "kinematograf", "филм", "film"),
    "documentary": ("документ", "dokument", "documentar", "editorial", "новин", "novin"),
    "energetic": ("енерги", "energi", "hype"),
    "clean": ("чист", "chist", "clean", "минимал", "minimal", "прост", "prost"),
    "podcast": ("подкаст", "podkast", "podcast", "интервю", "intervu", "interview"),
}


def _style_in_text(instructions: str) -> str | None:
    t = instructions.lower()
    for m in re.finditer(r"(?:стил|stil|style)[\s:=„“\"']*([\w-]+)|([\w-]+)\s+(?:стил|stil|style)\b", t):
        word = m.group(1) or m.group(2)
        for key, names in STYLE_NAMES.items():
            if word.startswith(names):
                return key
    if _has(t, "подкаст", "podkast", "podcast", "интервю", "intervu", "interview"):
        return "podcast"
    return None


def resolve_style(instructions: str, options: dict) -> Style:
    return get_style(_style_in_text(instructions or "") or options.get("style"))


def pause_preset_from(instructions: str, chosen: str | None, style: Style | None = None) -> str:
    t = (instructions or "").lower()
    if _has(t, "ne mahai pauz", "не махай пауз", "keep pauses", "bez rqzane", "без рязане",
            "запази паузите", "zapazi pauzite", "остави паузите", "ostavi pauzite"):
        return "off"
    if _has(t, "agresiv", "агресив", "tight", "brzo", "бързо", "burzo", "dinamich", "динамич",
            "fast pace"):
        return "tight"
    if _has(t, "спокойно темпо", "spokoino tempo", "бавно темпо", "bavno tempo", "меко рязане",
            "meko rqzane"):
        return "gentle"
    if chosen and chosen != "auto":
        return chosen
    return style.pauses if style else "normal"


def _last_match(pattern: str, text: str):
    matches = [m for m in re.finditer(pattern, text, re.I) if m.group(1).strip()]
    return matches[-1] if matches else None  # a later correction wins


def _title(instructions: str) -> str | None:
    m = _last_match(r"(?:title|заглавие|zaglavie)\s*[:=]\s*[\"„“']?([^\"\n“”,.;!]+)", instructions)
    return m.group(1).strip()[:60] if m else None


def _lower_third(instructions: str) -> tuple[str, str]:
    m = _last_match(r"(?:\bиме|\bime|\bname|говорител|govoritel|speaker)\s*[:=]\s*"
                    r"[\"„“']?([^\n,.;!\"“”]+)", instructions)
    if not m:
        return "", ""
    parts = re.split(r"\s+[-–—|/]\s+", m.group(1).strip(), maxsplit=1)
    title = parts[0].strip()[:40]
    return title, (parts[1].strip()[:50] if len(parts) > 1 else "")


# ----------------------------------------------------------- strong moments

NUMBER_WORDS = {
    "две": "2", "два": "2", "три": "3", "четири": "4", "пет": "5", "шест": "6", "седем": "7",
    "осем": "8", "девет": "9", "десет": "10", "двадесет": "20", "двайсет": "20",
    "тридесет": "30", "трийсет": "30", "петдесет": "50", "сто": "100", "хиляда": "1000",
    "dve": "2", "dva": "2", "tri": "3", "chetiri": "4", "pet": "5", "shest": "6", "sedem": "7",
    "osem": "8", "devet": "9", "deset": "10", "sto": "100", "hilqda": "1000",
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "twenty": "20", "fifty": "50", "hundred": "100", "thousand": "1000",
}
STOP_WORDS = {"и", "в", "на", "за", "от", "с", "със", "по", "до", "да", "се", "е", "че", "не",
              "the", "and", "of", "to", "in", "for", "on", "at", "a", "is", "are", "with",
              "i", "v", "na", "za", "ot", "s", "po", "do", "se"}


def _clean(word: str) -> str:
    return word.lower().strip(".,!?;:\"'„“”()[]…«»")


def _number_callout(seg: Segment, uppercase: bool) -> str | None:
    words = [w.text.strip(".,!?;:\"'„“”()…") for w in seg.words]
    for i, w in enumerate(words):
        low = w.lower()
        num = low if re.fullmatch(r"\d[\d.,]*%?", low) else NUMBER_WORDS.get(low)
        if not num:
            continue
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if nxt and len(nxt) >= 3 and nxt.lower() not in STOP_WORDS and not re.search(r"\d", nxt):
            text = f"{num} {nxt}"
        elif "%" in num or len(num.replace(".", "").replace(",", "")) >= 2:
            text = num
        else:
            continue
        return text.upper() if uppercase else text
    return None


def _pick_emphasis(kept: list[Segment], share: float) -> set[int]:
    """The strongest moments, never two next to each other."""
    n = round(len(kept) * share)
    if len(kept) >= 3:
        n = max(1, n)
    pos = {s.index: i for i, s in enumerate(kept)}
    chosen: set[int] = set()
    ranked = sorted(kept, key=lambda s: -s.emphasis)
    for floor in (0.6, -0.5):
        for s in ranked:
            if len(chosen) >= n:
                break
            if s.emphasis >= floor and s.index not in chosen and \
                    all(abs(pos[s.index] - pos[c]) > 1 for c in chosen):
                chosen.add(s.index)
        if len(chosen) >= n * 0.5:
            break
    return chosen


def _emphasis_words(kept: list[Segment], strong: set[int]) -> list[str]:
    out: list[str] = []
    for s in kept:
        for w in s.words:
            c = _clean(w.text)
            if not c or c in out:
                continue
            if re.search(r"\d", c) or (s.index in strong and len(c) >= 3
                                        and c.startswith(POWER_WORDS)):
                out.append(c)
    return out[:40]


# ------------------------------------------------------------------- b-roll

_LAT = dict(zip("абвгдежзийклмнопрстуфхцчшщъьюя",
                ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                 "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sht", "a", "", "yu", "ya"]))


def _latin(text: str) -> str:
    t = "".join(_LAT.get(ch, ch) for ch in text.lower())
    # Common ways people type Bulgarian in Latin letters.
    return t.replace("j", "zh").replace("w", "v").replace("q", "ya").replace("4", "ch").replace("6", "sh")


def _tokens(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{3,}", _latin(text))}


def _broll_match(seg: Segment, brolls: list[dict], recent: str) -> str | None:
    words = _tokens(seg.text)
    for b in brolls:
        if b["name"] != recent and words & _tokens(Path(b["name"]).stem):
            return b["name"]
    return None


# --------------------------------------------------------------- auto planner

TRANS_SFX = {"whip": "whoosh", "slide": "swoosh", "zoom": "whoosh", "glitch": "glitch",
             "flash": "shutter", "fade": "none", "dip_black": "none"}
CAMERA_SFX = {"crash_zoom": "swoosh", "impact": "boom", "push_in": "boom", "pull_out": "swoosh",
              "punch_in": "click"}
EFFECT_SFX = {"glitch": "glitch", "flash": "shutter"}
POPUP_SFX = {"pop": "pop", "slide": "swoosh", "label": "click", "stamp": "boom", "typewriter": "type"}
SFX_GAP = {1: 6.0, 2: 4.0, 3: 2.5}


def auto_plan(segments: list[Segment], instructions: str, options: dict, drop: set[int],
              references: list[dict], brolls: list[dict] | None = None,
              accent: str | None = None) -> EditPlan:
    text = instructions or ""
    t = text.lower()
    style = resolve_style(text, options)
    brolls = brolls or []

    k = INTENSITY.get(options.get("intensity") or "normal", 1.0)
    if _has(t, *MORE_FX):
        k *= 1.4
    if _has(t, *LESS_FX):
        k *= 0.6
    # A fast-cut reference asks for a busier edit, a slow one for a calmer edit.
    shots = [r["avg_shot_seconds"] for r in references if r.get("avg_shot_seconds")]
    if shots:
        k *= 1.25 if min(shots) < 2.5 else 0.85 if min(shots) > 6 else 1.0
    k = max(0.3, min(2.5, k))

    camera_on = (_switch(t, ZOOM_WORDS, skip_after=("crash", "краш", "krash", "рязък", "rqzuk"))
                 is not False and _switch(t, MOVE_WORDS) is not False)
    much_zoom = _has(t, *MUCH_ZOOM)
    fx_on = _switch(t, FX_WORDS, skip_after=SOUND_PREFIX) is not False
    sfx_switch = _switch(t, SFX_WORDS)
    trans_switch = _switch(t, TRANS_WORDS)
    has_words = any(s.words for s in segments)
    captions = (bool(options.get("captions", True)) and _switch(t, CAPTION_WORDS) is not False
                and has_words)
    want_crash, want_shake = _switch(t, CRASH), _switch(t, SHAKE)
    want_glitch, want_flash = _switch(t, GLITCH), _switch(t, FLASH)
    want_whip, want_leak, want_pans = _switch(t, WHIP), _switch(t, LEAK), _switch(t, PANS)

    # -- what to use for camera, effects, transitions, sound
    cycle = list(style.camera_cycle)
    if much_zoom:
        cycle = [("push_in" if c == "none" else c) for c in cycle]
    if want_pans and not any(c.startswith("pan") for c in cycle):
        cycle += ["pan_right", "pan_left"]
    if want_shake is False:
        cycle = [("none" if c == "shake" else c) for c in cycle]
    emph_camera = style.camera_emphasis
    if want_crash:
        emph_camera = "crash_zoom"
    elif want_crash is False and emph_camera in ("crash_zoom", "impact"):
        emph_camera = "push_in"
    if want_shake:
        emph_camera = "impact"
    elif want_shake is False and emph_camera in ("impact", "shake"):
        emph_camera = "crash_zoom"
    if much_zoom and emph_camera == "punch_in":
        emph_camera = "crash_zoom"

    emph_effect = style.emphasis_effect
    for flag, fx in ((want_leak, "light_leak"), (want_flash, "flash"), (want_glitch, "glitch")):
        if flag:
            emph_effect = fx
        elif flag is False and emph_effect == fx:
            emph_effect = "none"
    if not fx_on:
        emph_effect = "none"

    transitions = list(style.transitions)
    for flag, tr in ((want_whip, "whip"), (want_glitch, "glitch"), (want_flash, "flash")):
        if flag and tr not in transitions:
            transitions.append(tr)
        elif flag is False and tr in transitions:
            transitions.remove(tr)
    if trans_switch and not transitions:
        transitions = ["fade", "slide"]
    if trans_switch is False or not fx_on:
        transitions = []
    trans_pause = style.transition_pause if style.transitions else 1.0

    level = style.sfx_level
    if sfx_switch and level == 0:
        level = 2
    if sfx_switch is False or not fx_on:
        level = 0
    sfx_gap = SFX_GAP.get(level, 4.0) / k

    broll_every = style.broll_every or (9.0 if _switch(t, BROLL_WORDS) else 0.0)
    if _switch(t, BROLL_WORDS) is False or not brolls:
        broll_every = 0.0
    broll_every /= k

    # -- per segment
    kept = [s for s in segments if s.index not in drop] or list(segments)
    keep_ids = {s.index for s in kept}
    strong = _pick_emphasis(kept, style.emphasis_share * k)
    top = max((s for s in kept[1:] if s.index in strong), key=lambda s: s.emphasis, default=None)
    edits = {s.index: SegmentEdit(index=s.index, keep=s.index in keep_ids, camera="none",
                                  transition_in="cut", effect="none", sfx="none", broll="")
             for s in segments}
    now = 0.0
    last_trans = last_fx = last_sfx = last_broll = -99.0
    prev_camera = None
    recent_broll = ""
    used: dict[str, int] = {b["name"]: 0 for b in brolls}
    ci = ti = 0
    for i, seg in enumerate(kept):
        e = edits[seg.index]
        is_strong = seg.index in strong
        if camera_on:
            if is_strong:
                cam = emph_camera
            elif seg.duration >= 4.5 and style.camera_long != "none":
                cam = style.camera_long
            else:
                cam = cycle[ci % len(cycle)]
                ci += 1
            jump = i > 0 and (seg.pause_before > 0 or seg.source != kept[i - 1].source
                              or kept[i - 1].index + 1 != seg.index)
            if jump and cam == prev_camera and cam in ("none", "punch_in"):
                cam = "punch_in" if cam == "none" else "none"  # hide the jump cut
            e.camera = cam
            prev_camera = cam
        if i > 0 and transitions:
            new_clip = seg.source != kept[i - 1].source
            if (seg.pause_before >= trans_pause or new_clip) and now - last_trans >= 8.0 / k:
                e.transition_in = transitions[ti % len(transitions)]
                ti += 1
                last_trans = now
        if fx_on:
            if e.transition_in == "glitch":
                e.effect = "glitch"
            elif is_strong and emph_effect != "none" and now - last_fx >= 4.0 / k:
                e.effect = emph_effect
            if e.effect != "none":
                last_fx = now
        if broll_every and i > 0 and not is_strong and seg.duration >= 1.8:
            since = now - last_broll
            name = _broll_match(seg, brolls, recent_broll) if since >= broll_every * 0.5 else None
            if name is None and since >= broll_every:
                # Nothing matches what is being said: show the least used one (each at most twice).
                fresh = sorted((b["name"] for b in brolls if used[b["name"]] < 2 and (
                    b["name"] != recent_broll or since >= 3 * broll_every)), key=lambda n: used[n])
                name = fresh[0] if fresh else None
            if name:
                e.broll = name
                used[name] += 1
                recent_broll = name
                last_broll = now
        if level:
            sfx = "none"
            if e.transition_in != "cut":
                sfx = TRANS_SFX.get(e.transition_in, "none")
                if e.transition_in == "dip_black" and level >= 2:
                    sfx = "boom"
            elif is_strong:
                sfx = EFFECT_SFX.get(e.effect) or CAMERA_SFX.get(e.camera, "none")
                if level == 1 and sfx not in ("boom", "click"):
                    sfx = "none"
                if level >= 3 and top is not None and seg.index == top.index and now > 3:
                    sfx = "riser"  # builds up into the biggest moment
            elif e.broll and level >= 2:
                sfx = "swoosh"
            if sfx != "none" and (e.transition_in != "cut" or now - last_sfx >= sfx_gap):
                e.sfx = sfx
                last_sfx = now
        now += seg.duration

    # -- text on screen
    uppercase = style.caption_uppercase
    if _switch(t, UPPER):
        uppercase = True
    if _switch(t, LOWER):
        uppercase = False
    popup_sfx = (lambda st: POPUP_SFX[st]) if level else (lambda st: "none")
    popups: list[TextPopup] = []
    title = _title(text)
    if title:
        popups.append(TextPopup(segment_index=kept[0].index, text=title, style=style.popup_style,
                                sfx=popup_sfx(style.popup_style)))
    if style.callouts and fx_on and has_words:
        last_pop = 0.0 if title else -99.0
        now = 0.0
        for seg in kept:
            if seg.index != kept[0].index or not title:
                callout = _number_callout(seg, uppercase or style.popup_style in ("pop", "stamp"))
                if callout and now - last_pop >= 10.0 / k:
                    popups.append(TextPopup(segment_index=seg.index, text=callout,
                                            style=style.popup_style,
                                            sfx=popup_sfx(style.popup_style)))
                    last_pop = now
            now += seg.duration
    lt_title, lt_sub = _lower_third(text)

    # -- look
    grade = style.grade
    for words, g in ((("топл", "topl", "warm"), "warm"),
                     (("студен", "studen", "cool", "хладн", "hladn"), "cool"),
                     (("кино цвет", "kino cvet", "cinematic", "филмов", "filmov", "teal"), "cinematic"),
                     (("ярки", "qrki", "наситени", "nasiteni", "vivid", "punchy"), "punchy"),
                     (("черно-бял", "черно бял", "cherno-bql", "cherno bql", "cherno bial",
                       "black and white", "black & white"), "bw")):
        if _has(t, *words):
            grade = g
    if _has(t, "без цвят", "без цветове", "bez cvqt", "bez cvetove", "no color", "no grade",
            "естествени цветове", "estestveni cvetove", "natural colo"):
        grade = "none"

    fmt = options.get("output_format") or "original"
    if _has(t, "tiktok", "reels", "shorts", "9:16", "vertikal", "вертикал", "тикток", "рийлс"):
        fmt = "vertical_9_16"
    elif _has(t, "квадрат", "kvadrat", "1:1", "square"):
        fmt = "square_1_1"
    elif _has(t, "16:9", "хоризонтал", "horizontal", "youtube", "ютюб"):
        fmt = "horizontal_16_9"

    def on_off(words: tuple[str, ...], default: bool) -> bool:
        v = _switch(t, words)
        return default if v is None else v

    grain = on_off(GRAIN, style.grain) and fx_on
    letterbox = on_off(LETTERBOX, style.letterbox) and fmt not in ("vertical_9_16", "square_1_1")
    progress = on_off(PROGRESS, style.progress_bar)

    music_volume = style.music_volume
    if _has(t, "тиха музика", "tiha muzika", "quiet music", "по-тиха музика", "po-tiha muzika"):
        music_volume = 0.08
    elif _has(t, "силна музика", "silna muzika", "loud music", "по-силна музика", "po-silna muzika"):
        music_volume = 0.3
    if _switch(t, MUSIC_WORDS) is False:
        music_volume = 0.0

    caption_style = options.get("caption_style")
    if caption_style not in ("bold_yellow", "clean_white", "boxed"):
        caption_style = style.caption_style
    per_caption = style.words_per_caption
    if fmt == "vertical_9_16":
        per_caption = min(per_caption, 3)

    plan_edits = [edits[s.index] for s in segments]
    kept_edits = [e for e in plan_edits if e.keep]
    n_cam = sum(1 for e in kept_edits if e.camera != "none")
    n_fx = sum(1 for e in kept_edits if e.effect != "none" or e.transition_in != "cut")
    n_sfx = sum(1 for e in kept_edits if e.sfx != "none") + sum(1 for p in popups if p.sfx != "none")
    n_broll = sum(1 for e in kept_edits if e.broll)
    dropped = len(segments) - len(kept_edits)
    parts = [f"Стил „{style.name}“: {len(kept_edits)} кадъра"]
    if dropped:
        parts.append(f"махнах {dropped} грешни дубли и шумове")
    if n_cam:
        parts.append(f"{n_cam} движения на камерата")
    if n_fx:
        parts.append(f"{n_fx} ефекта и прехода")
    if n_sfx:
        parts.append(f"{n_sfx} звука")
    if n_broll:
        parts.append(f"{n_broll} b-roll вмъквания")
    if captions:
        parts.append("надписи")
    summary = ", ".join(parts) + "."

    return EditPlan(
        summary=summary,
        style=style.key,
        segments=plan_edits,
        captions=captions,
        caption_style=caption_style,
        caption_uppercase=uppercase,
        caption_pop=style.caption_pop,
        words_per_caption=per_caption,
        emphasis_words=_emphasis_words(kept, strong) if fx_on else [],
        text_popups=popups,
        lower_third_title=lt_title,
        lower_third_subtitle=lt_sub,
        color_grade=grade,
        film_grain=grain,
        letterbox=letterbox,
        progress_bar=progress,
        accent_color=_hex(options.get("accent")) or accent or style.accent,
        output_format=fmt,
        music_volume=music_volume,
    )


def _hex(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        return value.strip().upper()
    return None


# ------------------------------------------------------------- claude planner

SYSTEM = """You are the editor inside an AI video studio for talking-head videos. The footage has
already been cut into short speech segments (long silences removed). You decide the edit; ffmpeg
renders it. Imitate a professional short-form editor: every choice must serve retention and clarity.

You get: the user's instructions, the chosen style (with a brief), every segment with its
transcript, loudness (energy, dB) and an `emphasis` score (higher = louder / numbers / power words),
optional reference videos (pacing + frames), optional b-roll files, and `draft_plan`: a rule-based
draft in the chosen style. Start from the draft and improve it with what only you can judge.

Rules:
- Return exactly one SegmentEdit per input segment, same index order.
- keep=false for failed takes, repeated sentences (keep the LAST good take), false starts, filler,
  coughs and off-topic rambling. Never drop the only take of an important sentence.
- camera (per segment): none, punch_in (static tighter framing), push_in / pull_out (slow dolly),
  crash_zoom (fast zoom on a punchline), impact (crash zoom + shake, the hardest hit), pan_left /
  pan_right / tilt_up / tilt_down (slow drift), shake (handheld energy), float (dreamy drift).
  Alternate framing on jump cuts (none <-> punch_in) so cuts feel intentional; save crash_zoom and
  impact for the real punchlines, numbers, reveals (about 1 in 5 cuts in viral styles, fewer in calm ones).
- transition_in: cut (default, most of the time), fade, dip_black, slide, whip, zoom, glitch, flash.
  Only on topic changes or new clips, not more than every ~8 s.
- effect: none, flash (white flash-in), glitch (RGB split), light_leak (warm light sweep), bw.
- sfx: whoosh/swoosh with whip/slide/zoom transitions and crash zooms, boom on impact and big reveals,
  riser before the biggest moment, pop/click/type/ding for popups and lists, glitch / shutter with
  those effects. Respect the style's amount; never stack sounds closer than ~2.5 s.
- broll: a file name from `broll` when the speaker talks about what it shows (matching names first),
  else "" . Not on the first segment or a punchline. Keep b-roll to roughly one every 8-10 s.
- text_popups: 1-4 words (numbers, key terms, list items), punchy, in the speaker's language.
  style: pop, slide, label (editorial underline label), stamp, typewriter. Give each popup a matching sfx.
- emphasis_words: individual caption words (lowercase, as spoken) to colour in the accent colour.
- lower_third_title/subtitle only if the user gave a name/role ("" otherwise).
- Keep the user's explicit wishes above the style (e.g. "без zoom" = all camera none).
- `summary`: 1-3 short sentences in the user's language (Bulgarian in Latin letters -> answer in
  Bulgarian Cyrillic) describing what you did."""


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def claude_plan(segments: list[Segment], instructions: str, options: dict,
                references: list[dict], previous: EditPlan | None, draft: EditPlan,
                brolls: list[dict] | None = None) -> EditPlan:
    import anthropic

    style = STYLES.get(draft.style) or get_style(None)
    client = anthropic.Anthropic()
    payload = {
        "user_instructions": instructions,
        "studio_options": options,
        "style": {"key": style.key, "name": style.name, "brief": style.brief},
        "segments": [s.to_dict() for s in segments],
        "references": [{k: v for k, v in r.items() if k != "frames"} for r in references],
        "broll": [{k: b[k] for k in ("name", "image", "duration")} for b in brolls or []],
        "draft_plan": draft.model_dump(),
    }
    if previous is not None:
        payload["previous_plan_user_wants_changed"] = previous.model_dump()
    content: list[dict] = []
    frames = [f for ref in references for f in ref.get("frames", [])[:4]][:8]
    for frame in frames:
        p = Path(frame)
        if p.exists() and p.stat().st_size < 4_000_000:
            mt = {".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower(), "image/jpeg")
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
    return sanitize(response.parsed_output, segments, draft, brolls or [])


def sanitize(plan: EditPlan, segments: list[Segment], draft: EditPlan,
             brolls: list[dict]) -> EditPlan:
    """Make a plan safe to render: one edit per segment, known b-roll, sane numbers."""
    by_index = {e.index: e for e in plan.segments}
    fallback = {e.index: e for e in draft.segments}
    plan.segments = [by_index.get(s.index) or fallback[s.index] for s in segments]
    names = {b["name"] for b in brolls}
    for e in plan.segments:
        if e.broll and e.broll not in names:
            e.broll = ""
    if not any(e.keep for e in plan.segments):
        for e in plan.segments:
            e.keep = True
    ids = {s.index for s in segments}
    plan.text_popups = [p for p in plan.text_popups if p.segment_index in ids and p.text.strip()][:40]
    plan.emphasis_words = [_clean(w) for w in plan.emphasis_words if _clean(w)][:80]
    plan.words_per_caption = min(max(plan.words_per_caption, 1), 6)
    plan.music_volume = min(max(plan.music_volume, 0.0), 1.0)
    plan.accent_color = _hex(plan.accent_color) or draft.accent_color
    plan.lower_third_title = plan.lower_third_title.strip()[:40]
    plan.lower_third_subtitle = plan.lower_third_subtitle.strip()[:50]
    if plan.output_format in ("vertical_9_16", "square_1_1"):
        plan.letterbox = False
    return plan
