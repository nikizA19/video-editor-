"""Self-updater: download the latest version from GitHub (no git or GitHub account needed).

Runs from start.bat / start.sh. Keeps your projects (workspace/), your key (.env),
your own sound effects and the installed packages (.venv/). Any failure is harmless:
the studio just starts with the version you already have.
"""
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "nikizA19/video-editor-"
BRANCH = "main"
HERE = Path(__file__).resolve().parent
VERSION_FILE = HERE / ".version"
KEEP = {"workspace", ".env", ".venv", ".version", ".git"}
KEEP_UNDER = ("studio/assets/sfx/",)


def latest_sha() -> str:
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "ai-video-studio-updater"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["sha"]


def main() -> int:
    if (HERE / ".git").exists():
        return 0  # git checkouts update with git pull instead
    try:
        sha = latest_sha()
    except Exception as exc:
        print(f"(Не успях да проверя за нова версия: {exc})")
        return 0
    current = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""
    if sha == current:
        print("Имаш най-новата версия.")
        return 0
    print("Има нова версия, обновявам…")
    try:
        url = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for item in zf.infolist():
                parts = item.filename.split("/", 1)
                if len(parts) < 2 or not parts[1] or item.is_dir():
                    continue
                rel = parts[1]
                top = rel.split("/", 1)[0]
                if top in KEEP or (rel.startswith(KEEP_UNDER) and (HERE / rel).exists()):
                    continue
                dest = (HERE / rel).resolve()
                if HERE not in dest.parents:
                    continue  # never write outside the app folder
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        VERSION_FILE.write_text(sha)
        print("Обновено ✔")
    except Exception as exc:
        print(f"(Обновяването не стана: {exc}. Пускам текущата версия.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
