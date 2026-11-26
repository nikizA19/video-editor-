"""Render an EditPlan to a finished video with ffmpeg."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .analysis import Segment
from .media import MediaInfo, even, filter_graph_args, run, stream_duration
from .planner import EditPlan

ASSET_SFX = Path(__file__).parent / "assets" / "sfx"

TRANSITION_SECONDS = {"fade": 0.3, "whoosh_slide": 0.3, "flash": 0.2}
XFADE_NAME = {"fade": "fade", "whoosh_slide": "smoothleft", "flash": "fadewhite"}

GRADES = {
    "none": "",
    "punchy": "eq=contrast=1.06:saturation=1.15:gamma=0.98",
    "warm": "eq=contrast=1.05:saturation=1.1,colorbalance=rs=0.05:gs=0.01:bs=-0.05",
    "cool": "eq=contrast=1.05:saturation=1.05,colorbalance=rs=-0.04:bs=0.05",
    "cinematic": "eq=contrast=1.1:saturation=0.9:gamma=0.97,colorbalance=rs=0.03:bs=0.04:rh=0.04:bh=-0.03,vignette=PI/6",
}

# Synthesised sound effects so the app works with zero assets. Drop your own
# whoosh.wav / pop.wav / ... into studio/assets/sfx/ to override them.
SFX_SYNTH = {
    "whoosh": ("anoisesrc=d=0.6:c=pink:a=0.5,highpass=f=400,lowpass=f=5000,"
               "afade=t=in:d=0.35:curve=exp,afade=t=out:st=0.35:d=0.25"),
    "pop": "aevalsrc='0.7*sin(2*PI*(500+2500*exp(-40*t))*t)*exp(-25*t)':d=0.18",
    "click": "aevalsrc='0.6*(random(0)-0.5)*exp(-250*t)':d=0.05",
    "boom": "aevalsrc='0.9*sin(2*PI*(45+80*exp(-6*t))*t)*exp(-3.5*t)':d=1.2,lowpass=f=300",
    "ding": "aevalsrc='0.35*(sin(2*PI*1320*t)+0.5*sin(2*PI*2640*t))*exp(-4*t)':d=1.3",
}
SFX_LEAD = {"whoosh": 0.3}  # start a whoosh slightly before the cut so it peaks on it
SFX_VOLUME = {"whoosh": 0.5, "pop": 0.6, "click": 0.5, "boom": 0.7, "ding": 0.45}

Log = Callable[[str], None]


@dataclass
class Frame:
    width: int
    height: int
    fps: float


def output_frame(first: MediaInfo, fmt: str) -> Frame:
    w, h = first.width or 1920, first.height or 1080
    if fmt == "vertical_9_16":
        w, h = 1080, 1920
    elif fmt == "horizontal_16_9":
        w, h = 1920, 1080
    elif fmt == "square_1_1":
        w, h = 1080, 1080
    else:  # keep original shape, cap at 1080p-ish
        scale = min(1.0, 1920 / max(w, h))
        w, h = even(w * scale), even(h * scale)
    fps = min(max(round(first.fps), 24), 60)
    return Frame(w, h, float(fps))


def _fit(frame: Frame) -> str:
    """Scale + centre-crop any input to the output frame."""
    W, H = frame.width, frame.height
    return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1")


def _zoom(kind: str, frame: Frame, frames: int) -> str:
    W, H = frame.width, frame.height
    if kind == "punch_in":
        z = 1.18
        zw, zh = even(W * z), even(H * z)
        # Aim slightly above centre: that is where faces usually are.
        return f"scale={zw}:{zh},crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.4"
    if kind in ("slow_zoom_in", "slow_zoom_out"):
        n = max(frames, 1)
        expr = f"1+0.12*on/{n}" if kind == "slow_zoom_in" else f"1.12-0.12*on/{n}"
        # Upscale first so the zoom moves smoothly instead of in 1px steps.
        return (f"scale={W * 2}:{H * 2},zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':"
                f"y='ih*0.45-(ih/zoom/2)':d=1:s={W}x{H}:fps={frame.fps}")
    return ""


def render_segment(seg: Segment, info: MediaInfo, kind: str, frame: Frame, out: Path) -> float:
    frames = int(seg.duration * frame.fps)
    vf = ",".join(f for f in [_fit(frame), f"fps={frame.fps}", _zoom(kind, frame, frames),
                              "setsar=1", "format=yuv420p"] if f)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", f"{seg.start:.3f}", "-to", f"{seg.end:.3f}",
           "-i", str(info.path)]
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{seg.duration:.3f}", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if info.has_audio else "1:a:0",
            "-vf", vf, "-af", "aresample=48000,aformat=channel_layouts=stereo",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    run(cmd)
    return stream_duration(out)


def join_segments(files: list[Path], durations: list[float], transitions: list[str],
                  out: Path) -> list[float]:
    """Concatenate segments, using xfade where a transition was asked for.

    Returns the start time of every segment in the joined video (for captions/SFX).
    """
    # Group runs of hard cuts; xfade between groups.
    groups: list[list[int]] = [[0]]
    for i in range(1, len(files)):
        t = transitions[i]
        td = TRANSITION_SECONDS.get(t, 0)
        if td and durations[i] > td * 2 + 0.2 and durations[i - 1] > td * 2 + 0.2:
            groups.append([i])
        else:
            transitions[i] = "cut"
            groups[-1].append(i)

    parts = []
    starts = [0.0] * len(files)
    labels = []
    for g, idxs in enumerate(groups):
        if len(idxs) == 1:
            src_v, src_a = f"[{idxs[0]}:v]", f"[{idxs[0]}:a]"
        else:
            ins = "".join(f"[{i}:v][{i}:a]" for i in idxs)
            parts.append(f"{ins}concat=n={len(idxs)}:v=1:a=1[cv{g}][ca{g}]")
            src_v, src_a = f"[cv{g}]", f"[ca{g}]"
        if len(groups) > 1:  # xfade needs identical timebases on both inputs
            parts.append(f"{src_v}settb=AVTB,setpts=PTS-STARTPTS[gv{g}]")
            parts.append(f"{src_a}asetpts=PTS-STARTPTS[ga{g}]")
            src_v, src_a = f"[gv{g}]", f"[ga{g}]"
        labels.append((src_v, src_a))

    cur_v, cur_a = labels[0]
    cur_dur = 0.0
    for i in groups[0]:
        starts[i] = cur_dur
        cur_dur += durations[i]
    for g in range(1, len(groups)):
        first = groups[g][0]
        t = transitions[first]
        td = TRANSITION_SECONDS[t]
        gv, ga = labels[g]
        offset = cur_dur - td
        parts.append(f"{cur_v}{gv}xfade=transition={XFADE_NAME[t]}:duration={td}:"
                     f"offset={offset:.3f}[xv{g}]")
        parts.append(f"{cur_a}{ga}acrossfade=d={td}[xa{g}]")
        cur_v, cur_a = f"[xv{g}]", f"[xa{g}]"
        cur_dur = offset
        for i in groups[g]:
            starts[i] = cur_dur
            cur_dur += durations[i]

    cmd = ["ffmpeg", "-y", "-hide_banner"]
    for f in files:
        cmd += ["-i", str(f)]
    if parts:
        cmd += [*filter_graph_args(";".join(parts), out.with_suffix(".filter.txt")),
                "-map", cur_v, "-map", cur_a]
    else:
        cmd += ["-map", "0:v", "-map", "0:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out)]
    run(cmd)
    return starts


# ---------------------------------------------------------------- captions

def _ass_time(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


CAPTION_STYLES = {
    # primary, outline, back, border style, outline width
    "bold_yellow": ("&H00FFFFFF", "&H00000000", "&H80000000", 1, 5),
    "clean_white": ("&H00FFFFFF", "&H40000000", "&H60000000", 1, 2),
    "boxed": ("&H00FFFFFF", "&H00000000", "&HA0000000", 3, 3),
}
HIGHLIGHT = {"bold_yellow": "&H0000E5FF&", "clean_white": "&H00FFFFFF&", "boxed": "&H0000E5FF&"}


def caption_chunks(timed_words: list[tuple[str, float, float, int]], per_caption: int):
    """Group words into the short phrases shown on screen together."""
    chunks: list[list[tuple[str, float, float, int]]] = []
    for w in timed_words:
        if (not chunks or len(chunks[-1]) >= per_caption
                or w[3] != chunks[-1][-1][3]  # never run a caption across a cut
                or w[1] - chunks[-1][-1][2] > 0.6
                or chunks[-1][-1][0][-1:] in ".!?,"):
            chunks.append([])
        chunks[-1].append(w)
    return chunks


def write_ass(path: Path, frame: Frame, plan: EditPlan, timed_words: list[tuple[str, float, float, int]],
              popups: list[tuple[str, float]]) -> None:
    W, H = frame.width, frame.height
    vertical = H > W
    size = int(H * (0.055 if vertical else 0.065))
    margin_v = int(H * (0.25 if vertical else 0.09))
    primary, outline, back, border, ow = CAPTION_STYLES[plan.caption_style]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,Arial,{size},{primary},&H000000FF,{outline},{back},1,0,0,0,100,100,0,0,"
        f"{border},{ow},2,2,{int(W*0.08)},{int(W*0.08)},{margin_v},1",
        f"Style: Pop,Arial,{int(size*1.3)},&H00000000,&H000000FF,&H0000E5FF,&H0000E5FF,1,0,0,0,"
        f"100,100,0,0,3,{int(size*0.25)},0,8,40,40,{int(H*0.12)},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    hl = HIGHLIGHT[plan.caption_style]
    if plan.captions and timed_words:
        chunks = caption_chunks(timed_words, plan.words_per_caption)
        for ci, chunk in enumerate(chunks):
            chunk_end = chunk[-1][2] + 0.15
            if ci + 1 < len(chunks):
                chunk_end = min(chunk_end + 0.3, chunks[ci + 1][0][1])
            for wi, (_, ws, _, _) in enumerate(chunk):
                we = chunk[wi + 1][1] if wi + 1 < len(chunk) else chunk_end
                if we - ws < 0.02:
                    continue
                parts = []
                for wj, (txt, _, _, _) in enumerate(chunk):
                    txt = _ass_escape(txt.upper() if plan.caption_uppercase else txt)
                    parts.append(f"{{\\c{hl}}}{txt}{{\\c{primary}&}}" if wj == wi else txt)
                anim = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
                lines.append(f"Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Cap,,0,0,0,,"
                             f"{anim}{' '.join(parts)}")
    for text, t in popups:
        lines.append(f"Dialogue: 1,{_ass_time(t)},{_ass_time(t + 1.6)},Pop,,0,0,0,,"
                     "{\\fscx40\\fscy40\\t(0,140,\\fscx110\\fscy110)\\t(140,220,\\fscx100\\fscy100)"
                     f"\\fad(0,200)}}{_ass_escape(text.upper())}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- sfx

def sfx_file(name: str, cache: Path) -> Path:
    for ext in (".wav", ".mp3"):
        custom = ASSET_SFX / f"{name}{ext}"
        if custom.exists():
            return custom
    out = cache / f"sfx_{name}.wav"
    if not out.exists():
        run(["ffmpeg", "-y", "-hide_banner", "-f", "lavfi", "-i", SFX_SYNTH[name],
             "-ar", "48000", "-ac", "2", str(out)])
    return out


# ------------------------------------------------------------------- final

def final_mix(joined: Path, out: Path, workdir: Path, plan: EditPlan, ass: Path | None,
              sfx_events: list[tuple[str, float]], music: Path | None, duration: float) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(joined)]
    vf = [f for f in [GRADES.get(plan.color_grade, "")] if f]
    if ass is not None:
        vf.append(f"ass={ass.name}")
    filters = [f"[0:v]{','.join(vf) or 'null'}[v]"]

    audio_inputs = ["[voice]"]
    voice_chain = "[0:a]aresample=48000"
    n = 1
    sfx_labels = []
    for k, (name, t) in enumerate(sfx_events):
        cmd += ["-i", str(sfx_file(name, workdir))]
        delay = int(max(0.0, t - SFX_LEAD.get(name, 0.0)) * 1000)
        filters.append(f"[{n}:a]aformat=channel_layouts=stereo,volume={SFX_VOLUME[name]},"
                       f"adelay={delay}|{delay}[s{k}]")
        sfx_labels.append(f"[s{k}]")
        n += 1
    if music is not None:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(f"{voice_chain},asplit=2[voice][sc]")
        filters.append(f"[{n}:a]aformat=channel_layouts=stereo,aresample=48000,"
                       f"atrim=0:{duration:.2f},volume={plan.music_volume:.2f},"
                       f"afade=t=out:st={max(0.0, duration - 2):.2f}:d=2[mraw]")
        filters.append("[mraw][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:"
                       "release=400[music]")
        audio_inputs.append("[music]")
        n += 1
    else:
        filters.append(f"{voice_chain}[voice]")
    audio_inputs += sfx_labels
    filters.append(f"{''.join(audio_inputs)}amix=inputs={len(audio_inputs)}:normalize=0:"
                   "duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")

    cmd += [*filter_graph_args(";".join(filters), workdir / "final.filter.txt"),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out.resolve())]
    run(cmd, cwd=workdir)
