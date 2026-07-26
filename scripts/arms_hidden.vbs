' ---------------------------------------------------------------
' arms_hidden.vbs - launch arms_core.bat with NO console window.
'
' Why this file exists:
'   A visible console window is a "kill the whole stack" button. Closing it
'   sends CTRL_CLOSE_EVENT and every child dies at once (exit 0xC000013A =
'   STATUS_CONTROL_C_EXIT). That happened on 2026-07-26.
'   WScript.Shell.Run with intWindowStyle = 0 starts the batch hidden, so
'   there is no window to close. Logging is unaffected: arms_core.bat still
'   redirects to logs\launcher.log.
'
' ASCII only on purpose: WSH reads .vbs as ANSI (cp932 here) unless it is
' UTF-16, so non-ASCII comments risk breaking the script. Japanese notes live
' in scripts\AUTOSTART.md and scripts\start_arms.bat.
' ---------------------------------------------------------------
Option Explicit
Dim shell, fso, core
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
core = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "arms_core.bat")
' 0 = hidden window, False = do not wait for it to finish
shell.Run """" & core & """", 0, False
