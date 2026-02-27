# -*- coding: utf-8 -*-
"""
Gmail AI Secretary
メールのトリアージ（重要度判定）、要約、Obsidian保存を行う。
Briefing Manager の Provider として自動登録される。
"""

import os
import sys
import datetime

sys.stdout.reconfigure(encoding='utf-8')

from lib.lib_shared import (
    client, MODEL_NAME, VAULT_ROOT, retry, safe_json_loads,
)
from lib.lib_obsidian_utils import get_obsidian_uri
from lib.lib_gmail_client import fetch_recent_emails
from tools.tool_briefing_manager import add_provider


# ---------------------------------------------------------
# AI Triage
# ---------------------------------------------------------

@retry(max_retries=2)
def _triage_emails(emails: list[dict]) -> dict:
    """Gemini でメールを重要度別に分類する。

    Returns:
        {"high": [email_dict...], "medium": [...], "low": [...]}
    """
    if not emails:
        return {"high": [], "medium": [], "low": []}

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    email_list = []
    for i, e in enumerate(emails):
        email_list.append(
            f"[{i}] From: {e['from']}\n"
            f"    Subject: {e['subject']}\n"
            f"    Snippet: {e['snippet'][:200]}"
        )
    emails_text = "\n\n".join(email_list)

    prompt = f"""あなたはメール秘書です。今日は {today_str} です。
以下のメール一覧を、重要度で分類してください。

分類基準:
- high: 即時確認が必要（仕事の依頼、緊急連絡、重要な返信待ち、支払い・契約関連）
- medium: 今日中に確認すれば良い（ニュースレター、SNS通知、定期レポート）
- low: 無視してよい（広告、プロモーション、自動通知、スパム）

メール一覧:
{emails_text}

JSONで回答してください:
{{"high": [0, 3], "medium": [1], "low": [2, 4]}}
番号のみのリストで。"""

    response = client.models.generate_content(
        model=MODEL_NAME, contents=prompt,
    )
    result = safe_json_loads((response.text or "").strip())

    categorized = {"high": [], "medium": [], "low": []}
    for level in ("high", "medium", "low"):
        for idx in result.get(level, []):
            if isinstance(idx, int) and 0 <= idx < len(emails):
                categorized[level].append(emails[idx])

    return categorized


# ---------------------------------------------------------
# Obsidian Save
# ---------------------------------------------------------

def _save_important_to_vault(high_emails: list[dict]) -> list[str]:
    """重要度「高」のメールを 30_Main/ に .md ファイルとして保存する。

    Returns:
        保存したファイルパスのリスト
    """
    if not high_emails:
        return []

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    save_dir = os.path.join(VAULT_ROOT, "30_Main")
    os.makedirs(save_dir, exist_ok=True)
    saved_paths = []

    for i, email in enumerate(high_emails):
        # ファイル名のサニタイズ
        safe_subject = "".join(
            c if c.isalnum() or c in (' ', '-', '_') else '_'
            for c in email['subject'][:50]
        ).strip()
        filename = f"Email_{today_str}_{i+1}_{safe_subject}.md"
        filepath = os.path.join(save_dir, filename)

        # 同日再実行時の上書き防止
        if os.path.exists(filepath):
            hhmm = datetime.datetime.now().strftime("%H%M")
            filename = f"Email_{today_str}_{i+1}_{safe_subject}_{hhmm}.md"
            filepath = os.path.join(save_dir, filename)

        obsidian_uri = get_obsidian_uri(filepath)
        gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{email['id']}"

        # YAML safe: ダブルクォートをエスケープ
        safe_from = email['from'].replace('"', '\\"')
        safe_subj = email['subject'].replace('"', '\\"')

        content = f"""---
up: "[[10_MOCs/Dashboard]]"
date: {today_str}
type: email
from: "{safe_from}"
subject: "{safe_subj}"
---

> [Obsidian で開く]({obsidian_uri}) | [Gmail で開く]({gmail_url})

# {email['subject']}

**From:** {email['from']}
**Date:** {email['date']}

---

{email['body'][:1500]}
"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        saved_paths.append(filepath)
        print(f"[Gmail] 📝 Saved: {filepath}")

    return saved_paths


# ---------------------------------------------------------
# Briefing Provider
# ---------------------------------------------------------

def get_gmail_summary() -> str:
    """Briefing Provider: 直近12hの重要メール要約を返す。

    認証エラー時はエラーメッセージを返し、ブリーフィングの他プロバイダーは継続する。
    """
    try:
        emails = fetch_recent_emails(hours=12, max_results=10)
    except Exception as e:
        return f"Gmail: 接続エラー ({e})"

    if not emails:
        return "Gmail: 直近12時間の新着メールはありません。"

    try:
        triaged = _triage_emails(emails)
    except Exception as e:
        return f"Gmail: トリアージエラー ({e}). 未読{len(emails)}件あり。"

    lines = []
    high_count = len(triaged["high"])
    med_count = len(triaged["medium"])
    low_count = len(triaged["low"])

    lines.append(f"- 📧 合計 {len(emails)} 件 (高: {high_count}, 中: {med_count}, 低: {low_count})")

    for email in triaged["high"][:3]:
        lines.append(f"- 🔴 [{email['from'].split('<')[0].strip()}] {email['subject']}")

    for email in triaged["medium"][:2]:
        lines.append(f"- 🟡 [{email['from'].split('<')[0].strip()}] {email['subject']}")

    return "\n".join(lines)


# ---------------------------------------------------------
# Discord / Agent Tool
# ---------------------------------------------------------

def tool_gmail_check(hours: int = 12) -> str:
    """直近の重要メール3-5件を要約して返す。

    Args:
        hours: 何時間前まで遡るか（デフォルト12時間）

    Returns:
        Success メッセージ + ファイルパス、またはエラーメッセージ
    """
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    print(f"[Gmail] 📧 Checking emails (last {hours}h)...")

    try:
        emails = fetch_recent_emails(hours=hours, max_results=10)
    except Exception as e:
        return f"Error: Gmail接続失敗 ({e})"

    if not emails:
        return f"直近{hours}時間の新着メールはありません。"

    try:
        triaged = _triage_emails(emails)
    except Exception as e:
        return f"Error: トリアージ失敗 ({e})"

    # 重要メールをVaultに保存
    saved_paths = _save_important_to_vault(triaged["high"])

    # Gemini で要約生成
    try:
        summary = _generate_email_summary(triaged)
    except Exception as e:
        summary = f"(要約生成失敗: {e})"

    # 結果組み立て
    result_lines = [f"📧 **Gmail Check** — {today_str} (直近{hours}h)\n"]
    result_lines.append(summary)

    if saved_paths:
        result_lines.append("\n---\n📁 **Obsidian に保存済み:**")
        for j, (path, email) in enumerate(zip(saved_paths, triaged["high"])):
            filename = os.path.basename(path)
            wiki = filename.replace('.md', '')
            gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{email['id']}"
            result_lines.append(
                f"- [[30_Main/{wiki}]] (Path: {path})"
                f" (Gmail{j+1}: {gmail_url})"
            )

    return "\n".join(result_lines)


@retry(max_retries=2)
def _generate_email_summary(triaged: dict) -> str:
    """トリアージ結果を Gemini で要約する。"""
    sections = []

    for level, label, emoji in [
        ("high", "即時確認", "🔴"),
        ("medium", "後で確認", "🟡"),
    ]:
        for email in triaged.get(level, [])[:3]:
            sections.append(
                f"{emoji} **{label}** | {email['from'].split('<')[0].strip()}\n"
                f"件名: {email['subject']}\n"
                f"概要: {email['snippet'][:200]}"
            )

    if not sections:
        return "重要なメールはありません。"

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    prompt = f"""あなたは40代ビジネスパーソンのメール秘書です。今日は {today_str} です。
以下のメール一覧を、各メール1-2行で箇条書き要約してください。
アクションが必要な場合は明示してください。

{chr(10).join(sections)}

要約:"""

    response = client.models.generate_content(
        model=MODEL_NAME, contents=prompt,
    )
    return (response.text or "").strip()


# ---------------------------------------------------------
# Provider Registration
# ---------------------------------------------------------

add_provider("Gmail", get_gmail_summary)
