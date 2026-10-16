@echo off
cd /d "%~dp0..\.."
".venv-brokers\Scripts\python.exe" -m quant_lab probe --broker all
pause
