#!/usr/bin/env bash
# Mac / Linux: run ./start.sh
# Everything is inside main() so bash reads the whole file before running it:
# the updater may replace this file while it runs.
main() {
  cd "$(dirname "$0")" || exit 1
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg липсва. Mac: brew install ffmpeg   |   Linux: sudo apt install ffmpeg"
    exit 1
  fi
  # The speech-recognition packages ship ready-made builds only for Python 3.10-3.13,
  # so pick one of those (Homebrew's default python3 can be newer).
  PY=""
  for v in 3.12 3.13 3.11 3.10; do
    if command -v "python$v" >/dev/null 2>&1; then PY="python$v"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Трябва ти Python 3.12. Пусни:  brew install python@3.12   и после пак ./start.sh"
    exit 1
  fi
  if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(not (3,10) <= sys.version_info[:2] <= (3,13))'; then
    echo "Пресъздавам средата с $PY…"
    rm -rf .venv
  fi
  if [ ! -d .venv ]; then
    echo "Първо пускане: инсталирам нужното (отнема 1-2 минути)…"
    "$PY" -m venv .venv || exit 1
    .venv/bin/pip install -q --upgrade pip
  fi
  if [ -d .git ] && command -v git >/dev/null 2>&1; then
    git pull --ff-only -q || echo "(Не успях да обновя, пускам текущата версия.)"
  else
    .venv/bin/python update.py
  fi
  .venv/bin/pip install -q -r requirements.txt || exit 1
  if [ -f .env ]; then set -a; . ./.env; set +a; fi
  exec .venv/bin/python -m studio
}
main "$@"
exit
