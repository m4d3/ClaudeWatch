@echo off
setlocal
rem Classic console windows do not support clickable OSC 8 file links.
rem Reuse Windows Terminal when already inside it; otherwise open it once.
if defined WT_SESSION goto console
if defined CLAUDEWATCH_CURRENT_CONSOLE goto console
if defined CLAUDEWATCH_TERMINAL_STARTED goto console
set "WATCH_TERMINAL="
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" set "WATCH_TERMINAL=%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"
if defined WATCH_TERMINAL goto terminal
"%SystemRoot%\System32\where.exe" wt.exe >nul 2>nul
if not errorlevel 1 set "WATCH_TERMINAL=wt.exe"
if not defined WATCH_TERMINAL goto fallback
:terminal
rem Guard against repeated launches if the host does not provide WT_SESSION.
set "CLAUDEWATCH_TERMINAL_STARTED=1"
"%WATCH_TERMINAL%" -d "%~dp0." cmd.exe /d /c call "%~f0" %*
if not errorlevel 1 exit /b 0
set "CLAUDEWATCH_TERMINAL_STARTED="
:fallback
echo Windows Terminal is unavailable. File paths will remain copyable in this console.
:console
title ClaudeWatch
"%SystemRoot%\System32\where.exe" py.exe >nul 2>nul
if errorlevel 1 goto findpython
py.exe -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto findpython
py.exe -3 -u "%~dp0claudewatch.py" --color on %*
goto done
:findpython
set "WATCH_PYTHON="
"%SystemRoot%\System32\where.exe" python.exe >nul 2>nul
if errorlevel 1 goto localpython
python.exe -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto localpython
set "WATCH_PYTHON=python.exe"
goto run
:localpython
for /d %%D in ("%USERPROFILE%\AppData\Local\Programs\Python\Python3*") do if exist "%%~D\python.exe" (
    "%%~D\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
    if not errorlevel 1 set "WATCH_PYTHON=%%~D\python.exe"
)
if not defined WATCH_PYTHON goto missing
:run
"%WATCH_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto missing
"%WATCH_PYTHON%" -u "%~dp0claudewatch.py" --color on %*
:done
set "WATCH_EXIT=%ERRORLEVEL%"
if not "%WATCH_EXIT%"=="0" pause
exit /b %WATCH_EXIT%
:missing
echo ClaudeWatch requires Python 3.11 or newer. Install Python and try again.
pause
exit /b 1
