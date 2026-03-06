# -*- coding: utf-8 -*-
"""
Gmail AI Secretary (gws CLI Integration)
メールのトリアージ（重要度判定）、要約、Obsidian保存を行う。
Briefing Manager の Provider として自動登録される。
"""

import os
import sys
import datetime
import logging

sys.stdout.reconfigure(encoding='utf-8')

from lib.lib_shared import (
    client, MODEL_NAME, VAULT_ROOT, retry, safe_json_loads, _run_gws_command
)
from lib.lib_obsidian_utils import get_obsidian_uri
from tools.tool_briefing_manager import add_provider

logger = logging.getLogger("GmailManager")

# ---------------------------------------------------------
# Gmail Data Fetching (via gws CLI)
# ---------------------------------------------------------

def fetch_emails_via_gws(query: str = "is:unread", max_results: int = 20) -> list[dict]:
    """gws gmail +triage を使用してメール一覧を取得する。"""
    args = ["gmail", "+triage", "--query", query, "--max", str(max_results), "--format", "json"]
    data = _run_gws_command(args)
    if not data or "messages" not in data:
        return []
    return data["messages"]

# ---------------------------------------------------------
# Briefing Provider
# ---------------------------------------------------------

def get_gmail_summary() -> str:
    """Briefing Provider: 未読メールのトリアージ要約を返す。"""
    emails = fetch_emails_via_gws(max_results=15)
    
    if not emails:
        return "Gmail: 新着（未読）メールはありません。"

    # Gemini に渡してトリアージ & 要約
    email_list_text = "\n".join([
        f"- From: {e['from']} | Subject: {e['subject']}"
        for e in emails
    ])
    
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    prompt = f"""あなたは優秀な秘書です。今日は {today_str} です。
以下の未読メール一覧から、ユーザーが今日確認すべき重要なメールを最大3通ピックアップし、
それぞれ1行で要約してください。緊急性やアクションの要否を重視してください。

【未読メール一覧】
{email_list_text}

【出力形式】
- 📧 合計 {len(emails)} 件の未読
- [🔴/🟡] (差出人) 件名の要約 (アクション)
...
"""
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME, contents=prompt,
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.error(f"Gmail summary error: {e}")
        return f"Gmail: {len(emails)} 件の未読あり (要約エラー)"

# ---------------------------------------------------------
# Obsidian Save & Detail Check
# ---------------------------------------------------------

def tool_gmail_sync_important() -> str:
    """重要と思われるメールを検出し、Obsidian に保存する（gwsベース）。"""
    emails = fetch_emails_via_gws(max_results=10)
    if not emails:
        return "新着メールはありません。"

    # 重要度判定 (Gemini)
    email_indices = "\n".join([f"[{i}] {e['from']}: {e['subject']}" for i, e in enumerate(emails)])
    prompt = f"""以下のメールリストから、Obsidianに記録保存すべき重要なメール（仕事、請求書、重要な連絡）を選んでください。
選んだ番号をJSON形式で返してください。

{email_indices}

形式: {{"important": [0, 2]}}
"""
    try:
        resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        result = safe_json_loads(resp.text)
        indices = result.get("important", [])
    except Exception as e:
        logger.warning(f"Gemini triage error: {e}")
        indices = []

    saved_files = []
    for idx in indices:
        if 0 <= idx < len(emails):
            email = emails[idx]
            # 詳細は snippet しかないが、gws+triage では十分
            # 本文が必要な場合は gws users messages get を使う必要があるが、まずは簡易版
            path = _save_email_to_vault(email)
            saved_files.append(path)

    if not saved_files:
        return f"{len(emails)} 件中、重要なメールは見つかりませんでした。"
    
    return f"Success: {len(saved_files)} 件の重要メールを保存しました: {', '.join([os.path.basename(p) for p in saved_files])}"

def _save_email_to_vault(email: dict) -> str:
    """メール情報を Markdown として保存する。"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    safe_subject = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in email['subject'][:50]).strip()
    filename = f"Email_{today_str}_{safe_subject}.md"
    filepath = os.path.join(VAULT_ROOT, "30_Main", filename)
    
    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{email['id']}"
    
    content = f"""---
date: {today_str}
type: email
from: "{email['from']}"
subject: "{email['subject']}"
link: "{gmail_url}"
---

# {email['subject']}

**From:** {email['from']}
**Date:** {email['date']}

> [Gmailで開く]({gmail_url})

---
（※全文の取得が必要な場合は個別に「詳細を取得」と指示してください）
"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath

# ---------------------------------------------------------
# Registration
# ---------------------------------------------------------

add_provider("Gmail", get_gmail_summary)
