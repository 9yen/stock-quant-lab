@echo off
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m quant_lab run-alpaca --execute-paper
pause
