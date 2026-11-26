@echo off
chcp 65001 >nul
REM Windows: double-click this file
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg липсва. Отвори PowerShell и пусни:  winget install Gyan.FFmpeg
  echo После затвори и отвори този прозорец отново.
  pause
  exit /b 1
)
if not exist .venv (
  echo Първо пускане: инсталирам нужното ^(отнема 1-2 минути^)...
  py -3 -m venv .venv || python -m venv .venv || (echo Инсталирай Python от python.org & pause & exit /b 1)
)
.venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
if exist .env for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
.venv\Scripts\python -m studio
pause
