# -*- coding: utf-8 -*-
"""
Analytics Skill — エージェントパフォーマンス & ログ分析ツール
読み取り専用。元ファイルを一切変更しない。
"""

import os
import sys
import json
import re
from datetime import datetime
from collections import Counter

from lib.lib_shared import load_json

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# Paths (ReadOnly)
# ---------------------------------------------------------

LEARNING_DB = r'C:\AI_agent\learning_db.json'
SCHEDULER_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scheduler.log')
ERROR_LOG = r'D:\マイドライブ\Obsidian\Dev_Logs\error.log'
CHAT_HISTORY = r'D:\マイドライブ\Obsidian\Dev_Logs\chat_history.json'


# ---------------------------------------------------------
# 1. Agent Performance Analysis
# ---------------------------------------------------------

def analyze_agent_performance() -> str:
    """learning_db.json からエージェントの実行パフォーマンスを分析する。"""
    if not os.path.exists(LEARNING_DB):
        return "❌ learning_db.json が見つかりません。"

    data = load_json(LEARNING_DB, default={})

    executions = data.get('executions', [])
    if not executions:
        return "📭 実行記録がありません。"

    total = len(executions)

    # --- 成功率 ---
    outcomes = Counter(e.get('outcome', 'unknown') for e in executions)
    success = outcomes.get('success', 0)
    partial = outcomes.get('partial', 0)

    success_rate = (success / total) * 100 if total else 0
    partial_rate = (partial / total) * 100 if total else 0
    fail_rate = 100 - success_rate - partial_rate

    # --- 所要時間 ---
    durations = [e['duration_seconds'] for e in executions if 'duration_seconds' in e]
    avg_duration = sum(durations) / len(durations) if durations else 0

    success_durations = [
        e['duration_seconds'] for e in executions
        if e.get('outcome') == 'success' and 'duration_seconds' in e
    ]
    fail_durations = [
        e['duration_seconds'] for e in executions
        if e.get('outcome') != 'success' and 'duration_seconds' in e
    ]
    avg_success_dur = sum(success_durations) / len(success_durations) if success_durations else 0
    avg_fail_dur = sum(fail_durations) / len(fail_durations) if fail_durations else 0

    # --- ツール使用頻度 ---
    tool_counter = Counter()
    for e in executions:
        for t in e.get('tools_used', []):
            tool_counter[t] += 1

    top_tools = tool_counter.most_common(10)

    # --- 失敗理由 ---
    failure_reasons = [
        e.get('failure_reason', '')
        for e in executions
        if e.get('failure_reason')
    ]
    reason_counter = Counter()
    for r in failure_reasons:
        # エラーの種類を正規化（具体的な引数名等を除去）
        simplified = re.sub(r"'[^']*'", "'...'", r)
        simplified = re.sub(r'"[^"]*"', '"..."', simplified)
        reason_counter[simplified] += 1

    top_failures = reason_counter.most_common(5)

    # --- Critique ステータス ---
    critique_counter = Counter(e.get('critique_status', 'unknown') for e in executions)

    # --- 日次推移 ---
    daily = {}
    for e in executions:
        ts = e.get('timestamp', '')[:10]  # YYYY-MM-DD
        if ts not in daily:
            daily[ts] = {'total': 0, 'success': 0}
        daily[ts]['total'] += 1
        if e.get('outcome') == 'success':
            daily[ts]['success'] += 1

    # --- レポート生成 ---
    lines = []
    lines.append("## 📊 Agent Performance Report")
    lines.append("")
    lines.append(f"**分析期間**: {executions[0].get('timestamp', '?')[:10]} 〜 {executions[-1].get('timestamp', '?')[:10]}")
    lines.append(f"**総実行回数**: {total}")
    lines.append("")

    lines.append("### 成功率")
    lines.append(f"- ✅ 成功: {success}/{total} ({success_rate:.1f}%)")
    lines.append(f"- ⚠️ 部分成功: {partial}/{total} ({partial_rate:.1f}%)")
    lines.append(f"- ❌ 失敗: {total - success - partial}/{total} ({fail_rate:.1f}%)")
    lines.append("")

    lines.append("### 所要時間")
    lines.append(f"- 平均: {avg_duration:.1f}秒")
    lines.append(f"- 成功時の平均: {avg_success_dur:.1f}秒")
    lines.append(f"- 失敗時の平均: {avg_fail_dur:.1f}秒")
    lines.append("")

    lines.append("### ツール使用頻度 TOP10")
    for tool, count in top_tools:
        bar = "█" * count
        lines.append(f"- `{tool}`: {count}回 {bar}")
    lines.append("")

    if top_failures:
        lines.append("### よくある失敗理由")
        for reason, count in top_failures:
            lines.append(f"- ({count}回) {reason}")
        lines.append("")

    lines.append("### Critique ステータス")
    for status, count in critique_counter.most_common():
        lines.append(f"- {status}: {count}回")
    lines.append("")

    lines.append("### 日次推移")
    for date in sorted(daily.keys()):
        d = daily[date]
        rate = (d['success'] / d['total'] * 100) if d['total'] else 0
        lines.append(f"- {date}: {d['success']}/{d['total']} ({rate:.0f}%)")

    return "\n".join(lines)


# ---------------------------------------------------------
# 2. Scheduler Log Analysis
# ---------------------------------------------------------

def analyze_scheduler_log() -> str:
    """scheduler.log を解析してタスク実行状況を報告する。"""
    if not os.path.exists(SCHEDULER_LOG):
        return "❌ scheduler.log が見つかりません。"

    with open(SCHEDULER_LOG, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.strip():
        return "📭 scheduler.log は空です。"

    lines = content.strip().split('\n')

    # パターン: 2026-02-07 09:36:04,206 - INFO - ⏳ Started...
    log_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),\d+ - (\w+) - (.+)'
    )

    entries = []
    task_counter = Counter()
    status_counter = Counter()

    for line in lines:
        m = log_pattern.match(line)
        if m:
            timestamp_str, level, message = m.groups()
            entries.append({
                'timestamp': timestamp_str,
                'level': level,
                'message': message.strip(),
            })

            # タスク名を抽出
            if 'Started' in message or 'Completed' in message or 'Failed' in message:
                # メッセージからタスク名を推測
                task_name = re.sub(r'^[⏳✅❌]\s*', '', message)
                task_name = re.sub(r'^(Started|Completed|Failed)\s*', '', task_name)
                task_name = task_name.strip('. ')
                if task_name:
                    task_counter[task_name] += 1

            if '✅' in message or 'Completed' in message:
                status_counter['success'] += 1
            elif '❌' in message or 'Failed' in message:
                status_counter['failed'] += 1
            elif '⏳' in message or 'Started' in message:
                status_counter['started'] += 1

    report = []
    report.append("## 📋 Scheduler Log Report")
    report.append("")
    report.append(f"**ログ行数**: {len(lines)}")
    report.append(f"**解析エントリ数**: {len(entries)}")
    report.append("")

    report.append("### 実行ステータス")
    report.append(f"- ⏳ 開始: {status_counter.get('started', 0)}")
    report.append(f"- ✅ 成功: {status_counter.get('success', 0)}")
    report.append(f"- ❌ 失敗: {status_counter.get('failed', 0)}")
    report.append("")

    if task_counter:
        report.append("### タスク別回数")
        for task, count in task_counter.most_common():
            report.append(f"- {task}: {count}回")
        report.append("")

    if entries:
        report.append("### 最新ログ (直近5件)")
        for entry in entries[-5:]:
            report.append(f"- [{entry['timestamp']}] {entry['level']}: {entry['message']}")

    return "\n".join(report)


# ---------------------------------------------------------
# 3. Error Pattern Analysis
# ---------------------------------------------------------

def analyze_errors(pattern: str = None) -> str:
    """error.log を解析してエラーパターンを集計する。"""
    if not os.path.exists(ERROR_LOG):
        return "❌ error.log が見つかりません。"

    # エンコーディングを自動検出して読む
    content = None
    for enc in ['utf-8', 'utf-16', 'utf-16-le', 'shift_jis', 'cp932']:
        try:
            with open(ERROR_LOG, 'r', encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if content is None:
        return "❌ error.log のエンコーディングを判定できません。"

    if not content.strip():
        return "📭 error.log は空です。"

    lines = content.strip().split('\n')

    # パターンでフィルタ
    if pattern:
        lines = [l for l in lines if pattern.lower() in l.lower()]
        if not lines:
            return f"📭 パターン '{pattern}' に一致するエラーはありません。"

    # エラー種別の集計
    error_types = Counter()
    for line in lines:
        # Pythonトレースバックのエラー行を検出
        err_match = re.search(r'(\w+Error|\w+Exception|Error|Warning)[:（]', line)
        if err_match:
            error_types[err_match.group(1)] += 1

    report = []
    report.append("## 🔴 Error Log Report")
    report.append("")
    report.append(f"**総行数**: {len(lines)}")
    if pattern:
        report.append(f"**フィルタ**: `{pattern}`")
    report.append("")

    if error_types:
        report.append("### エラー種別")
        for err, count in error_types.most_common():
            report.append(f"- `{err}`: {count}回")
        report.append("")

    report.append("### ログ内容")
    # 最大20行まで表示
    for line in lines[:20]:
        report.append(f"  {line.strip()}")
    if len(lines) > 20:
        report.append(f"  ... (他 {len(lines) - 20} 行)")

    return "\n".join(report)


# ---------------------------------------------------------
# 4. Unified Runner (自然言語ルーティング)
# ---------------------------------------------------------

def run_analytics(question: str = "") -> str:
    """
    自然言語の質問に応じて適切な分析を実行する。
    main.py のツールルーターから呼ばれる。
    """
    q = question.lower() if question else ""

    # キーワードマッチングで分析を選択
    if any(k in q for k in ['パフォーマンス', '成功率', '失敗', 'ツール', '実行', '学習', 'agent', 'performance']):
        return analyze_agent_performance()
    elif any(k in q for k in ['スケジューラ', 'scheduler', 'タスク', '定時', 'cron']):
        return analyze_scheduler_log()
    elif any(k in q for k in ['エラー', 'error', 'ログ', 'log', 'バグ', 'bug']):
        # パターンを抽出（「XXXエラー」→ XXXをパターンに）
        pattern_match = re.search(r'[「『](.+?)[」』]', question)
        p = pattern_match.group(1) if pattern_match else None
        return analyze_errors(pattern=p)
    else:
        # デフォルト: 全分析を実行
        results = []
        results.append(analyze_agent_performance())
        results.append("")
        results.append("---")
        results.append("")
        results.append(analyze_scheduler_log())
        results.append("")
        results.append("---")
        results.append("")
        results.append(analyze_errors())
        return "\n".join(results)


# ---------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Analytics Skill — データ分析ツール')
    parser.add_argument('--agent', action='store_true', help='エージェントパフォーマンス分析')
    parser.add_argument('--scheduler', action='store_true', help='スケジューラログ分析')
    parser.add_argument('--errors', action='store_true', help='エラーログ分析')
    parser.add_argument('--pattern', type=str, help='エラーフィルタパターン')
    parser.add_argument('--all', action='store_true', help='全分析を実行')
    parser.add_argument('--test', action='store_true', help='簡易テスト（全分析を実行して成功を確認）')
    parser.add_argument('question', nargs='?', default='', help='自然言語の質問')

    args = parser.parse_args()

    if args.test:
        print("🧪 Analytics Skill テスト実行中...")
        print()
        result = run_analytics("全体のパフォーマンスを見せて")
        print(result)
        print()
        print("✅ テスト完了")
    elif args.agent:
        print(analyze_agent_performance())
    elif args.scheduler:
        print(analyze_scheduler_log())
    elif args.errors:
        print(analyze_errors(pattern=args.pattern))
    elif args.all or not args.question:
        print(run_analytics())
    else:
        print(run_analytics(args.question))
