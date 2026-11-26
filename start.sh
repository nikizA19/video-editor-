#!/usr/bin/env bash
# Mac / Linux: double-click or run ./start.sh
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg липсва. Mac: brew install ffmpeg   |   Linux: sudo apt install ffmpeg"
  exit 1
fi
if [ ! -d .venv ]; then
  echo "Първо пускане: инсталирам нужното (отнема 1-2 минути)…"
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/pip install -q -r requirements.txt || exit 1
[ -f .env ] && set -a && . ./.env && set +a
.venv/bin/python -m studio
