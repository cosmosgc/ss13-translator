@echo off
REM Build script for SS13 Translator - Creates a single EXE file

setlocal enabledelayedexpansion

echo.
echo ========================================
echo SS13 Translator - Build Script
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    pause
    exit /b 1
)

echo [1/4] Installing build dependencies...
python -m pip install -q pyinstaller
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    pause
    exit /b 1
)

echo [2/4] Installing application dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install application dependencies
    pause
    exit /b 1
)

echo [3/4] Building EXE...
pyinstaller --onefile ^
    --name "SS13Translator" ^
    --distpath "./dist" ^
    --workpath "./build" ^
    --specpath "./build" ^
    --exclude-module "argosmodel" ^
    --exclude-module "env" ^
    translate_ss13.py
if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    pause
    exit /b 1
)

echo [4/4] Setting up runtime environment...

REM Copy .env.example as .env to dist directory
if exist ".env.example" (
    copy ".env.example" ".\dist\.env" >nul
    echo Created .env file in dist directory
) else (
    echo WARNING: .env.example not found. Please create .env manually in the dist directory.
)

REM Copy .argosmodel if it exists
if exist "translate-en_pb-1_9.argosmodel" (
    copy "translate-en_pb-1_9.argosmodel" ".\dist\" >nul
    echo Copied Argos model to dist directory
) else (
    echo NOTE: Argos model (.argosmodel) not found in current directory.
    echo Please copy your .argosmodel file to the dist directory before running.
)

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo.
echo Output: .\dist\SS13Translator.exe
echo Config: .\dist\.env
echo.
echo Next steps:
echo 1. Review and update .\dist\.env with your configuration
echo 2. Ensure the Argos model is in .\dist\ directory
echo 3. Run: .\dist\SS13Translator.exe
echo.
pause
