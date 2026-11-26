"""Small ffmpeg / ffprobe helpers used by the rest of the studio."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class FFmpegError(RuntimeError):
    pass


# ------------------------------------------------------------ which ffmpeg

# Homebrew's plain `ffmpeg` has no libass since 2025, so it cannot draw captions.
# `brew install ffmpeg-full` can (it is keg-only, so it lives here, not on PATH).
FULL_FFMPEG_DIRS = ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin")

_bins: tuple[str, str] | None = None
_filters: dict[str, set[str]] = {}


def _exe(folder: str, name: str) -> str:
    return os.path.join(folder, name + (".exe" if os.name == "nt" else ""))


def _filters_of(ffmpeg: str) -> set[str]:
    if ffmpeg not in _filters:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        # Lines look like " TSC ass   V->V   Render ASS subtitles...".
        _filters[ffmpeg] = {parts[1] for line in out.splitlines()
                            if len(parts := line.split()) >= 3 and "->" in parts[2]}
    return _filters[ffmpeg]


def binaries() -> tuple[str, str]:
    """(ffmpeg, ffprobe) to use.

    STUDIO_FFMPEG wins, then Homebrew's ffmpeg-full, then whatever is on PATH.
    A build that can draw captions (libass) is preferred over one that cannot.
    """
    global _bins
    if _bins is None:
        env = os.environ.get("STUDIO_FFMPEG", "").strip()
        if env:
            ffmpeg = env if os.path.isfile(env) else shutil.which(env) or env
        else:
            found = [f for f in (*(_exe(d, "ffmpeg") for d in FULL_FFMPEG_DIRS),
                                 shutil.which("ffmpeg") or "") if f and os.path.isfile(f)]
            ffmpeg = next((f for f in found if "ass" in _filters_of(f)),
                          found[0] if found else "ffmpeg")
        ffprobe = _exe(os.path.dirname(ffmpeg), "ffprobe") if os.path.dirname(ffmpeg) else ""
        if not os.path.isfile(ffprobe):
            ffprobe = shutil.which("ffprobe") or "ffprobe"
        _bins = (ffmpeg, ffprobe)
    return _bins


def _reset_caches() -> None:
    global _bins, _ffmpeg_major, _encoder
    _bins = _ffmpeg_major = _encoder = None
    _filters.clear()
    _options.clear()


def _tool(cmd: list[str]) -> list[str]:
    ffmpeg, ffprobe = binaries()
    if cmd and cmd[0] == "ffmpeg":
        return [ffmpeg, *cmd[1:]]
    if cmd and cmd[0] == "ffprobe":
        return [ffprobe, *cmd[1:]]
    return cmd


def ffmpeg_available() -> bool:
    ffmpeg, ffprobe = binaries()
    return bool((os.path.isfile(ffmpeg) or shutil.which(ffmpeg))
                and (os.path.isfile(ffprobe) or shutil.which(ffprobe)))


def require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise FFmpegError(
            "ffmpeg не е намерен. Инсталирай го (виж README) и пусни студиото отново."
        )


def has_filter(name: str) -> bool:
    return name in _filters_of(binaries()[0])


_options: dict[str, str] = {}


def has_option(filter_name: str, option: str) -> bool:
    """Does this ffmpeg's `filter_name` know `option`? (Filters gain options between versions.)"""
    if filter_name not in _options:
        try:
            _options[filter_name] = subprocess.run(
                [binaries()[0], "-hide_banner", "-h", f"filter={filter_name}"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            _options[filter_name] = ""
    return re.search(rf"^\s+{re.escape(option)}\s", _options[filter_name], re.M) is not None


def can_draw_text() -> bool:
    """Captions, titles and lower thirds need ffmpeg built with libass."""
    return has_filter("ass")


def ensure_full_ffmpeg(log: Callable[[str], None] = print) -> None:
    """On a Mac whose ffmpeg cannot draw captions, install Homebrew's ffmpeg-full once."""
    if sys.platform != "darwin" or os.environ.get("STUDIO_NO_AUTO_INSTALL") == "1":
        return
    if not ffmpeg_available() or can_draw_text():
        return
    brew = shutil.which("brew") or next(
        (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.isfile(b)), None)
    if brew is None:
        return
    log("\n  Твоят ffmpeg не може да рисува надписи. Инсталирам пълната версия"
        " (brew install ffmpeg-full), само веднъж е и отнема няколко минути…\n")
    try:
        subprocess.run([brew, "install", "ffmpeg-full"], check=False)
    except OSError:
        return
    _reset_caches()


# ------------------------------------------------------------------- running

def run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command, raise FFmpegError with the tail of stderr on failure."""
    cmd = _tool(cmd)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.strip().splitlines()
                 if not ln.startswith(("  built with", "  configuration:", "  lib"))]
        tail = "\n".join(lines[-15:])
        raise FFmpegError(f"Command failed: {Path(cmd[0]).name} {' '.join(cmd[1:6])} ...\n{tail}")
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
    hdr: bool = False  # iPhone HDR (HLG) or PQ footage that needs tone mapping


def probe(path: Path) -> MediaInfo:
    out = subprocess.run(
        _tool(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]),
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
    hdr = False
    if v:
        hdr = v.get("color_transfer") in ("arib-std-b67", "smpte2084")
        width, height = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(sd["rotation"])) % 360
        if str(v.get("tags", {}).get("rotate", "0")) in ("90", "270"):
            rot = 90
        if rot in (90, 270):
            width, height = height, width
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                num, den = (v.get(key) or "0/0").split("/")
                fps = float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                continue
            if 10 <= fps <= 240:
                break
        if not 10 <= fps <= 240:
            fps = 30.0
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0 and v and v.get("duration"):
        duration = float(v["duration"])
    return MediaInfo(path, duration, width, height, fps, a is not None, v is not None, hdr)


def stream_duration(path: Path) -> float:
    out = subprocess.run(
        _tool(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=duration:format=duration", "-of", "json", str(path)]),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        out = subprocess.run(_tool(["ffmpeg", "-hide_banner", "-version"]),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def sdr_filter(info: MediaInfo) -> str:
    """Tone-map HDR phone footage to normal (SDR) video so it does not look grey."""
    if not info.hdr:
        return ""
    if has_filter("zscale") and has_filter("tonemap"):
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    # Without zimg: HLG still looks acceptable as SDR, just a bit flat.
    return "format=yuv420p,eq=contrast=1.06:saturation=1.12"


# ----------------------------------------------------------------- encoding

_encoder: str | None = None


def encoder() -> str:
    """libx264 everywhere; Apple's hardware encoder on a Mac when it works (much faster)."""
    global _encoder
    if _encoder is None:
        choice = os.environ.get("STUDIO_ENCODER", "").lower()
        if choice in ("x264", "libx264", "cpu"):
            _encoder = "libx264"
        elif sys.platform == "darwin" or choice == "videotoolbox":
            test = subprocess.run(
                _tool(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=0.2",
                       "-c:v", "h264_videotoolbox", "-b:v", "2M", "-f", "null", "-"]),
                capture_output=True)
            _encoder = "h264_videotoolbox" if test.returncode == 0 else "libx264"
        else:
            _encoder = "libx264"
    return _encoder


def video_codec_args(width: int, height: int, fps: float, final: bool,
                     grainy: bool = False) -> list[str]:
    if encoder() == "h264_videotoolbox":
        mbps = max(4.0, width * height * fps * (0.16 if final else 0.3) / 1e6)
        if grainy:
            mbps *= 1.5  # film grain needs more bits
        return ["-c:v", "h264_videotoolbox", "-b:v", f"{mbps:.1f}M", "-profile:v", "high",
                "-allow_sw", "1"]
    if final:
        # Film grain is noise: x264 spends ages on it for no visible gain, so go faster.
        return ["-c:v", "libx264", "-preset", "veryfast" if grainy else "fast", "-crf", "20"]
    # Intermediate files: near-lossless so the final encode is the only real one.
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "15"]


def run_encode(build: Callable[[list[str]], list[str]], width: int, height: int, fps: float,
               final: bool, cwd: Path | None = None, grainy: bool = False) -> str:
    """Run an encoding command; if the hardware encoder fails, retry once on the CPU."""
    global _encoder
    try:
        return run(build(video_codec_args(width, height, fps, final, grainy)), cwd=cwd)
    except FFmpegError:
        if encoder() != "h264_videotoolbox":
            raise
        _encoder = "libx264"
        return run(build(video_codec_args(width, height, fps, final, grainy)), cwd=cwd)


def even(x: float) -> int:
    n = int(round(x))
    return max(2, n - (n % 2))
