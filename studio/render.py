"""Render an EditPlan to a finished video with ffmpeg.

1. Every kept segment is cut out, framed and given its camera move, effect and
   b-roll (several at once, into near-lossless intermediate files).
2. One final pass joins them (hard cuts + transitions), grades the colour, adds
   film grain / letterbox / progress bar, burns in the captions and motion
   graphics, and mixes voice, music and sound effects.
"""
from __future__ import annotations

import colorsys
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import media
from .analysis import Segment
from .media import (MediaInfo, even, filter_graph_args, has_filter, has_option, run,
                    run_encode, sdr_filter)
from .planner import EditPlan, SegmentEdit

ASSET_SFX = Path(__file__).parent / "assets" / "sfx"

# plan name -> (xfade transition, seconds)
TRANSITIONS = {
    "fade": ("fade", 0.35),
    "dip_black": ("fadeblack", 0.5),
    "slide": ("smoothleft", 0.35),
    "whip": ("hblur", 0.25),
    "zoom": ("zoomin", 0.3),
    "glitch": ("pixelize", 0.3),
    "flash": ("fadewhite", 0.25),
}

# Colour grades. They run per shot, in parallel. RGB-based filters (colorbalance, curves)
# are slow, so everything here works directly on YUV.
GRADES = {
    "none": "",
    "punchy": "eq=contrast=1.06:saturation=1.18:gamma=0.98",
    "warm": "eq=contrast=1.05:saturation=1.1:gamma_r=1.05:gamma_b=0.95",
    "cool": "eq=contrast=1.05:saturation=1.05:gamma_r=0.96:gamma_b=1.05",
    "cinematic": "eq=contrast=1.1:saturation=0.9:gamma=0.97,vignette=PI/5",
    "bw": "hue=s=0,eq=contrast=1.15:gamma=0.97",
}
# Teal shadows, warm highlights: shift the colour planes by the brightness of each pixel.
TEAL_ORANGE = ("c0=x:c1='clip(x+7*pow(1-y/255,2)-7*pow(y/255,2),0,255)':"
               "c2='clip(x-6*pow(1-y/255,2)+8*pow(y/255,2),0,255)'")


def grade_graph(grade: str, src: str, dst: str) -> list[str]:
    chain = GRADES.get(grade, "")
    if not chain:
        return []
    if grade != "cinematic":
        return [f"{src}{chain},format=yuv420p{dst}"]
    if has_option("mergeplanes", "map1s"):  # ffmpeg 6+: build a frame whose U/V planes hold luma
        return [f"{src}{chain},format=yuv420p,split=3[tg0][tg1][tg2]",
                "[tg1]extractplanes=y,scale=iw/2:ih/2:flags=fast_bilinear,split[tgu][tgv]",
                "[tg2][tgu][tgv]mergeplanes=map0s=0:map0p=0:map1s=1:map1p=0:map2s=2:map2p=0:"
                "format=yuv420p[tgl]",
                f"[tg0][tgl]lut2={TEAL_ORANGE}{dst}"]
    return [f"{src}{chain},format=yuv444p,split[tg0][tg1]", "[tg1]shuffleplanes=0:0:0[tgl]",
            f"[tg0][tgl]lut2={TEAL_ORANGE},format=yuv420p{dst}"]


# Synthesised sound effects so the app works with zero assets. Drop your own
# whoosh.wav / pop.wav / ... into studio/assets/sfx/ to override them.
SFX_SYNTH = {
    "whoosh": ("anoisesrc=d=0.6:c=pink:a=0.5:r=48000,highpass=f=400,lowpass=f=5000,"
               "afade=t=in:d=0.35:curve=exp,afade=t=out:st=0.35:d=0.25"),
    "swoosh": ("anoisesrc=d=0.35:c=white:a=0.4:r=48000,bandpass=f=2600:width_type=o:w=1.6,"
               "afade=t=in:d=0.12:curve=exp,afade=t=out:st=0.12:d=0.23"),
    "pop": "aevalsrc='0.7*sin(2*PI*(500+2500*exp(-40*t))*t)*exp(-25*t)':s=48000:d=0.18",
    "click": "aevalsrc='0.6*(random(0)-0.5)*exp(-250*t)':s=48000:d=0.05",
    "boom": "aevalsrc='0.9*sin(2*PI*(45+80*exp(-6*t))*t)*exp(-3.5*t)':s=48000:d=1.2,lowpass=f=300",
    "ding": "aevalsrc='0.35*(sin(2*PI*1320*t)+0.5*sin(2*PI*2640*t))*exp(-4*t)':s=48000:d=1.3",
    "riser": ("aevalsrc='(0.22*sin(2*PI*(160*t+260*t*t))+0.12*(random(0)-0.5))*pow(t/1.4,2)':"
              "s=48000:d=1.4,highpass=f=120,afade=t=out:st=1.32:d=0.08"),
    "glitch": ("aevalsrc='0.4*sgn(sin(2*PI*(90+600*floor(mod(t*28,4)))*t))*lt(mod(t,0.07),0.045)':"
               "s=48000:d=0.32,lowpass=f=6000"),
    "shutter": ("aevalsrc='0.7*(random(0)-0.5)*(exp(-140*t)+0.8*exp(-140*(t-0.07))*gte(t,0.07))':"
                "s=48000:d=0.2,highpass=f=800"),
    "type": "aevalsrc='0.55*(random(0)-0.5)*exp(-320*mod(t,0.085))':s=48000:d=0.6,highpass=f=1200",
}
SFX_LEAD = {"whoosh": 0.3, "swoosh": 0.15, "riser": 1.35}  # start early so it peaks on the cut
SFX_VOLUME = {"whoosh": 0.5, "swoosh": 0.45, "pop": 0.55, "click": 0.5, "boom": 0.7, "ding": 0.4,
              "riser": 0.4, "glitch": 0.3, "shutter": 0.5, "type": 0.35}

Log = Callable[[str], None]


@dataclass
class Frame:
    width: int
    height: int
    fps: float

    @property
    def vertical(self) -> bool:
        return self.height > self.width


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
    fps = first.fps or 30.0
    while fps > 31:  # 60 -> 30, 50 -> 25: plenty for talking heads, twice as fast to render
        fps /= 2
    return Frame(w, h, float(max(24, round(fps))))


def letterbox_bar(frame: Frame) -> int:
    """Height of each black bar for a 2.39:1 'cinema' look (horizontal video only)."""
    if frame.width / frame.height < 1.5:
        return 0
    return even(max(0.0, (frame.height - frame.width / 2.39) / 2))


# ------------------------------------------------------------ camera moves

def _fit(frame: Frame) -> str:
    """Scale + centre-crop any input to the output frame."""
    W, H = frame.width, frame.height
    return (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,"
            f"crop={W}:{H},setsar=1")


def _zoompan(frame: Frame, z: str, cx: str, cy: str) -> str:
    """A virtual camera: zoom `z` aimed at (cx, cy), both 0..1 of the picture."""
    W, H = frame.width, frame.height
    x = f"max(0,min(iw-iw/zoom,({cx})*iw-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,({cy})*ih-ih/zoom/2))"
    # Upscale first so slow moves glide instead of stepping a pixel at a time.
    return (f"scale={W * 2}:{H * 2}:flags=bicubic,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={frame.fps:g}")


def camera_filter(kind: str, frame: Frame, frames: int) -> str:
    W, H, fps = frame.width, frame.height, frame.fps
    p = f"min(on/{max(frames - 1, 1)},1)"   # 0 -> 1 over the shot
    ease = f"(3-2*{p})*{p}*{p}"             # smooth start and stop, like a real dolly
    if kind == "punch_in":
        # Static, tighter framing aimed a little above centre (where faces are).
        zw, zh = even(W * 1.18), even(H * 1.18)
        return f"scale={zw}:{zh}:flags=bicubic,crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.4"
    if kind == "push_in":
        return _zoompan(frame, f"1+0.12*{ease}", "0.5", "0.45")
    if kind == "pull_out":
        return _zoompan(frame, f"1.14-0.12*{ease}", "0.5", "0.45")
    if kind in ("crash_zoom", "impact"):
        q = f"min(on/{max(2, round(fps * (0.16 if kind == 'impact' else 0.22)))},1)"
        z = f"1+0.28*(1-pow(1-{q},3))+0.03*{p}"
        blur = ",tmix=frames=3:enable='lt(t,0.24)'"  # motion blur while it rushes in
        if kind == "crash_zoom":
            return _zoompan(frame, z, "0.5", "0.42") + blur
        decay = f"exp(-on/{fps * 0.35:.2f})"
        return _zoompan(frame, z, f"0.5+0.014*sin(on*2.7)*{decay}",
                        f"0.42+0.011*cos(on*3.4)*{decay}") + blur
    if kind == "pan_right":
        return _zoompan(frame, "1.15", f"0.445+0.11*{ease}", "0.47")
    if kind == "pan_left":
        return _zoompan(frame, "1.15", f"0.555-0.11*{ease}", "0.47")
    if kind == "tilt_up":
        return _zoompan(frame, "1.15", "0.5", f"0.555-0.11*{ease}")
    if kind == "tilt_down":
        return _zoompan(frame, "1.15", "0.5", f"0.445+0.11*{ease}")
    if kind == "shake":
        return _zoompan(frame, "1.08", "0.5+0.010*sin(on*0.5)+0.004*sin(on*1.7)",
                        "0.48+0.008*cos(on*0.6)+0.004*sin(on*1.9)")
    if kind == "float":
        return _zoompan(frame, f"1.06+0.05*{p}", f"0.5+0.018*sin(2*PI*on/{fps * 7:.0f})",
                        f"0.49+0.012*sin(2*PI*on/{fps * 5:.0f})")
    return ""


def effect_filter(kind: str, frame: Frame) -> str:
    if kind == "flash":
        return "fade=t=in:st=0:d=0.22:color=white"
    if kind == "glitch":
        s = max(4, frame.width // 110)
        flicker = "lt(t,0.45)*lt(mod(t,0.15),0.09)"
        return (f"rgbashift=rh=-{s}:bh={s}:gv={max(2, s // 2)}:enable='{flicker}',"
                "noise=alls=30:allf=t:enable='lt(t,0.3)'")
    if kind == "bw":
        return "hue=s=0"
    return ""


def _light_leak(src: str, frame: Frame, dst: str) -> list[str]:
    """A warm light sweep that blooms over the shot's first second (screen blend)."""
    W, H = frame.width, frame.height
    if not has_filter("gradients"):
        return [f"{src}eq=brightness='0.10*max(0,1-abs(t-0.45)/0.45)':eval=frame{dst}"]
    return [
        f"gradients=s={W}x{H}:r={frame.fps:g}:d=1.6:c0=0xFF7A2E:c1=0x000000:"
        f"x0=0:y0=0:x1={int(W * 0.8)}:y1={H}:speed=0.03,format=gbrp,"
        "fade=t=in:st=0:d=0.35,fade=t=out:st=0.55:d=1.0[leak]",
        f"{src}format=gbrp[leakbase]",
        f"[leakbase][leak]blend=all_mode=screen:all_opacity=0.85,format=yuv420p{dst}",
    ]


# ---------------------------------------------------------------- segments

@dataclass
class SegmentJob:
    seg: Segment
    info: MediaInfo
    edit: SegmentEdit
    out: Path
    broll: dict | None = None  # {"path", "image", "duration", "hdr"}
    grade: str = "none"        # colour grade, applied last so b-roll matches the footage


BROLL_MAX = 3.0


def broll_seconds(seg_duration: float) -> float:
    return min(seg_duration, BROLL_MAX)


def render_segment(job: SegmentJob, frame: Frame) -> float:
    """Render one kept segment. Returns its exact length in seconds."""
    seg, info, edit = job.seg, job.info, job.edit
    fps = frame.fps
    n = max(1, round(seg.duration * fps))
    dur = n / fps
    chain = ["setpts=PTS-STARTPTS"]
    if sdr := sdr_filter(info):
        chain.append(sdr)
    chain += [_fit(frame), f"fps={fps:g}"]
    if cam := camera_filter(edit.camera, frame, n):
        chain.append(cam)
    if fx := effect_filter(edit.effect, frame):
        chain.append(fx)
    # Clone the last frame if the source runs out early, so every shot has exactly n frames.
    chain += ["setsar=1", "format=yuv420p", "tpad=stop_mode=clone:stop_duration=1"]
    graph = [f"[0:v]{','.join(chain)}[v0]"]
    last = "[v0]"
    inputs = ["-ss", f"{seg.start:.3f}", "-to", f"{seg.end:.3f}", "-i", str(info.path)]

    if edit.effect == "light_leak":
        graph += _light_leak(last, frame, "[v1]")
        last = "[v1]"

    if job.broll:
        b = job.broll
        bdur = broll_seconds(dur)
        if b["image"]:
            nb = max(1, round(bdur * fps))
            inputs += ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{bdur:.3f}", "-i", b["path"]]
            W, H = frame.width, frame.height
            bchain = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase:flags=bicubic,"
                      f"crop={W * 2}:{H * 2},"
                      f"zoompan=z='1+0.08*on/{nb}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:"
                      f"s={W}x{H}:fps={fps:g}")
        else:
            start = b["duration"] * 0.2 if b["duration"] > bdur + 1.5 else 0.0
            inputs += ["-ss", f"{start:.3f}", "-t", f"{bdur:.3f}", "-i", b["path"]]
            hdr = sdr_filter(MediaInfo(Path(b["path"]), 0, 0, 0, fps, False, True, b.get("hdr", False)))
            bchain = ",".join(f for f in ["setpts=PTS-STARTPTS", hdr, _fit(frame), f"fps={fps:g}"] if f)
        graph.append(f"[1:v]{bchain},setsar=1,format=yuv420p[broll]")
        graph.append(f"{last}[broll]overlay=eof_action=pass:enable='lt(t,{bdur:.3f})'[v2]")
        last = "[v2]"

    if look := grade_graph(job.grade, last, "[v3]"):
        graph += look
        last = "[v3]"

    if info.has_audio:
        graph.append(f"[0:a]asetpts=PTS-STARTPTS,aresample=48000,"
                     f"aformat=sample_fmts=s16:channel_layouts=stereo,apad,atrim=0:{dur:.6f}[a]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.6f},aformat=sample_fmts=s16[a]")

    def build(codec: list[str]) -> list[str]:
        return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
                *filter_graph_args(";".join(graph), job.out.with_suffix(".filter.txt")),
                "-map", last, "-map", "[a]", "-frames:v", str(n), *codec, "-pix_fmt", "yuv420p",
                "-c:a", "pcm_s16le", "-f", "mov", str(job.out)]

    run_encode(build, frame.width, frame.height, fps, final=False)
    return dur


def workers() -> int:
    env = os.environ.get("STUDIO_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def render_segments(jobs: list[SegmentJob], frame: Frame,
                    on_done: Callable[[int], None] = lambda n: None) -> list[float]:
    """Render all segments, a few at a time (zoompan and friends use one core each)."""
    media.encoder()  # decide the encoder once, before the threads start
    durations = [0.0] * len(jobs)
    n_workers = min(workers(), len(jobs))
    if n_workers <= 1:
        for i, job in enumerate(jobs):
            durations[i] = render_segment(job, frame)
            on_done(i + 1)
        return durations
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(render_segment, job, frame): i for i, job in enumerate(jobs)}
        try:
            for done, fut in enumerate(as_completed(futures), 1):
                durations[futures[fut]] = fut.result()
                on_done(done)
        except BaseException:
            for fut in futures:
                fut.cancel()
            raise
    return durations


# --------------------------------------------------------------------- join

def plan_join(durations: list[float], transitions: list[str]):
    """Group hard cuts together; a transition starts each new group.

    Returns (groups, transitions actually used, start time of every segment, total length).
    """
    used = list(transitions)
    if used:
        used[0] = "cut"
    groups: list[list[int]] = [[0]] if durations else []
    for i in range(1, len(durations)):
        td = TRANSITIONS.get(used[i], ("", 0.0))[1]
        if td and durations[i] > td * 2 + 0.2 and durations[i - 1] > td * 2 + 0.2:
            groups.append([i])
        else:
            used[i] = "cut"
            groups[-1].append(i)
    starts = [0.0] * len(durations)
    cur = 0.0
    for g, idxs in enumerate(groups):
        if g:
            cur -= TRANSITIONS[used[idxs[0]]][1]
        for i in idxs:
            starts[i] = cur
            cur += durations[i]
    return groups, used, starts, cur


def _concat_line(path: Path) -> str:
    return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n"


# ------------------------------------------------------------- the final pass

def assemble(files: list[Path], durations: list[float], transitions: list[str], out: Path,
             workdir: Path, plan: EditPlan, frame: Frame, ass: Path | None,
             sfx_events: list[tuple[str, float]], music: Path | None) -> dict:
    """Join, grade, caption and mix everything in a single ffmpeg pass."""
    groups, used, starts, total = plan_join(durations, transitions)
    W, H, fps = frame.width, frame.height, frame.fps
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    # Each run of hard cuts is read as one input (concat demuxer), so even a video with
    # hundreds of cuts opens only a handful of files.
    for g, idxs in enumerate(groups):
        lst = workdir / f"group_{g:03d}.ffconcat"
        lst.write_text("ffconcat version 1.0\n" + "".join(_concat_line(files[i]) for i in idxs),
                       encoding="utf-8")
        cmd += ["-f", "concat", "-safe", "0", "-i", str(lst)]
    n_in = len(groups)
    parts: list[str] = []
    if len(groups) == 1:
        v, a = "[0:v]", "[0:a]"
    else:
        for g in range(len(groups)):
            parts.append(f"[{g}:v]fps={fps:g},setpts=PTS-STARTPTS[gv{g}]")
            parts.append(f"[{g}:a]asetpts=PTS-STARTPTS[ga{g}]")
        v, a = "[gv0]", "[ga0]"
        cur = sum(durations[i] for i in groups[0])
        for g in range(1, len(groups)):
            name, td = TRANSITIONS[used[groups[g][0]]]
            offset = cur - td
            parts.append(f"{v}[gv{g}]xfade=transition={name}:duration={td}:offset={offset:.4f}[xv{g}]")
            parts.append(f"{a}[ga{g}]acrossfade=d={td}[xa{g}]")
            v, a = f"[xv{g}]", f"[xa{g}]"
            cur = offset + sum(durations[i] for i in groups[g])

    # -- picture (the colour grade was already applied to every shot)
    vf = []
    if plan.film_grain:
        vf.append("noise=c0s=6:c0f=t")
    bar = letterbox_bar(frame) if plan.letterbox else 0
    if bar:
        vf.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,"
                  f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    if ass is not None:
        vf.append(f"ass={ass.name}")
    parts.append(f"{v}{','.join(vf) or 'null'}[vg]")
    v = "[vg]"
    if plan.progress_bar and total > 1:
        bh = even(max(4, H / 180))
        parts.append(f"color=c=0x{plan.accent_color.lstrip('#')}:s={W}x{bh}:r={fps:g}[pbar]")
        parts.append(f"{v}[pbar]overlay=x='-w+w*t/{total:.3f}':y={0 if frame.vertical else H - bh}:"
                     f"shortest=1[vp]")
        v = "[vp]"
    parts.append(f"{v}format=yuv420p[vout]")

    # -- sound: voice (cleaned up), music ducked under the voice, sound effects
    voice = f"{a}highpass=f=70,acompressor=threshold=0.125:ratio=2.5:attack=8:release=150"
    mix = ["[voice]"]
    if music is not None and plan.music_volume > 0:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"{voice},asplit=2[voice][sc]")
        parts.append(f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={plan.music_volume:.2f},"
                     f"afade=t=in:d=0.8,afade=t=out:st={max(0.0, total - 2):.3f}:d=2[mraw]")
        parts.append("[mraw][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=350[music]")
        mix.append("[music]")
        n_in += 1
    else:
        parts.append(f"{voice}[voice]")
    by_name: dict[str, list[float]] = defaultdict(list)
    for name, t in sfx_events:
        if name in SFX_SYNTH and 0 <= t < total:
            by_name[name].append(t)
    for name, times in by_name.items():
        cmd += ["-i", str(sfx_file(name, workdir))]
        labels = [f"[x{n_in}_{j}]" for j in range(len(times))]
        split = f",asplit={len(times)}" if len(times) > 1 else ""
        parts.append(f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={SFX_VOLUME[name]}{split}{''.join(labels)}")
        for label, t in zip(labels, times):
            ms = int(max(0.0, t - SFX_LEAD.get(name, 0.0)) * 1000)
            out_label = f"[s{len(mix)}]"
            parts.append(f"{label}adelay={ms}|{ms}{out_label}")
            mix.append(out_label)
        n_in += 1
    master = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    if len(mix) == 1:
        parts.append(f"[voice]{master}[aout]")
    else:
        parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,{master}[aout]")

    def build(codec: list[str]) -> list[str]:
        return [*cmd, *filter_graph_args(";".join(parts), workdir / "final.filter.txt"),
                "-map", "[vout]", "-map", "[aout]", "-t", f"{total + 0.05:.3f}", *codec,
                "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(out.resolve())]

    run_encode(build, W, H, fps, final=True, cwd=workdir, grainy=plan.film_grain)
    return {"starts": starts, "transitions": used, "duration": total}


# --------------------------------------------------------------------- sfx

def sfx_file(name: str, cache: Path) -> Path:
    for ext in (".wav", ".mp3"):
        custom = ASSET_SFX / f"{name}{ext}"
        if custom.exists():
            return custom
    out = cache / f"sfx_{name}.wav"
    if not out.exists():
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", SFX_SYNTH[name], "-ar", "48000", "-ac", "2", str(out)])
    return out


# ------------------------------------------------- captions + motion graphics

def _ass_time(t: float) -> str:
    cs = int(round(max(t, 0.0) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> ASS '&HAABBGGRR'."""
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _is_light(hex_rgb: str) -> bool:
    h = hex_rgb.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b > 0.6


def clean_word(word: str) -> str:
    return word.lower().strip(".,!?;:\"'„“”()[]…«»")


CAPTION_STYLES = {
    # font, primary, outline, back, border style, outline (x font size), shadow
    "bold_yellow": ("Arial Black", "&H00FFFFFF", "&H00000000", "&H80000000", 1, 0.085, 2),
    "clean_white": ("Arial", "&H00FFFFFF", "&H50000000", "&H70000000", 1, 0.04, 1),
    "boxed": ("Arial", "&H00FFFFFF", "&H40000000", "&H80000000", 3, 0.14, 0),
}
HIGHLIGHT = {"bold_yellow": "&H0000E5FF", "clean_white": None, "boxed": "&H0000E5FF"}
POPUP_SECONDS = {"pop": 1.6, "slide": 2.2, "label": 2.4, "stamp": 1.5, "typewriter": 2.4}


def _emphasis_color(accent: str, caption_style: str) -> str:
    """Accent colour for key words; green when the accent would vanish next to the yellow highlight."""
    if HIGHLIGHT.get(caption_style):
        h = accent.lstrip("#")
        hue, sat, _ = colorsys.rgb_to_hsv(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
        if 0.09 < hue < 0.2 and sat > 0.3:
            return "#4ADE80"
    return accent


def caption_chunks(timed_words: list[tuple[str, float, float, int]], per_caption: int,
                   max_chars: int = 28):
    """Group words into the short phrases shown on screen together."""
    chunks: list[list[tuple[str, float, float, int]]] = []
    for w in timed_words:
        cur = chunks[-1] if chunks else None
        if (cur is None or len(cur) >= per_caption
                or w[3] != cur[-1][3]  # never run a caption across a cut
                or w[1] - cur[-1][2] > 0.6
                or cur[-1][0][-1:] in ".!?,"
                or sum(len(x[0]) + 1 for x in cur) + len(w[0]) > max_chars):
            chunks.append([])
        chunks[-1].append(w)
    return chunks


def write_ass(path: Path, frame: Frame, plan: EditPlan,
              timed_words: list[tuple[str, float, float, int]],
              popups: list[tuple[str, float, str]],
              lower_third: tuple[str, str, float, float] | None,
              duration: float) -> None:
    W, H = frame.width, frame.height
    vertical = frame.vertical
    font, primary, outline, back, border, ow, shadow = CAPTION_STYLES[plan.caption_style]
    size = (W * 0.072 if vertical else H * 0.062) * (0.92 if font == "Arial Black" else 1.0)
    bar = letterbox_bar(frame) if plan.letterbox else 0
    margin_v = max(int(H * (0.24 if vertical else 0.08)), bar + int(H * 0.025))
    top_y = max(int(H * (0.16 if vertical else 0.11)), bar + int(H * 0.04))
    accent = plan.accent_color
    accent_ass = _ass_color(accent)
    on_accent = "&H00141414" if _is_light(accent) else "&H00FFFFFF"
    pop = int(size * 1.3)
    style_fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
                 "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                 "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")

    def style(name, fnt, fs, prim, outl, bk, bord, out_w, shad, align, mv, sec="&H000000FF"):
        return (f"Style: {name},{fnt},{int(fs)},{prim},{sec},{outl},{bk},1,0,0,0,100,100,0,0,"
                f"{bord},{max(0, int(out_w))},{shad},{align},{int(W * 0.07)},{int(W * 0.07)},{int(mv)},1")

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]", style_fmt,
        style("Cap", font, size, primary, outline, back, border, size * ow, shadow, 2, margin_v),
        style("Pop", "Arial Black", pop, on_accent, accent_ass, accent_ass, 3, pop * 0.22, 0, 8, top_y),
        style("Stamp", "Arial Black", size * 1.6, "&H00FFFFFF", accent_ass, "&H90000000", 1,
              size * 0.16, 3, 5, 0),
        style("Slide", "Arial", size * 1.2, "&H00FFFFFF", "&H60000000", "&H80000000", 1,
              size * 0.05, 2, 8, top_y),
        style("Label", "Arial", size * 1.05, on_accent, accent_ass, "&H80000000", 3, size * 0.3, 0,
              8, top_y),
        style("Type", "Courier New", size * 1.1, "&H00FFFFFF", "&H00000000", "&H80000000", 1,
              size * 0.08, 1, 8, top_y, sec="&HFF000000"),
        style("LT", "Arial", size * 0.95, "&H00FFFFFF", "&H60000000", "&H80000000", 1, size * 0.05,
              2, 7, 0),
        style("LT2", "Arial", size * 0.68, "&H20FFFFFF", "&H60000000", "&H80000000", 1,
              size * 0.04, 1, 7, 0),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def event(layer, start, end, style_name, text):
        end = min(end, duration)
        if end - start >= 0.02:
            lines.append(f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{style_name},,"
                         f"0,0,0,,{text}")

    # Captions: the phrase stays up, the word being said lights up.
    hl = HIGHLIGHT[plan.caption_style]
    emph_color = _ass_color(_emphasis_color(accent, plan.caption_style))
    emph = {clean_word(w) for w in plan.emphasis_words}
    if plan.captions and timed_words:
        chunks = caption_chunks(timed_words, plan.words_per_caption, 18 if vertical else 30)
        for ci, chunk in enumerate(chunks):
            chunk_end = chunk[-1][2] + 0.15
            if ci + 1 < len(chunks):
                chunk_end = min(chunk_end + 0.3, chunks[ci + 1][0][1])
            states = range(len(chunk)) if hl else [None]
            for wi in states:
                ws = chunk[wi][1] if wi is not None else chunk[0][1]
                we = (chunk[wi + 1][1] if wi is not None and wi + 1 < len(chunk) else chunk_end)
                words = []
                for wj, (txt, _, _, _) in enumerate(chunk):
                    shown = _ass_escape(txt.upper() if plan.caption_uppercase else txt)
                    if wj == wi:
                        grow = "\\fscx112\\fscy112" if plan.caption_pop and wi > 0 else ""
                        reset = "\\fscx100\\fscy100" if grow else ""
                        words.append(f"{{\\c{hl}&{grow}}}{shown}{{\\c{primary}&{reset}}}")
                    elif clean_word(txt) in emph:
                        words.append(f"{{\\c{emph_color}&}}{shown}{{\\c{primary}&}}")
                    else:
                        words.append(shown)
                intro = ""
                if wi in (0, None):
                    intro = ("{\\fscx80\\fscy80\\t(0,100,\\fscx100\\fscy100)}" if plan.caption_pop
                             else "{\\fad(80,0)}")
                event(0, ws, we, "Cap", intro + " ".join(words))

    # Titles, numbers and key words that pop up near the top.
    for text, t0, pstyle in popups:
        upper = pstyle in ("pop", "stamp")
        txt = _ass_escape(text.upper() if upper else text)
        shrink = min(1.0, (12 if vertical else 18) / max(1, len(txt)))
        end = t0 + POPUP_SECONDS.get(pstyle, 1.8)
        if pstyle == "pop":
            fs = f"\\fs{int(pop * shrink)}" if shrink < 1 else ""
            event(2, t0, end, "Pop", "{\\fscx40\\fscy40\\t(0,140,\\fscx110\\fscy110)"
                  f"\\t(140,220,\\fscx100\\fscy100)\\fad(0,200){fs}}}{txt}")
        elif pstyle == "stamp":
            fs = f"\\fs{int(size * 1.6 * shrink)}" if shrink < 1 else ""
            event(2, t0, end, "Stamp", f"{{\\an5\\pos({W // 2},{int(H * 0.3)})\\frz-5{fs}"
                  "\\fscx260\\fscy260\\alpha&H80&\\t(0,110,\\fscx100\\fscy100\\alpha&H00&)"
                  "\\t(110,170,\\fscx106\\fscy106)\\t(170,240,\\fscx100\\fscy100)\\fad(0,220)}"
                  f"{txt}")
        elif pstyle == "slide":
            fs = f"\\fs{int(size * 1.2 * shrink)}" if shrink < 1 else ""
            x1, x0 = W // 2, W // 2 - int(W * 0.06)
            event(2, t0, end, "Slide", f"{{\\an8\\move({x0},{top_y},{x1},{top_y},0,280)"
                  f"\\fad(180,250){fs}}}{txt}")
        elif pstyle == "label":
            fs_px = size * 1.05 * shrink
            fs = f"\\fs{int(fs_px)}" if shrink < 1 else ""
            half = int(len(txt) * fs_px * 0.33 + size * 0.5)  # rough text width / 2 + box
            xa, xb = max(0, W // 2 - half), min(W, W // 2 + half)
            event(2, t0, end, "Label", f"{{\\an8\\pos({W // 2},{top_y})\\clip({xa},0,{xa},{H})"
                  f"\\t(0,320,\\clip({xa},0,{xb},{H}))\\fad(0,250){fs}}}{txt}")
        else:  # typewriter
            fs = f"\\fs{int(size * 1.1 * shrink)}" if shrink < 1 else ""
            per = min(8, max(4, int(90 / max(1, len(txt)))))  # centiseconds per letter
            letters = "".join(f"{{\\ko{per}}}{ch}" for ch in txt)
            event(2, t0, end + len(txt) * per / 100, "Type",
                  f"{{\\an8\\pos({W // 2},{top_y})\\fad(0,250){fs}}}{letters}")

    # Name tag: an accent bar grows, then name and role slide in next to it.
    if lower_third:
        title, sub, t0, t1 = lower_third
        x = int(W * 0.06)
        y = int(H * (0.62 if vertical else 0.7))
        fs1, fs2 = size * 0.95, size * 0.68
        bar_w = max(6, int(size * 0.12))
        bar_h = int(fs1 * 1.15 + (fs2 * 1.25 if sub else 0))
        tx = x + bar_w + int(size * 0.3)
        event(3, t0, t1, "LT", f"{{\\an7\\pos({x},{y})\\p1\\c{accent_ass}&\\bord0\\shad0\\fscy0"
              f"\\t(0,260,\\fscy100)\\fad(0,300)}}m 0 0 l {bar_w} 0 {bar_w} {bar_h} 0 {bar_h}{{\\p0}}")
        event(3, t0 + 0.12, t1, "LT", f"{{\\an7\\move({tx - int(W * 0.03)},{y},{tx},{y},0,300)"
              f"\\fad(200,300)}}{_ass_escape(title)}")
        if sub:
            sy = y + int(fs1 * 1.15)
            event(3, t0 + 0.25, t1, "LT2", f"{{\\an7\\move({tx - int(W * 0.03)},{sy},{tx},{sy},0,300)"
                  f"\\fad(200,300)}}{_ass_escape(sub)}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
