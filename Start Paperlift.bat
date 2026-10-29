@echo off
REM Windows: double-click to start Paperlift. If SmartScreen warns, choose "More info" then "Run anyway".
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0paperlift.ps1" serve
if errorlevel 1 (
  echo.
  echo Paperlift stopped with an error. See the message above.
  pause
)
