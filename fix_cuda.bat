@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" install_runtime.py --cuda --torch-only
if errorlevel 1 pause
