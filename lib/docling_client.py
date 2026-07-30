# -*- coding: utf-8 -*-
"""
lib/docling_client.py — 論文PDF等を Markdown 化する層 (docling 優先・段階フォールバック)

なぜ独立モジュールにするか:
  docling は torch/transformers を引き連れる重量級依存で、**入っていない環境が
  普通にある** (CI の requirements.txt にも入れていない: Windows runner で
  torch を毎回落とすと数分かかるため)。ツール側で毎回 try-import を書くと、
  「docling が無い」ことの扱いがファイルごとに割れる。ここに閉じ込めて
  「使えるなら使う・無ければ落ちる代わりに劣化して続ける」を一箇所で決める。

フォールバックの順序と理由:
  1. docling      : レイアウト認識でPDF→Markdown。表・数式・セクション構造が残る
  2. HTML (ar5iv) : arXiv の HTML 版を bs4 で本文抽出。既存依存だけで動く
  3. abstract     : 呼び出し側が持っているアブストラクトを使う (このモジュールの外)
  **無言で 3 に落ちてはいけない** — どの経路で取れたかを backend として必ず返し、
  呼び出し側 (ノート/SNS下書き) に「全文を読んでいない」ことを明示させる。
  CLAUDE.md 所見2「無言の死」の同型を作らないための設計。

キャッシュ:
  docling の変換は1本あたり数十秒〜数分かかる。同じ論文を /paper で2回叩く、
  SNS下書きと note 記事で2回読む、が普通に起きるので変換結果を
  logs/paper_cache/<sha1>.md に残す。キャッシュの破損・書き込み失敗は
  変換本体を止めない (キャッシュはあくまで高速化)。

例外方針:
  ここは lib 層なので "Error: ..." 文字列は返さない (それは tools/ の戻り値契約)。
  全経路が失敗したときだけ CaptureError を送出し、tools/ 側が文字列へ翻訳する。
"""
import hashlib
import os
import re
import threading

import requests
from bs4 import BeautifulSoup

from lib.paths import LOGS_DIR

CACHE_DIR = os.path.join(LOGS_DIR, "paper_cache")

# HTML フォールバックのタイムアウト。docling 側には設けていない —
# 変換はライブラリ内部で回るので、外からタイムアウトを掛けてもスレッドは
# 止められず「打ち切ったつもりで裏で走り続ける」だけになる。長さの制御は
# max_chars と「PDFのURLを渡すかどうか」で行う。
HTTP_TIMEOUT_SEC = 30

# Gemini へ渡す上限。全文をそのまま渡すと入力トークンが跳ねるので既定で切る。
DEFAULT_MAX_CHARS = 60000

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_CONVERTER = None
_CONVERTER_LOCK = threading.Lock()
_DOCLING_ERROR = ""


class CaptureError(Exception):
    """全ての取得経路が失敗した。tools/ 側で "Error: ..." に翻訳する。"""


def is_available() -> bool:
    """docling が import 可能か。**モデルのロードまではしない** (import のみ)。

    ここで重い初期化までやると「使えるか確認しただけ」で数十秒固まるため、
    可用性チェックと変換器の生成 (_get_converter) を分けてある。
    """
    global _DOCLING_ERROR
    try:
        import docling  # noqa: F401
        return True
    except Exception as e:  # ImportError 以外 (依存の初期化失敗) も同じ扱いにする
        _DOCLING_ERROR = f"{type(e).__name__}: {e}"
        print(f"[docling] ⚠️ docling を import できません ({_DOCLING_ERROR})")
        return False


def docling_import_error() -> str:
    """直近の import 失敗理由 (未試行なら空文字)。ノートの注記に載せる用。"""
    return _DOCLING_ERROR


def _get_converter():
    """DocumentConverter をプロセス内で1つだけ生成する (モデルロードが重い)。"""
    global _CONVERTER
    if _CONVERTER is None:
        with _CONVERTER_LOCK:
            if _CONVERTER is None:
                from docling.document_converter import DocumentConverter
                print("[docling] 🔧 DocumentConverter を初期化中 (初回はモデル取得で時間がかかります)")
                _CONVERTER = DocumentConverter()
    return _CONVERTER


# ---------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------

def _cache_path(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{digest}.md")


def _read_cache(key: str) -> str:
    path = _cache_path(key)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[docling] ⚠️ キャッシュ読み込み失敗 ({e}) — 再変換します")
        return ""


def _write_cache(key: str, text: str) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        # キャッシュはあくまで高速化。書けなくても本体は成功扱いのままにする
        print(f"[docling] ⚠️ キャッシュ書き込み失敗 ({e}) — 続行します")


# ---------------------------------------------------------
# 各バックエンド
# ---------------------------------------------------------

def convert_with_docling(source: str) -> str:
    """docling で PDF/URL を Markdown 化する。失敗時は CaptureError。"""
    try:
        converter = _get_converter()
        result = converter.convert(source)
        markdown = result.document.export_to_markdown()
    except Exception as e:
        print(f"[docling] ❌ 変換失敗 ({type(e).__name__}: {e})")
        raise CaptureError(f"docling conversion failed: {e}") from e
    if not markdown or not markdown.strip():
        raise CaptureError("docling returned empty markdown")
    return markdown


def convert_with_html(url: str) -> str:
    """HTML版 (ar5iv / arXiv HTML) を bs4 でテキスト化する。既存依存のみ。

    docling が無い環境の受け皿。段落構造は落ちるが、アブストラクトだけよりは
    はるかに情報がある (手法・実験・限界の節が読める)。
    """
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    if resp.encoding is None:
        resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # ar5iv / arXiv HTML は本文を <article> or <div class="ltx_page_content"> に置く
    main = soup.find("article") or soup.find("div", class_="ltx_page_content") or soup
    text = main.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) < 500:
        raise CaptureError(f"HTML fallback got too little text ({len(cleaned)} chars): {url}")
    return cleaned


# ---------------------------------------------------------
# 公開API
# ---------------------------------------------------------

def capture_document(pdf_url: str, html_urls=(), max_chars: int = DEFAULT_MAX_CHARS,
                     use_cache: bool = True) -> dict:
    """論文本文を取得して Markdown/テキストで返す。

    Args:
        pdf_url  : docling に渡す PDF の URL (またはローカルパス)
        html_urls: docling が使えない/失敗したときに順に試す HTML の URL 群
        max_chars: 返すテキストの上限 (超過分は切り、truncated=True で知らせる)
        use_cache: logs/paper_cache を使うか

    Returns:
        {"text": str, "backend": "docling"|"html"|"cache:docling"|...,
         "chars": int, "truncated": bool, "source": str, "warnings": [str, ...]}

    Raises:
        CaptureError: 全経路が失敗したとき
    """
    warnings = []
    cache_key = f"docling::{pdf_url}"

    if use_cache:
        cached = _read_cache(cache_key)
        if cached:
            return _pack(cached, "cache", pdf_url, max_chars, warnings)

    if pdf_url and is_available():
        try:
            markdown = convert_with_docling(pdf_url)
            if use_cache:
                _write_cache(cache_key, markdown)
            return _pack(markdown, "docling", pdf_url, max_chars, warnings)
        except CaptureError as e:
            print(f"[docling] ↩️ HTML フォールバックへ ({e})")
            warnings.append(f"docling 変換に失敗したため HTML から抽出: {e}")
    elif pdf_url:
        warnings.append(
            "docling が未インストールのため HTML から抽出 "
            "(pip install -r requirements-docling.txt で本文品質が上がります)"
        )

    for url in html_urls:
        try:
            text = convert_with_html(url)
            return _pack(text, "html", url, max_chars, warnings)
        except Exception as e:
            print(f"[docling] ⚠️ HTML 取得失敗 {url} ({type(e).__name__}: {e})")
            warnings.append(f"HTML 取得失敗 {url}: {e}")

    raise CaptureError(
        "本文を取得できませんでした (docling/HTML の全経路が失敗). "
        + " / ".join(warnings)
    )


def _pack(text: str, backend: str, source: str, max_chars: int, warnings: list) -> dict:
    truncated = len(text) > max_chars
    if truncated:
        warnings.append(f"本文が長いため先頭 {max_chars} 文字のみ使用 (全 {len(text)} 文字)")
    return {
        "text": text[:max_chars],
        "backend": backend,
        "chars": len(text),
        "truncated": truncated,
        "source": source,
        "warnings": warnings,
    }
