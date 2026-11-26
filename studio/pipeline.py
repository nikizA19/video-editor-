"""End-to-end edit: footage in, finished video out."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from . import analysis, planner, render
from .media import probe, require_ffmpeg, stream_duration

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def list_files(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in exts)


def run_edit(project: Path, instructions: str, options: dict,
             log: Callable[[str], None] = print,
             progress: Callable[[float], None] = lambda p: None) -> dict:
    require_ffmpeg()
    project = project.resolve()
    work = project / "work"
    if work.exists():
        shutil.rmtree(work)
    (work / "segments").mkdir(parents=True)

    footage = list_files(project / "footage", VIDEO_EXT)
    if not footage:
        raise ValueError("Няма качени видеа. Качи поне един клип.")
    references = list_files(project / "references", VIDEO_EXT | IMAGE_EXT)
    music_files = list_files(project / "music", AUDIO_EXT)
    music = music_files[0] if music_files else None

    # 1. Look at the footage -------------------------------------------------
    log(f"Чета {len(footage)} клипа…")
    infos = [probe(p) for p in footage]
    infos = [i for i in infos if i.has_video and i.duration > 0.3]
    if not infos:
        raise ValueError("Не намерих видео в качените файлове.")

    want_captions = options.get("captions", True)
    transcripts: list[list[analysis.Word]] = []
    if want_captions or planner.claude_available():
        size = options.get("whisper_model") or "small"
        for k, info in enumerate(infos):
            log(f"Слушам речта в {info.path.name} (Whisper {size})…")
            try:
                transcripts.append(analysis.transcribe(
                    info.path, size, options.get("language") or None) if info.has_audio else [])
            except Exception as exc:  # captions are nice-to-have, never fatal
                log(f"⚠️ Транскрипцията не стана ({exc}). Продължавам без надписи.")
                transcripts.append([])
            progress(0.1 + 0.25 * (k + 1) / len(infos))
        if not any(transcripts):
            log("⚠️ Не чух реч (или faster-whisper не е инсталиран) – без надписи.")

    pause_preset = planner.pause_preset_from(instructions, options.get("pauses", "normal"))
    log(f"Махам паузите (режим: {pause_preset})…")
    segments = analysis.build_segments(infos, transcripts, pause_preset)
    raw_total = sum(i.duration for i in infos)
    kept_total = sum(s.duration for s in segments)
    log(f"От {raw_total:.0f}s остават {kept_total:.0f}s реч в {len(segments)} парчета.")

    ref_info = []
    for ref in references:
        try:
            ref_info.append(analysis.analyze_reference(ref, work / "ref_frames"))
        except Exception as exc:
            log(f"⚠️ Не успях да прочета примера {ref.name}: {exc}")
    for r in ref_info:
        if r.get("avg_shot_seconds"):
            log(f"Пример {r['name']}: смяна на кадър средно на {r['avg_shot_seconds']}s.")
    progress(0.4)

    # 2. Decide the edit -----------------------------------------------------
    previous = None
    prev_file = project / "last_plan.json"
    if options.get("revise") and prev_file.exists():
        try:
            previous = planner.EditPlan.model_validate_json(prev_file.read_text("utf-8"))
        except Exception:
            previous = None
    retakes = analysis.find_retakes(segments)
    plan = None
    used_claude = False
    if options.get("use_claude", True) and planner.claude_available():
        log("Claude планира монтажа…")
        try:
            plan = planner.claude_plan(segments, instructions, options, ref_info, previous)
            used_claude = True
        except Exception as exc:
            log(f"⚠️ Claude не отговори ({exc}). Ползвам авто режим.")
    if plan is None:
        plan = planner.auto_plan(segments, instructions, options, retakes, ref_info)
    if options.get("output_format") and options["output_format"] != "original":
        plan.output_format = options["output_format"]
    prev_file.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    log(plan.summary)

    # 3. Render --------------------------------------------------------------
    frame = render.output_frame(infos[0], plan.output_format)
    edits = {e.index: e for e in plan.segments}
    kept = [s for s in segments if edits[s.index].keep] or segments
    files, durations, transitions = [], [], []
    for k, seg in enumerate(kept):
        e = edits[seg.index]
        out = work / "segments" / f"seg_{k:04d}.mp4"
        durations.append(render.render_segment(seg, infos[seg.source], e.zoom, frame, out))
        files.append(out)
        transitions.append("cut" if k == 0 else e.transition_in)
        if k % 5 == 0:
            log(f"Режа кадри {k + 1}/{len(kept)}…")
        progress(0.4 + 0.4 * (k + 1) / len(kept))

    log("Сглобявам кадрите и преходите…")
    joined = work / "joined.mp4"
    starts = render.join_segments(files, durations, transitions, joined)
    total = stream_duration(joined)

    # Map transcript words onto the new timeline.
    timed_words = []
    for k, seg in enumerate(kept):
        for w in seg.words:
            ws = starts[k] + max(0.0, w.start - seg.start)
            we = starts[k] + min(seg.duration, w.end - seg.start)
            timed_words.append((w.text, ws, max(we, ws + 0.05), k))

    sfx_events = []
    last_sfx = -99.0
    for k, seg in enumerate(kept):
        name = edits[seg.index].sfx
        if name != "none" and starts[k] - last_sfx > 3:
            sfx_events.append((name, starts[k]))
            last_sfx = starts[k]
    popups = []
    kept_pos = {s.index: k for k, s in enumerate(kept)}
    for p in plan.text_popups:
        if p.segment_index in kept_pos:
            t = starts[kept_pos[p.segment_index]] + 0.15
            popups.append((p.text, t))
            if all(abs(t - st) > 1 for _, st in sfx_events):
                sfx_events.append(("pop", t))

    ass = None
    if (plan.captions and timed_words) or popups:
        ass = work / "captions.ass"
        render.write_ass(ass, frame, plan, timed_words if plan.captions else [], popups)

    log("Финален микс: цветове, надписи, звук…")
    progress(0.9)
    out_dir = project / "output"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "edited.mp4"
    render.final_mix(joined, out, work, plan, ass, sfx_events, music, total)
    progress(1.0)

    result = {
        "output": str(out),
        "summary": plan.summary,
        "raw_seconds": round(raw_total, 1),
        "final_seconds": round(stream_duration(out), 1),
        "cuts": len(kept),
        "dropped_takes": sum(1 for e in plan.segments if not e.keep),
        "zooms": sum(1 for s in kept if edits[s.index].zoom != "none"),
        "sound_effects": len(sfx_events),
        "captions": bool(ass and plan.captions and timed_words),
        "planner": "claude" if used_claude else "auto",
        "plan": json.loads(plan.model_dump_json()),
    }
    log(f"Готово! {result['raw_seconds']}s → {result['final_seconds']}s, {result['cuts']} кадъра.")
    return result
