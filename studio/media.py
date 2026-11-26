"""Small ffmpeg / ffprobe helpers used by the rest of the studio."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegError(
                f"'{tool}' не е намерен. Инсталирай ffmpeg (виж README) и пусни студиото отново."
            )


def run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command, raise FFmpegError with the tail of stderr on failure."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"Command failed: {' '.join(cmd[:6])} ...\n{tail}")
    return proc.stderr + proc.stdout


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool


def probe(path: Path) -> MediaInfo:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        raise FFmpegError(f"Не мога да прочета файла {path.name}: {out.stderr.strip()[-300:]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 30.0
    width = height = 0
    if v:
        width, height = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(sd["rotation"]))
        if str(v.get("tags", {}).get("rotate", "0")) in ("90", "270"):
            rot = 90
        if rot in (90, 270):
            width, height = height, width
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
        try:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) else 30.0
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        if not 10 <= fps <= 120:
            fps = 30.0
    duration = float(data.get("format", {}).get("duration") or 0)
    return MediaInfo(path, duration, width, height, fps, a is not None, v is not None)


def stream_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    data = json.loads(out.stdout or "{}")
    for s in data.get("streams", []):
        if s.get("duration"):
            return float(s["duration"])
    return float(data.get("format", {}).get("duration") or 0)


_ffmpeg_major: int | None = None


def ffmpeg_major() -> int:
    """Major version of the installed ffmpeg (99 for git builds without a number)."""
    global _ffmpeg_major
    if _ffmpeg_major is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True)
        m = re.search(r"ffmpeg version n?(\d+)\.", out.stdout)
        _ffmpeg_major = int(m.group(1)) if m else 99
    return _ffmpeg_major


def filter_graph_args(graph: str, script: Path) -> list[str]:
    """Arguments that hand ffmpeg a filter graph.

    Short graphs go inline (works on every ffmpeg). Long ones go through a file:
    ffmpeg 7+ reads it with `-/filter_complex FILE` (ffmpeg 8 removed the old
    `-filter_complex_script`, which versions before 7 still need).
    """
    if len(graph) < 6000:
        return ["-filter_complex", graph]
    script.write_text(graph, encoding="utf-8")
    if ffmpeg_major() >= 7:
        return ["-/filter_complex", str(script)]
    return ["-filter_complex_script", str(script)]


def even(x: float) -> int:
    n = int(round(x))
    return n - (n % 2)
