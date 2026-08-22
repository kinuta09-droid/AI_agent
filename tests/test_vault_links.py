# -*- coding: utf-8 -*-
"""tests/test_vault_links.py — GraphRAG 段階1: links テーブル + 1ホップ展開

検証する不変条件 (plans/... GraphRAG 段階1 の Phase 1c 対応表):
  [1]  抽出: エイリアス / 見出し / ブロック参照 / 埋め込み / 非.md / コードフェンス内 / 重複
  [2]  解決: rel_path一致・basename一致・曖昧時に毎回同じ答え・未作成ノートはNULL
  [3]  増分: ファイル更新でsrcの行が置換される / ファイル削除で行が消える
  [4]  バックフィル: mtime一致で全ファイルskipされる既存DBでもlinksが埋まる (D6)
  [5]  _link_hits: ハブ足切り / seed自身の除外 / dangling除外 / 共起数順 / 予算上限
  [6]  _link_hits: chunks に本文が無いファイルは返さない
  [7]  link_fn が [] を返すとき、出力が展開OFFと完全一致する
  [8]  既定OFFでは link_fn が解決されない (実DBを読まない)
  [9]  link_fn が例外を投げても検索が継続し、note に理由が残る
  [10] 展開ONで近傍が sources に link を持って出る

Ollama/Gemini は一切呼ばない (StubEmbedder による決定的フェイク埋め込み、
LLM リランク/multi-query は無効化)。tempfile の DB のみを使う。

実行: py tests/test_vault_links.py
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

# 実 Gemini を叩かないための無効化 (このプロセス内でのみ有効 — 子プロセスを起こさない)
os.environ["VAULT_SEARCH_MULTIQUERY"] = "0"
os.environ["VAULT_SEARCH_RERANK"] = "0"

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_index import (  # noqa: E402
    _connect, _extract_links, _link_hits, _resolve_link_targets, update_vault_index,
)
from tools.tool_vault_hybrid import tool_vault_hybrid_search  # noqa: E402


# ---------------------------------------------------------
# ヘルパ (tests/test_vault_index_mtime.py と同じ作法)
# ---------------------------------------------------------

DIM = 8
PAD = "本文がそこそこ長くなるようにダミーの文章を追加しておく。" * 2  # CHUNK_MIN_CHARS対策


class StubEmbedder:
    """ハッシュベースの決定的フェイク埋め込み。呼び出し回数を記録する。"""

    def __init__(self):
        self.call_count = 0

    def __call__(self, texts):
        self.call_count += 1
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([b / 255.0 for b in h[:DIM]])
        return out


def _write(vault, rel, content):
    path = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _kw_hit(rel_path, score, snippet="body"):
    return {"rel_path": rel_path, "filename": os.path.basename(rel_path),
            "score": score, "snippet": snippet}


def _search(*args, **kwargs):
    """activity 経路を必ず無効化して検索を呼ぶ (test_hybrid_search.py と同じ理由)。

    log_fn も既定で無効化する: link_fn フィクスチャが source="vault" の
    fabricated ヒット (例 "neighbor.md") を返すテストがあり、実装側の既定
    log_fn (logs/vault_search_log.jsonl への実書き込み) のままだと存在しない
    ファイル名で本番ログを汚染してしまう (Known Bug Pattern C と同型)。
    """
    kwargs.setdefault("activity_keyword_fn", lambda q, k=None: [])
    kwargs.setdefault("log_fn", lambda query, hits: None)
    return tool_vault_hybrid_search(*args, **kwargs)


# ---------------------------------------------------------
# [1] 抽出
# ---------------------------------------------------------

def test_extract_links_all_forms():
    text = (
        "```\n[[コード内リンク]]\n```\n\n"
        "[[Note A]] と [[Note B|エイリアス表示]] と [[Note C#見出し]] と "
        "[[Note D#^blockid]]\n"
        "![[画像.png]] は捨てる。![[Note E]] は埋め込みでもリンクとして残す。\n"
        "重複 [[Note A]] は1回だけ。\n"
        "~~~\n[[チルダフェンス内]]\n~~~\n"
    )
    names = _extract_links(text)
    assert names == ["Note A", "Note B", "Note C", "Note D", "Note E"], names
    print("✅ [1] 抽出: alias/heading/block/embed/非md/コードフェンス/重複")


# ---------------------------------------------------------
# [2] 解決
# ---------------------------------------------------------

def test_resolve_link_targets():
    tmp = tempfile.mkdtemp()
    try:
        db = os.path.join(tmp, "index.db")
        conn = _connect(db)
        for fp in ("30_Main/Foo.md", "30_Main/Bar.md", "20_Sources/Foo.md"):
            conn.execute(
                "INSERT INTO files(filepath, mtime, chunk_count, source) "
                "VALUES (?, 0, 0, 'vault')", (fp,))
        conn.executemany(
            "INSERT INTO links(src_path, dst_name, dst_path) VALUES (?, ?, NULL)",
            [
                ("30_Main/Bar.md", "30_Main/Foo"),  # rel_path 完全一致
                ("30_Main/Bar.md", "Foo"),           # basename 一致 (曖昧2件)
                ("30_Main/Bar.md", "来週やる"),       # 未作成ノート
            ],
        )
        conn.commit()

        dangling, ambiguous = _resolve_link_targets(conn)
        rows = dict(conn.execute(
            "SELECT dst_name, dst_path FROM links WHERE src_path=?",
            ("30_Main/Bar.md",)).fetchall())
        assert rows["30_Main/Foo"] == "30_Main/Foo.md", rows
        # 曖昧: rel_path 昇順で決定的に1つ (20_Sources/... < 30_Main/...)
        assert rows["Foo"] == "20_Sources/Foo.md", rows
        assert rows["来週やる"] is None, rows
        assert dangling == 1, dangling
        assert ambiguous == 1, ambiguous

        # 決定性: 再実行しても同じ答え
        _resolve_link_targets(conn)
        rows2 = dict(conn.execute(
            "SELECT dst_name, dst_path FROM links WHERE src_path=?",
            ("30_Main/Bar.md",)).fetchall())
        assert rows2 == rows, (rows, rows2)
        conn.close()
        print("✅ [2] 解決: rel_path一致/basename一致(決定的)/未解決NULL")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# [3] 増分
# ---------------------------------------------------------

def test_incremental_links_replace_and_prune():
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        db = os.path.join(tmp, "index.db")
        os.makedirs(vault)
        _write(vault, "A.md", f"## A\n[[B]]。{PAD}")
        _write(vault, "B.md", f"## B\n本文。{PAD}")
        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT dst_name FROM links WHERE src_path=?", ("A.md",)).fetchall()
        conn.close()
        assert rows == [("B",)], rows

        # 内容変更 (mtime を進める): リンク先が差し替わる
        path_a = os.path.join(vault, "A.md")
        _write(vault, "A.md", f"## A\n[[C]]。{PAD}")
        os.utime(path_a, (os.path.getmtime(path_a) + 10, os.path.getmtime(path_a) + 10))
        _write(vault, "C.md", f"## C\n本文C。{PAD}")
        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT dst_name FROM links WHERE src_path=?", ("A.md",)).fetchall()
        conn.close()
        assert rows == [("C",)], f"旧リンク(B)が残っている: {rows}"

        # 削除: 行が消える
        os.remove(os.path.join(vault, "A.md"))
        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT * FROM links WHERE src_path=?", ("A.md",)).fetchall()
        conn.close()
        assert rows == [], rows
        print("✅ [3] 増分: ファイル更新で置換 / 削除でprune")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# [4] バックフィル (D6 の回帰)
# ---------------------------------------------------------

def test_backfill_regression_on_existing_db():
    """既存DB (機能導入前を模す: links空・links_version未設定) で
    全ファイル mtime 一致 (増分ループが全skip) でも links が埋まること。"""
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        db = os.path.join(tmp, "index.db")
        os.makedirs(vault)
        _write(vault, "A.md", f"## A\n[[B]] へのリンク。{PAD}")
        _write(vault, "B.md", f"## B\n本文B。{PAD}")
        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)

        # 「既存DB」を模す: links テーブルと links_version だけを消す
        # (機能導入前は links テーブル自体が無かった状態に相当)。
        # ファイルの mtime は一切変えないので、増分ループは全ファイルをskipするはず。
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM meta WHERE key='links_version'")
        conn.commit()
        conn.close()

        stub2 = StubEmbedder()
        result = update_vault_index(embed_fn=stub2, db_path=db, vault_root=vault)
        assert "0 files updated" in result, (
            f"テスト前提が崩れている (mtime一致でskipされるはず): {result}")
        assert stub2.call_count == 1, (
            f"バックフィルは埋め込み不要のはず (probeの1回だけ想定): {stub2.call_count}回")

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT src_path, dst_name, dst_path FROM links").fetchall()
        conn.close()
        assert ("A.md", "B", "B.md") in rows, (
            f"mtime一致で全skipでも links が埋まらなければならない (D6): {rows}")
        print("✅ [4] バックフィル: mtime一致の既存DBでもlinksが埋まる (D6)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# [5] _link_hits: フィルタと並び
# ---------------------------------------------------------

def test_link_hits_filters_and_order():
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        db = os.path.join(tmp, "index.db")
        os.makedirs(vault)
        # MOC はハブ (26件から参照される -> 入次数26 > LINK_HUB_DEGREE=20)
        _write(vault, "MOC.md", f"## MOC\nインデックス。{PAD}")
        for i in range(26):
            _write(vault, f"Leaf{i}.md", f"## Leaf{i}\n[[MOC]]。{PAD}")
        _write(vault, "Friend1.md", f"## Friend1\n{PAD}")
        _write(vault, "Friend2.md", f"## Friend2\n{PAD}")
        # Leaf0/Leaf1 を上書き: Friend2 は両方から、Friend1 は Leaf0 からのみ
        _write(vault, "Leaf0.md",
               f"## Leaf0\n[[MOC]] と [[Friend1]] と [[Friend2]] と [[未作成ノート]]。{PAD}")
        _write(vault, "Leaf1.md", f"## Leaf1\n[[MOC]] と [[Friend2]]。{PAD}")

        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)

        hits = _link_hits(["Leaf0.md", "Leaf1.md"], limit=5, db_path=db)
        paths = [h["rel_path"] for h in hits]
        assert "MOC.md" not in paths, f"ハブが足切りされていない: {paths}"
        assert "Leaf0.md" not in paths and "Leaf1.md" not in paths, (
            f"seed自身が含まれている: {paths}")
        assert "未作成ノート" not in paths, f"dangling(未解決)リンクが含まれている: {paths}"
        assert paths[0] == "Friend2.md", (
            f"共起数2 (Leaf0+Leaf1) のFriend2が先頭に来ていない: {paths}")

        limited = _link_hits(["Leaf0.md", "Leaf1.md"], limit=1, db_path=db)
        assert len(limited) == 1 and limited[0]["rel_path"] == "Friend2.md", limited
        print("✅ [5] _link_hits: ハブ足切り/seed除外/dangling除外/共起順/予算上限")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# [6] _link_hits: 本文が無いファイルの除外
# ---------------------------------------------------------

def test_link_hits_excludes_files_without_chunks():
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        db = os.path.join(tmp, "index.db")
        os.makedirs(vault)
        # Tiny.md は短すぎて _chunk_markdown が 0 チャンクになる (CHUNK_MIN_CHARS=20)
        _write(vault, "A.md", f"## A\n[[Tiny]]。{PAD}")
        _write(vault, "Tiny.md", "x")

        update_vault_index(embed_fn=StubEmbedder(), db_path=db, vault_root=vault)

        conn = sqlite3.connect(db)
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE filepath='Tiny.md'").fetchone()[0]
        conn.close()
        assert chunk_count == 0, "テスト前提: Tiny.md はチャンクを持たないはず"

        hits = _link_hits(["A.md"], limit=5, db_path=db)
        assert hits == [], f"本文の無いファイルが返っている: {hits}"
        print("✅ [6] _link_hits: chunks が無いファイルは除外される")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# [7]〜[10] 検索側フック (tool_vault_hybrid._retrieve_once 経由)
# ---------------------------------------------------------

def test_link_expand_empty_matches_baseline():
    """link_fn が [] を返すとき、出力は展開OFFと完全一致する
    (再融合が既存順位を壊さないことの証明)。"""
    kw = [_kw_hit("a.md", 3), _kw_hit("b.md", 1)]
    sem = [_kw_hit("b.md", 0.9), _kw_hit("c.md", 0.8)]

    baseline = _search("q", keyword_fn=lambda q: kw, semantic_fn=lambda q, k: sem,
                       link_expand=False)
    expanded = _search("q", keyword_fn=lambda q: kw, semantic_fn=lambda q, k: sem,
                       link_expand=True, link_fn=lambda seeds, limit: [])
    assert expanded == baseline, f"\nbaseline:\n{baseline}\nexpanded:\n{expanded}"
    print("✅ [7] link_fn=[] は展開OFFと完全一致 (再融合が既存順位を壊さない)")


def test_link_expand_default_off_no_resolve():
    """既定OFFでは link_fn が解決されない (実DBを読まない、D4)。"""
    calls = []

    def spy(seeds, limit):
        calls.append((seeds, limit))
        return []

    kw = [_kw_hit("a.md", 2)]
    result = _search("q", keyword_fn=lambda q: kw, semantic_fn=lambda q, k: [],
                     link_fn=spy)  # link_expand 未指定 = コード既定 (LINK_EXPAND_DEFAULT=False)
    assert not calls, f"既定OFFなのに link_fn が呼ばれた: {calls}"
    assert "[[a.md]]" in result, result
    print("✅ [8] 既定OFF: link_fn は解決も呼び出しもされない")


def test_link_expand_exception_continues_with_note():
    """link_fn が例外を投げても検索は継続し、note に理由が残る。"""
    def broken(seeds, limit):
        raise RuntimeError("link db locked")

    kw = [_kw_hit("a.md", 2)]
    result = _search("q", keyword_fn=lambda q: kw, semantic_fn=lambda q, k: [],
                     link_expand=True, link_fn=broken)
    assert "[[a.md]]" in result, result
    assert "リンク展開は利用できません" in result and "link db locked" in result, result
    print("✅ [9] link_fn例外 → 検索継続 + noteに理由")


def test_link_expand_on_adds_link_source():
    """展開ONで近傍が sources に link を持って出る。"""
    kw = [_kw_hit("a.md", 2)]

    def link_fn(seeds, limit):
        assert seeds == ["a.md"], seeds
        return [{"rel_path": "neighbor.md", "filename": "neighbor.md", "score": 1.0,
                 "snippet": "近傍ノートの本文", "heading": None,
                 "chunk_id": "neighbor.md#0", "source": "vault"}]

    result = _search("q", keyword_fn=lambda q: kw, semantic_fn=lambda q, k: [],
                     link_expand=True, link_fn=link_fn, top_k=5)
    assert "[[neighbor.md]]" in result, result
    tail = result.split("[[neighbor.md]]", 1)[1].split("\n", 1)[0]
    assert "link" in tail, f"sourcesにlinkが出ていない: {tail}"
    print("✅ [10] 展開ON: 近傍が sources=link で出力される")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                failed += 1
                print(f"❌ {name}: {e}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    if failed:
        print(f"\nFAILURE: {failed}/{total} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {total} tests passed")
