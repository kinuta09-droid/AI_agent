@echo off
rem ---------------------------------------------------------------
rem ARMS 自動起動エントリポイント (Windows タスクスケジューラ用)
rem
rem これまで PC 起動時の立ち上げはリポジトリ外の設定に依存していたため、
rem 起動しなかったときに「何が起きたか」を示す記録がどこにも残らなかった。
rem 本スクリプトは main.py (スーパーバイザ) の出力を logs\launcher.log に
rem 落とし、直前のブートぶんを logs\launcher.prev.log として1世代だけ残す。
rem
rem 登録方法とトラブルシュートは scripts\AUTOSTART.md を参照。
rem ---------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

if not exist "logs" mkdir "logs"
rem 前回ブートのログを1世代だけ退避する (無限に肥大させない)
if exist "logs\launcher.log" move /y "logs\launcher.log" "logs\launcher.prev.log" >nul

echo [%date% %time%] launcher start (cwd=%CD%) > "logs\launcher.log"
rem -u (バッファなし): stdout をファイルへリダイレクトすると Python は既定で
rem ブロックバッファリングし、[Monitor] / [Watchdog] の行が 8KB 貯まるまで
rem ログに現れない。障害時に真っ先に読むログなので必ず即時反映させる。
py -u main.py >> "logs\launcher.log" 2>&1
echo [%date% %time%] launcher exited (code %errorlevel%) >> "logs\launcher.log"
endlocal
