@echo off
rem ---------------------------------------------------------------
rem Form Fill launcher.
rem
rem Drag a document image or PDF onto this file, or double-click it
rem and type the path when asked. A browser review screen opens
rem automatically; after you confirm, a filled PDF is written next
rem to the source file.
rem
rem chcp 65001 affects this console window only (no registry, no
rem profile write). LEVEL 1 in .claude/rules/encoding-safety.md.
rem
rem ASCII only on purpose: cmd.exe reads .bat in the OEM codepage
rem (cp932 here) and a UTF-8 Japanese comment can swallow the CR of
rem its own line, merging the next command into the comment.
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

set "IMAGE=%~1"
if "%IMAGE%"=="" (
    echo Drag a document image or PDF onto this file, or type its path below.
    set /p "IMAGE=Path to image/PDF: "
)
if "%IMAGE%"=="" (
    echo No file given.
    pause
    exit /b 1
)

for %%F in ("%IMAGE%") do set "OUTPUT=%%~dpnF_filled.pdf"

echo.
echo 1) Full print  - blank paper, no handwriting needed
echo 2) Trace guide - light print to trace by hand
choice /c 12 /n /m "Choose output type (1 or 2): "
if errorlevel 2 (set "OPACITY=0.15") else (set "OPACITY=1.0")

py tools\tool_form_fill.py --image "%IMAGE%" --output "%OUTPUT%" --opacity %OPACITY%
set "RC=%errorlevel%"

echo.
echo Output: %OUTPUT%
pause
endlocal & exit /b %RC%
