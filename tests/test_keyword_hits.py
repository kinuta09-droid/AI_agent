# -*- coding: utf-8 -*-
"""
_keyword_hits 全件スコアリングのユニットテスト (grill繰越項目)

一時Vaultを作り tool_obsidian.VAULT_ROOT を差し替えて実行するため、
実Vault・ネットワークには触れない。差し替えは _temp_vault() が必ず復元する
(runner 依存にすると pytest 実行時に復元が走らず、削除済みtmpdirを指したまま
後続テストが「0件ヒット」を静かに返す)。

実行: py tests/test_keyword_hits.py
"""
import os
import sys
import shutil
import tempfile
from contextlib import contextmanager

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools import tool_obsidian
from tools.tool_obsidian import _keyword_hits


@contextmanager
def _temp_vault(files: dict):
    """files: {"相対パス": "本文"} の一時Vaultを作り、VAULT_ROOT を差し替えて必ず復元する"""
    root = tempfile.mkdtemp(prefix="vault_test_")
    original = tool_obsidian.VAULT_ROOT
    try:
        for rel, body in files.items():
            abs_path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(body)
        tool_obsidian.VAULT_ROOT = root
        yield root
    finally:
        tool_obsidian.VAULT_ROOT = original
        shutil.rmtree(root, ignore_errors=True)


def _walk_order(root: str) -> list:
    """os.walk が実際に .md を返す順 (FS依存なのでハードコードせず実行時に求める)"""
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in tool_obsidian.EXCLUDE_DIRS]
        for f in files:
            if f.endswith('.md'):
                out.append(os.path.relpath(os.path.join(r, f), root).replace("\\", "/"))
    return out


def test_late_high_score_file_survives():
    """walk順で後方にある高スコアファイルが打ち切られず上位に入る (grill指摘の本体)"""
    files = {f"A_early/low{i:02d}.md": "BitNet\nfiller\n" for i in range(20)}
    files["Z_late/gold.md"] = "\n".join(["BitNet"] * 30)
    with _temp_vault(files) as root:
        # 前提: gold.md は walk 順で先着10件より後ろにある (でなければ回帰を検出できない)
        order = _walk_order(root)
        assert order.index("Z_late/gold.md") >= 10, \
            f"テスト前提崩壊: gold.md が walk順 {order.index('Z_late/gold.md')} 番目"

        hits = _keyword_hits("BitNet")
        assert len(hits) == 10, f"max_hits=10 が守られていない: {len(hits)}"
        assert hits[0]["rel_path"] == "Z_late/gold.md", \
            f"後方の高スコアファイルが1位のはず: {[h['rel_path'] for h in hits]}"
        assert hits[0]["score"] == 30, f"score はマッチ行数: {hits[0]['score']}"
        print("✅ keyword: walk順後方の高スコアファイルが打ち切られない")


def test_sorted_desc_and_stable_on_ties():
    """スコア降順。同点は walk 順 (安定ソート) を保つ"""
    files = {
        "a/1.md": "kw\nkw\n",      # score 2
        "a/2.md": "kw\nkw\n",      # score 2 (同点)
        "b/3.md": "kw\nkw\nkw\n",  # score 3
    }
    with _temp_vault(files) as root:
        hits = _keyword_hits("kw")
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True), f"降順でない: {scores}"

        # 同点グループの相対順が walk 順の相対順と一致すること (順序自体はFS依存なので比較で検証)
        order = _walk_order(root)
        tied = [h["rel_path"] for h in hits if h["score"] == 2]
        assert sorted(tied, key=order.index) == tied, \
            f"同点のwalk順が崩れた: {tied} (walk順: {order})"
        assert hits[0]["score"] == 3, f"最高スコアが先頭でない: {scores}"
        print("✅ keyword: スコア降順 + 同点はwalk順維持")


def test_snippet_and_no_match():
    """snippet は初回マッチ行の前5/後25行。0ヒットは空リスト"""
    body = "\n".join([f"line{i}" for i in range(10)] + ["needle"] + [f"tail{i}" for i in range(40)])
    with _temp_vault({"n/note.md": body}):
        hits = _keyword_hits("needle")
        assert len(hits) == 1 and hits[0]["filename"] == "note.md"
        snippet_lines = hits[0]["snippet"].split("\n")
        assert snippet_lines[0] == "line5", f"5行前から始まるはず: {snippet_lines[0]}"
        assert len(snippet_lines) == 30, f"前5+当該+後24=30行: {len(snippet_lines)}"
        assert _keyword_hits("存在しない語") == [], "0ヒットは空リスト"
        assert _keyword_hits("   ") == [], "空キーワードは空リスト"
        print("✅ keyword: snippet範囲 / 0ヒット / 空クエリ")


def test_exclude_dirs_and_non_md():
    """EXCLUDE_DIRS 配下と .md 以外は走査対象外"""
    with _temp_vault({
        "ok.md": "target\n",
        ".obsidian/cache.md": "target\n",
        "Trash/deleted.md": "target\n",
        "notes/plain.txt": "target\n",
    }):
        paths = [h["rel_path"] for h in _keyword_hits("target")]
        assert paths == ["ok.md"], f"除外が効いていない: {paths}"
        print("✅ keyword: EXCLUDE_DIRS / 非.md を除外")


def test_max_hits_override():
    """max_hits で件数を明示指定できる (0/None は全件 — ハイブリッド側から多めに取る用途)"""
    with _temp_vault({f"f{i:02d}.md": "\n".join(["kw"] * (i + 1)) for i in range(15)}):
        assert len(_keyword_hits("kw", max_hits=3)) == 3
        top = _keyword_hits("kw", max_hits=3)[0]
        assert top["rel_path"] == "f14.md", f"最高スコアが先頭のはず: {top['rel_path']}"
        assert len(_keyword_hits("kw", max_hits=0)) == 15, "max_hits=0 は全件"
        assert len(_keyword_hits("kw", max_hits=None)) == 15, "max_hits=None は全件"
        print("✅ keyword: max_hits オーバーライド")


def test_vault_root_restored_after_test():
    """_temp_vault は VAULT_ROOT を必ず復元する (runner非依存 = pytest実行でも漏れない)"""
    before = tool_obsidian.VAULT_ROOT
    try:
        with _temp_vault({"x.md": "kw\n"}):
            assert tool_obsidian.VAULT_ROOT != before, "差し替わっていない"
            raise RuntimeError("boom")  # 例外離脱でも復元されること
    except RuntimeError:
        pass
    assert tool_obsidian.VAULT_ROOT == before, "VAULT_ROOT が復元されていない (他テストへ汚染)"
    print("✅ keyword: VAULT_ROOT が例外時も復元される")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
