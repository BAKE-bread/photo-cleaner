@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Python 3.12 is required. Install it from https://www.python.org/downloads/
        exit /b 1
    )
)
echo Installing PhotoCleaner dependencies. This can take several minutes.
".venv\Scripts\python.exe" install_runtime.py
if errorlevel 1 exit /b 1
echo Setup complete. The vision model will be downloaded at first use.
exit /b 0
