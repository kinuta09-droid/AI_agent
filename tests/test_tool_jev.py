# -*- coding: utf-8 -*-
"""
tool_jev.py (Jev/TypeSafe AI クライアント) の回帰テスト。

方針:
  - ネットワークに一切出ない。requests.post / _jev_request をスタブに差し替える。
  - JEV_COUNTER は一時ディレクトリ上のカウンタに差し替え、本番の
    logs/jev_daily_calls.json には一切触れない (notify=False で実Discord投稿も防ぐ、
    known-bug-patterns.md Category C: DailyCounter の既定 notify=True を潰し忘れると
    テストのたびに実通知が飛ぶ)。
  - ワイヤーフォーマット (エンドポイント/リクエスト/レスポンスの実際のフィールド名) は
    ユーザーが共有した公式APIリファレンス (docs.typesafe.ai/api.md, 2026-09-20) に
    基づく。POST https://api.typesafe.ai/v1/systemone、questions はマップ (質問IDが
    キー)、model フィールド必須、Noulの回答は `noul` フィールド、Choiceは
    `choice`+`confidence`+`probabilities`、Scoreは`score`+`confidence`。429/529は
    指数バックオフで自前リトライする (SDKが無いため)。

  ARMS_LIVE_TESTS=1 かつ実 JEV_API_KEY が .env に揃っているときだけ、実APIとの疎通
  確認テストも実行する (既定はSKIP)。

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


class _FakeResp:
    """requests.Response の必要最小限のスタブ。"""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text else (str(json_data) if json_data is not None else "")

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} error", response=self)


# ---------------------------------------------------------
# 1. jev_choice / jev_score / jev_noul の正常系パース (_jev_request をスタブ)
# ---------------------------------------------------------

def test_jev_choice_converts_list_criteria_to_map_and_parses_confidence():
    print("\n[1] jev_choice: list criteria を {option: None} の map に変換し、confidence を返す")
    captured = {}

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        captured["args"] = (question_type, state, instructions, criteria)
        return {"type": "choice", "choice": "high",
                "probabilities": {"high": 0.83, "medium": 0.1, "low": 0.07}, "confidence": 0.83}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_choice("state-text", "fit は?", ["high", "medium", "low"])
    finally:
        jev._jev_request = orig

    check("choice/confidence が正しくパースされる (probabilityキーではない)",
          result == {"choice": "high", "confidence": 0.83}, str(result))
    check("question_type が choice", captured["args"][0] == "choice", str(captured))
    check("criteria が {option: None} の map に変換される",
          captured["args"][3] == {"high": None, "medium": None, "low": None}, str(captured))


def test_jev_choice_accepts_dict_criteria_unchanged():
    print("\n[1b] jev_choice: dict criteria (説明付き) はそのまま渡る")
    captured = {}

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        captured["criteria"] = criteria
        return {"choice": "billing", "confidence": 0.9}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            jev.jev_choice("s", "どの部署?", {"billing": "支払い関連", "technical": "技術的な問題"})
    finally:
        jev._jev_request = orig

    check("dict criteria がそのまま渡る",
          captured["criteria"] == {"billing": "支払い関連", "technical": "技術的な問題"},
          str(captured))


def test_jev_choice_empty_criteria_raises_value_error():
    print("\n[1c] jev_choice: 選択肢が空なら ValueError")
    with _TempConfig(), _TempCounter():
        try:
            jev.jev_choice("s", "q", [])
            check("空criteriaでValueError", False, "例外が発生しなかった")
        except ValueError:
            check("空criteriaでValueError", True)


def test_jev_score_requires_criteria_2_to_10_levels():
    print("\n[2] jev_score: criteria は2〜10件の必須項目 (省略/範囲外は ValueError)")
    with _TempConfig(), _TempCounter():
        for bad in (None, [], ["only one level"], ["l"] * 11):
            try:
                jev.jev_score("s", "q", bad)
                check(f"criteria={bad!r} で ValueError", False, "例外が発生しなかった")
            except ValueError:
                check(f"criteria={bad!r} で ValueError", True)
            except TypeError:
                # criteria=None は必須引数なので呼び出し側の書き方次第で TypeError にもなりうる
                check(f"criteria={bad!r} で ValueError", bad is None,
                      "None以外でTypeErrorは想定外")


def test_jev_score_parses_score_and_confidence():
    print("\n[3] jev_score: score/confidence を正しくパースする")

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        return {"type": "score", "score": 0.62, "legend": {"0": "低", "1": "中", "2": "高"},
                "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}, "confidence": 0.71}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_score("state-text", "興味との関連度は?", ["低", "中", "高"])
    finally:
        jev._jev_request = orig

    check("score/confidence が正しくパースされる",
          result == {"score": 0.62, "confidence": 0.71}, str(result))


def test_jev_noul_parses_noul_field_not_probability_or_boolean():
    print("\n[4] jev_noul: レスポンスの `noul` フィールドから probability(float) を返す")

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        return {"type": "noul", "noul": 0.95}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_noul("state-text", "目標は達成されたか")
    finally:
        jev._jev_request = orig

    check("`noul`フィールドから probability を取り出す",
          result == {"probability": 0.95}, str(result))
    check("probability は float で返る", isinstance(result["probability"], float), str(result))
    check("boolean 型では返らない (公式ドキュメント通り確率のまま)",
          not isinstance(result["probability"], bool), str(result))


def test_jev_noul_passes_optional_criteria_through():
    print("\n[4b] jev_noul: 任意の criteria (true/false説明) がそのまま渡る")
    captured = {}

    def fake_request(question_type, state, instructions, criteria=None, timeout=jev.HTTP_TIMEOUT):
        captured["criteria"] = criteria
        return {"noul": 0.5}

    orig = jev._jev_request
    jev._jev_request = fake_request
    try:
        with _TempConfig(), _TempCounter():
            jev.jev_noul("s", "q", criteria={"true": "緊急", "false": "緊急でない"})
    finally:
        jev._jev_request = orig
    check("criteria がそのまま渡る",
          captured["criteria"] == {"true": "緊急", "false": "緊急でない"}, str(captured))


# ---------------------------------------------------------
# 5. 設定不備 → JevConfigError
# ---------------------------------------------------------

def test_missing_api_key_raises_jev_config_error():
    print("\n[5] JEV_API_KEY 未設定 → JevConfigError")
    with _TempConfig(key="", base_url="https://api.typesafe.ai"), _TempCounter():
        try:
            jev.jev_noul("s", "q")
            check("JevConfigError が送出される", False, "例外が発生しなかった")
        except jev.JevConfigError:
            check("JevConfigError が送出される", True)
        except Exception as e:
            check("JevConfigError が送出される", False, f"別の例外: {type(e).__name__}: {e}")


def test_missing_base_url_raises_jev_config_error():
    print("\n[5b] JEV_API_BASE_URL が空 → JevConfigError")
    with _TempConfig(key="test-key", base_url=""), _TempCounter():
        try:
            jev.jev_noul("s", "q")
            check("JevConfigError が送出される", False, "例外が発生しなかった")
        except jev.JevConfigError:
            check("JevConfigError が送出される", True)


def test_default_base_url_is_the_real_typesafe_endpoint():
    print("\n[5c] JEV_API_BASE_URL の既定値が公式ドキュメント記載の実ホスト")
    check("既定値が https://api.typesafe.ai",
          jev.JEV_API_BASE_URL in ("https://api.typesafe.ai",) or
          os.getenv("JEV_API_BASE_URL"),  # .env で上書きされている環境では既定値と一致しなくてよい
          jev.JEV_API_BASE_URL)


# ---------------------------------------------------------
# 6. コストガード上限到達 → ブロックされる (_jev_request は呼ばれない)
# ---------------------------------------------------------

def test_budget_exceeded_blocks_call_before_request():
    print("\n[6] JEV_COUNTER 上限到達 → GeminiBudgetExceeded で _jev_request は呼ばれない")
    called = {"n": 0}

    def fake_request(*a, **k):
        called["n"] += 1
        return {"noul": 0.9}

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
# 7. リクエスト構造 (実エンドポイント/ペイロード形式)
# ---------------------------------------------------------

def test_jev_request_posts_to_real_endpoint_with_correct_payload_shape():
    print("\n[7] _jev_request: 正しいURL/ペイロード形式 (questionsはマップ、modelフィールド必須)")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp(200, {"model": "jev-1.0", "answers": {"q": {"noul": 0.5}}})

    orig_post = jev.requests.post
    jev.requests.post = fake_post
    try:
        with _TempConfig(key="k", base_url="https://api.typesafe.ai"), _TempCounter():
            jev.jev_noul("hello", "is it urgent?")
    finally:
        jev.requests.post = orig_post

    check("URLが /v1/systemone", captured["url"] == "https://api.typesafe.ai/v1/systemone",
          captured.get("url"))
    check("questions がマップ形式 (質問IDがキー)",
          "q" in captured["json"]["questions"], str(captured.get("json")))
    check("model フィールドが含まれる", captured["json"].get("model") == jev.JEV_MODEL,
          str(captured.get("json")))
    check("state がそのまま渡る", captured["json"]["state"] == "hello", str(captured.get("json")))
    check("Authorization ヘッダが Bearer 形式",
          captured["headers"]["Authorization"] == "Bearer k", str(captured.get("headers")))


# ---------------------------------------------------------
# 8. レスポンス形式不正 → JevAPIError
# ---------------------------------------------------------

def test_malformed_response_raises_jev_api_error():
    print("\n[8] レスポンスに answers.q が無い → JevAPIError")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp(200, {"unexpected": "shape"})

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
    print("\n[9] ネットワーク例外 → JevAPIError にラップされる")

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
# 10. 429/529 は指数バックオフでリトライ、401/422はリトライしない
# ---------------------------------------------------------

def test_429_is_retried_with_backoff_then_succeeds():
    print("\n[10] HTTP 429 → time.sleep を挟んで自動リトライし、最終的に成功する")
    calls = {"n": 0}
    sleeps = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResp(429, {"error": "rate limited"})
        return _FakeResp(200, {"answers": {"q": {"noul": 0.5}}})

    orig_post = jev.requests.post
    orig_sleep = jev.time.sleep
    jev.requests.post = fake_post
    jev.time.sleep = lambda s: sleeps.append(s)
    try:
        with _TempConfig(), _TempCounter():
            result = jev.jev_noul("s", "q")
    finally:
        jev.requests.post = orig_post
        jev.time.sleep = orig_sleep

    check("3回目で成功する (最大試行内)", calls["n"] == 3, str(calls))
    check("成功結果が返る", result == {"probability": 0.5}, str(result))
    check("リトライ間隔が指数的に増える (2回sleep)", sleeps == [1.0, 2.0], str(sleeps))


def test_401_is_not_retried():
    print("\n[11] HTTP 401 (認証エラー) はリトライされず1回で JevAPIError")
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(401, {"error": "invalid api key"})

    orig_post = jev.requests.post
    jev.requests.post = fake_post
    try:
        with _TempConfig(), _TempCounter():
            try:
                jev.jev_noul("s", "q")
                check("401でJevAPIError", False, "例外が発生しなかった")
            except jev.JevAPIError:
                check("401でJevAPIError", True)
    finally:
        jev.requests.post = orig_post
    check("401はリトライされず1回だけ呼ばれる", calls["n"] == 1, str(calls))


def test_429_exhausts_retries_and_raises():
    print("\n[12] HTTP 429 が最大試行回数を超えて続く → 最終的に JevAPIError")
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(429, {"error": "still limited"})

    orig_post = jev.requests.post
    orig_sleep = jev.time.sleep
    jev.requests.post = fake_post
    jev.time.sleep = lambda s: None
    try:
        with _TempConfig(), _TempCounter():
            try:
                jev.jev_noul("s", "q")
                check("リトライ枯渇でJevAPIError", False, "例外が発生しなかった")
            except jev.JevAPIError:
                check("リトライ枯渇でJevAPIError", True)
    finally:
        jev.requests.post = orig_post
        jev.time.sleep = orig_sleep
    check(f"最大試行回数 ({jev._MAX_ATTEMPTS}) だけ呼ばれる",
          calls["n"] == jev._MAX_ATTEMPTS, str(calls))


# ---------------------------------------------------------
# 13. 実APIとの疎通確認 (ARMS_LIVE_TESTS=1 のときだけ)
# ---------------------------------------------------------

def test_live_jev_noul():
    print("\n[13] (live) 実 Jev API との疎通確認")
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("  ⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 JEV_API_KEY のときだけ実行します")
        return
    if not jev.JEV_API_KEY:
        print("  ⏭️ SKIP: JEV_API_KEY が .env に未設定です")
        return
    try:
        result = jev.jev_noul("today is sunny", "is it a good day for a walk")
        check("live: probability が 0..1 の範囲",
              0.0 <= result["probability"] <= 1.0, str(result))
    except jev.JevAPIError as e:
        check("live: 疎通確認", False, f"実API呼び出しに失敗: {e}")


if __name__ == "__main__":
    print("=== test_tool_jev ===")
    test_jev_choice_converts_list_criteria_to_map_and_parses_confidence()
    test_jev_choice_accepts_dict_criteria_unchanged()
    test_jev_choice_empty_criteria_raises_value_error()
    test_jev_score_requires_criteria_2_to_10_levels()
    test_jev_score_parses_score_and_confidence()
    test_jev_noul_parses_noul_field_not_probability_or_boolean()
    test_jev_noul_passes_optional_criteria_through()
    test_missing_api_key_raises_jev_config_error()
    test_missing_base_url_raises_jev_config_error()
    test_default_base_url_is_the_real_typesafe_endpoint()
    test_budget_exceeded_blocks_call_before_request()
    test_jev_request_posts_to_real_endpoint_with_correct_payload_shape()
    test_malformed_response_raises_jev_api_error()
    test_network_error_raises_jev_api_error()
    test_429_is_retried_with_backoff_then_succeeds()
    test_401_is_not_retried()
    test_429_exhausts_retries_and_raises()
    test_live_jev_noul()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
