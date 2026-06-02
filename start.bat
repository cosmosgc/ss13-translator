@echo off
setlocal
cd /d "%~dp0"

echo Starting SS13 translation...

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 translate_ss13.py
) else (
    python translate_ss13.py
)

if errorlevel 1 (
    echo.
    echo Translation failed.
    pause
    exit /b 1
)

echo.
echo Translation finished successfully.
pause
