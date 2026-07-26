@echo off
rem ---------------------------------------------------------------
rem ARMS autostart entry point (for Windows Task Scheduler).
rem
rem This is a thin entry: it launches the real work hidden and exits.
rem The actual startup lives in scripts\arms_core.bat.
rem
rem Why the split (real incident, 2026-07-26): when the Task Scheduler
rem runs a .bat directly, a visible console window stays on the desktop.
rem Closing it sends CTRL_CLOSE_EVENT and main.py, the bot and the
rem scheduler all die at once (exit 0xC000013A = STATUS_CONTROL_C_EXIT).
rem Launching hidden via wscript leaves no window to close.
rem
rem The registered task action does not change: this file stays the entry.
rem
rem ASCII only on purpose (cmd.exe reads .bat in the OEM codepage).
rem Japanese notes and troubleshooting: scripts\AUTOSTART.md
rem ---------------------------------------------------------------
setlocal
wscript.exe //B //Nologo "%~dp0arms_hidden.vbs"
endlocal
