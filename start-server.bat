@echo off
cd /d "%~dp0"
echo Starting Open Engine Handoff Backend...
echo.
python server.py
if errorlevel 1 (
    echo.
    echo Server exited with an error. Make sure Python is installed and on PATH.
    pause
)
