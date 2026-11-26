"""The studio web app. Start with:  python -m studio  (then open http://localhost:8000)"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import pipeline, planner

ROOT = Path(os.environ.get("STUDIO_WORKSPACE", Path.cwd() / "workspace")).resolve()
STATIC = Path(__file__).parent / "static"
KINDS = {"footage", "references", "music"}

app = FastAPI(title="AI Video Studio")
jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _project(pid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{8,32}", pid):
        raise HTTPException(404, "Няма такъв проект")
    p = ROOT / pid
    if not p.exists():
        raise HTTPException(404, "Няма такъв проект")
    return p


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    stem = re.sub(r"[^\w.\- ]+", "_", Path(name).stem)[:80] or "file"
    return f"{stem}{Path(name).suffix.lower()}"


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text("utf-8")


@app.get("/api/status")
def status():
    return {"claude": planner.claude_available(), "ffmpeg": shutil.which("ffmpeg") is not None}


@app.post("/api/projects")
def create_project():
    pid = uuid.uuid4().hex[:12]
    for k in KINDS:
        (ROOT / pid / k).mkdir(parents=True, exist_ok=True)
    return {"id": pid}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = _project(pid)
    files = {k: sorted(f.name for f in (p / k).iterdir() if f.is_file()) if (p / k).exists() else []
             for k in KINDS}
    out = p / "output" / "edited.mp4"
    tl = p / "output" / "timeline.json"
    timeline = None
    if out.exists() and tl.exists():
        try:
            timeline = json.loads(tl.read_text("utf-8"))
        except ValueError:
            timeline = None
    return {"id": pid, "files": files, "has_output": out.exists(), "timeline": timeline}


@app.post("/api/projects/{pid}/upload/{kind}")
async def upload(pid: str, kind: str, files: list[UploadFile] = File(...)):
    p = _project(pid)
    if kind not in KINDS:
        raise HTTPException(400, "Непознат тип файл")
    saved = []
    for f in files:
        dest = p / kind / _safe_name(f.filename)
        with dest.open("wb") as fh:
            while chunk := await f.read(1024 * 1024):
                fh.write(chunk)
        saved.append(dest.name)
    if kind == "music":  # only one music track
        for other in (p / kind).iterdir():
            if other.name not in saved:
                other.unlink()
    return {"saved": saved}


@app.delete("/api/projects/{pid}/files/{kind}/{name}")
def delete_file(pid: str, kind: str, name: str):
    p = _project(pid)
    if kind not in KINDS:
        raise HTTPException(400, "Непознат тип файл")
    f = p / kind / _safe_name(name)
    if f.exists():
        f.unlink()
    return {"ok": True}


class EditRequest(BaseModel):
    instructions: str = ""
    options: dict = {}


@app.post("/api/projects/{pid}/edit")
def start_edit(pid: str, req: EditRequest):
    p = _project(pid)
    with _lock:
        if any(j["project"] == pid and j["state"] == "running" for j in jobs.values()):
            raise HTTPException(409, "Вече се монтира. Изчакай да свърши.")
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "project": pid, "state": "running", "log": [], "progress": 0.0,
               "result": None, "error": None, "started": time.time()}
        jobs[jid] = job

    def work():
        try:
            job["result"] = pipeline.run_edit(
                p, req.instructions, req.options,
                log=lambda m: job["log"].append(m),
                progress=lambda x: job.__setitem__("progress", x))
            job["state"] = "done"
        except Exception as exc:
            traceback.print_exc()
            job["error"] = str(exc)
            job["state"] = "error"

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Няма такава задача")
    return job


@app.get("/api/projects/{pid}/output")
def output(pid: str, download: bool = False):
    out = _project(pid) / "output" / "edited.mp4"
    if not out.exists():
        raise HTTPException(404, "Още няма готово видео")
    return FileResponse(out, media_type="video/mp4",
                        filename="edited.mp4" if download else None)


def main():
    import uvicorn
    import webbrowser

    ROOT.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  AI Video Studio → http://localhost:{port}\n  Проекти: {ROOT}\n")
    if os.environ.get("STUDIO_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
