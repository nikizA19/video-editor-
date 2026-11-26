"""One-click edit styles, the studio's answer to Higgsfield's preset gallery.

Higgsfield's big idea is that you pick a look from a gallery ("Crash Zoom",
"Dolly In", a genre that sets the pacing...) instead of writing a perfect prompt.
Each style below bundles pacing, camera moves, effects, sound design, captions,
motion graphics and colour into one choice. The user's text instructions still
win where they say something specific ("без zoom", "заглавие: ...").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    key: str
    name: str            # shown in the UI (Bulgarian)
    emoji: str
    blurb: str           # one line for the UI card (Bulgarian)
    brief: str           # what Claude should aim for (English)
    pauses: str          # off / gentle / normal / tight
    camera_cycle: tuple[str, ...]  # camera moves for ordinary cuts, in rotation
    camera_long: str               # for long, calm segments
    camera_emphasis: str           # for the strongest moments
    emphasis_effect: str           # visual effect on the strongest moments
    transitions: tuple[str, ...]   # used on topic changes (long pauses), in rotation
    transition_pause: float        # a removed pause at least this long = topic change
    sfx_level: int                 # 0 none, 1 subtle, 2 normal, 3 a lot
    caption_style: str
    caption_uppercase: bool
    words_per_caption: int
    caption_pop: bool              # current word scales up a little
    popup_style: str               # pop / slide / label / stamp / typewriter
    grade: str
    grain: bool = False
    letterbox: bool = False
    progress_bar: bool = False
    broll_every: float = 9.0       # seconds between b-roll cutaways (0 = never)
    emphasis_share: float = 0.18   # fraction of cuts treated as "strong moments"
    music_volume: float = 0.15
    accent: str = "#FFD43B"
    callouts: bool = False         # pop up numbers the speaker says ("3 ТРИКА", "50%")


STYLES: dict[str, Style] = {s.key: s for s in [
    Style(
        key="viral", name="Вирусен", emoji="🔥",
        blurb="Бързи резки, zoom на всяка смяна, удари и звуци на важните думи, жълти надписи.",
        brief=("High-retention talking-head edit (Hormozi / MrBeast style): tight jump cuts, "
               "alternate framing every cut, crash zooms with a shake on punchlines and numbers, "
               "whip transitions on topic changes, frequent but purposeful SFX, big bold captions "
               "with the current word highlighted, keyword popups."),
        pauses="tight",
        camera_cycle=("none", "punch_in"),
        camera_long="push_in",
        camera_emphasis="crash_zoom",
        emphasis_effect="none",
        transitions=("whip", "zoom"),
        transition_pause=1.0,
        sfx_level=3,
        caption_style="bold_yellow", caption_uppercase=True, words_per_caption=3, caption_pop=True,
        popup_style="pop",
        grade="punchy",
        progress_bar=True,
        broll_every=8.0,
        emphasis_share=0.2,
        callouts=True,
    ),
    Style(
        key="cinematic", name="Кино", emoji="🎬",
        blurb="Плавни движения на камерата, кино цветове, черни ленти, зърно и светлинни отблясъци.",
        brief=("Cinematic, calm and premium: slow dolly push-ins and pull-outs, floating camera, "
               "dip-to-black and soft fades between ideas, light leaks on big moments, film grain, "
               "letterbox bars, teal-orange grade, very few SFX (a soft boom on a reveal), "
               "clean small captions, elegant sliding titles."),
        pauses="gentle",
        camera_cycle=("push_in", "float", "pull_out", "pan_right"),
        camera_long="float",
        camera_emphasis="push_in",
        emphasis_effect="light_leak",
        transitions=("fade", "dip_black"),
        transition_pause=0.8,
        sfx_level=1,
        caption_style="clean_white", caption_uppercase=False, words_per_caption=4, caption_pop=False,
        popup_style="slide",
        grade="cinematic",
        grain=True, letterbox=True,
        broll_every=10.0,
        emphasis_share=0.12,
        music_volume=0.2,
        accent="#F2C27B",
    ),
    Style(
        key="documentary", name="Документален", emoji="📰",
        blurb="Спокойно темпо, анимирани етикети с ключовите думи, бавни панорами, чисти преходи.",
        brief=("Editorial / documentary motion-graphics style (like Higgsfield's editorial motion "
               "graphics skill): measured pacing, slow pans and push-ins, animated labels with an "
               "underline wipe for key facts, numbers and names, lower third for the speaker, "
               "fades and slides between topics, subtle pops and clicks, clean boxed captions, "
               "progress bar."),
        pauses="normal",
        camera_cycle=("none", "push_in", "pan_right", "none", "pan_left"),
        camera_long="push_in",
        camera_emphasis="punch_in",
        emphasis_effect="none",
        transitions=("fade", "slide"),
        transition_pause=0.9,
        sfx_level=1,
        caption_style="boxed", caption_uppercase=False, words_per_caption=4, caption_pop=False,
        popup_style="label",
        grade="warm",
        progress_bar=True,
        broll_every=9.0,
        emphasis_share=0.15,
        callouts=True,
        accent="#4DABF7",
    ),
    Style(
        key="energetic", name="Енергичен", emoji="⚡",
        blurb="Glitch, светкавици, треперене и whip преходи. За TikTok и Reels.",
        brief=("Hyper-energetic short-form edit: very tight cuts, crash zooms and impact shakes on "
               "strong words, glitch and flash hits, whip / glitch transitions, lots of SFX, stamped "
               "keyword popups, boxed uppercase captions two words at a time."),
        pauses="tight",
        camera_cycle=("punch_in", "none", "shake"),
        camera_long="push_in",
        camera_emphasis="impact",
        emphasis_effect="glitch",
        transitions=("whip", "glitch", "flash"),
        transition_pause=0.7,
        sfx_level=3,
        caption_style="boxed", caption_uppercase=True, words_per_caption=2, caption_pop=True,
        popup_style="stamp",
        grade="punchy",
        progress_bar=True,
        broll_every=7.0,
        emphasis_share=0.25,
        callouts=True,
        accent="#FF4D6D",
    ),
    Style(
        key="clean", name="Чист", emoji="✨",
        blurb="Махане на паузите, леки zoom-ове и прости надписи. Без излишни ефекти.",
        brief=("Clean and minimal: remove pauses and bad takes, gentle alternate framing, no flashy "
               "effects, almost no SFX, simple readable captions, a fading title at most."),
        pauses="normal",
        camera_cycle=("none", "punch_in"),
        camera_long="none",
        camera_emphasis="punch_in",
        emphasis_effect="none",
        transitions=(),
        transition_pause=99.0,
        sfx_level=0,
        caption_style="clean_white", caption_uppercase=False, words_per_caption=3, caption_pop=False,
        popup_style="slide",
        grade="punchy",
        broll_every=0.0,
        emphasis_share=0.1,
    ),
    Style(
        key="podcast", name="Подкаст", emoji="🎙️",
        blurb="Спокойно, без ефекти, бавен zoom на дългите изречения, четливи надписи.",
        brief=("Podcast / interview: keep the natural rhythm (only long pauses go), slow push-ins on "
               "long answers, no transitions or SFX, readable captions, lower third with the "
               "speaker's name if given."),
        pauses="gentle",
        camera_cycle=("none",),
        camera_long="push_in",
        camera_emphasis="punch_in",
        emphasis_effect="none",
        transitions=(),
        transition_pause=99.0,
        sfx_level=0,
        caption_style="clean_white", caption_uppercase=False, words_per_caption=4, caption_pop=False,
        popup_style="slide",
        grade="none",
        broll_every=0.0,
        emphasis_share=0.08,
    ),
]}

DEFAULT_STYLE = "viral"

INTENSITY = {"low": 0.6, "normal": 1.0, "high": 1.5}

# Words that usually mark a strong moment (checked case-insensitively, as prefixes).
POWER_WORDS = (
    # Bulgarian
    "най-", "важн", "тайн", "внимание", "никога", "винаги", "грешк", "проблем", "решени",
    "пари", "лев", "евро", "долар", "безплатн", "бърз", "лесн", "трик", "съвет", "стъпк",
    "първ", "втор", "трет", "накрая", "резултат", "истина", "шок", "невероят", "топ",
    # Bulgarian in Latin letters
    "nai-", "vajn", "tain", "nikoga", "vinagi", "greshk", "pari", "bezplat", "trik",
    # English
    "never", "always", "secret", "important", "mistake", "money", "free", "fast", "easy",
    "first", "second", "third", "finally", "result", "truth", "crazy", "insane", "best",
    "worst", "top", "how", "why", "stop",
)


def get_style(key: str | None) -> Style:
    return STYLES.get(key or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])


def catalog() -> list[dict]:
    """What the UI needs to draw the style gallery."""
    return [{"key": s.key, "name": s.name, "emoji": s.emoji, "blurb": s.blurb} for s in STYLES.values()]
