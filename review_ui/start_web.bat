@echo off
cd /d "%~dp0.."
echo Starting SS13 Translation Review Web UI...
echo Open http://127.0.0.1:5001 in your browser.
python -m review_ui
pause
