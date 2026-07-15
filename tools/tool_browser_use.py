# -*- coding: utf-8 -*-
"""
Browser Use Tool — AI エージェントによるブラウザ自動操作

静的 HTML 取得 (tool_web.py) との役割分担:
  tool_web.py       → 静的コンテンツ取得・Tavily 検索
  tool_browser_use  → JS レンダリング・クリック・フォーム入力など動的操作

Usage:
  from tools.tool_browser_use import BrowserSession, browse_and_extract

  # セッション管理 (複数操作)
  with BrowserSession() as b:
      b.navigate("https://example.com")
      b.click("button#submit")
      text = b.get_text("h1")

  # ワンショット (URL + 抽出タスクを渡すと Gemini が回答)
  result = browse_and_extract("https://example.com", "このページの主要サービスを箇条書きで")
"""

import os
import sys
import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(r'C:\AI_agent\.env')

from urllib.parse import urlparse

from lib.lib_shared import client, MODEL_NAME_HEAVY, WORK_DIR

# スクリーンショット保存先
SCREENSHOT_DIR = os.path.join(WORK_DIR, "40_Report", "BrowserCaptures")

# 許可する URL スキーム（file:// や javascript: 等によるローカルファイル読み取りを防ぐ）
ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_url_scheme(url: str) -> None:
    """http/https 以外のスキームを拒否する。

    Raises:
        ValueError: スキームが許可リスト外の場合
    """
    scheme = urlparse(url.strip()).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"許可されていない URL スキームです: '{scheme or '(なし)'}'。"
            "http / https のみ使用できます。"
        )


# ---------------------------------------------------------
# BrowserSession — 同期 Playwright セッション管理
# ---------------------------------------------------------

class BrowserSession:
    """同期 Playwright を使ったブラウザセッション。with 文で安全に開閉できる。

    Example::
        with BrowserSession(headless=True) as b:
            b.navigate("https://example.com")
            html = b.get_page_content()
    """

    def __init__(self, headless: bool = True, timeout: int = 30_000):
        """
        Args:
            headless: True でヘッドレス実行（デフォルト）
            timeout: 各操作のデフォルトタイムアウト ms
        """
        self._headless = headless
        self._timeout = timeout
        self._playwright = None
        self._browser = None
        self._page = None

    # --- context manager ---

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # --- lifecycle ---

    def open(self):
        """ブラウザを起動する。"""
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        self._page = context.new_page()
        self._page.set_default_timeout(self._timeout)

    def close(self):
        """ブラウザを閉じる。"""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._playwright = None
            self._page = None

    # --- navigation ---

    def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """指定 URL に移動する。

        Args:
            url: 移動先 URL
            wait_until: 待機条件 ('load' | 'domcontentloaded' | 'networkidle')

        Raises:
            ValueError: http/https 以外のスキームが指定された場合
        """
        _validate_url_scheme(url)
        self._page.goto(url, wait_until=wait_until)

    # --- interaction ---

    def click(self, selector: str) -> None:
        """CSS セレクタの要素をクリックする。"""
        self._page.click(selector)

    def fill_text(self, selector: str, text: str) -> None:
        """テキスト入力フィールドに値をセットする。"""
        self._page.fill(selector, text)

    def press_key(self, selector: str, key: str) -> None:
        """キーストロークを送信する（例: 'Enter'）。"""
        self._page.press(selector, key)

    def wait_for_selector(self, selector: str, state: str = "visible") -> None:
        """セレクタが指定状態になるまで待機する。"""
        self._page.wait_for_selector(selector, state=state)

    # --- extraction ---

    def get_page_content(self) -> str:
        """現在ページの HTML 全体を返す。"""
        return self._page.content()

    def get_text(self, selector: str) -> str:
        """セレクタに一致する要素のテキストを返す。"""
        el = self._page.query_selector(selector)
        return el.inner_text() if el else ""

    def get_all_text(self, selector: str) -> list[str]:
        """セレクタに一致する全要素のテキストをリストで返す。"""
        elements = self._page.query_selector_all(selector)
        return [el.inner_text() for el in elements]

    def get_inner_text(self) -> str:
        """ページ全体のプレーンテキスト（body.innerText）を返す。広告除去済み。"""
        return self._page.evaluate("() => document.body.innerText")

    def get_attribute(self, selector: str, attr: str) -> str:
        """セレクタ要素の属性値を返す。"""
        return self._page.get_attribute(selector, attr) or ""

    def current_url(self) -> str:
        """現在の URL を返す。"""
        return self._page.url

    # --- screenshot ---

    def screenshot(self, filename: str | None = None) -> str:
        """スクリーンショットを撮影して保存パスを返す。

        Args:
            filename: 保存ファイル名（省略時は自動生成）

        Returns:
            保存されたファイルの絶対パス
        """
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        if not filename:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{ts}.png"
        path = os.path.join(SCREENSHOT_DIR, filename)
        self._page.screenshot(path=path, full_page=True)
        return path


# ---------------------------------------------------------
# ワンショット高レベル API
# ---------------------------------------------------------

def browse_and_extract(url: str, task: str, headless: bool = True) -> str:
    """URL を開き、Gemini に task を与えてテキストを抽出・回答する。

    Args:
        url:     開く URL
        task:    「このページのXXXを教えて」のような抽出指示
        headless: ヘッドレス実行するか

    Returns:
        Gemini の回答テキスト
    """
    # ブラウザ起動前に事前チェック（拒否時は Playwright を立ち上げない）
    try:
        _validate_url_scheme(url)
    except ValueError as e:
        return f"[Browser] {e}"

    print(f"[Browser] Opening: {url}")
    try:
        with BrowserSession(headless=headless) as b:
            b.navigate(url)
            page_text = b.get_inner_text()
    except Exception as e:
        return f"[Browser] ページ取得エラー: {e}"

    # 長すぎる場合は先頭 8,000 文字に制限
    trimmed = page_text[:8000]
    if len(page_text) > 8000:
        trimmed += "\n...(以下省略)"

    prompt = f"""以下はウェブページ ({url}) のテキスト内容です。

--- ページ内容 ---
{trimmed}
--- ここまで ---

タスク: {task}

簡潔に日本語で回答してください。"""

    print(f"[Browser] Extracting with Gemini...")
    resp = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=prompt,
    )
    return resp.text.strip()


def browser_search_and_extract(query: str, task: str, headless: bool = True) -> str:
    """Google 検索して上位ページを開き、task に回答する。

    Args:
        query: 検索クエリ
        task:  抽出タスク
        headless: ヘッドレス実行するか

    Returns:
        Gemini の回答テキスト
    """
    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&hl=ja"
    print(f"[Browser] Searching: {query}")

    try:
        with BrowserSession(headless=headless) as b:
            b.navigate(search_url)
            # 検索結果の最初のリンク URL を取得
            hrefs = b.get_all_text("h3")
            first_link = b.get_attribute("a:has(h3)", "href")

            if first_link and first_link.startswith("http"):
                print(f"[Browser] Navigating to first result: {first_link}")
                b.navigate(first_link)
                page_text = b.get_inner_text()
            else:
                # 検索結果ページ自体から抽出
                page_text = b.get_inner_text()
    except Exception as e:
        return f"[Browser] 検索エラー: {e}"

    trimmed = page_text[:8000]
    prompt = f"""以下は検索クエリ「{query}」の結果ページのテキストです。

--- ページ内容 ---
{trimmed}
--- ここまで ---

タスク: {task}

簡潔に日本語で回答してください。"""

    resp = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=prompt,
    )
    return resp.text.strip()
