#!/usr/bin/env bash
# Mac / Linux: run  bash start.sh
# Everything is inside main() so bash reads the whole file before running it:
# the updater may replace this file while it runs.
main() {
  cd "$(dirname "$0")" || exit 1
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg липсва. Mac: brew install ffmpeg   |   Linux: sudo apt install ffmpeg"
    exit 1
  fi
  if [ ! -d .venv ]; then
    echo "Първо пускане: инсталирам нужното (отнема 1-2 минути)…"
    python3 -m venv .venv || exit 1
    .venv/bin/pip install -q --upgrade pip
  fi
  if [ -d .git ] && command -v git >/dev/null 2>&1; then
    git pull --ff-only -q || echo "(Не успях да обновя, пускам текущата версия.)"
  else
    .venv/bin/python update.py
  fi
  .venv/bin/pip install -q -r requirements.txt || exit 1

  # Speech recognition for captions: faster-whisper if this Python supports it,
  # otherwise the whisper.cpp tool, which works with any Python.
  if ! .venv/bin/python -c "import faster_whisper" 2>/dev/null \
     && ! command -v whisper-cli >/dev/null 2>&1; then
    if ! .venv/bin/pip install -q -r requirements-whisper.txt 2>/dev/null; then
      if command -v brew >/dev/null 2>&1; then
        echo "Инсталирам whisper.cpp за надписите (еднократно)…"
        brew install whisper-cpp || echo "(Без надписи, докато не се инсталира whisper-cpp.)"
      else
        echo "(Без надписи: инсталирай whisper.cpp, напр. brew install whisper-cpp.)"
      fi
    fi
  fi

  if [ -f .env ]; then set -a; . ./.env; set +a; fi
  exec .venv/bin/python -m studio
}
main "$@"
exit
