"""End-to-end edit: footage in, finished video out."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from . import analysis, media, planner, render
from .media import probe, require_ffmpeg, stream_duration
from .styles import POWER_WORDS

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
IMAGE_EXT = analysis.IMAGE_EXT
PAUSE_NAMES = {"off": "не махам", "gentle": "меко", "normal": "нормално", "tight": "агресивно"}


class Cancelled(Exception):
    """Raised from the progress callback when the user presses Stop."""


def list_files(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)


def _popup_time(text: str, seg: analysis.Segment, start: float) -> float:
    """Show a popup when its word is said, not just when the shot starts."""
    wanted = {render.clean_word(w) for w in text.split()} - {""}
    numbers = {k for k, v in planner.NUMBER_WORDS.items() if v in wanted}
    for w in seg.words:
        c = render.clean_word(w.text)
        if c and (c in wanted or c in numbers):
            return start + max(0.0, w.start - seg.start - 0.05)
    return start + 0.15


def run_edit(project: Path, instructions: str, options: dict,
             log: Callable[[str], None] = print,
             progress: Callable[[float], None] = lambda p: None) -> dict:
    require_ffmpeg()
    project = project.resolve()
    options = dict(options or {})
    work = project / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    (work / "segments").mkdir(parents=True, exist_ok=True)

    footage = list_files(project / "footage", VIDEO_EXT)
    if not footage:
        raise ValueError("Няма качени видеа. Качи поне един клип.")
    references = list_files(project / "references", VIDEO_EXT | IMAGE_EXT)
    broll_files = list_files(project / "broll", VIDEO_EXT | IMAGE_EXT)
    music_files = list_files(project / "music", AUDIO_EXT)
    music = music_files[0] if music_files else None

    # "Поправи последния монтаж": the new text is a correction on top of the last request.
    request_file = project / "last_request.json"
    text = instructions or ""
    if options.get("revise") and request_file.exists():
        try:
            earlier = json.loads(request_file.read_text("utf-8")).get("instructions", "")
        except (OSError, ValueError):
            earlier = ""
        if earlier and not text.strip().startswith(earlier.strip()):
            text = f"{earlier}\n{text}".strip()
    style = planner.resolve_style(text, options)
    log(f"Стил: {style.emoji} {style.name}")

    # 1. Look at the footage -------------------------------------------------
    log(f"Чета {len(footage)} клипа…")
    infos = []
    for p in footage:
        try:
            info = probe(p)
        except media.FFmpegError as exc:
            log(f"⚠️ Пропускам {p.name}: {exc}")
            continue
        if info.has_video and info.duration > 0.3:
            infos.append(info)
        else:
            log(f"⚠️ Пропускам {p.name}: в него няма видео.")
    if not infos:
        raise ValueError("Не намерих видео в качените файлове.")
    if any(i.hdr for i in infos):
        log("Видеото е HDR (iPhone) – превръщам цветовете в нормални, за да не излезе сиво.")

    profiles: list[analysis.AudioProfile | None] = []
    for info in infos:
        try:
            profiles.append(analysis.audio_profile(info.path) if info.has_audio else None)
        except Exception as exc:  # pauses then just stay in, never fatal
            log(f"⚠️ Не успях да измеря звука в {info.path.name}: {exc}")
            profiles.append(None)
    progress(0.05)

    size = options.get("whisper_model") or "small"
    transcripts: list[list[analysis.Word]] = []
    for k, info in enumerate(infos):
        words: list[analysis.Word] = []
        if info.has_audio:
            log(f"Слушам какво казваш в {info.path.name}…")
            try:
                words = analysis.transcribe(info.path, size, options.get("language") or None, log=log)
            except Exception as exc:  # captions and retakes are nice-to-have, never fatal
                log(f"⚠️ Разпознаването на реч не стана ({exc}). Продължавам без него.")
        transcripts.append(words)
        progress(0.05 + 0.3 * (k + 1) / len(infos))
    if not any(transcripts):
        hint = (" Инсталирай whisper-cpp (brew install whisper-cpp) и пусни студиото пак."
                if analysis.speech_engine() is None else "")
        log("⚠️ Не разпознах реч, затова няма надписи и не махам повторените дубли." + hint)

    pause_preset = planner.pause_preset_from(text, options.get("pauses"), style)
    log(f"Махам паузите ({PAUSE_NAMES.get(pause_preset, pause_preset)})…")
    segments = analysis.build_segments(infos, transcripts, pause_preset, profiles)
    has_words = any(s.words for s in segments)
    retakes = analysis.find_retakes(segments)
    junk = analysis.find_junk(segments, has_words) - retakes
    drop = retakes | junk
    analysis.score_emphasis(segments, POWER_WORDS)
    raw_total = sum(i.duration for i in infos)
    kept_total = sum(s.duration for s in segments if s.index not in drop)
    extra = []
    if retakes:
        extra.append(f"{len(retakes)} повторени дубли")
    if junk:
        extra.append(f"{len(junk)} кашляния/„ъъъ“")
    log(f"От {raw_total:.0f}s остават {kept_total:.0f}s в {len(segments) - len(drop)} кадъра"
        + (f" (махам {', '.join(extra)})" if extra else "") + ".")

    ref_info = []
    for ref in references:
        try:
            ref_info.append(analysis.analyze_reference(ref, work / "ref_frames"))
        except Exception as exc:
            log(f"⚠️ Не успях да прочета примера {ref.name}: {exc}")
    for r in ref_info:
        if r.get("avg_shot_seconds"):
            log(f"Пример {r['name']}: смяна на кадър средно на {r['avg_shot_seconds']}s.")
    accent = analysis.accent_color([f for r in ref_info for f in r.get("frames", [])]) if ref_info else None
    if accent:
        log(f"Взимам акцентния цвят от примера: {accent}.")
    brolls = analysis.broll_info(broll_files)
    if broll_files:
        log(f"B-roll: {len(brolls)} файла за вмъкване.")
    progress(0.4)

    # 2. Decide the edit -----------------------------------------------------
    previous = None
    prev_file = project / "last_plan.json"
    if options.get("revise") and prev_file.exists():
        try:
            previous = planner.EditPlan.model_validate_json(prev_file.read_text("utf-8"))
        except Exception:
            previous = None
    draft = planner.auto_plan(segments, text, options, drop, ref_info, brolls, accent)
    plan, used_claude = draft, False
    if options.get("use_claude", True) and planner.claude_available():
        log("Claude планира монтажа…")
        try:
            plan = planner.claude_plan(segments, text, options, ref_info, previous, draft, brolls)
            used_claude = True
        except Exception as exc:
            log(f"⚠️ Claude не отговори ({exc}). Ползвам авто режима.")
    if options.get("output_format") and options["output_format"] != "original":
        plan.output_format = options["output_format"]
        if plan.output_format in ("vertical_9_16", "square_1_1"):
            plan.letterbox = False
    log(plan.summary)

    can_text = media.can_draw_text()
    if not can_text and (plan.captions or plan.text_popups or plan.lower_third_title):
        log("⚠️ Този ffmpeg не може да рисува текст (няма libass), затова видеото е без надписи. "
            "На Mac пусни в Терминал: brew install ffmpeg-full (студиото ще го ползва само).")

    # 3. Render --------------------------------------------------------------
    frame = render.output_frame(infos[0], plan.output_format)
    edits = {e.index: e for e in plan.segments}
    kept = [s for s in segments if edits[s.index].keep] or segments
    by_name = {b["name"]: b for b in brolls}
    jobs = []
    for k, seg in enumerate(kept):
        e = edits[seg.index]
        b = by_name.get(e.broll) if e.broll and seg.duration >= 1.0 else None
        jobs.append(render.SegmentJob(seg, infos[seg.source], e,
                                      work / "segments" / f"seg_{k:04d}.mov", b, plan.color_grade))
    log(f"Режа и анимирам {len(jobs)} кадъра ({render.workers()} наведнъж)…")

    def done(n: int) -> None:
        progress(0.4 + 0.47 * n / len(jobs))
        if n % 10 == 0 or n == len(jobs):
            log(f"Готови кадри: {n}/{len(jobs)}")

    durations = render.render_segments(jobs, frame, done)
    transitions = ["cut"] + [edits[s.index].transition_in for s in kept[1:]]
    _, used_trans, starts, total = render.plan_join(durations, transitions)

    # Map transcript words onto the new timeline.
    timed_words = []
    for k, seg in enumerate(kept):
        for w in seg.words:
            ws = starts[k] + max(0.0, w.start - seg.start)
            we = starts[k] + min(durations[k], w.end - seg.start)
            if ws < starts[k] + durations[k] - 0.02:
                timed_words.append((w.text, ws, max(we, ws + 0.05), k))

    sfx_events: list[tuple[str, float]] = []
    for k, seg in enumerate(kept):
        name = edits[seg.index].sfx
        if name != "none":
            t = starts[k]
            if used_trans[k] != "cut":
                t += render.TRANSITIONS[used_trans[k]][1] / 2  # the middle of the transition
            sfx_events.append((name, t))
    popups = []
    kept_pos = {s.index: k for k, s in enumerate(kept)}
    for p in plan.text_popups:
        k = kept_pos.get(p.segment_index)
        if k is None:
            continue
        t = _popup_time(p.text, kept[k], starts[k])
        popups.append((p.text.strip()[:60], t, p.style))
        if p.sfx != "none":
            sfx_events.append((p.sfx, t))
    sfx_events.sort(key=lambda e: e[1])
    deduped: list[tuple[str, float]] = []
    for name, t in sfx_events:
        if not deduped or t - deduped[-1][1] >= 0.3:
            deduped.append((name, t))
    sfx_events = deduped

    lower = None
    if plan.lower_third_title and total > 2:
        t0 = min(1.0, total * 0.1)
        lower = (plan.lower_third_title, plan.lower_third_subtitle, t0, min(total, t0 + 4.5))

    ass = None
    show_captions = plan.captions and bool(timed_words) and can_text
    if can_text and (show_captions or popups or lower):
        ass = work / "captions.ass"
        render.write_ass(ass, frame, plan, timed_words if show_captions else [], popups, lower, total)

    log("Финален пас: преходи, цветове, надписи, музика и звук…")
    progress(0.9)
    out_dir = project / "output"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "edited.mp4"
    tmp = out_dir / "edited.part.mp4"
    files = [j.out for j in jobs]
    render.assemble(files, durations, transitions, tmp, work, plan, frame, ass, sfx_events, music)
    tmp.replace(out)  # the old video stays watchable until the new one is ready
    shutil.rmtree(work / "segments", ignore_errors=True)  # big near-lossless files
    progress(1.0)

    final_seconds = stream_duration(out) or total
    reasons = {i: "retake" for i in retakes} | {i: "junk" for i in junk}
    removed = [{"text": s.text or f"{s.start:.1f}s–{s.end:.1f}s",
                "reason": reasons.get(s.index, "plan")}
               for s in segments if not edits[s.index].keep]
    chunks = (render.caption_chunks(timed_words, plan.words_per_caption,
                                    18 if frame.vertical else 30) if ass and show_captions else [])
    timeline = {
        "duration": round(final_seconds, 2),
        "style": style.key,
        "style_name": f"{style.emoji} {style.name}",
        "clips": [
            {"start": round(starts[k], 2), "end": round(starts[k] + durations[k], 2),
             "camera": edits[seg.index].camera, "effect": edits[seg.index].effect,
             "broll": jobs[k].broll["name"] if jobs[k].broll else None,
             "broll_end": (round(starts[k] + render.broll_seconds(durations[k]), 2)
                           if jobs[k].broll else None),
             "text": seg.text, "source": infos[seg.source].path.name,
             "source_start": round(seg.start, 2), "source_end": round(seg.end, 2),
             "pause_removed": seg.pause_before}
            for k, seg in enumerate(kept)
        ],
        "transitions": [{"t": round(starts[k], 2), "kind": used_trans[k]}
                        for k in range(1, len(kept)) if used_trans[k] != "cut"],
        "sfx": [{"t": round(t, 2), "name": n} for n, t in sfx_events],
        "popups": [{"start": round(t, 2), "end": round(t + render.POPUP_SECONDS.get(st, 1.8), 2),
                    "text": txt, "style": st} for txt, t, st in popups] if ass else [],
        "lower_third": ({"start": round(lower[2], 2), "end": round(lower[3], 2),
                         "title": lower[0], "subtitle": lower[1]} if lower and ass else None),
        "captions": [{"start": round(c[0][1], 2), "end": round(c[-1][2] + 0.15, 2),
                      "text": " ".join(w[0] for w in c)} for c in chunks],
        "removed_takes": [r["text"] for r in removed],
        "removed": removed,
        "color_grade": plan.color_grade,
        "grain": plan.film_grain,
        "letterbox": bool(plan.letterbox and render.letterbox_bar(frame)),
        "progress_bar": plan.progress_bar,
        "accent": plan.accent_color,
        "music": music.name if music and plan.music_volume > 0 else None,
    }
    (out_dir / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), "utf-8")
    prev_file.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    request_file.write_text(json.dumps({"instructions": text}, ensure_ascii=False), "utf-8")

    kept_edits = [edits[s.index] for s in kept]
    camera_moves = sum(1 for e in kept_edits if e.camera != "none")
    result = {
        "output": str(out),
        "timeline": timeline,
        "summary": plan.summary,
        "style": style.key,
        "raw_seconds": round(raw_total, 1),
        "final_seconds": round(final_seconds, 1),
        "cuts": len(kept),
        "dropped_takes": sum(1 for e in plan.segments if not e.keep),
        "zooms": camera_moves,
        "camera_moves": camera_moves,
        "effects": sum(1 for e in kept_edits if e.effect != "none") + len(timeline["transitions"]),
        "broll": sum(1 for j in jobs if j.broll),
        "sound_effects": len(sfx_events),
        "graphics": len(timeline["popups"]) + (1 if timeline["lower_third"] else 0),
        "captions": bool(chunks),
        "planner": "claude" if used_claude else "auto",
        "plan": json.loads(plan.model_dump_json()),
    }
    log(f"Готово! {result['raw_seconds']}s → {result['final_seconds']}s, {result['cuts']} кадъра.")
    return result
