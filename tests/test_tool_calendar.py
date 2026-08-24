# -*- coding: utf-8 -*-
"""tool_create_task() / tool_get_tasks() の Google Tasks (gws) 一本化の回帰テスト。

このテストが存在する理由:
  2026-08-24、/remind で「登録しました」と成功表示されるのに Briefing/Dailynote に
  反映されない不具合が報告された。真因は tool_create_task が Todoist にしか
  書き込んでいなかったこと。Briefing (tool_daily_summary) / Dailynote
  (_fetch_tasks_content) はどちらも gws 経由の Google Tasks を主系統として読むため、
  登録先を揃える必要があった。2026-08-25、Todoist運用自体を廃止し gws (Google Tasks)
  一本化に踏み切ったため、Todoistフォールバック関連のコードとテストは全廃した。

実行: py tests/test_tool_calendar.py
"""
import os
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools import tool_calendar

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def test_create_task_gws_success():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"id": "t1", "title": "買い物"}) as mock_gws:
        result = tool_calendar.tool_create_task("買い物", due_date="2026-08-25")

    check("gws成功時にSuccessを返す", result.startswith("Success:"), result)
    check("tasks insertを呼ぶ", mock_gws.call_args[0][0][:3] == ["tasks", "tasks", "insert"], str(mock_gws.call_args))


def test_create_task_gws_unavailable():
    with patch.object(tool_calendar, "_run_gws_command", return_value=None):
        result = tool_calendar.tool_create_task("買い物")

    check("gws未インストール時はErrorを返す", result.startswith("Error:"), result)
    check("gws案内文を含む", "gws" in result, result)


def test_create_task_gws_auth_error():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"error": {"code": 401}}):
        result = tool_calendar.tool_create_task("買い物")

    check("gws認証切れ時はErrorを返す", result.startswith("Error:"), result)


def test_create_task_invalid_due_date_does_not_call_gws():
    """_parse_datetime が解けない期限指定は gws を呼ばず即 Error を返す
    (Todoist フォールバックが無くなったため、パース不能な入力は登録不能なことをそのまま伝える)。
    """
    with patch.object(tool_calendar, "_run_gws_command") as mock_gws:
        result = tool_calendar.tool_create_task("買い物", due_date="@@@___invalid_date___@@@")

    check("不正な期限指定はErrorを返す", result.startswith("Error:"), result)
    check("不正な期限指定はgwsを呼ばない", mock_gws.call_count == 0, f"call_count={mock_gws.call_count}")


def test_get_tasks_gws_success():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"items": [{"title": "買い物", "due": "2026-08-25T00:00:00.000Z"}]}) as mock_gws:
        result = tool_calendar.tool_get_tasks("today")

    check("gws成功時にタスクを列挙する", "買い物" in result, result)
    check("tasks listを呼ぶ", mock_gws.call_args[0][0][:3] == ["tasks", "tasks", "list"], str(mock_gws.call_args))


def test_get_tasks_gws_unavailable():
    with patch.object(tool_calendar, "_run_gws_command", return_value=None):
        result = tool_calendar.tool_get_tasks("today")

    check("gws未インストール時はErrorを返す(get_tasks)", result.startswith("Error:"), result)


def test_get_tasks_gws_auth_error():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"error": {"code": 401}}):
        result = tool_calendar.tool_get_tasks("today")

    check("gws認証切れ時はErrorを返す(get_tasks)", result.startswith("Error:"), result)


def test_get_tasks_filter_params():
    """today/overdue/week は期間境界付きの dueMin/dueMax を組み立て、
    未知の filter (旧 Todoist 固有クエリ等) は期間フィルタなしにフォールバックする。
    """
    today_params = tool_calendar._gws_task_filter_params("today")
    check("today は dueMin/dueMax を持つ", "dueMin" in today_params and "dueMax" in today_params, str(today_params))

    overdue_params = tool_calendar._gws_task_filter_params("overdue")
    check("overdue は dueMax のみ", "dueMax" in overdue_params and "dueMin" not in overdue_params, str(overdue_params))

    unknown_params = tool_calendar._gws_task_filter_params("p1 & today")
    check("未知のfilterは期間フィルタなし", "dueMin" not in unknown_params and "dueMax" not in unknown_params, str(unknown_params))


if __name__ == "__main__":
    print("=== test_tool_calendar ===")
    test_create_task_gws_success()
    test_create_task_gws_unavailable()
    test_create_task_gws_auth_error()
    test_create_task_invalid_due_date_does_not_call_gws()
    test_get_tasks_gws_success()
    test_get_tasks_gws_unavailable()
    test_get_tasks_gws_auth_error()
    test_get_tasks_filter_params()

    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
