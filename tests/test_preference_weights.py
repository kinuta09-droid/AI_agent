# -*- coding: utf-8 -*-
"""
lib/preference_weights の回帰テスト (実Vault不要、全てDI)

実行: py tests/test_preference_weights.py
"""
import os
import sys
import datetime
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.engagement_log import log_event, log_impressions
from lib.preference_weights import keyword_affinity, branch_affinity, engagement_context_block

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


NOW = datetime.datetime(2026, 8, 21, 12, 0, 0)
_TS = NOW.strftime("%Y-%m-%dT%H:%M:%S")


def _log_path(tmp):
    return os.path.join(tmp, "engagement.jsonl")


def test_keyword_affinity_no_data_means_no_adjustment():
    print("\n[1] データが無ければ空dict (呼び出し側は1.0扱い)")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        result = keyword_affinity(["仮想通貨", "介護"], now=NOW, log_path=log_path)
        check("イベント無しなら空dict", result == {}, str(result))
        check("空dictからのgetは1.0", result.get("仮想通貨", 1.0) == 1.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keyword_affinity_click_raises_impression_lowers():
    print("\n[2] クリックされたキーワードは上がる、放置されたキーワードは下がる")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        # 「仮想通貨」は繰り返しクリックされている
        log_event("click", "仮想通貨の最新動向", "curate", ts=_TS, log_path=log_path)
        log_event("query", "仮想通貨について教えて", "discord", ts=_TS, log_path=log_path)
        # 「介護」は提示されただけで一度もクリックされていない
        log_event("impression", "介護業界のDX", "curate", ts=_TS, log_path=log_path)
        log_event("impression", "介護業界のDX", "curate", ts=_TS, log_path=log_path)
        log_event("impression", "介護業界のDX", "curate", ts=_TS, log_path=log_path)

        result = keyword_affinity(["仮想通貨", "介護"], now=NOW, log_path=log_path)
        check("クリックされたキーワードは1.0超", result.get("仮想通貨", 1.0) > 1.0,
              str(result))
        check("放置キーワードは1.0未満", result.get("介護", 1.0) < 1.0, str(result))
        check("上限クランプ2.0以内", result["仮想通貨"] <= 2.0, str(result))
        check("下限クランプ0.5以上", result["介護"] >= 0.5, str(result))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keyword_affinity_old_events_decay():
    print("\n[3] 古いイベントは減衰して効きが弱まる")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        old_ts = (NOW - datetime.timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
        log_event("click", "量子コンピュータの話題", "curate", ts=old_ts, log_path=log_path)

        result = keyword_affinity(["量子コンピュータ"], now=NOW, days=365, log_path=log_path)
        check("120日前のクリックは減衰でほぼ効かない",
              result.get("量子コンピュータ", 1.0) < 1.1, str(result))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_branch_affinity_insufficient_data():
    print("\n[4] サンプル不足なら空dict (無調整)")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        for _ in range(3):
            log_impressions(
                [{"title": "t", "branch": "entropy"}], "curate", log_path=log_path
            )
        result = branch_affinity(min_impressions=20, now=NOW, log_path=log_path)
        check("閾値未満は空dict", result == {}, str(result))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_branch_affinity_ctr_ranking():
    print("\n[5] 十分なサンプルでCTR順位が正しく出る")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        # consistent: 10 impression, 8 click (高CTR)
        for i in range(10):
            gid = log_impressions(
                [{"title": f"c{i}", "branch": "consistent"}], "curate", log_path=log_path
            )
            if i < 8:
                log_event("click", f"c{i}", "curate", group_id=gid, ts=_TS,
                          meta={"branch": "consistent"}, log_path=log_path)
        # entropy: 10 impression, 1 click (低CTR)
        for i in range(10):
            gid = log_impressions(
                [{"title": f"e{i}", "branch": "entropy"}], "curate", log_path=log_path
            )
            if i < 1:
                log_event("click", f"e{i}", "curate", group_id=gid, ts=_TS,
                          meta={"branch": "entropy"}, log_path=log_path)

        result = branch_affinity(min_impressions=15, now=NOW, log_path=log_path)
        check("consistentがentropyより高CTR",
              result.get("consistent", 0) > result.get("entropy", 0), str(result))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_engagement_context_block_empty():
    print("\n[6] ログが空ならブロックも空文字")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        block = engagement_context_block(log_path=log_path)
        check("空ログは空文字", block == "", repr(block))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_engagement_context_block_has_content():
    print("\n[7] オープン履歴があればブロックに反映される")
    tmp = tempfile.mkdtemp()
    try:
        log_path = _log_path(tmp)
        log_event("open", "30_Main/Knowledge_Base/2026-08-08_量子コンピュータ.md",
                  "obsidian", ts=_TS, log_path=log_path)
        block = engagement_context_block(log_path=log_path)
        check("よく開いたノートに含まれる", "量子コンピュータ" in block, block)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("=== test_preference_weights ===")
    test_keyword_affinity_no_data_means_no_adjustment()
    test_keyword_affinity_click_raises_impression_lowers()
    test_keyword_affinity_old_events_decay()
    test_branch_affinity_insufficient_data()
    test_branch_affinity_ctr_ranking()
    test_engagement_context_block_empty()
    test_engagement_context_block_has_content()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
