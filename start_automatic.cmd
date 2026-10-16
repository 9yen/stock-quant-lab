@echo off
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
powershell -NoProfile -NonInteractive -Command "Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList @('-u','-m','quant_lab','supervise') -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\automation_supervisor.log' -RedirectStandardError '%~dp0logs\automation_supervisor_error.log'"
