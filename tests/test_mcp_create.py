# -*- coding: utf-8 -*-
"""gws MCP の drive_files_create のスキーマ/引数形を調べるプローブ (手動デバッグ)。

**実際に Google Drive にフォルダを作る = 消えない副作用がある** ので、
`gws` CLI の存在に加えて ARMS_LIVE_TESTS=1 のオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_mcp_create.py

線引き: 読み取りだけのプローブ (test_gws_mcp.py) は前提が無ければ SKIP で足りるが、
外部サービスへ書き込むものは、前提が揃っている開発機で必ず走ってしまうため
オプトインの後ろに置く (Session 2 で決めた規約)。

2026-07-30 に直した欠陥:
  1. `gws` が無い環境 (CI) では Popen が空プロセスになり、最初の応答を
     `json.loads("")` して JSONDecodeError で落ちていた。
  2. ゲートが無く、開発機のスイープ1回ごとに Drive にフォルダが1つ増えていた。
  3. 判定結果が終了コードに反映されていなかった。
"""
import sys
import os
import json
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')


def _live_allowed() -> bool:
    return os.getenv("ARMS_LIVE_TESTS") == "1" and bool(shutil.which("gws"))


def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        print(f"DEBUG (非JSON): {line.strip()}")
        return None


def test_mcp_create() -> int:
    if not _live_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と `gws` CLI のときだけ実行します")
        print("   (drive_files_create は実際に Google Drive にフォルダを作成します)")
        return 0

    p = subprocess.Popen(
        ["gws", "mcp", "--services", "drive"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        encoding="utf-8", errors="replace", bufsize=1, shell=True,
    )
    try:
        init = send(p, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }, 1)
        if not init:
            print("❌ initialize に応答がありません (gws mcp が起動していない可能性)")
            return 1

        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        p.stdin.flush()

        res = send(p, "tools/list", {}, 2)
        if not res or "result" not in res:
            print("❌ tools/list に応答がありません")
            return 1
        for t in res["result"]["tools"]:
            if t["name"] == "drive_files_create":
                print(f"Tool Schema: {json.dumps(t, indent=2, ensure_ascii=False)}")
                break

        print("\nTrying top-level arguments...")
        res1 = send(p, "tools/call", {
            "name": "drive_files_create",
            "arguments": {"name": "Test_TopLevel", "mimeType": "application/vnd.google-apps.folder"},
        }, 3)
        print(f"Res1: {json.dumps(res1, indent=2, ensure_ascii=False)}")
        if not res1 or "result" not in res1:
            print("❌ drive_files_create が result を返しませんでした")
            return 1
        print("\n⚠️ Drive に 'Test_TopLevel' フォルダが作成されました。手動で削除してください。")
        return 0
    finally:
        p.terminate()


if __name__ == "__main__":
    sys.exit(test_mcp_create())
