@echo off
rem ---------------------------------------------------------------
rem ARMS manual control (start / stop / restart / status / logs).
rem
rem Double-click with no arguments to get a numbered menu.
rem All logic lives in scripts\arms_ctl.py (shares lib\service_control.py
rem with the Discord /status and /restart commands). This file is a thin
rem launcher on purpose.
rem
rem chcp 65001 affects this console window only (no registry, no profile
rem write) so the UTF-8 output of arms_ctl.py renders correctly.
rem That is LEVEL 1 in .claude/rules/encoding-safety.md.
rem
rem ASCII only on purpose: cmd.exe reads .bat in the OEM codepage
rem (cp932 here) and a UTF-8 Japanese comment can swallow the CR of its
rem own line, merging the next command into the comment.
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

py "%~dp0arms_ctl.py" %*
set "RC=%errorlevel%"

rem Keep the window open only when double-clicked (no arguments).
if "%~1"=="" pause
endlocal & exit /b %RC%
