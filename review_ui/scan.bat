@echo off
cd /d "%~dp0.."
python -m review_ui.scan
pause