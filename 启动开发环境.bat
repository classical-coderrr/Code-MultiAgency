@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Agent Team - Development Environment

rem This file is the stable double-click entry point. Keep it ASCII-only so
rem it works regardless of the active Windows console code page.
set "PROJECT_ROOT=%~dp0"
set "START_SCRIPT=%PROJECT_ROOT%start-dev.ps1"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%POWERSHELL_EXE%" (
  echo [STOP] Windows PowerShell was not found: %POWERSHELL_EXE%
  if not defined AGENT_TEAM_NO_PAUSE pause
  exit /b 1
)

if not exist "%START_SCRIPT%" (
  echo [STOP] Missing launcher script: %START_SCRIPT%
  if not defined AGENT_TEAM_NO_PAUSE pause
  exit /b 1
)

pushd "%PROJECT_ROOT%" >nul
"%POWERSHELL_EXE%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%START_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

if "%EXIT_CODE%"=="0" (
  echo [DONE] Agent Team is ready.
) else (
  echo [STOP] Agent Team could not be started. Exit code: %EXIT_CODE%
  echo [INFO] Diagnostic logs: %PROJECT_ROOT%backend\data\logs
)

if not defined AGENT_TEAM_NO_PAUSE pause
exit /b %EXIT_CODE%
