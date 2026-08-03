# -*- coding: utf-8 -*-
"""_hybrid_hits の「3本とも注入する」契約が tests/ で守られているかの静的検査。

守るもの:
  [1] tests/ の中の _hybrid_hits 呼び出しが、keyword_fn / semantic_fn /
      activity_keyword_fn の3本すべてを塞いでいること。
  [2] 3本を塞ぐヘルパ (_hits3) 自身が、本当に3本とも setdefault していること。
  [3] 検出器の両方向 — 2本しか渡さない呼び出しを赤にし、
      3本渡す呼び出しと転送ヘルパを緑にすること。

なぜ要るか (2026-08-04 に実際に踏んだ):
  `tools/tool_vault_hybrid._hybrid_hits` の docstring には「ランクリストは
  keyword / semantic / activity の3本ある。None のものは実データに解決される
  ので、隔離したいテストは3本とも渡すこと」と**既に書いてあった**。それでも
  test_multiquery_rerank.py の6箇所と test_rerank_backend.py の1箇所が2本しか
  渡しておらず、行動ログの実インデックス (logs/vault_index.db) が混ざっていた。

  症状が厄介なのは、行動ログが**エージェントを使うたびに行が増える**こと。
  新規チェックアウトでは行動ログが空なので緑になり、開発機でだけ赤くなる。
  実際 2026-08-04 に `tool_web_search_grounded` の実行実績が1件記録された
  時点で `test_search_survives_dead_ollama` が落ち始めた。CLAUDE.md の
  「テストが本番と共有する可変資源は必ず一時パスへ隔離する」と同じ型で、
  さらに「CI と実機の失敗一覧が一致しない」の原因にもなる
  (CI は clone 直後なので永久に緑)。

  **docstring に書いた規約は、書いただけでは守られない**。この検査はその規約を
  機械で強制するために置く。文字列走査ではなく ast で書くのは、この docstring
  自体が違反の形 (`_hybrid_hits(` + 2本だけの引数名) を含むため。

実行: py tests/test_hybrid_injection_contract.py
"""
import ast
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

TESTS_DIR = os.path.join(_PROJECT_ROOT, "tests")
TARGET = "_hybrid_hits"
REQUIRED = ("keyword_fn", "semantic_fn", "activity_keyword_fn")

_PASS = []
_FAIL = []


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------------------------
# ast ヘルパ
# ---------------------------------------------------------------------------
def _call_name(node):
    """Call ノードの呼び先の名前 (`mod.f()` も `f()` も末尾セグメントで返す)。"""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return getattr(fn, "id", None)


def _setdefault_keys(func_node):
    """関数本体にある `<x>.setdefault("key", ...)` の key 集合を返す。"""
    keys = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and _call_name(node) == "setdefault":
            if node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    keys.add(node.args[0].value)
    return keys


def violations(src, path="<src>"):
    """2本以下しか塞いでいない _hybrid_hits 呼び出しを (lineno, 不足) で返す。

    転送ヘルパ (本体で3本とも setdefault してから `_hybrid_hits(**kw)` を
    呼ぶ形) は違反にしない — それが規約を満たすための正しい書き方だから。
    """
    tree = ast.parse(src, path)

    # 各 Call を「直近で囲んでいる関数」に1度だけ結び付ける。
    # ast.walk を関数ごとに回すと、入れ子の呼び出しを外側と内側で二重に
    # 数えて偽陽性が出る (最初の実装で実際に出した)。
    out = []

    def visit(node, enclosing_keys):
        keys = enclosing_keys
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            keys = _setdefault_keys(node)
        if isinstance(node, ast.Call) and _call_name(node) == TARGET:
            passed = {kw.arg for kw in node.keywords if kw.arg}
            # `**kw` 転送は kw.arg が None になる。囲む関数の setdefault で補う
            if any(kw.arg is None for kw in node.keywords):
                passed |= keys
            missing = [k for k in REQUIRED if k not in passed]
            if missing:
                out.append((node.lineno, tuple(missing)))
        for child in ast.iter_child_nodes(node):
            visit(child, keys)

    visit(tree, set())
    return sorted(out)


# ---------------------------------------------------------------------------
# [1] tests/ の実ファイル
# ---------------------------------------------------------------------------
def test_all_test_files_inject_three():
    print("\n[1] tests/ の _hybrid_hits 呼び出しが3本とも塞いでいる")
    scanned = 0
    for name in sorted(os.listdir(TESTS_DIR)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(TESTS_DIR, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if TARGET not in src:
            continue
        scanned += 1
        bad = violations(src, path)
        detail = "; ".join(f"{name}:{ln} -> {list(miss)}" for ln, miss in bad)
        check(f"{name} の全呼び出しが3本とも注入", not bad, detail)
    check("走査対象が1本以上ある (import 名の変更で空振りしていない)",
          scanned > 0, f"scanned={scanned}")


# ---------------------------------------------------------------------------
# [2] ヘルパ自身が3本とも塞いでいるか
# ---------------------------------------------------------------------------
def test_helpers_guard_all_three():
    print("\n[2] 転送ヘルパ _hits3 が3本とも setdefault している")
    found = 0
    for name in sorted(os.listdir(TESTS_DIR)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(TESTS_DIR, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "_hits3" not in src:
            continue
        tree = ast.parse(src, path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_hits3":
                found += 1
                keys = _setdefault_keys(node)
                missing = [k for k in REQUIRED if k not in keys]
                check(f"{name}: _hits3 が3本とも setdefault",
                      not missing, f"不足: {missing}")
    check("_hits3 の定義が1つ以上見つかる", found > 0, f"found={found}")


# ---------------------------------------------------------------------------
# [3] 検出器そのものの両方向
# ---------------------------------------------------------------------------
_BAD = '''
def t():
    hits, notes, q = _hybrid_hits(
        "質問", top_k=3,
        keyword_fn=lambda q: [],
        semantic_fn=lambda q, k: [])
'''

_GOOD_EXPLICIT = '''
def t():
    hits, notes, q = _hybrid_hits(
        "質問", top_k=3,
        keyword_fn=lambda q: [],
        semantic_fn=lambda q, k: [],
        activity_keyword_fn=lambda q, k: [])
'''

_GOOD_HELPER = '''
def _hits3(*args, **kw):
    kw.setdefault("keyword_fn", lambda q: [])
    kw.setdefault("semantic_fn", lambda q, k: [])
    kw.setdefault("activity_keyword_fn", lambda q, k: [])
    return _hybrid_hits(*args, **kw)
'''

_GOOD_HELPER_PARTIAL = '''
def _hits3(*args, **kw):
    kw.setdefault("semantic_fn", lambda q, k: [])
    return _hybrid_hits(*args, **kw)
'''


def test_detector_both_directions():
    print("\n[3] 検出器の両方向 (違反を赤に・正しい書き方を緑に)")
    bad = violations(_BAD)
    check("2本だけの呼び出しを違反として報告する", len(bad) == 1, str(bad))
    check("不足しているキー名を具体的に挙げる",
          bad and "activity_keyword_fn" in bad[0][1], str(bad))
    check("3本を明示した呼び出しは緑", violations(_GOOD_EXPLICIT) == [],
          str(violations(_GOOD_EXPLICIT)))
    check("3本を setdefault する転送ヘルパは緑", violations(_GOOD_HELPER) == [],
          str(violations(_GOOD_HELPER)))
    check("1本しか setdefault しない転送ヘルパは赤 (素通ししない)",
          len(violations(_GOOD_HELPER_PARTIAL)) == 1,
          str(violations(_GOOD_HELPER_PARTIAL)))
    # この検査自身が ast であることの確認: docstring 中の違反の形を拾わない
    doc_only = 'x = "_hybrid_hits(keyword_fn=1, semantic_fn=2)"\n'
    check("文字列リテラル中の違反の形は誤検知しない (ast である証拠)",
          violations(doc_only) == [], str(violations(doc_only)))


if __name__ == "__main__":
    test_all_test_files_inject_three()
    test_helpers_guard_all_three()
    test_detector_both_directions()
    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
