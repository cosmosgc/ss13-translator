@echo off
cd /d "%~dp0"
echo Starting SS13 LLM Translate...
echo Open http://127.0.0.1:5002 in your browser.
python -m llm_translate
pause
