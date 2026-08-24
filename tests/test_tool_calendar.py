# -*- coding: utf-8 -*-
"""tool_create_task() の gws (Google Tasks) 優先 / Todoist フォールバックの回帰テスト。

このテストが存在する理由:
  2026-08-24、/remind で「登録しました」と成功表示されるのに Briefing/Dailynote に
  反映されない不具合が報告された。真因は tool_create_task が Todoist にしか
  書き込んでいなかったこと。Briefing (tool_daily_summary) / Dailynote
  (_fetch_tasks_content) はどちらも gws 経由の Google Tasks を主系統として読むため、
  登録先を揃えないと同じ食い違いが必ず再発する。ここでは「gws を優先する」
  「gws が使えない時だけ Todoist に落ちる」の分岐を、実 API を一切叩かずに検査する。

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


def test_gws_success_does_not_touch_todoist():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"id": "t1", "title": "買い物"}) as mock_gws, \
         patch.object(tool_calendar, "_get_todoist_client") as mock_todoist:
        result = tool_calendar.tool_create_task("買い物", due_date="2026-08-25")

    check("gws成功時にSuccessを返す", result.startswith("Success:"), result)
    check("gws成功時はtasks insertを呼ぶ", mock_gws.call_args[0][0][:3] == ["tasks", "tasks", "insert"], str(mock_gws.call_args))
    check("gws成功時はTodoistに触らない", mock_todoist.call_count == 0, f"call_count={mock_todoist.call_count}")


def test_gws_none_falls_back_to_todoist():
    with patch.object(tool_calendar, "_run_gws_command", return_value=None), \
         patch.object(tool_calendar, "_get_todoist_client", return_value=None) as mock_todoist, \
         patch.object(tool_calendar, "TODOIST_API_KEY", ""):
        result = tool_calendar.tool_create_task("買い物")

    check("gws未インストール時はTodoistフォールバックに落ちる", mock_todoist.call_count == 1, f"call_count={mock_todoist.call_count}")
    check("Todoist未設定時の案内文を返す", "Todoist" in result, result)


def test_gws_error_dict_falls_back_to_todoist():
    with patch.object(tool_calendar, "_run_gws_command", return_value={"error": {"code": 401}}), \
         patch.object(tool_calendar, "_get_todoist_client", return_value=None) as mock_todoist, \
         patch.object(tool_calendar, "TODOIST_API_KEY", ""):
        result = tool_calendar.tool_create_task("買い物")

    check("gws認証切れ時はTodoistフォールバックに落ちる", mock_todoist.call_count == 1, f"call_count={mock_todoist.call_count}")
    check("Todoist未設定時の案内文を返す(認証切れ経路)", "Todoist" in result, result)


def test_unparsable_due_date_skips_gws_and_falls_back_to_todoist():
    """_parse_datetime が解けない表現 (Todoist 拡張構文の "every monday" 等を想定) は、
    gws を呼ばずに Todoist フォールバックへ渡る。Todoist の due_string は生文字列のまま
    渡され、Todoist 自身の自然言語パーサーが解釈する (この関数の中では検証しない)。
    以前の実装 (先に _parse_datetime で失敗したら即 Error) はここが regression だった。
    """
    with patch.object(tool_calendar, "_run_gws_command") as mock_gws, \
         patch.object(tool_calendar, "_get_todoist_client", return_value=None) as mock_todoist, \
         patch.object(tool_calendar, "TODOIST_API_KEY", ""):
        result = tool_calendar.tool_create_task("買い物", due_date="every monday")

    check("gwsを呼ばずTodoistへ落ちる", mock_gws.call_count == 0, f"call_count={mock_gws.call_count}")
    check("Todoistフォールバックが呼ばれる", mock_todoist.call_count == 1, f"call_count={mock_todoist.call_count}")
    check("Todoist未設定時の案内文を返す(パース不能due_date経路)", "Todoist" in result, result)


if __name__ == "__main__":
    print("=== test_tool_calendar ===")
    test_gws_success_does_not_touch_todoist()
    test_gws_none_falls_back_to_todoist()
    test_gws_error_dict_falls_back_to_todoist()
    test_unparsable_due_date_skips_gws_and_falls_back_to_todoist()

    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
