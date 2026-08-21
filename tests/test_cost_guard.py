# -*- coding: utf-8 -*-
"""
cost_guard (Tier2-b APIコストガード) のユニットテスト。

一時ディレクトリ上に DailyCounter を新規構築してテストするため、
実カウンタファイル logs/gemini_light_daily_calls.json 等には一切触れない
(グローバルシングルトン GEMINI_LIGHT_COUNTER/GEMINI_HEAVY_COUNTER も使わない)。

実行: py tests/test_cost_guard.py
"""
import os
import sys
import json
import shutil
import datetime
import tempfile
import threading
from contextlib import contextmanager

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.cost_guard import DailyCounter, GeminiBudgetExceeded, get_limit
from lib.lib_shared import retry

_TODAY = datetime.date.today().isoformat()


@contextmanager
def _temp_counter(limit: int, initial: dict = None, notify: bool = False):
    """一時ディレクトリ上のカウンタを作り、必ず掃除する。

    yield されるのは (counter, path)。initial があれば事前にファイルへ書く。
    """
    root = tempfile.mkdtemp(prefix="cost_guard_test_")
    path = os.path.join(root, "counter.json")
    try:
        if initial is not None:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(initial, f, ensure_ascii=False)
        yield DailyCounter(path=path, limit=limit, name="TestAPI",
                           notify=notify), path
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _read(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 基本動作: 初回・加算・永続化
# --------------------------------------------------------------------------

def test_first_call_creates_file_with_today():
    """ファイルなし → 初回呼び出しは 1 を返し、本日の日付で作成される。"""
    with _temp_counter(limit=10) as (c, path):
        assert c.check_and_increment() == 1
        data = _read(path)
        assert data["date"] == _TODAY
        assert data["count"] == 1
    print("✅ test_first_call_creates_file_with_today")


def test_increment_persists_to_disk():
    """3回呼び出し → ディスク上のカウントが 3。"""
    with _temp_counter(limit=10) as (c, path):
        for expected in (1, 2, 3):
            assert c.check_and_increment() == expected
        assert _read(path)["count"] == 3
    print("✅ test_increment_persists_to_disk")


def test_rollover_resets_previous_day():
    """前日 count=199 → 日付ロールオーバーでリセットして 1 を返す。"""
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    initial = {"date": yesterday, "count": 199,
               "warned": True, "limit_notified": True}
    with _temp_counter(limit=200, initial=initial) as (c, path):
        assert c.check_and_increment() == 1
        data = _read(path)
        assert data["date"] == _TODAY
        assert data["warned"] is False, "ロールオーバーで警告フラグも初期化されるべき"
        assert data["limit_notified"] is False
    print("✅ test_rollover_resets_previous_day")


# --------------------------------------------------------------------------
# 上限超過
# --------------------------------------------------------------------------

def test_limit_raises_and_does_not_increment():
    """limit=3 → 4回目が GeminiBudgetExceeded、ファイルは 3 のまま。"""
    with _temp_counter(limit=3) as (c, path):
        for _ in range(3):
            c.check_and_increment()
        try:
            c.check_and_increment()
            assert False, "4回目は GeminiBudgetExceeded を送出すべき"
        except GeminiBudgetExceeded as e:
            assert "上限" in str(e), "日本語の完成された文言を持つべき"
        assert _read(path)["count"] == 3, "超過分は加算されないべき"
        assert _read(path)["limit_notified"] is True
    print("✅ test_limit_raises_and_does_not_increment")


def test_limit_zero_means_unlimited_but_still_counts():
    """limit=0 (キルスイッチ) → 500回でも送出せず、カウントは継続。"""
    with _temp_counter(limit=0) as (c, path):
        for _ in range(500):
            c.check_and_increment()
        assert _read(path)["count"] == 500
    print("✅ test_limit_zero_means_unlimited_but_still_counts")


def test_corrupt_file_treated_as_zero_and_repaired():
    """破損JSON → count 0 扱いで続行し、次の保存でファイル修復。"""
    with _temp_counter(limit=10) as (c, path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write("{{not json")
        assert c.check_and_increment() == 1, "破損はガード側で吸収して続行すべき"
        data = _read(path)
        assert data["count"] == 1 and data["date"] == _TODAY
    print("✅ test_corrupt_file_treated_as_zero_and_repaired")


def test_bool_count_treated_as_corrupt():
    """count: true (bool は int のサブクラス) は破損としてリセットされる。"""
    initial = {"date": _TODAY, "count": True,
               "warned": False, "limit_notified": False}
    with _temp_counter(limit=10, initial=initial) as (c, path):
        assert c.check_and_increment() == 1, "bool count は 0 起点でやり直すべき"
        assert _read(path)["count"] == 1
    print("✅ test_bool_count_treated_as_corrupt")


# --------------------------------------------------------------------------
# 通知 (80%警告 / 超過通知は1日1回)
# --------------------------------------------------------------------------

def test_warn_fires_exactly_once_at_80_percent():
    """limit=10 → 8回目 (ceil(10*0.8)) で警告が1回だけ発火し、フラグ永続化。"""
    with _temp_counter(limit=10, notify=True) as (c, path):
        notices = []
        c._notify = notices.append  # HTTP を飛ばさずに記録
        for _ in range(10):
            c.check_and_increment()
        warn_notices = [m for m in notices if "80%" in m]
        assert len(warn_notices) == 1, f"警告は1回のみのはず: {notices}"
        assert "8/10" in warn_notices[0]
        assert _read(path)["warned"] is True
    print("✅ test_warn_fires_exactly_once_at_80_percent")


def test_exceed_notifies_exactly_once():
    """超過通知は初回のみ。2回目以降の超過は黙って送出。"""
    with _temp_counter(limit=2, notify=True) as (c, path):
        notices = []
        c._notify = notices.append
        c.check_and_increment()
        c.check_and_increment()
        for _ in range(3):  # 3連続で超過させる
            try:
                c.check_and_increment()
                assert False, "超過は送出すべき"
            except GeminiBudgetExceeded:
                pass
        exceed_notices = [m for m in notices if "日次上限" in m]
        assert len(exceed_notices) == 1, f"超過通知は初回のみのはず: {notices}"
    print("✅ test_exceed_notifies_exactly_once")


# --------------------------------------------------------------------------
# 並行性・retry連携
# --------------------------------------------------------------------------

def test_thread_safety_within_process():
    """8スレッド×50回 → 欠落なく 400 (プロセス内はLockで直列化)。"""
    with _temp_counter(limit=0) as (c, path):
        def worker():
            for _ in range(50):
                c.check_and_increment()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _read(path)["count"] == 400
    print("✅ test_thread_safety_within_process")


def test_retry_does_not_retry_budget_exceeded():
    """@retry 装飾関数の GeminiBudgetExceeded は即時再送出 (バックオフなし)。"""
    calls = {"n": 0}

    @retry(max_retries=3, initial_delay=1)
    def budget_error_func():
        calls["n"] += 1
        raise GeminiBudgetExceeded("TestAPI", 5)

    try:
        budget_error_func()
        assert False, "送出されるべき"
    except GeminiBudgetExceeded:
        pass
    assert calls["n"] == 1, f"再試行してはならない (呼び出し {calls['n']} 回)"
    print("✅ test_retry_does_not_retry_budget_exceeded")


# --------------------------------------------------------------------------
# get_limit の env パース
# --------------------------------------------------------------------------

def test_get_limit_env_parsing():
    """未設定→default / 不正値→default / 負値→0 / 正常値→その値。"""
    key = "COST_GUARD_TEST_LIMIT_ENV"
    try:
        os.environ.pop(key, None)
        assert get_limit(key, 200) == 200
        os.environ[key] = "abc"
        assert get_limit(key, 200) == 200
        os.environ[key] = "-5"
        assert get_limit(key, 200) == 0
        os.environ[key] = "42"
        assert get_limit(key, 200) == 42
    finally:
        os.environ.pop(key, None)
    print("✅ test_get_limit_env_parsing")


# --------------------------------------------------------------------------
# LIGHT/HEAVY別カウンタ + HEAVY上限時のLIGHTフォールバック (2026-08-17)
# --------------------------------------------------------------------------

def test_light_and_heavy_use_separate_counters():
    """LIGHTモデルとHEAVYモデルの呼び出しが別々のカウンタに計上されること
    (単一カウンタからモデル別カウンタへ分離。GoogleのRPD上限もモデル毎に
    別枠のため、合算カウンタだと片方が余っていてももう片方に引っ張られていた)。
    """
    import lib.lib_shared as ls
    from lib import cost_guard

    root = tempfile.mkdtemp(prefix="cost_guard_split_")
    light = DailyCounter(path=os.path.join(root, "light.json"), limit=0, name="LIGHT-test", notify=False)
    heavy = DailyCounter(path=os.path.join(root, "heavy.json"), limit=0, name="HEAVY-test", notify=False)
    orig_light, orig_heavy = cost_guard.GEMINI_LIGHT_COUNTER, cost_guard.GEMINI_HEAVY_COUNTER
    orig_call = ls._original_generate_content
    try:
        cost_guard.GEMINI_LIGHT_COUNTER = light
        cost_guard.GEMINI_HEAVY_COUNTER = heavy
        ls._original_generate_content = lambda *a, **k: "stub-response"

        ls.client.models.generate_content(model=ls.MODEL_NAME, contents="x")
        assert light.current() == 1, "LIGHT呼び出しがLIGHTカウンタに計上されていない"
        assert heavy.current() == 0, "LIGHT呼び出しがHEAVYカウンタにも誤って計上された"

        ls.client.models.generate_content(model=ls.MODEL_NAME_HEAVY, contents="x")
        assert heavy.current() == 1, "HEAVY呼び出しがHEAVYカウンタに計上されていない"
        assert light.current() == 1, "HEAVY呼び出しがLIGHTカウンタにも誤って計上された"
    finally:
        cost_guard.GEMINI_LIGHT_COUNTER = orig_light
        cost_guard.GEMINI_HEAVY_COUNTER = orig_heavy
        ls._original_generate_content = orig_call
        shutil.rmtree(root, ignore_errors=True)
    print("✅ test_light_and_heavy_use_separate_counters")


def test_heavy_falls_back_to_light_when_heavy_exhausted():
    """HEAVYが日次上限到達 → 例外を出さずLIGHTへ切り替えて続行すること。"""
    import lib.lib_shared as ls
    from lib import cost_guard

    root = tempfile.mkdtemp(prefix="cost_guard_fallback_")
    light = DailyCounter(path=os.path.join(root, "light.json"), limit=10, name="LIGHT-test", notify=False)
    heavy = DailyCounter(path=os.path.join(root, "heavy.json"), limit=1, name="HEAVY-test", notify=False)
    heavy.check_and_increment()  # HEAVYを使い切っておく (limit=1に対しcount=1)
    orig_light, orig_heavy = cost_guard.GEMINI_LIGHT_COUNTER, cost_guard.GEMINI_HEAVY_COUNTER
    orig_call = ls._original_generate_content
    calls = []
    try:
        cost_guard.GEMINI_LIGHT_COUNTER = light
        cost_guard.GEMINI_HEAVY_COUNTER = heavy

        def _fake_call(*a, **k):
            calls.append(k.get("model"))
            return "stub-response"
        ls._original_generate_content = _fake_call

        result = ls.client.models.generate_content(model=ls.MODEL_NAME_HEAVY, contents="x")
        assert result == "stub-response", "フォールバック後もAPI呼び出し自体は成功するべき"
        assert calls == [ls.MODEL_NAME], f"実際に呼ばれたモデルがLIGHTに差し替わっていない: {calls}"
        assert light.current() == 1, "フォールバック分がLIGHTカウンタに計上されていない"
        assert heavy.current() == 1, "HEAVYカウンタが二重加算されている"
    finally:
        cost_guard.GEMINI_LIGHT_COUNTER = orig_light
        cost_guard.GEMINI_HEAVY_COUNTER = orig_heavy
        ls._original_generate_content = orig_call
        shutil.rmtree(root, ignore_errors=True)
    print("✅ test_heavy_falls_back_to_light_when_heavy_exhausted")


def test_heavy_fallback_raises_when_light_also_exhausted():
    """HEAVY・LIGHT両方が上限到達 → フォールバック先も無いのでGeminiBudgetExceededが伝播すること。"""
    import lib.lib_shared as ls
    from lib import cost_guard

    root = tempfile.mkdtemp(prefix="cost_guard_fallback_fail_")
    light = DailyCounter(path=os.path.join(root, "light.json"), limit=1, name="LIGHT-test", notify=False)
    heavy = DailyCounter(path=os.path.join(root, "heavy.json"), limit=1, name="HEAVY-test", notify=False)
    light.check_and_increment()
    heavy.check_and_increment()
    orig_light, orig_heavy = cost_guard.GEMINI_LIGHT_COUNTER, cost_guard.GEMINI_HEAVY_COUNTER
    orig_call = ls._original_generate_content
    try:
        cost_guard.GEMINI_LIGHT_COUNTER = light
        cost_guard.GEMINI_HEAVY_COUNTER = heavy
        ls._original_generate_content = lambda *a, **k: "should-not-be-called"

        raised = False
        try:
            ls.client.models.generate_content(model=ls.MODEL_NAME_HEAVY, contents="x")
        except GeminiBudgetExceeded:
            raised = True
        assert raised, "LIGHTも上限のときはGeminiBudgetExceededが伝播するべき"
    finally:
        cost_guard.GEMINI_LIGHT_COUNTER = orig_light
        cost_guard.GEMINI_HEAVY_COUNTER = orig_heavy
        ls._original_generate_content = orig_call
        shutil.rmtree(root, ignore_errors=True)
    print("✅ test_heavy_fallback_raises_when_light_also_exhausted")


# --------------------------------------------------------------------------
# /future ツール層の日次上限 (API呼び出しゼロで検証)
# --------------------------------------------------------------------------

def test_future_search_blocked_when_counter_full():
    """カウンタ充填済み → run_future_search が即時エラー文字列を返す
    (Tavily/Gemini どちらも呼ばれない = ガードがパイプライン先頭で効く)。"""
    import tools.tool_future_search as fsm
    root = tempfile.mkdtemp(prefix="cost_guard_future_")
    path = os.path.join(root, "future.json")
    orig_counter = fsm._RUN_COUNTER
    try:
        full = DailyCounter(path=path, limit=1, name="FutureSearch", notify=False)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({"date": _TODAY, "count": 1,
                       "warned": False, "limit_notified": False}, f)
        fsm._RUN_COUNTER = full
        result = fsm.run_future_search("テストトピック")
        assert result.startswith("Error:"), f"エラー文字列を返すべき: {result[:80]}"
        assert "上限" in result
    finally:
        fsm._RUN_COUNTER = orig_counter
        shutil.rmtree(root, ignore_errors=True)
    print("✅ test_future_search_blocked_when_counter_full")


if __name__ == "__main__":
    tests = [
        test_first_call_creates_file_with_today,
        test_increment_persists_to_disk,
        test_rollover_resets_previous_day,
        test_limit_raises_and_does_not_increment,
        test_limit_zero_means_unlimited_but_still_counts,
        test_corrupt_file_treated_as_zero_and_repaired,
        test_bool_count_treated_as_corrupt,
        test_warn_fires_exactly_once_at_80_percent,
        test_exceed_notifies_exactly_once,
        test_thread_safety_within_process,
        test_retry_does_not_retry_budget_exceeded,
        test_get_limit_env_parsing,
        test_light_and_heavy_use_separate_counters,
        test_heavy_falls_back_to_light_when_heavy_exhausted,
        test_heavy_fallback_raises_when_light_also_exhausted,
        test_future_search_blocked_when_counter_full,
    ]
    for t in tests:
        t()
    print(f"\n🎉 ALL {len(tests)} COST GUARD TESTS PASSED")
