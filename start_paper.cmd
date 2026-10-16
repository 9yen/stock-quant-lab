@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -u paper_intraday.py --execute-paper --session 2026-10-16
pause
