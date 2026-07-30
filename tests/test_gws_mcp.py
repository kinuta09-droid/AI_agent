# -*- coding: utf-8 -*-
"""gws CLI の MCP サーバ (JSON-RPC over stdio) 疎通テスト (手動デバッグスクリプト)。

外部前提 (`gws` CLI + Google 認証済み) が要る。**読み取りのみ**なので、前提が
無ければ SKIP して正常終了する形にそろえる (書き込み・課金を伴うものは
ARMS_LIVE_TESTS=1 のオプトインの後ろ。この線引きは test_mcp_create.py を参照)。

2026-07-30 に直した欠陥:
  1. `gws` が無い環境 (CI) では Popen が空プロセスになり、`p.stdin.flush()` が
     `OSError: [Errno 22] Invalid argument` を投げて落ちていた。
     前提の不在は「テストの失敗」ではないので SKIP にする。
  2. 判定結果が終了コードに反映されておらず、Drive の取得に失敗しても exit 0 だった。
"""
import sys
import os
import json
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

# 個人の Drive 上のプローブ対象。差し替えるときはここだけ変える。
PROBE_FILE_ID = "1jla8CbApXoH66nmzgBUP5y4qDpxugKn-"


def test_gws_mcp() -> int:
    if not shutil.which("gws"):
        print("⏭️ SKIP: `gws` CLI が見つかりません (Google Workspace CLI 未インストール)")
        return 0

    print("Starting gws mcp...")
    p = subprocess.Popen(
        ["gws", "mcp", "--services", "calendar,drive,sheets,gmail,docs"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )

    def send(method, params=None, req_id=1):
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
        except OSError as e:
            # サーバが起動していない/既に死んでいる。前提不備と区別できるよう明示する。
            print(f"❌ MCP サーバへ書き込めません: {e}")
            return None

        while True:
            line = p.stdout.readline()
            if not line:
                return None
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # サーバがログを stdout に混ぜることがあるので読み飛ばす
                print(f"DEBUG: {line.strip()}")
                continue
            if data.get("id") == req_id:
                return data

    try:
        print("Initializing JSON-RPC session...")
        init_res = send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "antigravity-test", "version": "1.0"},
        }, 1)
        print(f"Init Response: {json.dumps(init_res, indent=2, ensure_ascii=False)}")
        if not init_res:
            print("❌ initialize に応答がありません (gws mcp が起動していない可能性)")
            return 1

        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        p.stdin.flush()

        print("\nCalling drive_files_get...")
        get_res = send("tools/call", {
            "name": "drive_files_get",
            "arguments": {"params": {"fileId": PROBE_FILE_ID}},
        }, 2)
        print(f"Get Response: {json.dumps(get_res, indent=2, ensure_ascii=False)}")

        if not get_res or "result" not in get_res:
            print(f"\n❌ Failed to get file (fileId={PROBE_FILE_ID})")
            return 1

        content = get_res["result"].get("content", [])
        if content:
            print(f"\n✅ Success! File info: {content[0].get('text')}")
        else:
            print("\n⚠️ result はあるが content が空です")
        return 0
    finally:
        p.terminate()


if __name__ == "__main__":
    sys.exit(test_gws_mcp())
