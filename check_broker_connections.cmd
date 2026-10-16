@echo off
cd /d "%~dp0"
".venv-brokers\Scripts\python.exe" broker_connect.py %*
pause
