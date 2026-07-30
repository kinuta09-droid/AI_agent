# -*- coding: utf-8 -*-
"""
Calendar / Task Integration Module
Google Calendar + Todoist + Obsidian Daily Note 連携ツール群。
"""

import os
import sys
import datetime
import subprocess
import json

sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv

from lib.paths import PROJECT_ROOT, ENV_PATH, VAULT_ROOT

load_dotenv(ENV_PATH)

from lib.lib_shared import _run_gws_command
from lib.bot_errors import is_failure_result

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

CREDENTIALS_PATH = os.path.join(PROJECT_ROOT, "credentials.json")
TOKEN_PATH = os.path.join(PROJECT_ROOT, "token.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

DAILY_NOTE_DIR = os.path.join(VAULT_ROOT, "01_Daily")

GOOGLE_CALENDAR_ENABLED = os.getenv("GOOGLE_CALENDAR_ENABLED", "false").lower() == "true"
TODOIST_API_KEY = os.getenv("TODOIST_API_KEY", "")

# Lazy imports (未インストール時にagent起動が壊れない)
_HAS_GCAL = False
_HAS_TODOIST = False
_HAS_DATEPARSER = False

try:
    import dateparser
    _HAS_DATEPARSER = True
except ImportError:
    pass

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    _HAS_GCAL = True
except ImportError:
    pass

try:
    from todoist_api_python.api import TodoistAPI
    _HAS_TODOIST = True
except ImportError:
    pass


# ---------------------------------------------------------
# Helpers: DateTime Parser
# ---------------------------------------------------------

def _parse_datetime(text: str) -> datetime.datetime:
    """自然言語の日時表現を datetime に変換する。"""
    now = datetime.datetime.now()
    text = text.strip()

    # 手動マッピング（高速）
    manual_map = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "今日": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "tomorrow": now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        "明日": now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        "明後日": now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=2),
    }
    if text.lower() in manual_map:
        return manual_map[text.lower()]

    # ISO 形式 (2026-02-10, 2026-02-10 14:00)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue

    # dateparser フォールバック
    if _HAS_DATEPARSER:
        parsed = dateparser.parse(text, languages=["ja", "en"],
                                  settings={"PREFER_DATES_FROM": "future"})
        if parsed:
            return parsed

    raise ValueError(f"日時を解析できませんでした: '{text}'")


# ---------------------------------------------------------
# Helpers: Service Clients
# ---------------------------------------------------------

def _get_calendar_service():
    """Google Calendar API サービスオブジェクトを取得する。"""
    if not GOOGLE_CALENDAR_ENABLED:
        return None
    if not _HAS_GCAL:
        return None
    if not os.path.exists(CREDENTIALS_PATH):
        return None

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds)


def _get_todoist_client():
    """Todoist API クライアントを取得する。"""
    if not TODOIST_API_KEY:
        return None
    if not _HAS_TODOIST:
        return None
    return TodoistAPI(TODOIST_API_KEY)


# ---------------------------------------------------------
# Tool: Google Calendar （gws CLI 版）
# ---------------------------------------------------------


# ---------------------------------------------------------
# Tool: Google Calendar （gws CLI 版）
# ---------------------------------------------------------

def tool_get_calendar(date: str = "today", days: int = 1) -> str:
    """Google Calendar から予定を取得する。"""
    # 日時の解析（従来と同じヘルパーを再利用）
    try:
        start_dt = _parse_datetime(date)
    except ValueError as e:
        return f"Error: {e}"

    end_dt = start_dt + datetime.timedelta(days=days)
    time_min = start_dt.isoformat() + "Z" if start_dt.tzinfo is None else start_dt.isoformat()
    time_max = end_dt.isoformat() + "Z" if end_dt.tzinfo is None else end_dt.isoformat()

    # gws CLI で予定を取得（JSON形式）
    data = _run_gws_command([
        "calendar", "events", "list",
        "--params", json.dumps({
            "calendarId": "primary",
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime"
        }),
        "--format", "json"
    ])

    # gws CLI が失敗した場合、従来の Google API 版にフォールバック
    if data is None:
        return _tool_get_calendar_legacy(date, days)

    events = data.get("items", [])
    if not events:
        date_str = start_dt.strftime("%Y-%m-%d")
        return f"## {date_str} の予定\n予定はありません。"

    # 従来と完全に同じ出力フォーマットを維持
    lines = [f"## {start_dt.strftime('%Y-%m-%d')} の予定"]
    for event in events:
        start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))
        end = event.get('end', {}).get('dateTime', event.get('end', {}).get('date', ''))

        if 'T' in start:
            start_time_str = datetime.datetime.fromisoformat(start).strftime('%H:%M')
            end_time_str = datetime.datetime.fromisoformat(end).strftime('%H:%M')
            time_range = f"{start_time_str}-{end_time_str}"
        else:
            time_range = "終日"

        summary = event.get('summary', '(無題)')
        lines.append(f"- {time_range}: {summary}")

    return "\n".join(lines)


# ---------------------------------------------------------
# Legacy: 旧 Google API 版 tool_get_calendar（フォールバック用）
# gws CLI が利用できない場合にのみ呼ばれる。
# ---------------------------------------------------------

def _tool_get_calendar_legacy(date: str = "today", days: int = 1) -> str:
    """Google Calendar から予定を取得する（旧 Google API 版）。"""
    service = _get_calendar_service()
    if service is None:
        if not GOOGLE_CALENDAR_ENABLED:
            return ("Google Calendar が未設定です。\n"
                    "1. Google Cloud Console で Calendar API を有効化\n"
                    f"2. OAuth 2.0 クライアントID を作成し credentials.json を {CREDENTIALS_PATH} に配置\n"
                    "3. .env の GOOGLE_CALENDAR_ENABLED=true に変更")
        if not _HAS_GCAL:
            return "Error: google-api-python-client がインストールされていません。"
        return "Error: credentials.json が見つかりません。"

    try:
        start_dt = _parse_datetime(date)
    except ValueError as e:
        return f"Error: {e}"

    time_min = start_dt.isoformat() + "Z" if start_dt.tzinfo is None else start_dt.isoformat()
    end_dt = start_dt + datetime.timedelta(days=days)
    time_max = end_dt.isoformat() + "Z" if end_dt.tzinfo is None else end_dt.isoformat()

    try:
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
        ).execute()
        events = events_result.get('items', [])
    except Exception as e:
        return f"Error: Google Calendar API エラー: {e}"

    if not events:
        date_str = start_dt.strftime("%Y-%m-%d")
        return f"## {date_str} の予定\n予定はありません。"

    lines = [f"## {start_dt.strftime('%Y-%m-%d')} の予定"]
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        end = event['end'].get('dateTime', event['end'].get('date'))

        if 'T' in start:
            start_time = datetime.datetime.fromisoformat(start).strftime('%H:%M')
            end_time = datetime.datetime.fromisoformat(end).strftime('%H:%M')
            time_range = f"{start_time}-{end_time}"
        else:
            time_range = "終日"

        summary = event.get('summary', '(無題)')
        lines.append(f"- {time_range}: {summary}")

    return "\n".join(lines)


def tool_add_event(title: str, start_time: str,
                   end_time: str = "", description: str = "") -> str:
    """Google Calendar に予定を追加する。"""
    # 日時の解析（従来と同じヘルパーを再利用）
    try:
        start_dt = _parse_datetime(start_time)
    except ValueError as e:
        return f"Error: 開始時刻の解析に失敗: {e}"

    if end_time:
        try:
            end_dt = _parse_datetime(end_time)
        except ValueError:
            end_dt = start_dt + datetime.timedelta(hours=1)
    else:
        end_dt = start_dt + datetime.timedelta(hours=1)

    event_body = {
        'summary': title,
        'description': description,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
    }

    # gws CLI で予定を追加
    data = _run_gws_command([
        "calendar", "events", "insert",
        "--params", json.dumps({
            "calendarId": "primary",
            "requestBody": event_body
        }),
        "--format", "json"
    ])

    # gws CLI が失敗した場合、従来の Google API 版にフォールバック
    if data is None:
        return _tool_add_event_legacy(title, start_time, end_time, description)

    return (f"Success: 予定を追加しました: {title}\n"
            f"  日時: {start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%H:%M')}")


# ---------------------------------------------------------
# Legacy: 旧 Google API 版 tool_add_event（フォールバック用）
# gws CLI が利用できない場合にのみ呼ばれる。
# ---------------------------------------------------------

def _tool_add_event_legacy(title: str, start_time: str,
                           end_time: str = "", description: str = "") -> str:
    """Google Calendar に予定を追加する（旧 Google API 版）。"""
    service = _get_calendar_service()
    if service is None:
        return "Google Calendar が未設定です。credentials.json を配置し .env を設定してください。"

    try:
        start_dt = _parse_datetime(start_time)
    except ValueError as e:
        return f"Error: 開始時刻の解析に失敗: {e}"

    if end_time:
        try:
            end_dt = _parse_datetime(end_time)
        except ValueError:
            end_dt = start_dt + datetime.timedelta(hours=1)
    else:
        end_dt = start_dt + datetime.timedelta(hours=1)

    event = {
        'summary': title,
        'description': description,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Tokyo'},
    }

    try:
        created = service.events().insert(calendarId='primary', body=event).execute()
        return (f"Success: 予定を追加しました: {title}\n"
                f"  日時: {start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%H:%M')}")
    except Exception as e:
        return f"Error: 予定の追加に失敗: {e}"


# ---------------------------------------------------------
# Tool: Daily Summary（gws workflow +standup-report）
# カレンダー予定と Google Tasks を一括取得してサマリーを生成する。
# ---------------------------------------------------------

def tool_daily_summary() -> str:
    """今日のカレンダー予定と未完了タスクをまとめたデイリーサマリーを生成する。"""
    data = _run_gws_command(["workflow", "+standup-report", "--format", "json"])

    if data is None or "error" in data:
        # gws が利用できない/認証切れの場合、カレンダーAPIのみのフォールバック
        # (エラーdictを .get() で処理すると「予定なし」と偽装されるため必ずここで弾く)
        return tool_get_calendar("today")

    date_str = data.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
    meetings = data.get("meetings", [])
    tasks = data.get("tasks", [])

    lines = [f"# 📅 {date_str} のデイリーサマリー", ""]

    # カレンダー予定セクション
    lines.append(f"## 予定 ({data.get('meetingCount', len(meetings))}件)")
    if meetings:
        for m in meetings:
            start = m.get("start", "")
            end = m.get("end", "")
            summary = m.get("summary", "(無題)")
            if "T" in start:
                start_fmt = datetime.datetime.fromisoformat(start).strftime("%H:%M")
                end_fmt = datetime.datetime.fromisoformat(end).strftime("%H:%M")
                lines.append(f"- {start_fmt}-{end_fmt}: {summary}")
            else:
                lines.append(f"- 終日: {summary}")
    else:
        lines.append("- 予定はありません。")

    lines.append("")

    # タスクセクション
    lines.append(f"## 未完了タスク ({data.get('taskCount', len(tasks))}件)")
    if tasks:
        for t in tasks:
            title = t.get("title", "(無題)")
            due = t.get("due", "")
            due_str = f" (期限: {due})" if due else ""
            lines.append(f"- [ ] {title}{due_str}")
    else:
        lines.append("- タスクはありません。")

    return "\n".join(lines)


# ---------------------------------------------------------
# Tool: Todoist
# ---------------------------------------------------------

_PRIORITY_MAP = {1: 4, 2: 3, 3: 2, 4: 1}  # ユーザー向け(1=最高) → Todoist内部(4=最高)
_PRIORITY_LABEL = {4: "最高", 3: "高", 2: "中", 1: "低"}


def tool_create_task(title: str, due_date: str = "",
                     priority: int = 1, project: str = "Inbox") -> str:
    """Todoist にタスクを追加する。"""
    api = _get_todoist_client()
    if api is None:
        if not TODOIST_API_KEY:
            return ("Todoist が未設定です。\n"
                    "1. https://todoist.com/app/settings/integrations → API トークン取得\n"
                    "2. .env の TODOIST_API_KEY に設定")
        return "Error: todoist-api-python がインストールされていません。"

    todoist_priority = _PRIORITY_MAP.get(priority, 1)

    kwargs = {
        "content": title,
        "priority": todoist_priority,
    }
    if due_date:
        kwargs["due_string"] = due_date
    if project and project != "Inbox":
        # プロジェクト名からIDを解決
        try:
            projects = api.get_projects()
            for p in projects:
                if p.name == project:
                    kwargs["project_id"] = p.id
                    break
        except Exception:
            pass

    try:
        task = api.add_task(**kwargs)
        due_info = f" (期限: {due_date})" if due_date else ""
        return f"Success: タスクを追加しました: {title}{due_info}"
    except Exception as e:
        return f"Error: タスクの追加に失敗: {e}"


def tool_get_tasks(filter: str = "today") -> str:
    """Todoist からタスク一覧を取得する。"""
    api = _get_todoist_client()
    if api is None:
        if not TODOIST_API_KEY:
            return ("Todoist が未設定です。\n"
                    "1. https://todoist.com/app/settings/integrations → API トークン取得\n"
                    "2. .env の TODOIST_API_KEY に設定")
        return "Error: todoist-api-python がインストールされていません。"

    filter_map = {
        "today": "today",
        "overdue": "overdue",
        "week": "7 days",
        "all": "all",
    }
    todoist_filter = filter_map.get(filter, filter)

    try:
        # todoist-api-python v3+: filter_tasks(query=) returns Iterator[list[Task]]
        tasks = []
        for page in api.filter_tasks(query=todoist_filter):
            tasks.extend(page)
    except Exception as e:
        return f"Error: タスクの取得に失敗: {e}"

    if not tasks:
        return f"## {filter} のタスク\nタスクはありません。"

    lines = [f"## {filter} のタスク ({len(tasks)}件)"]
    for task in tasks:
        p_label = _PRIORITY_LABEL.get(task.priority, "")
        due_str = ""
        if task.due:
            due_str = f" (期限: {task.due.date})"
        lines.append(f"- [ ] {task.content}{due_str} {f'[{p_label}]' if p_label else ''}")

    return "\n".join(lines)


# ---------------------------------------------------------
# Tool: Sync to Daily Note
# ---------------------------------------------------------

def tool_sync_to_daily_note() -> str:
    """Google Calendar と Todoist の内容を Obsidian デイリーノートに同期する。"""
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    daily_path = os.path.join(DAILY_NOTE_DIR, f"{today_str}.md")

    if not os.path.exists(daily_path):
        return f"Error: デイリーノートが見つかりません: {daily_path}"

    with open(daily_path, 'r', encoding='utf-8') as f:
        content = f.read()

    synced_items = []

    # Google Calendar 同期
    cal_content = ""
    if GOOGLE_CALENDAR_ENABLED and _get_calendar_service() is not None:
        cal_result = tool_get_calendar("today")
        # 失敗判定は is_failure_result() に一本化する。ここが取りこぼすと
        # エラー文がそのまま「今日の予定」としてデイリーノートに書き込まれる。
        # "Google Calendar が未設定です" は失敗ではなく未設定のセンチネルなので別扱い。
        if not is_failure_result(cal_result) and not cal_result.startswith("Google Calendar"):
            # ヘッダーを除去してコンテンツのみ
            cal_lines = cal_result.split("\n")[1:]  # "## ... の予定" を除去
            cal_content = "\n".join(cal_lines) if cal_lines else "予定はありません。"
            synced_items.append("予定")

    # Todoist 同期
    tasks_content = ""
    if TODOIST_API_KEY and _get_todoist_client() is not None:
        tasks_result = tool_get_tasks("today")
        if not is_failure_result(tasks_result) and not tasks_result.startswith("Todoist"):
            tasks_lines = tasks_result.split("\n")[1:]  # ヘッダー除去
            tasks_content = "\n".join(tasks_lines) if tasks_lines else "タスクはありません。"
            synced_items.append("タスク")

    if not synced_items:
        return "同期するサービスが設定されていません。Google Calendar または Todoist を設定してください。"

    updated = content
    if cal_content and "<!-- CALENDAR_PLACEHOLDER -->" in updated:
        updated = updated.replace("<!-- CALENDAR_PLACEHOLDER -->", cal_content)
    if tasks_content and "<!-- TASKS_PLACEHOLDER -->" in updated:
        updated = updated.replace("<!-- TASKS_PLACEHOLDER -->", tasks_content)

    if updated != content:
        with open(daily_path, 'w', encoding='utf-8') as f:
            f.write(updated)

    return f"Success: デイリーノートを同期しました ({', '.join(synced_items)})"
