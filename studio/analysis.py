"""Look at the raw footage: find pauses, transcribe speech, study reference videos."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .media import MediaInfo, probe, run


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
        d.pop("words")
        return d


# ---------------------------------------------------------------- silence

PAUSE_PRESETS = {
    # name: (noise floor dB, min silence seconds, padding seconds)
    "off": None,
    "gentle": (-38, 0.9, 0.18),
    "normal": (-35, 0.55, 0.12),
    "tight": (-32, 0.35, 0.08),
}


def detect_silences(path: Path, noise_db: float, min_dur: float) -> list[tuple[float, float]]:
    log = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-",
    ])
    silences: list[tuple[float, float]] = []
    start = None
    for line in log.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:
        silences.append((start, float("inf")))
    return silences


def speech_ranges(info: MediaInfo, preset: str) -> list[tuple[float, float, float]]:
    """Return (start, end, pause_before) ranges that contain speech."""
    settings = PAUSE_PRESETS.get(preset)
    if settings is None or not info.has_audio:
        return [(0.0, info.duration, 0.0)]
    noise, min_dur, pad = settings
    silences = detect_silences(info.path, noise, min_dur)
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


def transcribe(path: Path, model_size: str = "small", language: str | None = None) -> list[Word]:
    """Word-level transcript with faster-whisper. Returns [] if it is not installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return []
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


# ------------------------------------------------------------- segmenting

def build_segments(
    infos: list[MediaInfo],
    transcripts: list[list[Word]],
    pause_preset: str,
    max_len: float = 6.0,
) -> list[Segment]:
    """Cut every clip into speech segments, then split long ones so we can vary zoom."""
    segments: list[Segment] = []
    for src, info in enumerate(infos):
        words = transcripts[src] if src < len(transcripts) else []
        for s, e, pause in speech_ranges(info, pause_preset):
            for ps, pe in _split_long(s, e, words, max_len):
                seg = Segment(len(segments), src, round(ps, 3), round(pe, 3),
                              round(pause if ps == s else 0.0, 2))
                seg.words = [w for w in words if ps <= (w.start + w.end) / 2 < pe]
                segments.append(seg)
    return segments


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
    """Indexes of segments that look like a failed take repeated right after."""
    drop = set()
    for a, b in zip(segments, segments[1:]):
        ta, tb = _norm(a.text), _norm(b.text)
        if len(ta) < 12 or len(tb) < 12:
            continue
        # Same sentence said twice, or the first one is a cut-off start of the second.
        ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
        if ratio > 0.75 or (tb.startswith(ta[: max(12, len(ta) // 2)]) and len(ta) < len(tb)):
            drop.add(a.index)
    return drop


def _norm(t: str) -> str:
    return re.sub(r"[^\w ]+", "", t.lower()).strip()


# --------------------------------------------------------------- reference

def analyze_reference(path: Path, frames_dir: Path, max_frames: int = 4) -> dict:
    """Measure pacing of a reference video and grab a few frames to look at."""
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
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
