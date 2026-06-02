@echo off
setlocal
cd /d "%~dp0"

echo Installing translation tool dependencies...

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt
) else (
    python -m pip install -r requirements.txt
)

if errorlevel 1 (
    echo.
    echo Install failed.
    pause
    exit /b 1
)

echo.
echo Install complete.
pause
