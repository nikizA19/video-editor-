@echo off
chcp 65001 >nul
REM Windows: double-click this file.
REM Everything is inside one ( ) block so Windows reads the whole file before
REM running it: the updater may replace this file while it runs.
(
  cd /d "%~dp0"
  where ffmpeg >nul 2>nul || (
    echo ffmpeg липсва. Отвори PowerShell и пусни:  winget install Gyan.FFmpeg
    echo После затвори и отвори този прозорец отново.
    pause
    exit /b 1
  )
  if not exist .venv (
    echo Първо пускане: инсталирам нужното, отнема 1-2 минути...
    py -3 -m venv .venv 2>nul || python -m venv .venv || (
      echo Инсталирай Python от python.org и сложи отметка на "Add python.exe to PATH".
      pause
      exit /b 1
    )
  )
  if exist .git (
    git pull --ff-only -q 2>nul || echo Не успях да обновя с git, пускам текущата версия.
  ) else (
    .venv\Scripts\python update.py
  )
  .venv\Scripts\python -m pip install -q -r requirements.txt || (
    pause
    exit /b 1
  )
  if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
  .venv\Scripts\python -m studio
  pause
  exit /b 0
)
