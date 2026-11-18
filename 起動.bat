@echo off
setlocal
title TrapMatch
set "CHECK=import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 7) else 1)"
set "PY=py -3"
%PY% -c "%CHECK%" >nul 2>nul
if not errorlevel 1 goto run
set "PY=python"
%PY% -c "%CHECK%" >nul 2>nul
if not errorlevel 1 goto run
echo Python 3.7 or later was not found.
echo Install Python from https://www.python.org/ and run this file again.
pause
exit /b 1
:run
%PY% "%~dp0app.py" %*
if not errorlevel 1 goto end
echo.
echo TrapMatch stopped with an error. See the message above.
pause
:end
endlocal
