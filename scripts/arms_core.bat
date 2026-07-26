@echo off
rem ---------------------------------------------------------------
rem ARMS core startup: rotate log, then run the supervisor.
rem
rem Do NOT run this directly. A visible console window is a
rem "kill the whole stack" button: closing it sends CTRL_CLOSE_EVENT
rem and every child dies at once (exit 0xC000013A). Always enter via
rem scripts\start_arms.bat -> scripts\arms_hidden.vbs (hidden window).
rem
rem -u keeps stdout unbuffered so [Monitor] / [Watchdog] / [Discord]
rem lines reach logs\launcher.log immediately instead of after 8KB.
rem
rem ASCII only on purpose: cmd.exe reads .bat in the OEM codepage
rem (cp932 here), and a UTF-8 Japanese comment can swallow the CR of
rem its own line, merging the next command into the comment.
rem Japanese notes live in scripts\AUTOSTART.md.
rem ---------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

if not exist "logs" mkdir "logs"
if exist "logs\launcher.log" move /y "logs\launcher.log" "logs\launcher.prev.log" >nul

echo [%date% %time%] launcher start (cwd=%CD%) > "logs\launcher.log"
py -u main.py >> "logs\launcher.log" 2>&1
echo [%date% %time%] launcher exited (code %errorlevel%) >> "logs\launcher.log"
endlocal
