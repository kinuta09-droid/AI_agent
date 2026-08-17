# -*- coding: utf-8 -*-
"""search_and_save_topic の保存経路の回帰テスト

このテストが存在する理由 (2026-07-26):
  /curate が「Knowledge_Base に作成した」と表示したのに Obsidian にノートが
  現れない、という報告があった。調査の結果、原因は書き込み失敗ではなく:
    1. Google ドライブ上の Vault では外部プロセスの書き込みが Obsidian の
       ファイル監視に載らず、開くまでインデックスされない。tool_create_note は
       書き込み後に run_obsidian_cli(["open", ...]) を叩いていたが、Curator は
       叩いていなかった (この非対称性が症状の正体)。
    2. 返り値の [[Knowledge_Base/...]] が Vault ルート基準になっておらず
       (実体は 30_Main/Knowledge_Base/...)、Obsidian 上で未解決リンクになる。
       Chatlog に残るリンクをクリックすると「新規作成」ダイアログが出るため、
       「作成されていない」ように見えていた。

Gemini / Tavily / Obsidian CLI は全てスタブ。Vault は一時ディレクトリ。
cost_guard の日次カウンタも一時ファイルへ逃がす (理由は _isolate_gemini_counter)。
外部通信も Vault への書き込みも一切しない。

実行: py tests/test_curate_note_save.py
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
# ---------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeModels:
    """generate_content の戻り値を順番に払い出す。"""

    def __init__(self):
        self.responses = []

    def generate_content(self, model=None, contents=None):
        text = self.responses.pop(0) if self.responses else "STUB"
        return types.SimpleNamespace(text=text)


class _FakeGenaiClient:
    def __init__(self, **kw):
        self.models = _FakeModels()


class _FakeTavily:
    def __init__(self, **kw):
        self.results = []

    def search(self, **kw):
        return {"results": self.results}


def _install_module_stubs():
    _stub("dotenv", load_dotenv=lambda *a, **k: None)
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

import lib.lib_shared as ls  # noqa: E402
import tools.tool_obsidian as tobs  # noqa: E402
import tools.tool_curator as tc  # noqa: E402


def _isolate_gemini_counter():
    """本番と共有の日次カウンタから切り離す。

    なぜ必要か (2026-07-30 に実際に踏んだ):
      lib_shared は client.models.generate_content を予算チェックで包むので
      (lib_shared.py:302-308)、google.genai をスタブしても
      cost_guard.GEMINI_LIGHT_COUNTER/GEMINI_HEAVY_COUNTER への加算は必ず走る。
      その参照先は **本番の**
      logs/gemini_light_daily_calls.json / gemini_heavy_daily_calls.json を見ている。
      さらに上の _install_module_stubs() が dotenv をスタブするため
      cost_guard の load_dotenv が無効化され、.env の
      DAILY_GEMINI_LIGHT_LIMIT/HEAVY_LIMIT=0 (無効) が読まれず既定値 200 に落ちる。
      結果このテストは **本番のカウントが200を超えた時刻以降だけ赤くなる** =
      同じコードが朝は緑・夜は赤という状態だった (実測: count 212 で再現)。

      lib_shared側はこの2つをモジュールグローバルとして呼び出し時に引くので、
      ここで両方差し替えれば経路全体に効く (2026-08-17: LIGHT/HEAVY分離に伴い、
      片方だけ差し替えると使われなかった方の経路だけ本番ファイルを汚染し続ける
      という新しい抜け穴になるため両方必須)。
    """
    from lib import cost_guard
    counter_dir = tempfile.mkdtemp(prefix="curate_counter_")
    for attr, filename in (
        ("GEMINI_LIGHT_COUNTER", "gemini_light_daily_calls.json"),
        ("GEMINI_HEAVY_COUNTER", "gemini_heavy_daily_calls.json"),
    ):
        setattr(cost_guard, attr, cost_guard.DailyCounter(
            path=os.path.join(counter_dir, filename),
            limit=0,  # 0 = ガード無効。予算超過はここで演じない (test_cost_guard の担当)
            name=f"Gemini API (test:{attr})",
        ))
    return counter_dir


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

## リンク
### 参照元
-
### 関連ノート
- [[ ]]
'''


class _Vault:
    """一時 Vault を作り、tool_curator の定数を丸ごとそこへ向ける。"""

    def __init__(self, with_template: bool = True):
        self.root = tempfile.mkdtemp(prefix="curate_vault_")
        self.calls = []          # run_obsidian_cli の呼び出し記録
        self.cli_error = None    # 例外を注入したいときにセット
        self._with_template = with_template

    def __enter__(self):
        for mod in (ls, tobs, tc):
            mod.WORK_DIR = self.root
            mod.VAULT_ROOT = self.root
        tc.KB_DIR = os.path.join(self.root, "30_Main", "Knowledge_Base")
        tc.TEMPLATE_PATH = os.path.join(
            self.root, "99_Assets", "Templates", "Tpl_Permanent.md")
        if self._with_template:
            os.makedirs(os.path.dirname(tc.TEMPLATE_PATH), exist_ok=True)
            with open(tc.TEMPLATE_PATH, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)

        def _fake_cli(args, **kw):
            self.calls.append(list(args))
            if self.cli_error:
                raise self.cli_error
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        tc.run_obsidian_cli = _fake_cli
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def save(self, topic: str, **kw) -> str:
        """外部APIの戻り値を仕込んでから search_and_save_topic を実行する。"""
        tc.tavily_client.results = [
            {"title": "T1", "url": "https://example.com/1", "content": "c1"}
        ]
        tc.client.models.responses = [
            "## 💡 3行でわかる要点\n- 本文\n\n## 📚 References\n- [T1](https://example.com/1)",
            "要約テキスト",
            "MOC_AI",
        ]
        return tc.search_and_save_topic(topic, **kw)

    def kb_files(self) -> list:
        kb = tc.KB_DIR
        return sorted(os.listdir(kb)) if os.path.isdir(kb) else []


def _wikilink(result: str) -> str:
    return result.split("[[")[1].split("]]")[0]


# ---------------------------------------------------------
# 1. 成功時: 実ファイル・wikilink・CLI通知
# ---------------------------------------------------------

def test_success_path():
    print("\n[1] 成功時の保存・リンク・Obsidian通知")
    with _Vault() as v:
        result = v.save("マルチエージェント設計")

        check("Success を返す", result.startswith("Success:"), result)
        check("ノートが1件だけ作られる", len(v.kb_files()) == 1, str(v.kb_files()))

        # (Path: ...) の絶対パスが実在すること
        abs_path = result.split("(Path:")[1].rstrip(")").strip()
        check("(Path:) の絶対パスが実在する", os.path.exists(abs_path), abs_path)

        # wikilink が Vault ルート基準で解決できること = Obsidian が辿れること
        link = _wikilink(result)
        resolved = os.path.join(v.root, *link.split("/"))
        check("wikilink が Vault ルート基準で解決できる",
              os.path.exists(resolved), f"link={link} resolved={resolved}")
        check("wikilink に 30_Main/ が含まれる",
              link.startswith("30_Main/Knowledge_Base/"), link)

        # Obsidian にファイルを掴ませる CLI が呼ばれること
        check("run_obsidian_cli が呼ばれる", len(v.calls) == 1, str(v.calls))
        if v.calls:
            args = v.calls[0]
            check("CLI 引数が open + Vault相対パス",
                  args[0] == "open" and args[1] == link, str(args))
            check("CLI パスに区切りのバックスラッシュが混ざらない",
                  "\\" not in args[1], str(args))


# ---------------------------------------------------------
# 2. CLI が落ちてもノート作成は成功のまま (@retry の重複生成を防ぐ)
# ---------------------------------------------------------

def test_cli_failure_is_swallowed():
    print("\n[2] Obsidian CLI 失敗時もノート作成は成功扱い")
    with _Vault() as v:
        v.cli_error = FileNotFoundError("Obsidian.exe not found")
        result = v.save("CLI が無い環境のトピック")

        check("Success を返す (例外を外に出さない)",
              result.startswith("Success:"), result)
        check("ノートは1件だけ (retry で重複生成しない)",
              len(v.kb_files()) == 1, str(v.kb_files()))
        check("「既にノートが存在します」に化けない",
              "既にノート" not in result, result)


# ---------------------------------------------------------
# 3. テンプレート不在でも保存・リンク・通知が成立する
# ---------------------------------------------------------

def test_without_template():
    print("\n[3] テンプレート不在時のフォールバック")
    with _Vault(with_template=False) as v:
        result = v.save("テンプレなしトピック")

        check("Success を返す", result.startswith("Success:"), result)
        link = _wikilink(result)
        check("wikilink は Vault ルート基準",
              os.path.exists(os.path.join(v.root, *link.split("/"))), link)
        check("run_obsidian_cli が呼ばれる", len(v.calls) == 1, str(v.calls))


# ---------------------------------------------------------
# 4. 同名既存: 上書きせず、CLI も叩かない
# ---------------------------------------------------------

def test_duplicate_same_day():
    print("\n[4] 同日・同名の2回目")
    with _Vault() as v:
        first = v.save("重複トピック")
        check("1回目は Success", first.startswith("Success:"), first)
        calls_after_first = len(v.calls)

        second = v.save("重複トピック")
        check("2回目は Success を返さない",
              not second.startswith("Success:"), second)
        check("2回目は「既にノートが存在します」",
              "既にノートが存在します" in second, second)
        check("ノートは1件のまま", len(v.kb_files()) == 1, str(v.kb_files()))
        check("2回目は CLI を叩かない",
              len(v.calls) == calls_after_first, str(v.calls))


# ---------------------------------------------------------
# 5. ファイル名に使えない文字を含むタイトル
# ---------------------------------------------------------

def test_unsafe_title():
    print("\n[5] ファイル名に使えない文字を含むタイトル")
    with _Vault() as v:
        result = v.save('AI: 未来/展望*?"<>|')

        check("Success を返す", result.startswith("Success:"), result)
        link = _wikilink(result)
        check("wikilink が解決できる",
              os.path.exists(os.path.join(v.root, *link.split("/"))), link)
        name = os.path.basename(link)
        check("禁止文字が残っていない",
              not any(c in name for c in '\\/:*?"<>|'), name)
        check("KB 直下に1件 (パス脱出していない)",
              len(v.kb_files()) == 1, str(v.kb_files()))


def main() -> int:
    print("=" * 60)
    print("search_and_save_topic 保存経路テスト")
    print("=" * 60)
    for fn in (test_success_path, test_cli_failure_is_swallowed,
               test_without_template, test_duplicate_same_day,
               test_unsafe_title):
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
