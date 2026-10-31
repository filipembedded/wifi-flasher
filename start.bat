@echo off
cd /d "%~dp0"
set "PATH=%~dp0;%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
    echo Wireless Flasher needs uv, a small Python tool manager from astral.sh.
    choice /m "Install uv now"
    if errorlevel 2 exit /b 1
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
uv run wireless_flasher.py %*
if errorlevel 1 pause
