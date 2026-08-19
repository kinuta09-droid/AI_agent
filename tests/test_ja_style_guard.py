# -*- coding: utf-8 -*-
"""lib/ja_style_guard.py (B系統: プロジェクトローカルの文体チェック) のテスト。

対象は B系統 (lib/ja_style_guard.py) のみ。A系統 (グローバル
~/.claude/hooks/guard_ja_style.py) はこのリポジトリ外にあり、このリポジトリの
テストスイープの対象外なので扱わない。

隔離方針:
  - google.genai / tavily / requests をスタブし、実APIには一切触れない
    (tests/test_create_note.py の _install_module_stubs パターンを踏襲)。
  - cost_guard.GEMINI_LIGHT_COUNTER / GEMINI_HEAVY_COUNTER を一時ディレクトリへ
    差し替える。enforce_style のテストでは lib.ja_style_guard.client.models.
    generate_content 自体もカウント用スタブへ直接差し替えるため、実際には
    cost_guard を経由しない (二重の安全策)。

実行: py tests/test_ja_style_guard.py
"""
import json
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# 外部依存のスタブ (lib.lib_shared の import より前に入れる)
# ---------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeModels:
    def generate_content(self, model=None, contents=None, config=None):
        return types.SimpleNamespace(text="stub")


class _FakeGenaiClient:
    def __init__(self, **kw):
        self.models = _FakeModels()


class _FakeTavily:
    def __init__(self, **kw):
        pass

    def search(self, **kw):
        return {"results": []}


def _install_module_stubs():
    genai = _stub("google.genai", Client=_FakeGenaiClient)
    _stub("google.genai.types", HttpOptions=lambda **kw: None)
    genai.types = sys.modules["google.genai.types"]
    google_mod = _stub("google")
    google_mod.genai = genai
    _stub("tavily", TavilyClient=_FakeTavily)
    _stub("requests")
    os.environ.setdefault("GOOGLE_API_KEY", "stub")
    os.environ.setdefault("TAVILY_API_KEY", "stub")


_install_module_stubs()

import lib.ja_style_guard as jsg  # noqa: E402
from lib import cost_guard  # noqa: E402


def _isolate_gemini_counter() -> str:
    """本番と共有の日次カウンタから切り離す (tests/test_create_note.py と同型)。

    LIGHT/HEAVY 両方を差し替える — 片方だけだと使われなかった方の経路だけ
    本番ファイルを汚染し続ける抜け穴になる。
    """
    d = tempfile.mkdtemp(prefix="ja_style_guard_counter_")
    for attr, filename in (
        ("GEMINI_LIGHT_COUNTER", "gemini_light_daily_calls.json"),
        ("GEMINI_HEAVY_COUNTER", "gemini_heavy_daily_calls.json"),
    ):
        setattr(cost_guard, attr, cost_guard.DailyCounter(
            path=os.path.join(d, filename),
            limit=0,  # 0 = ガード無効
            name=f"Gemini API (test:{attr})",
        ))
    return d


_COUNTER_DIR = _isolate_gemini_counter()

# 本番の日次カウンタファイルが増えていないことを確認するための事前スナップショット
_PROD_LIGHT_PATH = os.path.join(_PROJECT_ROOT, "logs", "gemini_light_daily_calls.json")
_PROD_HEAVY_PATH = os.path.join(_PROJECT_ROOT, "logs", "gemini_heavy_daily_calls.json")
_prod_snapshot = {
    p: (os.path.getmtime(p) if os.path.exists(p) else None)
    for p in (_PROD_LIGHT_PATH, _PROD_HEAVY_PATH)
}

# 未解消ログも本番と共有の logs/ から切り離す (同じ理由で isolate)
_UNRESOLVED_LOG_DIR = tempfile.mkdtemp(prefix="ja_style_guard_unresolved_")
jsg.JA_STYLE_UNRESOLVED_LOG = os.path.join(_UNRESOLVED_LOG_DIR, "ja_style_unresolved.jsonl")
_PROD_UNRESOLVED_LOG_PATH = os.path.join(_PROJECT_ROOT, "logs", "ja_style_unresolved.jsonl")
_prod_unresolved_snapshot = (
    os.path.getmtime(_PROD_UNRESOLVED_LOG_PATH) if os.path.exists(_PROD_UNRESOLVED_LOG_PATH) else None
)


class _CountingStub:
    """generate_content の呼び出し回数を数えつつ、固定の応答テキストを返す。"""

    def __init__(self, response_text: str):
        self.calls = 0
        self._response_text = response_text

    def __call__(self, model=None, contents=None, config=None):
        self.calls += 1
        return types.SimpleNamespace(text=self._response_text)


_ORIGINAL_GENERATE_CONTENT = jsg.client.models.generate_content


def _run_enforce_with_stub(text, prompt, response_text, max_retries=1):
    stub = _CountingStub(response_text)
    jsg.client.models.generate_content = stub
    try:
        result = jsg.enforce_style(text, prompt, model="dummy-model", max_retries=max_retries)
    finally:
        jsg.client.models.generate_content = _ORIGINAL_GENERATE_CONTENT
    return result, stub


# ---------------------------------------------------------
# [1] load_dictionary: 実ファイルから正しく読める
# ---------------------------------------------------------
_dictionary = jsg.load_dictionary()
check("[1] load_dictionary: entries が非空", len(_dictionary.get("entries", [])) > 0, str(_dictionary))
check("[1] load_dictionary: aggregate_checks が非空", len(_dictionary.get("aggregate_checks", [])) > 0, str(_dictionary))
_expected_ids = {
    "vague-effect-verb", "negation-lead", "ai-cliche-hype",
    "format-clash", "literal-translation", "redundant-predicate",
    "preview-and-summary-cliche", "empty-adjective", "empty-verb-llm",
    "weak-hedge-llm", "posture-without-content",
}
_actual_ids = {e["id"] for e in _dictionary.get("entries", [])}
check("[1] load_dictionary: 既定entry id が揃っている", _expected_ids <= _actual_ids, str(_actual_ids))

# ---------------------------------------------------------
# [2] load_dictionary: 存在しないパスでも例外を出さず fail-safe
# ---------------------------------------------------------
_missing = jsg.load_dictionary(path=r"C:\this\path\does\not\exist\dict.json")
check(
    "[2] load_dictionary: 存在しないpathでfail-safe空辞書",
    _missing == {"entries": [], "aggregate_checks": [], "manual_notes": []},
    str(_missing),
)

# ---------------------------------------------------------
# [3] check_style: 既知の悪文で複数違反が検出される
# ---------------------------------------------------------
_bad_text = "このツールは効きます。単純な連携ではなく統合的な基盤である。"
_bad_violations = jsg.check_style(_bad_text, _dictionary)
_bad_ids = {v.id for v in _bad_violations}
check("[3] check_style: vague-effect-verb 検出", "vague-effect-verb" in _bad_ids, str(_bad_ids))
check("[3] check_style: negation-lead 検出", "negation-lead" in _bad_ids, str(_bad_ids))
check("[3] check_style: style-mixing 検出", "style-mixing" in _bad_ids, str(_bad_ids))

# ---------------------------------------------------------
# [3b] check_style: japanese-tech-writing由来の新規5entries + connector-spamの検出確認
#      (2026-08-17、gist:k16shikano/fd287c3133457c4fd8f5601d34aa817d より導入)
# ---------------------------------------------------------
_new_entry_cases = {
    "preview-and-summary-cliche": "重要なのはこの設計です。",
    "empty-adjective": "これは不可欠な機能です。",
    "empty-verb-llm": "この課題はまだ言語化していない。",
    "weak-hedge-llm": "これは非常に有効と言えるだろう。",
    "posture-without-content": "この課題に正面から扱う。",
}
for _entry_id, _case_text in _new_entry_cases.items():
    _case_ids = {v.id for v in jsg.check_style(_case_text, _dictionary)}
    check(f"[3b] check_style: {_entry_id} 検出", _entry_id in _case_ids, str(_case_ids))

_connector_spam_text = "さらに検証した。また別の観点も見た。加えて第三の観点も確認した。さらに念のため再検証した。"
_connector_spam_ids = {v.id for v in jsg.check_style(_connector_spam_text, _dictionary)}
check("[3b] check_style: connector-spam 検出", "connector-spam" in _connector_spam_ids, str(_connector_spam_ids))

# ---------------------------------------------------------
# [3c] check_style: 「触れる」「言及する」は empty-verb-llm を誤検知しない
#      (このリポジトリ自身の.claude/rules・skillsで精密な意味の正当な用例が実測されたため
#      entryから除外し manual_notes の llm-verb-context-dependent へ降格した回帰確認)
# ---------------------------------------------------------
_context_dependent_verb_text = "要約は失敗に触れるとは限らない。「実行する」と「言及する」を区別できない。"
_context_dependent_verb_ids = {v.id for v in jsg.check_style(_context_dependent_verb_text, _dictionary)}
check(
    "[3c] check_style: 触れる/言及するでempty-verb-llm誤検知しない",
    "empty-verb-llm" not in _context_dependent_verb_ids,
    str(_context_dependent_verb_ids),
)

# ---------------------------------------------------------
# [4] check_style: 正常な文では違反ゼロ
# ---------------------------------------------------------
_good_text = "このツールは処理速度が従来比30%向上します。"
_good_violations = jsg.check_style(_good_text, _dictionary)
check("[4] check_style: 正常文で違反ゼロ", _good_violations == [], str(_good_violations))

# ---------------------------------------------------------
# [5] check_style: 太字コロン単体 (絵文字なし) では format-clash を誤検知しない
#     (このセッションで実際に踏んだ回帰の再発防止ケース)
# ---------------------------------------------------------
_bold_colon_text = "**重要**: この項目は必須です。**注意**: 期限を守ること。"
_bold_colon_violations = jsg.check_style(_bold_colon_text, _dictionary)
_bold_colon_ids = {v.id for v in _bold_colon_violations}
check(
    "[5] check_style: 太字コロン単体でformat-clash誤検知しない",
    "format-clash" not in _bold_colon_ids,
    str(_bold_colon_ids),
)

# ---------------------------------------------------------
# [5b] check_style: 番号付きリストの語尾統一はsentence-ending-monotonyを誤検知しない
#      (logs/ja_style_unresolved.jsonl の実例 [AutoDesign paper-digest下書き] より)
# ---------------------------------------------------------
_list_text = (
    "具体的には、以下の3ステップで自動改善を行います。\n\n"
    "1. **実行と評価**: エージェントが生成した成果物を、人間やルールに基づく評価器が採点します。\n"
    "2. **コードの書き換え**: 評価結果を基に、コーディングエージェントが「設計基盤（ハーネス）」のコードを直接書き換えます。\n"
    "3. **適用判断**: 性能が向上する場合のみ、その更新をシステムに反映させます。"
)
_list_violations = jsg.check_style(_list_text, _dictionary)
check(
    "[5b] check_style: 番号付きリストでsentence-ending-monotony誤検知しない",
    not any(v.id == "sentence-ending-monotony" for v in _list_violations),
    str([v.id for v in _list_violations]),
)

# ---------------------------------------------------------
# [5c] check_style: リストでない通常のプロ文で3文連続する語尾は引き続き検出される
#      (5bの除外がsentence-ending-monotonyの検出そのものを潰していないことの回帰確認)
# ---------------------------------------------------------
_monotony_prose_text = "まず設定を確認します。次に依存関係を導入します。最後に起動を確認します。"
_monotony_prose_violations = jsg.check_style(_monotony_prose_text, _dictionary)
check(
    "[5c] check_style: リストでないプロ文の3文連続は引き続き検出される",
    any(v.id == "sentence-ending-monotony" for v in _monotony_prose_violations),
    str([v.id for v in _monotony_prose_violations]),
)

# ---------------------------------------------------------
# [6] enforce_style: 違反ゼロなら generate_content を呼ばず即返す
# ---------------------------------------------------------
_result6, _stub6 = _run_enforce_with_stub(_good_text, "元の指示", response_text="使われない")
check("[6] enforce_style: 違反ゼロならテキストをそのまま返す", _result6 == _good_text, _result6)
check("[6] enforce_style: 違反ゼロならgenerate_content呼び出し0回", _stub6.calls == 0, str(_stub6.calls))

# ---------------------------------------------------------
# [7] enforce_style: 違反あり、1回の書き直しで解消される場合
# ---------------------------------------------------------
_result7, _stub7 = _run_enforce_with_stub(_bad_text, "元の指示", response_text=_good_text, max_retries=1)
check("[7] enforce_style: 解消されればgenerate_content呼び出し1回", _stub7.calls == 1, str(_stub7.calls))
check("[7] enforce_style: 解消後は警告文言が付かない", "⚠️ 文体チェック未解消" not in _result7, _result7)
check("[7] enforce_style: 解消後はスタブの書き直し結果を返す", _result7 == _good_text, _result7)

# ---------------------------------------------------------
# [8] enforce_style: 違反あり、書き直しても解消しない場合 (max_retries=1)
# ---------------------------------------------------------
_result8, _stub8 = _run_enforce_with_stub(_bad_text, "元の指示", response_text=_bad_text, max_retries=1)
check("[8] enforce_style: max_retries=1なのでgenerate_content呼び出しは1回のみ", _stub8.calls == 1, str(_stub8.calls))
check("[8] enforce_style: 未解消時は警告文言が末尾に付記される", "⚠️ 文体チェック未解消:" in _result8, _result8)

with open(jsg.JA_STYLE_UNRESOLVED_LOG, encoding="utf-8") as f:
    _unresolved_records = [json.loads(line) for line in f if line.strip()]
_unresolved_ids = {r["id"] for r in _unresolved_records}
_bad_ids_for_log = {v.id for v in jsg.check_style(_bad_text)}
check(
    "[8] enforce_style: 未解消の実例がJSONLに記録される",
    _unresolved_ids == _bad_ids_for_log,
    f"{_unresolved_ids} != {_bad_ids_for_log}",
)
check(
    "[8] enforce_style: 記録されたmatchedが実際の該当文である(説明文でない)",
    all(r["matched"] and "3文以上連続" not in r["matched"] for r in _unresolved_records),
    str(_unresolved_records),
)

# ---------------------------------------------------------
# [9] カウンタ隔離の確認: 一時パスを指しており、本番ログファイルを汚染していない
# ---------------------------------------------------------
check(
    "[9] GEMINI_LIGHT_COUNTER が一時パスを指す",
    cost_guard.GEMINI_LIGHT_COUNTER.path.startswith(_COUNTER_DIR),
    cost_guard.GEMINI_LIGHT_COUNTER.path,
)
check(
    "[9] GEMINI_HEAVY_COUNTER が一時パスを指す",
    cost_guard.GEMINI_HEAVY_COUNTER.path.startswith(_COUNTER_DIR),
    cost_guard.GEMINI_HEAVY_COUNTER.path,
)
check(
    "[9] JA_STYLE_UNRESOLVED_LOG が一時パスを指す",
    jsg.JA_STYLE_UNRESOLVED_LOG.startswith(_UNRESOLVED_LOG_DIR),
    jsg.JA_STYLE_UNRESOLVED_LOG,
)
check(
    "[9] 本番の未解消ログファイルが増減していない",
    (os.path.getmtime(_PROD_UNRESOLVED_LOG_PATH) if os.path.exists(_PROD_UNRESOLVED_LOG_PATH) else None)
    == _prod_unresolved_snapshot,
    f"before={_prod_unresolved_snapshot}",
)
_prod_after = {
    p: (os.path.getmtime(p) if os.path.exists(p) else None)
    for p in (_PROD_LIGHT_PATH, _PROD_HEAVY_PATH)
}
check(
    "[9] 本番の日次カウンタファイルが増減していない",
    _prod_after == _prod_snapshot,
    f"before={_prod_snapshot} after={_prod_after}",
)


# ---------------------------------------------------------
print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
if _FAIL:
    for f in _FAIL:
        print(f"  FAILED: {f}")
sys.exit(1 if _FAIL else 0)
