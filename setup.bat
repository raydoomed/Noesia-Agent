@echo off
rem ASCII-only wrapper on purpose. All Chinese text lives in scripts\setup.ps1:
rem cmd reads .bat files in 512-byte chunks, so multi-byte UTF-8 gets cut in half
rem and the tail is run as a command. PowerShell has no such problem.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
    echo.
    echo   Setup failed. Read the lines above.
    pause
    exit /b 1
)
pause
