@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -c "import PIL,numpy,torch,transformers,pythoncom" >nul 2>&1
if errorlevel 1 (
    call setup.bat
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" run.py gui
if errorlevel 1 pause
