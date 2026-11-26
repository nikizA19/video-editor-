"""Look at the raw footage: loudness, pauses, speech, strong moments, references, b-roll."""
from __future__ import annotations

import colorsys
import difflib
import math
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .media import MediaInfo, probe, run

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Word:
    text: str
    start: float  # seconds in the source clip
    end: float


@dataclass
class Segment:
    """A piece of a source clip that we keep."""
    index: int
    source: int  # index into the footage list
    start: float
    end: float
    pause_before: float = 0.0  # length of the silence removed right before it
    words: list[Word] = field(default_factory=list)
    energy: float = -99.0      # average loudness in dB
    emphasis: float = 0.0      # how much this looks like a strong moment (higher = stronger)
    false_start: bool = False  # the speaker stopped and began this sentence again right after

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        d["text"] = self.text
        d["energy"] = round(self.energy, 1)
        d["emphasis"] = round(self.emphasis, 2)
        d.pop("words")
        return d


# ------------------------------------------------------------- loudness

@dataclass
class AudioProfile:
    """Loudness (RMS, dB) of a clip in small windows."""
    step: float
    levels: list[float]

    def window(self, start: float, end: float) -> list[float]:
        a = max(0, int(start / self.step))
        b = max(a + 1, int(math.ceil(end / self.step)))
        return self.levels[a:b]

    def mean_db(self, start: float, end: float) -> float:
        vals = self.window(start, end)
        if not vals:
            return -99.0
        power = sum(10 ** (v / 10) for v in vals) / len(vals)
        return 10 * math.log10(power) if power > 0 else -99.0

    def percentile(self, q: float) -> float:
        vals = sorted(self.levels)
        if not vals:
            return -99.0
        return vals[min(len(vals) - 1, int(len(vals) * q / 100))]


def audio_profile(path: Path, step: float = 0.05) -> AudioProfile:
    rate = 16000
    n = int(rate * step)
    log = run([
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path), "-vn",
        "-af", (f"aresample={rate},aformat=channel_layouts=mono,asetnsamples=n={n}:p=0,"
                "astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
                "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level"),
        "-f", "null", "-",
    ])
    levels = []
    for m in re.finditer(r"RMS_level=(-?inf|-?[\d.]+)", log):
        v = m.group(1)
        levels.append(-100.0 if "inf" in v else max(-100.0, float(v)))
    return AudioProfile(step, levels)


# ---------------------------------------------------------------- silence

PAUSE_PRESETS = {
    # name: (threshold shift dB, min silence seconds, padding seconds)
    "off": None,
    "gentle": (-3.0, 0.9, 0.18),
    "normal": (0.0, 0.55, 0.12),
    "tight": (3.0, 0.35, 0.08),
}


def silence_threshold(profile: AudioProfile, shift: float = 0.0) -> float:
    """Pick the 'this is silence' level from the clip itself.

    A fixed -35 dB works in a quiet room but cuts nothing next to a fan and eats
    quiet speakers. Instead: somewhere between the background noise (10th
    percentile) and the voice (90th percentile).
    """
    noise, voice = profile.percentile(10), profile.percentile(90)
    thr = noise + 0.4 * (voice - noise) + shift
    return max(-50.0, min(-20.0, thr))


def detect_silences(profile: AudioProfile, threshold: float, min_dur: float
                    ) -> list[tuple[float, float]]:
    silences = []
    run_start = None
    for i, v in enumerate(profile.levels + [0.0]):  # sentinel closes a trailing run
        if v < threshold and i < len(profile.levels):
            if run_start is None:
                run_start = i
        elif run_start is not None:
            s, e = run_start * profile.step, i * profile.step
            if e - s >= min_dur:
                silences.append((s, e))
            run_start = None
    return silences


def speech_ranges(info: MediaInfo, preset: str, profile: AudioProfile | None
                  ) -> list[tuple[float, float, float]]:
    """Return (start, end, pause_before) ranges that contain speech."""
    settings = PAUSE_PRESETS.get(preset)
    if settings is None or not info.has_audio or profile is None or not profile.levels:
        return [(0.0, info.duration, 0.0)]
    shift, min_dur, pad = settings
    silences = detect_silences(profile, silence_threshold(profile, shift), min_dur)
    ranges = []
    cursor = 0.0
    last_pause = 0.0
    for s, e in silences:
        e = min(e, info.duration)
        if s - cursor > 0.05:
            ranges.append((max(0.0, cursor - pad), min(info.duration, s + pad), last_pause))
        last_pause = e - s
        cursor = e
    if info.duration - cursor > 0.05:
        ranges.append((max(0.0, cursor - pad), info.duration, last_pause))
    # Merge overlaps caused by padding, drop blips that are too short to be speech.
    merged: list[list[float]] = []
    for s, e, p in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, p])
    return [(s, e, p) for s, e, p in merged if e - s >= 0.3] or [(0.0, info.duration, 0.0)]


# ---------------------------------------------------------- transcription

_whisper_cache: dict[str, object] = {}
Log = Callable[[str], None]


def transcribe(path: Path, model_size: str = "small", language: str | None = None,
               log: Log | None = None) -> list[Word]:
    """Word-level transcript.

    Uses faster-whisper when it is installed, otherwise the whisper.cpp command line
    tool (`brew install whisper-cpp`), which works with any Python version.
    Returns [] when neither is available.
    """
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return _transcribe_whisper_cpp(path, model_size, language, log)
    return _transcribe_faster_whisper(path, model_size, language)


def speech_engine() -> str | None:
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        return "whisper.cpp" if whisper_cpp_binary() else None


def _transcribe_faster_whisper(path: Path, model_size: str, language: str | None) -> list[Word]:
    from faster_whisper import WhisperModel

    model = _whisper_cache.get(model_size)
    if model is None:
        model = WhisperModel(model_size, device="auto", compute_type="int8")
        _whisper_cache[model_size] = model
    segments, _ = model.transcribe(
        str(path), word_timestamps=True, language=language or None, vad_filter=True
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(text, float(w.start), float(w.end)))
    return words


WHISPER_CPP_BINARIES = ("whisper-cli", "whisper-cpp")
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{size}.bin"


def whisper_cpp_binary() -> str | None:
    import shutil

    return next((shutil.which(n) for n in WHISPER_CPP_BINARIES if shutil.which(n)), None)


def _whisper_cpp_model(size: str, log: Log | None) -> Path:
    import shutil
    import urllib.request

    cache = Path.home() / ".cache" / "ai-video-studio"
    cache.mkdir(parents=True, exist_ok=True)
    model = cache / f"ggml-{size}.bin"
    if not model.exists() or model.stat().st_size < 1_000_000:
        if log:
            log(f"Свалям модела за реч ({size}), само първия път е. Може да отнеме няколко минути…")
        tmp = cache / f"ggml-{size}.bin.part"
        with urllib.request.urlopen(MODEL_URL.format(size=size), timeout=60) as r, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(r, out, 1024 * 1024)
        tmp.replace(model)
    return model


def _transcribe_whisper_cpp(path: Path, model_size: str, language: str | None,
                            log: Log | None) -> list[Word]:
    import json
    import tempfile

    binary = whisper_cpp_binary()
    if binary is None:
        return []
    model = _whisper_cpp_model(model_size, log)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        run(["ffmpeg", "-y", "-hide_banner", "-i", str(path), "-vn", "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(wav)])
        out = Path(tmp) / "words"
        # -ml 1 + -sow: one output segment per word, so its offsets are word timings.
        run([binary, "-m", str(model), "-f", str(wav), "-l", language or "auto",
             "-ml", "1", "-sow", "-oj", "-of", str(out), "-np"])
        data = json.loads((out.with_suffix(".json")).read_text("utf-8", errors="replace"))
    return parse_whisper_cpp_json(data)


def parse_whisper_cpp_json(data: dict) -> list[Word]:
    words = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        off = seg.get("offsets") or {}
        if not text or text.startswith("[") or "from" not in off:
            continue
        start, end = off["from"] / 1000, off["to"] / 1000
        if words and not seg["text"].startswith(" ") and words[-1].end >= start - 0.01:
            # Continuation of the previous word (whisper.cpp splits some words in pieces).
            words[-1] = Word(words[-1].text + text, words[-1].start, end)
        else:
            words.append(Word(text, start, max(end, start + 0.05)))
    return words


# ------------------------------------------------------------- segmenting

def build_segments(
    infos: list[MediaInfo],
    transcripts: list[list[Word]],
    pause_preset: str,
    profiles: list[AudioProfile | None] | None = None,
    max_len: float = 6.0,
) -> list[Segment]:
    """Cut every clip into speech segments.

    Long silences split first; then sentence ends and restarts (so a failed take said again
    after only a short breath still becomes its own segment); then anything still long, so
    the framing can change.
    """
    segments: list[Segment] = []
    for src, info in enumerate(infos):
        words = transcripts[src] if src < len(transcripts) else []
        profile = profiles[src] if profiles and src < len(profiles) else None
        for s, e, pause in speech_ranges(info, pause_preset, profile):
            inside = [w for w in words if s <= (w.start + w.end) / 2 < e]
            for qs, qe, false_start in _sentence_pieces(s, e, inside):
                for ps, pe in _split_long(qs, qe, words, max_len):
                    seg = Segment(len(segments), src, round(ps, 3), round(pe, 3),
                                  round(pause if ps == s else 0.0, 2))
                    seg.words = [w for w in words if ps <= (w.start + w.end) / 2 < pe]
                    seg.false_start = false_start
                    if profile is not None:
                        seg.energy = profile.mean_db(ps, pe)
                    segments.append(seg)
    return segments


SENTENCE_END = (".", "!", "?", "…")


def _restarts(words: list[Word], start: int, j: int) -> bool:
    """Does the speaker begin the sentence (started at `start`) again at word j?"""
    n = min(3, j - start)
    if n < 2 or j + n > len(words):
        return False
    a = [_norm(w.text) for w in words[start:start + n]]
    b = [_norm(w.text) for w in words[j:j + n]]
    if a != b or len("".join(a)) < 5:
        return False
    before = words[start:j]
    if len(before) <= 4 and not before[-1].text.rstrip().endswith(SENTENCE_END):
        return True  # "Днес ще ви… Днес ще ви покажа": cut off mid-sentence
    # A finished sentence only counts if the next one says it again (not "И това е X. И това е Y.").
    return is_retake(" ".join(w.text for w in before), " ".join(w.text for w in words[j:]))


def is_retake(a: str, b: str) -> bool:
    """Is `a` a failed take of what `b` then says properly?

    Nearly all of a's words must come back, in order, at the start of b ("три трика за бърз
    монтаж" -> "три трика за супер бърз монтаж"), or the two must be almost identical.
    """
    aw, bw = _norm(a).split(), _norm(b).split()
    if len(aw) < 2 or len("".join(aw)) < 8:
        return False
    window = bw[:len(aw) + 4]
    matched = sum(m.size for m in difflib.SequenceMatcher(None, aw, window).get_matching_blocks())
    if matched >= len(aw) - (1 if len(aw) >= 6 else 0):
        return True
    return difflib.SequenceMatcher(None, " ".join(aw), " ".join(bw)).ratio() > 0.85


def _sentence_pieces(s: float, e: float, words: list[Word], min_len: float = 0.8):
    """Split a speech range at sentence ends (with a breath after) and at restarts."""
    cuts: list[tuple[float, bool]] = []
    sentence = 0
    for j in range(1, len(words)):
        prev, cur = words[j - 1], words[j]
        restart = _restarts(words, sentence, j)
        if restart or (prev.text.rstrip().endswith(SENTENCE_END) and cur.start - prev.end >= 0.2):
            cuts.append(((prev.end + cur.start) / 2, restart))
            sentence = j
    pieces: list[list] = []
    start = s
    for t, restart in cuts:
        if t - start < min_len and not restart:
            continue  # too short to stand alone: keep it with the next sentence
        pieces.append([start, t, restart])
        start = t
    pieces.append([start, e, False])
    # A short piece at the end joins the one before it (unless that one is a false start).
    if len(pieces) > 1 and pieces[-1][1] - pieces[-1][0] < min_len and not pieces[-2][2]:
        pieces[-2][1] = pieces.pop()[1]
    return [(a, b, fs) for a, b, fs in pieces]


def _split_long(s: float, e: float, words: list[Word], max_len: float):
    if e - s <= max_len * 1.4:
        yield s, e
        return
    target = s + (e - s) / 2
    inside = [w for w in words if s < w.end < e]
    # Prefer cutting in the biggest gap between two words near the middle.
    best = None
    for a, b in zip(inside, inside[1:]):
        mid = (a.end + b.start) / 2
        if abs(mid - target) < (e - s) * 0.3:
            score = (b.start - a.end) - abs(mid - target) * 0.05
            if best is None or score > best[0]:
                best = (score, mid)
    cut = best[1] if best else target
    yield from _split_long(s, cut, words, max_len)
    yield from _split_long(cut, e, words, max_len)


def find_retakes(segments: list[Segment]) -> set[int]:
    """Indexes of segments that look like a failed take that was said again right after."""
    drop: set[int] = {s.index for k, s in enumerate(segments)
                      if s.false_start and k + 1 < len(segments)}
    for i, a in enumerate(segments):
        if a.index in drop or not a.words:
            continue
        for j in (i + 1, i + 2):
            if j >= len(segments) or segments[j].source != a.source:
                break
            if is_retake(a.text, segments[j].text):
                drop.add(a.index)
                # A short bit between the two takes ("no wait…") goes too.
                if j == i + 2 and len(segments[i + 1].words) <= 5 and segments[i + 1].duration < 2.5:
                    drop.add(segments[i + 1].index)
                break
    return drop


FILLERS = {"ъ", "ъъ", "ъъъ", "ам", "ами", "мм", "ммм", "ъм", "ееее", "еее", "ее",
           "uh", "um", "umm", "uhm", "hmm", "mm", "er", "erm", "eh", "ah"}


def find_junk(segments: list[Segment], has_transcript: bool) -> set[int]:
    """Coughs, clicks and lone filler sounds ("ъъъ", "um") that got their own segment."""
    if not has_transcript:
        return set()
    junk = set()
    for s in segments:
        words = [_norm(w.text) for w in s.words]
        if not words and s.duration < 0.6:
            junk.add(s.index)
        elif words and all(w in FILLERS for w in words) and s.duration < 1.5:
            junk.add(s.index)
    if len(junk) >= len(segments):
        return set()
    return junk


def _norm(t: str) -> str:
    return re.sub(r"[^\w ]+", "", t.lower()).strip()


def score_emphasis(segments: list[Segment], power_words: tuple[str, ...]) -> None:
    """Mark how much each segment looks like a strong moment.

    Louder than the speaker's usual level, numbers, exclamations and "power words"
    all count. The very first segment is the hook and always counts a little.
    """
    energies = sorted(s.energy for s in segments if s.energy > -90)
    median = energies[len(energies) // 2] if energies else -99.0
    for s in segments:
        score = 0.0
        if s.energy > -90 and median > -90:
            score += max(-1.0, min(2.5, (s.energy - median) / 3.0))
        text = s.text.lower()
        if re.search(r"\d", text):
            score += 1.0
        if "!" in text:
            score += 0.7
        hits = sum(1 for w in re.findall(r"[\w-]+", text) for p in power_words if w.startswith(p))
        score += min(1.2, 0.6 * hits)
        s.emphasis = score
    if segments:
        segments[0].emphasis += 1.0


# --------------------------------------------------------------- reference

def analyze_reference(path: Path, frames_dir: Path, max_frames: int = 4) -> dict:
    """Measure pacing of a reference video and grab a few frames to look at."""
    if path.suffix.lower() in IMAGE_EXT:
        return {"name": path.name, "kind": "image", "frames": [str(path)]}
    info = probe(path)
    result = {"name": path.name, "duration": round(info.duration, 1), "frames": []}
    if info.has_video and info.duration > 0:
        log = run([
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
            "-vf", "scale=320:-2,select='gt(scene,0.3)',showinfo", "-f", "null", "-",
        ])
        cuts = len(re.findall(r"pts_time:", log))
        result["scene_cuts"] = cuts
        result["avg_shot_seconds"] = round(info.duration / (cuts + 1), 2)
        frames_dir.mkdir(parents=True, exist_ok=True)
        for i in range(max_frames):
            t = info.duration * (i + 0.5) / max_frames
            out = frames_dir / f"{path.stem}_{i}.jpg"
            try:
                run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                     "-vf", "scale=640:-2", "-q:v", "4", str(out)])
                result["frames"].append(str(out))
            except Exception:
                pass
    return result


def accent_color(images: list[str]) -> str | None:
    """The most vivid colour in the reference frames, used for highlights and graphics."""
    best = None
    for img in images[:12]:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", img, "-frames:v", "1", "-vf", "scale=12:12:flags=area",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
        data = proc.stdout
        for k in range(0, len(data) - 2, 3):
            r, g, b = data[k] / 255, data[k + 1] / 255, data[k + 2] / 255
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            if s < 0.45 or v < 0.5:
                continue
            score = s * v
            if best is None or score > best[0]:
                best = (score, h)
    if best is None:
        return None
    # Use the hue, but at full brightness so text stays readable on top of video.
    r, g, b = colorsys.hsv_to_rgb(best[1], 0.85, 1.0)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


# ------------------------------------------------------------------ b-roll

def broll_info(paths: list[Path]) -> list[dict]:
    items = []
    for p in paths:
        if p.suffix.lower() in IMAGE_EXT:
            items.append({"name": p.name, "path": str(p), "image": True, "duration": 0.0,
                          "hdr": False})
            continue
        try:
            info = probe(p)
        except Exception:
            continue
        if info.has_video and info.duration > 0.5:
            items.append({"name": p.name, "path": str(p), "image": False,
                          "duration": round(info.duration, 2), "hdr": info.hdr})
    return items
