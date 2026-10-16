@echo off
powershell -NoProfile -Command "$r=Get-Content -LiteralPath '%~dp0state\web_process.json' -Raw | ConvertFrom-Json; Start-Process $r.url"
