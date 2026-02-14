# -*- coding: utf-8 -*-
"""
Gmail API Client
OAuth 2.0 認証と直近メール取得を担当する。
credentials_gmail.json / token_gmail.json を使用し、
既存の Calendar 認証 (credentials.json) とは独立して動作する。
"""

import os
import sys
import base64
import datetime

sys.stdout.reconfigure(encoding='utf-8')

from bs4 import BeautifulSoup

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

CREDENTIALS_GMAIL_PATH = os.path.join(r"C:\AI_agent", "credentials_gmail.json")
TOKEN_GMAIL_PATH = os.path.join(r"C:\AI_agent", "token_gmail.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ---------------------------------------------------------
# .gitignore 自動保護
# ---------------------------------------------------------

_GITIGNORE_PATH = os.path.join(r"C:\AI_agent", ".gitignore")


def _ensure_gitignore():
    """credentials_gmail.json と token_gmail.json が .gitignore に含まれていることを確認する。"""
    entries = ["credentials_gmail.json", "token_gmail.json"]
    if not os.path.exists(_GITIGNORE_PATH):
        return

    with open(_GITIGNORE_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    missing = [e for e in entries if e not in content]
    if missing:
        with open(_GITIGNORE_PATH, 'a', encoding='utf-8') as f:
            f.write("\n# Gmail credentials\n")
            for entry in missing:
                f.write(f"{entry}\n")
        print(f"[Gmail] .gitignore に追加: {', '.join(missing)}")


_ensure_gitignore()

# ---------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------

_HAS_GMAIL = False

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    _HAS_GMAIL = True
except ImportError:
    print("[Gmail] ⚠️ google-api-python-client が未インストール。Gmail機能は無効です。")


# ---------------------------------------------------------
# OAuth Service
# ---------------------------------------------------------

def _get_gmail_service():
    """Gmail API サービスオブジェクトを返す。認証失敗時は None。"""
    if not _HAS_GMAIL:
        return None
    if not os.path.exists(CREDENTIALS_GMAIL_PATH):
        print(f"[Gmail] ⚠️ {CREDENTIALS_GMAIL_PATH} が見つかりません。")
        return None

    creds = None
    if os.path.exists(TOKEN_GMAIL_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_GMAIL_PATH, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"[Gmail] ⚠️ トークンリフレッシュ失敗、再認証します: {e}")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_GMAIL_PATH, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_GMAIL_PATH, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


# ---------------------------------------------------------
# HTML → Plain Text
# ---------------------------------------------------------

def _html_to_text(html: str) -> str:
    """HTMLメール本文をプレーンテキストに変換する。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


# ---------------------------------------------------------
# Email Fetching
# ---------------------------------------------------------

def fetch_recent_emails(hours: int = 12, max_results: int = 10) -> list[dict]:
    """直近 N 時間のメールを取得する。

    Args:
        hours: 何時間前まで遡るか
        max_results: 取得する最大件数

    Returns:
        [{"id", "subject", "from", "date", "snippet", "body", "labels"}]
        認証失敗・エラー時は空リスト（システム停止しない）
    """
    try:
        service = _get_gmail_service()
    except Exception as e:
        print(f"[Gmail] ❌ 認証エラー: {e}")
        return []

    if not service:
        return []

    # after: UNIX timestamp
    after_ts = int((datetime.datetime.now() - datetime.timedelta(hours=hours)).timestamp())
    query = f"after:{after_ts}"

    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=max_results
        ).execute()
    except Exception as e:
        print(f"[Gmail] ❌ メール一覧取得失敗: {e}")
        return []

    messages = results.get('messages', [])
    if not messages:
        return []

    emails = []
    for msg_info in messages:
        try:
            msg = service.users().messages().get(
                userId='me', id=msg_info['id'], format='full'
            ).execute()
            emails.append(_parse_message(msg))
        except Exception as e:
            print(f"[Gmail] ⚠️ メール {msg_info['id']} 取得失敗: {e}")

    return emails


def _extract_body_from_parts(parts: list) -> str:
    """multipart の parts を再帰的に走査して本文を取得する。"""
    plain_text = ""
    html_text = ""

    for part in parts:
        mime = part.get('mimeType', '')

        # ネストされた multipart を再帰処理
        if mime.startswith('multipart/') and part.get('parts'):
            nested = _extract_body_from_parts(part['parts'])
            if nested:
                return nested

        data = part.get('body', {}).get('data', '')
        if data:
            decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if mime == 'text/plain':
                plain_text = decoded
            elif mime == 'text/html' and not html_text:
                html_text = _html_to_text(decoded)

    return plain_text or html_text


def _parse_message(msg: dict) -> dict:
    """Gmail API レスポンスから必要なフィールドを抽出する。"""
    headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

    # 本文取得
    body = ""
    payload = msg.get('payload', {})

    if payload.get('mimeType', '').startswith('text/'):
        body_data = payload.get('body', {}).get('data', '')
        if body_data:
            body = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')
    elif payload.get('parts'):
        body = _extract_body_from_parts(payload['parts'])

    return {
        "id": msg['id'],
        "subject": headers.get('Subject', '(件名なし)'),
        "from": headers.get('From', '(不明)'),
        "date": headers.get('Date', ''),
        "snippet": msg.get('snippet', ''),
        "body": body[:2000],  # 長すぎるメールは切り詰め
        "labels": msg.get('labelIds', []),
    }
