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
    """Word-level transcript.

    Uses faster-whisper when it is installed, otherwise the whisper.cpp command line
    tool (`brew install whisper-cpp`), which works with any Python version.
    Returns [] when neither is available.
    """
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return _transcribe_whisper_cpp(path, model_size, language)
    return _transcribe_faster_whisper(path, model_size, language)


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


def _whisper_cpp_model(size: str) -> Path:
    import urllib.request

    cache = Path.home() / ".cache" / "ai-video-studio"
    cache.mkdir(parents=True, exist_ok=True)
    model = cache / f"ggml-{size}.bin"
    if not model.exists() or model.stat().st_size < 1_000_000:
        tmp = model.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL.format(size=size), tmp)
        tmp.replace(model)
    return model


def _transcribe_whisper_cpp(path: Path, model_size: str, language: str | None) -> list[Word]:
    import json
    import tempfile

    binary = whisper_cpp_binary()
    if binary is None:
        return []
    model = _whisper_cpp_model(model_size)
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
