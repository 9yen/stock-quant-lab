@echo off
cd /d "%~dp0..\.."
if exist "state\container_migrated.json" (
  if not defined QUANT_LAB_CONTAINER_HOME set "QUANT_LAB_CONTAINER_HOME=%LOCALAPPDATA%\StockQuantLabDocker"
  call :container_stop
  exit /b
)
echo STOP>STOP_PAPER
echo Stop requested. Verify broker orders and positions.
pause
exit /b
:container_stop
if not exist "%QUANT_LAB_CONTAINER_HOME%\state\container_authorization.json" (
  echo Container runtime unavailable. Use the dashboard stop controls.
  exit /b 1
)
echo STOP>"%QUANT_LAB_CONTAINER_HOME%\STOP_PAPER"
echo Container Alpaca stop requested. Verify broker orders and positions.
pause
