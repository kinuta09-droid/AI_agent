# -*- coding: utf-8 -*-
"""
毎朝のニュース取得トリガースクリプト。
デイリーノートが未作成なら自動生成 → tool_fetch_daily_news() を実行。
n8n や Windows Task Scheduler から呼び出して使う。

Usage:
    python C:\AI_agent\trigger_daily_news.py
    python C:\AI_agent\trigger_daily_news.py --days 3
"""

import sys
import os
import datetime
import argparse

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\AI_agent')

from dotenv import load_dotenv
load_dotenv(r'C:\AI_agent\.env')

import agent_hello

VAULT_ROOT = r'D:\マイドライブ\Obsidian'
DAILY_DIR = os.path.join(VAULT_ROOT, '01_Daily')
TEMPLATE_PATH = os.path.join(VAULT_ROOT, '99_Assets', 'Templates', 'Tpl_Daily.md')


def ensure_daily_note():
    """今日のデイリーノートがなければテンプレートから自動作成する"""
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    daily_path = os.path.join(DAILY_DIR, f"{today_str}.md")

    if os.path.exists(daily_path):
        print(f"[Trigger] Daily note exists: {today_str}.md")
        return

    print(f"[Trigger] Creating daily note: {today_str}.md")
    os.makedirs(DAILY_DIR, exist_ok=True)

    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('{{date}}', today_str)
    else:
        content = f"---\ntags:\n  - type/daily\ncreated: {today_str}\n---\n\n# {today_str}\n"

    with open(daily_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"[Trigger] Created: {daily_path}")


def main():
    parser = argparse.ArgumentParser(description='Fetch daily news')
    parser.add_argument('--days', type=int, default=1,
                        help='How many days back to search (default: 1)')
    args = parser.parse_args()

    ensure_daily_note()
    result = agent_hello.tool_fetch_daily_news(days=args.days)
    print(result)


if __name__ == '__main__':
    main()
