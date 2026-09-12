@echo off
setlocal
cd /d "%~dp0"

set "DATA_ARGS="
if exist "runtime\voice-data\models" set "DATA_ARGS=--data-dir runtime\voice-data"

if exist "runtime\venv312\Scripts\python.exe" (
    "runtime\venv312\Scripts\python.exe" -m article_reader %DATA_ARGS% serve
    goto :finished
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m article_reader %DATA_ARGS% serve
    goto :finished
)

where uv.exe >nul 2>nul
if not errorlevel 1 (
    uv run --locked article-reader %DATA_ARGS% serve
    goto :finished
)

echo Article Reader could not find its Python environment.
echo Run the setup steps in docs\SETUP.md first.
pause
exit /b 1

:finished
set "READER_EXIT=%ERRORLEVEL%"
if not "%READER_EXIT%"=="0" (
    echo.
    echo Article Reader stopped with an error. Review the message above.
    pause
)
exit /b %READER_EXIT%
