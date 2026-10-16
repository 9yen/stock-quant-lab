@echo off
cd /d "%~dp0..\.."
powershell -NoProfile -Command "$r=Get-Content -LiteralPath 'state\web_process.json' -Raw | ConvertFrom-Json; Start-Process $r.url"
