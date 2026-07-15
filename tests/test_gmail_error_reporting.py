# -*- coding: utf-8 -*-
"""
fetch_emails_via_gws のエラー報告回帰テスト (2026-07-16)

背景: gws CLI は認証エラーでも終了コード0で {"error": {...}} を返すため、
旧実装では認証切れが「新着（未読）メールはありません」と誤報告されていた
(実際に数ヶ月の認証切れが「新着なし」として気づかれずに放置された)。

実行: py tests/test_gmail_error_reporting.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_gmail_manager as gm


def _with_stub(stub_response, fn):
    """_run_gws_command を差し替えて fn を実行する (実gws/実APIを呼ばない)。"""
    original = gm._run_gws_command
    gm._run_gws_command = lambda args, timeout=30: stub_response
    try:
        return fn()
    finally:
        gm._run_gws_command = original


def test_auth_error_not_masked_as_empty():
    """認証エラー (gws exit 0 + errorキー) が「新着なし」に化けないこと。"""
    auth_error = {"error": {"code": 401, "message": "Gmail auth failed: Failed to get token",
                            "reason": "authError"}}
    result = _with_stub(auth_error, lambda: gm.fetch_emails_via_gws())
    assert isinstance(result, str) and result.startswith("[ERROR]"), result
    assert "gws auth login" in result
    print("✅ 401 auth error -> [ERROR] string (not empty list)")

    summary = _with_stub(auth_error, gm.get_gmail_summary)
    assert "新着" not in summary, f"error must not be reported as 'no new mail': {summary}"
    assert "連携エラー" in summary, summary
    print("✅ get_gmail_summary reports 連携エラー, never 新着なし")

    sync = _with_stub(auth_error, gm.tool_gmail_sync_important)
    assert "連携エラー" in sync, sync
    print("✅ tool_gmail_sync_important reports 連携エラー")


def test_cli_failure_not_masked():
    """gws 未インストール/実行失敗 (None) も「新着なし」に化けないこと。"""
    result = _with_stub(None, lambda: gm.fetch_emails_via_gws())
    assert isinstance(result, str) and result.startswith("[ERROR]"), result
    summary = _with_stub(None, gm.get_gmail_summary)
    assert "新着" not in summary and "連携エラー" in summary, summary
    print("✅ gws CLI failure (None) -> 連携エラー")


def test_genuinely_empty_inbox_still_reports_no_mail():
    """本当に未読ゼロのときは従来通り「新着なし」と報告されること。"""
    result = _with_stub({"messages": []}, lambda: gm.fetch_emails_via_gws())
    assert result == [], result
    summary = _with_stub({"messages": []}, gm.get_gmail_summary)
    assert "新着（未読）メールはありません" in summary, summary
    print("✅ genuinely empty inbox -> 新着なし (unchanged behavior)")


def test_unexpected_shape_reported():
    """未知の応答形式もエラーとして報告されること。"""
    result = _with_stub({"foo": "bar"}, lambda: gm.fetch_emails_via_gws())
    assert isinstance(result, str) and result.startswith("[ERROR]"), result
    print("✅ unexpected response shape -> [ERROR]")


if __name__ == "__main__":
    test_auth_error_not_masked_as_empty()
    test_cli_failure_not_masked()
    test_genuinely_empty_inbox_still_reports_no_mail()
    test_unexpected_shape_reported()
    print("\n🎉 ALL GMAIL ERROR-REPORTING TESTS PASSED")
