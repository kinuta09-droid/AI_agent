# -*- coding: utf-8 -*-
"""
tools/tool_twitter_post.py の post_tweet_for_approval 成否分類ロジックの回帰テスト。

requests.post をモックし、実X APIは一切叩かない。「通信不明」(タイムアウト・
接続エラー・5xx・想定外の例外)は needs_review、「レスポンスは受け取ったが
明確に拒否された」(4xx)は failed、に分類されることを検証する
(known-bug-patterns.md: わからないものは失敗と決めつけない安全側設計)。

実行: py tests/test_tool_twitter_post.py
"""
import os
import sys
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests  # noqa: E402
import tools.tool_twitter_post as twp  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


@contextmanager
def _stub_auth():
    with patch.object(twp, "_get_auth", return_value=MagicMock()):
        yield


def _make_response(status_code: int, json_data: dict = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        resp.raise_for_status.side_effect = None
    return resp


def test_missing_auth_returns_failed():
    with patch.object(twp, "_get_auth", return_value=None):
        result = twp.post_tweet_for_approval("hello")
    check("missing auth outcome=failed", result["outcome"] == "failed", result)
    check("missing auth url is None", result["url"] is None)


def test_success_2xx_returns_posted_with_url():
    resp = _make_response(201, json_data={"data": {"id": "12345"}})
    with _stub_auth(), patch.object(twp.requests, "post", return_value=resp):
        result = twp.post_tweet_for_approval("hello")
    check("2xx outcome=posted", result["outcome"] == "posted", result)
    check("url built from tweet id", result["url"] == "https://x.com/i/web/status/12345", result["url"])


def test_4xx_returns_failed():
    resp = _make_response(403, text="Forbidden")
    with _stub_auth(), patch.object(twp.requests, "post", return_value=resp):
        result = twp.post_tweet_for_approval("hello")
    check("4xx outcome=failed (server clearly rejected, safe to mark as failed)", result["outcome"] == "failed", result)
    check("4xx detail includes response text", "Forbidden" in result["detail"], result["detail"])


def test_5xx_returns_needs_review():
    resp = _make_response(503, text="Service Unavailable")
    with _stub_auth(), patch.object(twp.requests, "post", return_value=resp):
        result = twp.post_tweet_for_approval("hello")
    check("5xx outcome=needs_review (server error, post success unknown)", result["outcome"] == "needs_review", result)


def test_timeout_returns_needs_review():
    with _stub_auth(), patch.object(twp.requests, "post", side_effect=requests.exceptions.Timeout("timed out")):
        result = twp.post_tweet_for_approval("hello")
    check("timeout outcome=needs_review (request may have reached server)", result["outcome"] == "needs_review", result)


def test_connection_error_returns_needs_review():
    with _stub_auth(), patch.object(twp.requests, "post", side_effect=requests.exceptions.ConnectionError("refused")):
        result = twp.post_tweet_for_approval("hello")
    check("connection error outcome=needs_review", result["outcome"] == "needs_review", result)


def test_unexpected_exception_returns_needs_review_not_raise():
    with _stub_auth(), patch.object(twp.requests, "post", side_effect=RuntimeError("boom")):
        result = twp.post_tweet_for_approval("hello")
    check("unexpected exception outcome=needs_review (safe side, does not propagate)",
          result["outcome"] == "needs_review", result)


def test_existing_cli_dry_run_function_unaffected():
    """既存のtool_post_tweet(CLI/ドライラン用、戻り値契約Error:文字列)は今回変更していない。"""
    with patch.object(twp, "_get_auth", return_value=None):
        result = twp.tool_post_tweet("hello")
    check("existing tool_post_tweet still returns Error: string contract", result.startswith("Error:"), result)


if __name__ == "__main__":
    tests = [
        test_missing_auth_returns_failed,
        test_success_2xx_returns_posted_with_url,
        test_4xx_returns_failed,
        test_5xx_returns_needs_review,
        test_timeout_returns_needs_review,
        test_connection_error_returns_needs_review,
        test_unexpected_exception_returns_needs_review_not_raise,
        test_existing_cli_dry_run_function_unaffected,
    ]
    for t in tests:
        t()

    print("\n" + "=" * 60)
    print(f"結果: {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("失敗:")
        for f in _FAIL:
            print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
