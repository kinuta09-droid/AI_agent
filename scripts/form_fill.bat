@echo off
rem ---------------------------------------------------------------
rem Form Fill launcher.
rem
rem Drag a document image or PDF onto this file, or double-click it
rem and type the path when asked. A browser review screen opens
rem automatically; after you confirm, a PDF is written next to the
rem source file.
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
rem "Copy as path" in Explorer wraps the path in quotes; strip them.
set "IMAGE=%IMAGE:"=%"
if "%IMAGE%"=="" (
    echo No file given.
    pause
    exit /b 1
)
if not exist "%IMAGE%" (
    echo File not found: %IMAGE%
    pause
    exit /b 1
)

echo.
echo 1) Full print   - prints the form with your details filled in (blank paper)
echo 2) Trace guide  - prints the form with light text to trace by hand (blank paper)
echo 3) Text only    - prints ONLY light text; feed the ORIGINAL form into the printer
choice /c 123 /n /m "Choose output type (1, 2 or 3): "
set "MODE=%errorlevel%"

if "%MODE%"=="1" (
    set "OPTS=--opacity 1.0"
    set "SUFFIX=_filled"
)
if "%MODE%"=="2" (
    set "OPTS=--opacity 0.15"
    set "SUFFIX=_guide"
)
if "%MODE%"=="3" (
    set "OPTS=--opacity 0.15 --no-background"
    set "SUFFIX=_textonly"
)
for %%F in ("%IMAGE%") do set "OUTPUT=%%~dpnF%SUFFIX%.pdf"

py tools\tool_form_fill.py --image "%IMAGE%" --output "%OUTPUT%" %OPTS%
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo Output: %OUTPUT%
) else if "%RC%"=="2" (
    echo Cancelled. Nothing was written.
) else (
    echo Failed with code %RC%. See the messages above.
)
pause
endlocal & exit /b %RC%
