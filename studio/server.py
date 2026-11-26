"""The studio web app. Start with:  python -m studio  (then open http://localhost:8000)"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import analysis, media, pipeline, planner, render, styles

ROOT = Path(os.environ.get("STUDIO_WORKSPACE", Path.cwd() / "workspace")).resolve()
STATIC = Path(__file__).parent / "static"
ALLOWED = {
    "footage": pipeline.VIDEO_EXT,
    "references": pipeline.VIDEO_EXT | pipeline.IMAGE_EXT,
    "broll": pipeline.VIDEO_EXT | pipeline.IMAGE_EXT,
    "music": pipeline.AUDIO_EXT | {".mp4", ".mov", ".m4v"},  # a song saved as a video works too
}
KINDS = set(ALLOWED)
KEEP_JOBS = 20

app = FastAPI(title="AI Video Studio")
jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _project(pid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{8,32}", pid):
        raise HTTPException(404, "Няма такъв проект")
    p = ROOT / pid
    if not p.is_dir():
        raise HTTPException(404, "Няма такъв проект")
    return p


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(400, "Непознат тип файл")
    return kind


def _safe_name(name: str) -> str:
    name = Path((name or "file").replace("\\", "/")).name
    stem = re.sub(r"[^\w.\- ]+", "_", Path(name).stem).strip(" .")[:80] or "file"
    return f"{stem}{Path(name).suffix.lower()}"


def _running_job(pid: str) -> dict | None:
    return next((j for j in jobs.values() if j["project"] == pid and j["state"] == "running"), None)


def _public(job: dict) -> dict:
    """A snapshot that is safe to serialise while the worker thread keeps writing."""
    return {"id": job["id"], "project": job["project"], "state": job["state"],
            "log": list(job["log"]), "progress": job["progress"], "result": job["result"],
            "error": job["error"], "started": job["started"],
            "elapsed": round((job.get("finished") or time.time()) - job["started"], 1)}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text("utf-8")


@app.get("/api/status")
def status():
    ffmpeg = media.ffmpeg_available()
    return {
        "claude": planner.claude_available(),
        "ffmpeg": ffmpeg,
        "text": ffmpeg and media.can_draw_text(),
        "speech": analysis.speech_engine(),
        "workers": render.workers(),
        "styles": styles.catalog(),
        "default_style": styles.DEFAULT_STYLE,
    }


@app.post("/api/projects")
def create_project():
    pid = uuid.uuid4().hex[:12]
    for k in KINDS:
        (ROOT / pid / k).mkdir(parents=True, exist_ok=True)
    return {"id": pid}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = _project(pid)
    files = {k: sorted(f.name for f in (p / k).iterdir() if f.is_file() and not f.name.startswith("."))
             if (p / k).is_dir() else [] for k in KINDS}
    out = p / "output" / "edited.mp4"
    tl = p / "output" / "timeline.json"
    timeline = None
    if out.exists() and tl.exists():
        try:
            timeline = json.loads(tl.read_text("utf-8"))
        except ValueError:
            timeline = None
    instructions = ""
    req = p / "last_request.json"
    if req.exists():
        try:
            instructions = json.loads(req.read_text("utf-8")).get("instructions", "")
        except ValueError:
            pass
    running = _running_job(pid)
    return {"id": pid, "files": files, "has_output": out.exists(), "timeline": timeline,
            "last_instructions": instructions, "job": running["id"] if running else None}


@app.post("/api/projects/{pid}/upload/{kind}")
async def upload(pid: str, kind: str, files: list[UploadFile] = File(...)):
    p = _project(pid)
    folder = p / _kind(kind)
    folder.mkdir(exist_ok=True)
    saved, skipped = [], []
    for f in files:
        name = _safe_name(f.filename)
        if Path(name).suffix.lower() not in ALLOWED[kind]:
            skipped.append(f.filename or name)
            continue
        dest = folder / name
        part = folder / f".{name}.part"  # hidden until complete, so a half upload is never edited
        try:
            with part.open("wb") as fh:
                while chunk := await f.read(1024 * 1024):
                    fh.write(chunk)
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        saved.append(dest.name)
    if kind == "music" and saved:  # only one music track
        for other in folder.iterdir():
            if other.is_file() and other.name != saved[-1]:
                other.unlink(missing_ok=True)
        saved = saved[-1:]
    if skipped and not saved:
        raise HTTPException(400, "Този вид файл не става тук: " + ", ".join(skipped))
    return {"saved": saved, "skipped": skipped}


@app.delete("/api/projects/{pid}/files/{kind}/{name}")
def delete_file(pid: str, kind: str, name: str):
    p = _project(pid)
    f = p / _kind(kind) / _safe_name(name)
    f.unlink(missing_ok=True)
    return {"ok": True}


class EditRequest(BaseModel):
    instructions: str = ""
    options: dict = {}


def _prune_jobs() -> None:
    done = sorted((j for j in jobs.values() if j["state"] != "running"), key=lambda j: j["started"])
    for j in done[:-KEEP_JOBS]:
        jobs.pop(j["id"], None)


@app.post("/api/projects/{pid}/edit")
def start_edit(pid: str, req: EditRequest):
    p = _project(pid)
    with _lock:
        if _running_job(pid):
            raise HTTPException(409, "Вече се монтира. Изчакай да свърши.")
        _prune_jobs()
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "project": pid, "state": "running", "log": [], "progress": 0.0,
               "result": None, "error": None, "started": time.time(), "finished": None,
               "cancel": False}
        jobs[jid] = job

    def progress(x: float) -> None:
        if job["cancel"]:
            raise pipeline.Cancelled()
        job["progress"] = x

    def work():
        try:
            job["result"] = pipeline.run_edit(p, req.instructions, req.options,
                                              log=job["log"].append, progress=progress)
            job["state"] = "done"
        except pipeline.Cancelled:
            job["log"].append("⏹ Спрях монтажа. Старото видео (ако има) си стои.")
            job["state"] = "cancelled"
        except Exception as exc:
            traceback.print_exc()
            job["error"] = str(exc) or exc.__class__.__name__
            job["state"] = "error"
        finally:
            job["finished"] = time.time()

    threading.Thread(target=work, daemon=True, name=f"edit-{jid}").start()
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Няма такава задача")
    return _public(job)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Няма такава задача")
    if job["state"] == "running":
        job["cancel"] = True
        job["log"].append("Спирам… (довършвам текущата стъпка)")
    return {"ok": True}


@app.get("/api/projects/{pid}/output")
def output(pid: str, download: bool = False):
    out = _project(pid) / "output" / "edited.mp4"
    if not out.exists():
        raise HTTPException(404, "Още няма готово видео")
    return FileResponse(out, media_type="video/mp4",
                        filename="edited.mp4" if download else None,
                        headers={"Cache-Control": "no-cache"})


def _free_port(start: int) -> int:
    """8000, or the next free port if another program (or an old studio) already uses it."""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main():
    import uvicorn
    import webbrowser

    ROOT.mkdir(parents=True, exist_ok=True)
    media.ensure_full_ffmpeg()
    if not media.ffmpeg_available():
        print("\n  ⚠️ ffmpeg липсва. Mac: brew install ffmpeg-full   |   Windows: winget install Gyan.FFmpeg\n")
    elif not media.can_draw_text():
        print("\n  ⚠️ Този ffmpeg не може да рисува надписи. Mac: brew install ffmpeg-full\n")
    wanted = int(os.environ.get("PORT", 8000))
    port = _free_port(wanted)
    if port != wanted:
        print(f"  (Порт {wanted} е зает, ползвам {port}.)")
    print(f"\n  AI Video Studio → http://localhost:{port}\n  Проекти: {ROOT}\n"
          "  За да спреш студиото: Ctrl+C в този прозорец.\n")
    if os.environ.get("STUDIO_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
