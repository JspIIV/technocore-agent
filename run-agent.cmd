@echo off
REM Scheduled entry point for the measurement agent.
REM Appends every run to agent.log, so a failed run still leaves a trace.
REM
REM Register it to run every 6 hours:
REM   schtasks /create /tn "TechnocoreAgent" /tr "%CD%\run-agent.cmd" /sc HOURLY /mo 6 /f
REM
REM Set PY below if "python" is not on your PATH.

if "%PY%"=="" set PY=python
set DIR=%~dp0

echo. >> "%DIR%agent.log"
echo ===== %DATE% %TIME% ===== >> "%DIR%agent.log"
"%PY%" -u "%DIR%agent.py" --publish --budget 240 >> "%DIR%agent.log" 2>&1
echo exit=%ERRORLEVEL% >> "%DIR%agent.log"
