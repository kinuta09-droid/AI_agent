@echo off
rem ---------------------------------------------------------------
rem ARMS 手動コントロール (start / stop / restart / status / logs)
rem
rem 引数なしでダブルクリックすると番号メニューが出る。
rem 判定と表示のロジックは scripts\arms_ctl.py (lib\service_control.py を共有)。
rem ここは薄いランチャーに徹する。
rem
rem chcp 65001 はこのコンソールウィンドウ限定の変更 (レジストリにもプロファイル
rem にも書かない)。Python 側が UTF-8 で出力するため、これが無いと日本語が
rem 化ける。CLAUDE.md の Encoding Safety Rules でいう LEVEL 1 相当。
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

py "%~dp0arms_ctl.py" %*
set "RC=%errorlevel%"

rem 引数なし (= ダブルクリック) のときだけウィンドウを残す
if "%~1"=="" pause
endlocal & exit /b %RC%
