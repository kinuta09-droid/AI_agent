# -*- coding: utf-8 -*-
"""
tool_jev.py (Jev/TypeSafe AI クライアント) の回帰テスト。

方針:
  - ネットワークに一切出ない。_jev_request をスタブに差し替える。
  - JEV_COUNTER は一時ディレクトリ上のカウンタに差し替え、本番の
    logs/jev_daily_calls.json には一切触れない (notify=False で実Discord投稿も防ぐ、
    known-bug-patterns.md Category C: DailyCounter の既定 notify=True を潰し忘れると
    テストのたびに実通知が飛ぶ)。

  ARMS_LIVE_TESTS=1 かつ実 JEV_API_KEY/JEV_API_BASE_URL が .env に揃っているときだけ、
  実APIとの疎通確認テストも実行する (既定はSKIP)。このテストが
  「サンドボックスから検証できなかった _jev_request の配線」をユーザー実機で
  検証する役目を兼ねる。

実行: py tests/test_tool_jev.py
"""
import os
import sys
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_jev as jev
from lib import cost_guard
from lib.cost_guard import DailyCounter, GeminiBudgetExceeded

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


class _TempCounter:
    """cost_guard.JEV_COUNTER を一時ディレクトリ上のカウンタへ差し替え、必ず戻す。"""

    def __init__(self, limit: int = 1000):
        self.limit = limit
        self.root = None
        self.orig = None

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="jev_test_")
        self.orig = cost_guard.JEV_COUNTER
        cost_guard.JEV_COUNTER = DailyCounter(
            path=os.path.join(self.root, "jev_counter.json"),
            limit=self.limit, name="Jev API (test)", notify=False,
        )
        return cost_guard.JEV_COUNTER

    def __exit__(self, *exc):
        cost_guard.JEV_COUNTER = self.orig
        shutil.rmtree(self.root, ignore_errors=True)


class _TempConfig:
    """JEV_API_KEY / JEV_API_BASE_URL を一時的に差し替え、必ず戻す。"""

    def __init__(self, key: str = "test-key", base_url: str = "https://example.invalid"):
        self.key = key
        self.base_url = base_url
        self._orig_key = None
        self._orig_base = None

    def __enter__(self):
        self._orig_key = jev.JEV_API_KEY
        self._orig_base = jev.JEV_API_BASE_URL
        jev.JEV_API_KEY = self.key
        jev.JEV_API_BASE_URL = self.base_url
        return self

    def __exit__(self, *exc):
        jev.JEV_API_KEY = self._orig_key
        jev.JEV_API_BASE_URL = self._orig_base


# ---------------------------------------------------------
# 1. jev_choice / jev_score / jev_noul の正常系パース
# ---------------------------------------------------------

def test_jev_choice_parses_answer():
    print("\n[1] jev_choice: 正常系のパースと criteria の受け渡し")
    captured = {}

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        captured["args"] = (question_type, state, instructions, criteria)
        return {"choice": "high", "probability": 0.83}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_choice("state-text", "fit は?", ["high", "medium", "low"])
    finally:
        jev._jev_request = orig

    check("choice が正しくパースされる", result == {"choice": "high", "probability": 0.83}, str(result))
    check("question_type が choice", captured["args"][0] == "choice", str(captured))
    check("criteria がリストのまま渡る",
          captured["args"][3] == ["high", "medium", "low"], str(captured))


def test_jev_score_parses_answer_without_criteria():
    print("\n[2] jev_score: criteria省略時も正常にパースされる")

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        return {"score": 0.62, "confidence": 0.71}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_score("state-text", "興味との関連度は?")
    finally:
        jev._jev_request = orig

    check("score/confidence が正しくパースされる",
          result == {"score": 0.62, "confidence": 0.71}, str(result))


def test_jev_noul_returns_probability_not_boolean():
    print("\n[3] jev_noul: probability (float) を返す (boolean ではない)")

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        return {"probability": 0.42}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_noul("state-text", "目標は達成されたか")
    finally:
        jev._jev_request = orig

    check("probability が float で返る",
          isinstance(result["probability"], float) and result["probability"] == 0.42, str(result))
    check("boolean 型では返らない (公式ドキュメント通り確率のまま)",
          not isinstance(result["probability"], bool), str(result))


# ---------------------------------------------------------
# 4. 設定不備 → JevConfigError
# ---------------------------------------------------------

def test_missing_config_raises_jev_config_error():
    print("\n[4] JEV_API_KEY/JEV_API_BASE_URL 未設定 → JevConfigError")
    with _TempConfig(key="", base_url=""), _TempCounter():
        try:
            jev.jev_noul("s", "q")
            check("JevConfigError が送出される", False, "例外が発生しなかった")
        except jev.JevConfigError:
            check("JevConfigError が送出される", True)
        except Exception as e:
            check("JevConfigError が送出される", False, f"別の例外: {type(e).__name__}: {e}")


# ---------------------------------------------------------
# 5. コストガード上限到達 → ブロックされる (_jev_request は呼ばれない)
# ---------------------------------------------------------

def test_budget_exceeded_blocks_call_before_request():
    print("\n[5] JEV_COUNTER 上限到達 → GeminiBudgetExceeded で _jev_request は呼ばれない")
    called = {"n": 0}

    def fake_request(*a, **k):
        called["n"] += 1
        return {"probability": 0.9}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter(limit=1) as counter:
            counter.check_and_increment()  # 上限(1)を先に使い切る
            try:
                jev.jev_noul("s", "q")
                check("上限到達で例外になる", False, "例外が発生しなかった")
            except GeminiBudgetExceeded:
                check("上限到達で例外になる", True)
            check("_jev_request は呼ばれない", called["n"] == 0, str(called))
    finally:
        jev._jev_request = orig


# ---------------------------------------------------------
# 6. レスポンス形式不正 → JevAPIError
# ---------------------------------------------------------

def test_malformed_response_raises_jev_api_error():
    print("\n[6] レスポンスに answers.q が無い → JevAPIError")

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"unexpected": "shape"}

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp()

    orig_post = jev.requests.post
    jev.requests.post = fake_post
    try:
        with _TempConfig(), _TempCounter():
            try:
                jev.jev_noul("s", "q")
                check("不正形式で JevAPIError", False, "例外が発生しなかった")
            except jev.JevAPIError:
                check("不正形式で JevAPIError", True)
    finally:
        jev.requests.post = orig_post


def test_network_error_raises_jev_api_error():
    print("\n[7] ネットワーク例外 → JevAPIError にラップされる")

    def fake_post(url, json=None, headers=None, timeout=None):
        raise jev.requests.RequestException("connection refused")

    orig_post = jev.requests.post
    jev.requests.post = fake_post
    try:
        with _TempConfig(), _TempCounter():
            try:
                jev.jev_noul("s", "q")
                check("ネットワークエラーで JevAPIError", False, "例外が発生しなかった")
            except jev.JevAPIError:
                check("ネットワークエラーで JevAPIError", True)
    finally:
        jev.requests.post = orig_post


# ---------------------------------------------------------
# 8. 実APIとの疎通確認 (ARMS_LIVE_TESTS=1 のときだけ)
# ---------------------------------------------------------

def test_live_jev_noul():
    print("\n[8] (live) 実 Jev API との疎通確認")
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("  ⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 JEV_API_KEY/JEV_API_BASE_URL のときだけ実行します")
        return
    if not jev.JEV_API_KEY or not jev.JEV_API_BASE_URL:
        print("  ⏭️ SKIP: JEV_API_KEY/JEV_API_BASE_URL が .env に未設定です")
        return
    try:
        result = jev.jev_noul("today is sunny", "is it a good day for a walk")
        check("live: probability が 0..1 の範囲",
              0.0 <= result["probability"] <= 1.0, str(result))
    except jev.JevAPIError as e:
        check("live: 疎通確認", False,
              f"実API呼び出しに失敗 — _jev_request のフィールド名がずれている可能性: {e}")


if __name__ == "__main__":
    print("=== test_tool_jev ===")
    test_jev_choice_parses_answer()
    test_jev_score_parses_answer_without_criteria()
    test_jev_noul_returns_probability_not_boolean()
    test_missing_config_raises_jev_config_error()
    test_budget_exceeded_blocks_call_before_request()
    test_malformed_response_raises_jev_api_error()
    test_network_error_raises_jev_api_error()
    test_live_jev_noul()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
