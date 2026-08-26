# -*- coding: utf-8 -*-
r"""
毎朝のニュース取得トリガースクリプト。
tool_fetch_daily_news() を実行する。
n8n や Windows Task Scheduler から呼び出して使う。

Usage:
    python C:\AI_agent\trigger_daily_news.py
    python C:\AI_agent\trigger_daily_news.py --days 3
"""

import sys
import os
import argparse

sys.stdout.reconfigure(encoding='utf-8')
# ルート直書きはこのマシン専用になるので __file__ 相対 (このファイルはルート直下)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

import agent_hello

def main():
    parser = argparse.ArgumentParser(description='Fetch daily news')
    parser.add_argument('--days', type=int, default=1,
                        help='How many days back to search (default: 1)')
    args = parser.parse_args()

    result = agent_hello.tool_fetch_daily_news(days=args.days)
    print(result)


if __name__ == '__main__':
    main()
