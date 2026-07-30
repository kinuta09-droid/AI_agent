# -*- coding: utf-8 -*-
"""tool_create_note の保存パス regression テスト

もともとの目的 (パス結合の退行検出) は維持しつつ、2026-07-30 に密閉化した。

### なぜ書き換えたか

旧実装は `tool_create_note("30_Main/TestNote.md", ...)` を **本番の Vault**
(`D:\\マイドライブ\\Obsidian`・パス直書き) に対して実行していた。結果:

  1. **実 Vault にノートが残り続けた。** tool_obsidian は同名ファイルがあると
     タイムスタンプ接尾辞で退避する仕様 (tool_obsidian.py:204-209) なので、
     上書きではなく **スイープ1周ごとに1件増える**。2026-07-30 の調査時点で
     `30_Main/TestNote.md` + `TestNote_HHMMSS.md` が18件蓄積していた。
  2. **実 Gemini を呼んでいた。** テンプレートがある経路は
     `_determine_parent_moc()` で MOC 分類を行う。日次カウンタが1周ごとに
     +1 されており、「テストは課金ゼロ」が成立していなかった。
  3. 判定が print だけで **終了コードに反映されていなかった** (常に exit 0)。
  4. Vault ルートを直書きしていたため、このマシン以外では必ず失敗した。

いまは一時 Vault + スタブで、外部通信も本番 Vault への書き込みも一切しない。
そのため CI でも実行でき、`ARMS_LIVE_TESTS` ゲートは不要 (副作用が無いので)。

実行: py tests/test_create_note.py
"""
import os
import shutil
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
#
# dotenv は **あえてスタブしない**。スタブすると cost_guard の load_dotenv が
# 無効化され、.env の DAILY_GEMINI_CALL_LIMIT が読まれず既定値 200 に落ちる。
# カウンタは下の _isolate_gemini_counter() で一時ファイルへ逃がすので、
# 上限値が 0 でも 200 でもこのテストの結果は変わらない。
# ---------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, model=None, contents=None, config=None):
        self.calls += 1
        return types.SimpleNamespace(text="MOC_AI")


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

import tools.tool_obsidian as tobs  # noqa: E402


def _isolate_gemini_counter() -> str:
    """本番と共有の日次カウンタ (logs/gemini_daily_calls.json) から切り離す。

    lib_shared は client.models.generate_content を予算チェックで包むので
    (lib_shared.py:302-308)、google.genai をスタブしても
    cost_guard.check_and_increment() は必ず走り、本番のカウントを進めてしまう。
    cost_guard.check_and_increment() はモジュールグローバルを呼び出し時に引くため、
    ここで差し替えれば経路全体に効く。
    """
    from lib import cost_guard
    d = tempfile.mkdtemp(prefix="create_note_counter_")
    cost_guard.GEMINI_COUNTER = cost_guard.DailyCounter(
        path=os.path.join(d, "gemini_daily_calls.json"),
        limit=0,  # 0 = ガード無効。予算超過の検証は test_cost_guard の担当
        name="Gemini API (test)",
    )
    return d


_COUNTER_DIR = _isolate_gemini_counter()

TEMPLATE = '''---
up: "[[00_Inbox]]"
tags:
  - type/permanent
created: {{date}}
---
# {{title}}

> このノートが言いたいことを1〜3行で書く。

## 本文
- ここに詳細を書く
'''


class _Vault:
    """一時 Vault を作り、tool_obsidian の定数をそこへ向ける。"""

    def __init__(self, with_template: bool = True):
        self.root = tempfile.mkdtemp(prefix="create_note_vault_")
        self._with_template = with_template
        self.cli_calls = []

    def __enter__(self):
        self._saved_work_dir = tobs.WORK_DIR
        self._saved_cli = tobs.run_obsidian_cli
        tobs.WORK_DIR = self.root
        if self._with_template:
            tpl = os.path.join(self.root, '99_Assets', 'Templates', 'Tpl_Permanent.md')
            os.makedirs(os.path.dirname(tpl), exist_ok=True)
            with open(tpl, 'w', encoding='utf-8') as f:
                f.write(TEMPLATE)

        def _fake_cli(args, **kw):
            # 本物は Obsidian を起動する。テストでは呼ばれた事実だけ記録する。
            self.cli_calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        tobs.run_obsidian_cli = _fake_cli
        return self

    def __exit__(self, *exc):
        tobs.WORK_DIR = self._saved_work_dir
        tobs.run_obsidian_cli = self._saved_cli
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def main_files(self) -> list:
        d = os.path.join(self.root, '30_Main')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ---------------------------------------------------------
# 1. パス結合: 30_Main/ 付きで渡しても二重フォルダを作らない
# ---------------------------------------------------------

def test_no_double_folder():
    print("\n[1] '30_Main/TestNote.md' を渡しても二重フォルダを作らない")
    with _Vault() as v:
        result = tobs.tool_create_note("30_Main/TestNote.md", "Test Content")
        check("Error を返さない", not result.startswith("Error"), result)
        check("30_Main/TestNote.md が作られる",
              os.path.exists(os.path.join(v.root, '30_Main', 'TestNote.md')),
              str(v.main_files()))
        check("30_Main/30_Main/ を作らない",
              not os.path.exists(os.path.join(v.root, '30_Main', '30_Main')),
              "二重フォルダが出来ている")
        check("Obsidian CLI に open を通知する",
              any('open' in c for c in v.cli_calls), str(v.cli_calls))


# ---------------------------------------------------------
# 2. 同名衝突: 上書きせずタイムスタンプ接尾辞で退避する
#    (この仕様が、旧テストで実 Vault に18件溜めた原因そのもの)
# ---------------------------------------------------------

def test_duplicate_gets_suffix():
    print("\n[2] 同名は上書きせず接尾辞で退避する")
    with _Vault() as v:
        tobs.tool_create_note("TestNote.md", "one")
        tobs.tool_create_note("TestNote.md", "two")
        files = v.main_files()
        check("2件になる (上書きしていない)", len(files) == 2, str(files))
        check("元の名前が残る", "TestNote.md" in files, str(files))
        check("2件目に接尾辞が付く",
              any(f.startswith("TestNote_") for f in files), str(files))


# ---------------------------------------------------------
# 3. テンプレート無しでも書ける
# ---------------------------------------------------------

def test_without_template():
    print("\n[3] テンプレートが無くても本文をそのまま書く")
    with _Vault(with_template=False) as v:
        tobs.tool_create_note("Plain.md", "本文だけ")
        path = os.path.join(v.root, '30_Main', 'Plain.md')
        check("ファイルが作られる", os.path.exists(path), str(v.main_files()))
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                body = f.read()
            check("本文が入っている", "本文だけ" in body, body[:80])


# ---------------------------------------------------------
# 4. 本番の Vault とカウンタに触っていないこと
# ---------------------------------------------------------

def test_no_production_side_effects():
    print("\n[4] 本番 Vault と日次カウンタに触らない")
    from lib import cost_guard
    check("日次カウンタが一時パスを向いている",
          _COUNTER_DIR in str(cost_guard.GEMINI_COUNTER.path),
          str(cost_guard.GEMINI_COUNTER.path))
    check("tool_obsidian.WORK_DIR が復元されている",
          tobs.WORK_DIR != _COUNTER_DIR and os.path.isabs(str(tobs.WORK_DIR)),
          str(tobs.WORK_DIR))
    # Gemini は必ずスタブ経由。実クライアントなら型名が違う
    import lib.lib_shared as ls
    check("Gemini クライアントがスタブである",
          type(ls.client).__name__ == "_FakeGenaiClient",
          type(ls.client).__name__)


def main() -> int:
    print("=" * 60)
    print("tool_create_note 保存パステスト (密閉)")
    print("=" * 60)
    for fn in (test_no_double_folder, test_duplicate_gets_suffix,
               test_without_template, test_no_production_side_effects):
        fn()

    print("\n" + "=" * 60)
    print(f"PASS {len(_PASS)} / FAIL {len(_FAIL)}")
    print("=" * 60)
    for f in _FAIL:
        print(f"  ❌ {f}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_COUNTER_DIR, ignore_errors=True)
    sys.exit(code)
