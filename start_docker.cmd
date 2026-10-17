@echo off
setlocal
cd /d "%~dp0"
if not defined QUANT_LAB_CONTAINER_HOME set "QUANT_LAB_CONTAINER_HOME=%LOCALAPPDATA%\StockQuantLabDocker"
if not exist "%QUANT_LAB_CONTAINER_HOME%\state\container_authorization.json" (
  echo Private PAPER runtime is not migrated. See docs\DOCKER.md.
  exit /b 1
)
docker compose -f compose.paper.yaml -f compose.host.yaml up -d --build --wait --wait-timeout 120
