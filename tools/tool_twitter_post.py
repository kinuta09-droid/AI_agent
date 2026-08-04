# -*- coding: utf-8 -*-
"""
X (Twitter) 投稿ツール
OAuth 1.0a (Read and Write) で自分のアカウントに投稿するだけの最小ツール。
Pay Per Use課金 (投稿1件 $0.015、リンク付き $0.20) のため呼び出し回数に注意。
"""

import os
import sys
import requests
from requests_oauthlib import OAuth1

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

_TWEET_URL = "https://api.twitter.com/2/tweets"


def _get_auth():
    consumer_key = os.environ.get("X_CONSUMER_KEY")
    consumer_secret = os.environ.get("X_CONSUMER_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")
    if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
        return None
    return OAuth1(consumer_key, consumer_secret, access_token, access_token_secret)


def tool_post_tweet(text: str) -> str:
    """テキストをXに投稿する (OAuth 1.0a, Read and Write権限が必要)"""
    print(f"[Tool] 🐦 Posting to X: {text[:50]}...")
    auth = _get_auth()
    if auth is None:
        return "Error: X APIの認証情報(.env の X_CONSUMER_KEY等)が未設定です。"

    try:
        response = requests.post(_TWEET_URL, auth=auth, json={"text": text}, timeout=15)
        response.raise_for_status()
        tweet_id = response.json()["data"]["id"]
        return f"Success: 投稿しました https://x.com/i/web/status/{tweet_id}"
    except requests.exceptions.HTTPError as e:
        return f"Error posting tweet: {e} - {response.text}"
    except Exception as e:
        return f"Error posting tweet: {e}"


if __name__ == "__main__":
    import sys

    if "--live" in sys.argv:
        result = tool_post_tweet("ARMS投稿ツール 動作確認 (自動投稿テスト)")
        print(result)
        assert result.startswith("Success:"), result
    else:
        auth = _get_auth()
        assert auth is not None, "認証情報が.envに揃っていません"
        print("OK (dry run): 認証情報は揃っています。実投稿するには --live を付けて実行してください。")
