# -*- coding: utf-8 -*-
"""
update_vault_index の mtime 差分ロジックテスト (スタブ埋め込み、ネットワークなし)

実行: py tests/test_vault_index_mtime.py
"""
import os
import sys
import sqlite3
import hashlib
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_index import update_vault_index

DIM = 8


class StubEmbedder:
    """ハッシュベースの決定的フェイク埋め込み。呼び出し回数を記録する。"""

    def __init__(self):
        self.call_count = 0
        self.texts_embedded = 0

    def __call__(self, texts):
        self.call_count += 1
        self.texts_embedded += len(texts)
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([b / 255.0 for b in h[:DIM]])
        return out


class FailingEmbedder:
    def __call__(self, texts):
        return "[ERROR] Ollama not running. Start with: ollama serve"


def _write(vault, rel, content):
    path = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _chunk_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp()
    vault = os.path.join(tmp, "vault")
    db = os.path.join(tmp, "index.db")
    os.makedirs(vault)

    try:
        # --- Ollama不通: 即座にエラー文字列。ただし GraphRAG 段階1 で links の
        # バックフィルが probe より前に走るようになったため (D1: リンク抽出は
        # 埋め込み不要で、Ollama 停止中もグラフだけは追いつかせる)、
        # DB ファイル自体は作られる (空vaultなのでchunks/filesは0のまま)。
        result = update_vault_index(embed_fn=FailingEmbedder(), db_path=db, vault_root=vault)
        assert result.startswith("[ERROR]"), result
        assert os.path.exists(db), "DB should exist (links backfill runs before probe)"
        assert _chunk_count(db) == 0, "no chunks should be created when probe fails"
        print("✅ probe failure -> immediate [ERROR], DB created (links backfill only)")

        # --- 初回フルインデックス ---
        _write(vault, "note_a.md", "## トピックA\nAIエージェントについてのメモです。設計と実装の考察。")
        _write(vault, "sub/note_b.md", "見出しのないノートです。今日の作業ログを記録しています。")
        _write(vault, "Templates/tpl.md", "## テンプレート\nこれはインデックスされてはいけないテンプレートです。")
        _write(vault, "Dev_Logs/log.md", "## ログ\nEXCLUDE_DIRS配下なのでインデックス対象外です。")

        stub = StubEmbedder()
        result = update_vault_index(embed_fn=stub, db_path=db, vault_root=vault)
        assert "2 files updated" in result, result
        assert _chunk_count(db) == 2
        print(f"✅ initial full index: {result}")

        # --- 変更なしの再実行: 埋め込み呼び出しはprobeの1回だけ ---
        stub2 = StubEmbedder()
        result = update_vault_index(embed_fn=stub2, db_path=db, vault_root=vault)
        assert "0 files updated" in result, result
        assert stub2.call_count == 1, f"only probe expected, got {stub2.call_count} calls"
        print("✅ no-change rerun: zero re-embeds (probe only)")

        # --- 1ファイルのmtime変更: そのファイルだけ再埋め込み ---
        path_a = os.path.join(vault, "note_a.md")
        os.utime(path_a, (os.path.getmtime(path_a) + 10, os.path.getmtime(path_a) + 10))
        stub3 = StubEmbedder()
        result = update_vault_index(embed_fn=stub3, db_path=db, vault_root=vault)
        assert "1 files updated" in result, result
        print("✅ mtime change: only that file re-embedded")

        # --- ファイル削除: 行が除去される ---
        os.remove(os.path.join(vault, "sub", "note_b.md"))
        result = update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)
        assert "1 removed" in result, result
        assert _chunk_count(db) == 1
        print("✅ deleted file pruned from index")

        # --- 除外ディレクトリが本当に除外されているか ---
        conn = sqlite3.connect(db)
        try:
            paths = [r[0] for r in conn.execute("SELECT filepath FROM files").fetchall()]
        finally:
            conn.close()
        assert all("Templates" not in p and "Dev_Logs" not in p for p in paths), paths
        print("✅ Templates / Dev_Logs excluded from index")

        print("\n🎉 ALL MTIME-DIFF INDEX TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
