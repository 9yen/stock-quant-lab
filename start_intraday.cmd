@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" intraday_bot.py --watch
pause
