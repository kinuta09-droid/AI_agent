# ---------------------------------------------------------------
# scripts/install_shortcut.ps1 — ARMS Control のショートカットを作成する
#
# デスクトップとスタートメニューに scripts\arms.bat へのショートカットを置く。
# PC 再起動後やスタック停止時に、ターミナルを開かずクリック1回で復帰できるようにする。
#
# 実行 (通常の PowerShell でよい。管理者権限は不要):
#     powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
#
# 取り消し: 作成された .lnk を削除するだけ (他には何も変更しない)。
#
# 注意: このファイルは **UTF-8 BOM 付き** で保存すること。Windows PowerShell 5.1 は
# .ps1 を BOM が無いと ANSI (cp932) として読むため、日本語がそのまま構文エラーになる。
# ---------------------------------------------------------------
$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$target = Join-Path $PSScriptRoot "arms.bat"
if (-not (Test-Path $target)) {
    Write-Error "arms.bat が見つかりません: $target"
}

$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "ARMS Control.lnk"
$desktop   = Join-Path ([Environment]::GetFolderPath("Desktop"))  "ARMS Control.lnk"

$shell = New-Object -ComObject WScript.Shell
foreach ($path in @($desktop, $startMenu)) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath       = $target
    $lnk.WorkingDirectory = $root
    $lnk.Description      = "ARMS スタックの起動・再起動・状態確認"
    # shell32.dll の 25 番 = ネットワーク/サーバー風アイコン。ターミナルと区別しやすい
    # $env: は部分式で囲む ("$env:SystemRoot\..." は変数名の切れ目が曖昧でパースが揺れる)
    $lnk.IconLocation     = "$($env:SystemRoot)\System32\shell32.dll,25"
    $lnk.Save()
    Write-Host "作成しました: $path"
}

Write-Host ""
Write-Host "ダブルクリックで番号メニュー (1 起動 / 2 再起動 / 3 状態 / 4 停止 / 5 ログ) が開きます。"
