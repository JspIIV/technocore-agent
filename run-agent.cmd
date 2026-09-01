@echo off
REM Scheduled entry point. Appends every run to agent.log so a failed run
REM still leaves a trace.
REM
REM Register it to run every 6 hours:
REM   schtasks /create /tn "TechnocoreAgent" /tr "%CD%\run-agent.cmd" /sc HOURLY /mo 6 /f
REM
REM Set PY below if "python" is not on your PATH.

if "%PY%"=="" set PY=python
set DIR=%~dp0

echo. >> "%DIR%agent.log"
echo ===== %DATE% %TIME% ===== >> "%DIR%agent.log"

REM Settle first. A claim past its deadline counts as a miss, and the window
REM is 8h so nothing can fall between two 6-hourly runs.
echo --- settle --- >> "%DIR%agent.log"
"%PY%" -u "%DIR%prereg_claims.py" settle --publish --within 8 >> "%DIR%agent.log" 2>&1
echo settle exit=%ERRORLEVEL% >> "%DIR%agent.log"

echo --- claim --- >> "%DIR%agent.log"
"%PY%" -u "%DIR%prereg_claims.py" claim --publish >> "%DIR%agent.log" 2>&1
echo claim exit=%ERRORLEVEL% >> "%DIR%agent.log"

echo --- measure --- >> "%DIR%agent.log"
"%PY%" -u "%DIR%agent.py" --publish --budget 240 >> "%DIR%agent.log" 2>&1
echo exit=%ERRORLEVEL% >> "%DIR%agent.log"
