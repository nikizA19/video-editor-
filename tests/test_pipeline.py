"""End-to-end smoke test: synthetic 'talking' clip -> edited video.

Speech is faked with tone bursts separated by silence, and the transcript is
faked too, so the test needs only ffmpeg (no Whisper model download).
Run: python -m pytest -q
"""
import subprocess
from pathlib import Path

import pytest

from studio import analysis, pipeline
from studio.analysis import Word
from studio.media import probe

LINES = [
    "Hello everyone welcome back",
    "Today I show you editing",
    "Today I show you fast editing",  # retake of the previous line
    "First remove the pauses",
    "Then add captions and zooms",
]
TALK, GAP = 2.0, 1.5


def _make_clip(path: Path) -> None:
    parts = []
    for i in range(len(LINES)):
        parts.append(f"sine=f={300 + 40 * i}:d={TALK}")
        parts.append(f"anullsrc=r=44100:cl=mono:d={GAP}")
    # Build audio with explicit durations via atrim.
    filt = []
    for i, p in enumerate(parts):
        d = TALK if p.startswith("sine") else GAP
        filt.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                    f"atrim=0:{d},asetpts=PTS-STARTPTS[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(parts)))
                + f"concat=n={len(parts)}:v=0:a=1[aout]")
    total = len(LINES) * (TALK + GAP)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in parts:
        src = p if p.startswith("sine") else "anullsrc=r=44100:cl=mono"
        cmd += ["-f", "lavfi", "-i", src]
    cmd += ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
            "-filter_complex", ";".join(filt),
            "-map", f"{len(parts)}:v", "-map", "[aout]", "-t", str(total),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True)


def _fake_transcript(path, model_size="small", language=None):
    words = []
    for i, line in enumerate(LINES):
        start = i * (TALK + GAP)
        tokens = line.split()
        step = TALK / len(tokens)
        for k, t in enumerate(tokens):
            words.append(Word(t, start + k * step, start + (k + 0.8) * step))
    return words


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "footage").mkdir()
    _make_clip(tmp_path / "footage" / "talk.mp4")
    monkeypatch.setattr(analysis, "transcribe", _fake_transcript)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return tmp_path


def test_full_edit(project):
    logs = []
    r = pipeline.run_edit(project, "mahni pauzite, dobavi nadpisi. title: 3 TRIKA",
                          {"pauses": "normal"}, log=logs.append)
    out = Path(r["output"])
    assert out.exists()
    info = probe(out)
    assert info.has_audio and info.has_video
    # 5 x 3.5s raw; pauses + one retake removed -> roughly 4 x 2s
    assert r["raw_seconds"] == pytest.approx(17.5, abs=0.3)
    assert 6.5 < r["final_seconds"] < 10.5
    assert r["dropped_takes"] == 1
    assert r["captions"] is True
    assert r["zooms"] >= 1
    tl = r["timeline"]
    assert len(tl["clips"]) == r["cuts"] and tl["captions"] and tl["popups"][0]["text"] == "3 TRIKA"
    assert tl["clips"][-1]["end"] <= tl["duration"] + 0.1
    assert len(tl["removed_takes"]) == 1
    assert (project / "output" / "timeline.json").exists()
    ass = (project / "work" / "captions.ass").read_text("utf-8")
    assert "3 TRIKA" in ass and "welcome" in ass


def test_vertical_no_captions(project):
    r = pipeline.run_edit(project, "bez nadpisi, za tiktok", {}, log=lambda m: None)
    info = probe(Path(r["output"]))
    assert (info.width, info.height) == (1080, 1920)
    assert r["captions"] is False


def test_transitions_render(project, monkeypatch):
    # Force a transition on every cut to exercise the xfade path.
    from studio import planner

    real = planner.auto_plan

    def with_transitions(*a, **k):
        plan = real(*a, **k)
        for i, e in enumerate(plan.segments):
            if i:
                e.transition_in = ["fade", "whoosh_slide", "flash"][i % 3]
                e.sfx = "whoosh"
        return plan

    monkeypatch.setattr(planner, "auto_plan", with_transitions)
    r = pipeline.run_edit(project, "", {"pauses": "normal"}, log=lambda m: None)
    assert Path(r["output"]).exists()
    assert r["sound_effects"] >= 1
    assert r["timeline"]["transitions"] and r["timeline"]["sfx"]
