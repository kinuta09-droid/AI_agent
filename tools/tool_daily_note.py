# -*- coding: utf-8 -*-
"""
Daily Note Generator
Obsidian デイリーノートを生成・更新するツール。
Google Calendar / Todoist / News と連携。
"""

import os
import sys
import datetime
import re

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

VAULT_ROOT = r"D:\マイドライブ\Obsidian"
DAILY_NOTE_DIR = os.path.join(VAULT_ROOT, "01_Daily")
TEMPLATE_PATH = os.path.join(VAULT_ROOT, "99_Assets", "Templates", "Tpl_Daily.md")

# ---------------------------------------------------------
# Imports from other tools
# ---------------------------------------------------------

try:
    from tools.tool_calendar import (
        tool_get_calendar,
        tool_get_tasks,
        GOOGLE_CALENDAR_ENABLED,
        TODOIST_API_KEY,
    )
    _HAS_CALENDAR_TOOL = True
except ImportError:
    _HAS_CALENDAR_TOOL = False
    print("[DailyNote] Warning: tool_calendar not available")

try:
    from lib.agent_hello import tool_fetch_daily_news
    _HAS_NEWS_TOOL = True
except ImportError:
    _HAS_NEWS_TOOL = False
    print("[DailyNote] Warning: agent_hello (news) not available")


# ---------------------------------------------------------
# Helper: Create Daily Note from Template
# ---------------------------------------------------------

def _create_daily_note_from_template(date: datetime.date) -> str:
    """テンプレートから新しいデイリーノートを作成する。"""
    date_str = date.strftime('%Y-%m-%d')
    daily_path = os.path.join(DAILY_NOTE_DIR, f"{date_str}.md")
    
    if os.path.exists(daily_path):
        print(f"[DailyNote] Note already exists: {daily_path}")
        return daily_path
    
    if not os.path.exists(TEMPLATE_PATH):
        print(f"[DailyNote] Template not found: {TEMPLATE_PATH}")
        return ""
    
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # Replace template placeholders
    content = template.replace('{{date}}', date_str)
    
    os.makedirs(DAILY_NOTE_DIR, exist_ok=True)
    with open(daily_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"[DailyNote] Created: {daily_path}")
    return daily_path


# ---------------------------------------------------------
# Helper: Fetch Calendar Events
# ---------------------------------------------------------

def _fetch_calendar_content(date: datetime.date) -> str:
    """指定日のカレンダー予定を取得してMarkdown形式で返す。"""
    if not _HAS_CALENDAR_TOOL:
        return "カレンダー連携が未設定です。"
    
    if not GOOGLE_CALENDAR_ENABLED:
        return "Google Calendar が未設定です。"
    
    try:
        result = tool_get_calendar(date.strftime('%Y-%m-%d'))
        if result.startswith("Error:") or result.startswith("Google Calendar"):
            return result
        
        # Remove header line (## YYYY-MM-DD の予定)
        lines = result.split('\n')
        if lines and lines[0].startswith('##'):
            lines = lines[1:]
        
        return '\n'.join(lines).strip() if lines else "予定はありません。"
    except Exception as e:
        return f"カレンダー取得エラー: {e}"


# ---------------------------------------------------------
# Helper: Fetch Tasks
# ---------------------------------------------------------

def _fetch_tasks_content() -> str:
    """今日のタスクを取得してMarkdown形式で返す。"""
    if not _HAS_CALENDAR_TOOL:
        return "タスク連携が未設定です。"
    
    if not TODOIST_API_KEY:
        return "Todoist が未設定です。"
    
    try:
        result = tool_get_tasks("today")
        if result.startswith("Error:") or result.startswith("Todoist"):
            return result
        
        # Remove header line
        lines = result.split('\n')
        if lines and lines[0].startswith('##'):
            lines = lines[1:]
        
        return '\n'.join(lines).strip() if lines else "タスクはありません。"
    except Exception as e:
        return f"タスク取得エラー: {e}"


# ---------------------------------------------------------
# Helper: Fetch News
# ---------------------------------------------------------

def _fetch_news_content(date: datetime.date) -> str:
    """今日のニュースサマリーを取得する。"""
    if not _HAS_NEWS_TOOL:
        return "ニュース機能が利用できません。"
    
    date_str = date.strftime('%Y-%m-%d')
    news_file_path = os.path.join(VAULT_ROOT, "02_News", f"News_{date_str}.md")
    
    try:
        # Generate news file if not exists
        result = tool_fetch_daily_news()
        
        # Read the generated news file for summary
        if os.path.exists(news_file_path):
            with open(news_file_path, 'r', encoding='utf-8') as f:
                news_content = f.read()
            
            # Extract first paragraph after "要約" or after frontmatter
            lines = news_content.split('\n')
            summary_lines = []
            in_summary = False
            for line in lines:
                if '要約' in line or 'まとめ' in line or '概要' in line:
                    in_summary = True
                    continue
                if in_summary:
                    if line.strip() and not line.startswith('#') and not line.startswith('---'):
                        summary_lines.append(line.strip())
                        if len(summary_lines) >= 3:  # Max 3 lines
                            break
            
            if summary_lines:
                summary = '\n'.join(summary_lines)
            else:
                # Fallback: find first paragraph
                for line in lines:
                    if line.strip() and not line.startswith('#') and not line.startswith('---') and not line.startswith('>'):
                        summary = line.strip()[:200] + "..." if len(line) > 200 else line.strip()
                        break
                else:
                    summary = "ニュースの詳細はリンク先を参照してください。"
            
            return f"> {summary}\n\n👉 [[News_{date_str}|詳細はこちら]]"
        else:
            return result  # Return tool result if file not found
            
    except Exception as e:
        return f"ニュース取得エラー: {e}"


# ---------------------------------------------------------
# Main: Generate or Update Daily Note
# ---------------------------------------------------------

def generate_daily_note(date_str: str = None, include_news: bool = True) -> str:
    """
    デイリーノートを生成または更新する。
    
    Args:
        date_str: 日付文字列 (YYYY-MM-DD)。None の場合は今日。
        include_news: ニュースを含めるかどうか。
    
    Returns:
        結果メッセージ。
    """
    if date_str:
        try:
            date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return f"Error: 無効な日付形式: {date_str} (YYYY-MM-DD 形式で指定)"
    else:
        date = datetime.date.today()
    
    date_str = date.strftime('%Y-%m-%d')
    daily_path = os.path.join(DAILY_NOTE_DIR, f"{date_str}.md")
    
    # 1. Create from template if not exists
    if not os.path.exists(daily_path):
        created_path = _create_daily_note_from_template(date)
        if not created_path:
            return f"Error: デイリーノートの作成に失敗しました。"
    
    # 2. Read current content
    with open(daily_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    updated = content
    synced_items = []
    
    # 3. Replace Calendar Placeholder
    if '<!-- CALENDAR_PLACEHOLDER -->' in updated:
        cal_content = _fetch_calendar_content(date)
        updated = updated.replace('<!-- CALENDAR_PLACEHOLDER -->', cal_content)
        synced_items.append("カレンダー")
    
    # 4. Replace Tasks Placeholder
    if '<!-- TASKS_PLACEHOLDER -->' in updated:
        tasks_content = _fetch_tasks_content()
        updated = updated.replace('<!-- TASKS_PLACEHOLDER -->', tasks_content)
        synced_items.append("タスク")
    
    # 5. Replace News Placeholder
    if include_news and '<!-- NEWS_PLACEHOLDER -->' in updated:
        news_content = _fetch_news_content(date)
        updated = updated.replace('<!-- NEWS_PLACEHOLDER -->', news_content)
        synced_items.append("ニュース")
    
    # 6. Save
    if updated != content:
        with open(daily_path, 'w', encoding='utf-8') as f:
            f.write(updated)
        return f"Success: デイリーノート ({date_str}) を更新しました。同期: {', '.join(synced_items)}"
    else:
        return f"デイリーノート ({date_str}) は既に最新です。"


# ---------------------------------------------------------
# Standalone Execution
# ---------------------------------------------------------

if __name__ == "__main__":
    print("=== Daily Note Generator ===")
    result = generate_daily_note()
    print(result)
