@echo off
rem ASCII-only wrapper on purpose: cmd reads .bat files in 512-byte chunks, so multi-byte
rem UTF-8 Chinese would get cut and run as garbage. All text lives in scripts\uninstall.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1"
if errorlevel 1 (
    echo.
    echo   Uninstall failed. Read the lines above.
    pause
    exit /b 1
)
pause
