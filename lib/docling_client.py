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
  キャッシュキーは URL 文字列だけでなく (source, mtime, size, ocr, table_structure)
  を含める — ローカルファイルは同じパスのまま更新されうるので、URL のみをキーに
  すると「更新したのに古い変換結果が返る」無言の嘘になる
  (CLAUDE.md 所見「表示と実態の食い違い」の同型)。
  この形式変更 (2026-07-31) より前に書かれた logs/paper_cache/*.md はキー形式が
  違うため二度と参照されない (孤児化する)。実害は無い (キャッシュミスして
  再変換するだけ) が、掃除する仕組みは無いので溜まる。数十本規模なら放置可。

例外方針:
  ここは lib 層なので "Error: ..." 文字列は返さない (それは tools/ の戻り値契約)。
  全経路が失敗したときだけ CaptureError を送出し、tools/ 側が文字列へ翻訳する。
"""
import hashlib
import json
import os
import re
import stat as stat_module
import threading

import requests
from bs4 import BeautifulSoup

from lib.paths import DOCLING_ARTIFACTS_PATH, LOGS_DIR

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

_CONVERTERS = {}  # (ocr, table_structure) -> DocumentConverter
_CONVERTERS_LOCK = threading.Lock()
_DOCLING_ERROR = ""
_AVAILABLE_CACHE = None  # None=未判定 / True / False。プロセス内で一度判定すれば変わらない


class CaptureError(Exception):
    """全ての取得経路が失敗した。tools/ 側で "Error: ..." に翻訳する。"""


def is_available() -> bool:
    """docling が import 可能か。**モデルのロードまではしない** (import のみ)。

    ここで重い初期化までやると「使えるか確認しただけ」で数十秒固まるため、
    可用性チェックと変換器の生成 (_get_converter) を分けてある。
    結果はプロセス内でキャッシュする (毎回 import を試みると import 自体が
    地味に重く、1リクエストで何度も呼ばれる呼び出し元がある)。
    """
    global _DOCLING_ERROR, _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None:
        return _AVAILABLE_CACHE
    try:
        import docling  # noqa: F401
        _AVAILABLE_CACHE = True
    except Exception as e:  # ImportError 以外 (依存の初期化失敗) も同じ扱いにする
        _DOCLING_ERROR = f"{type(e).__name__}: {e}"
        print(f"[docling] ⚠️ docling を import できません ({_DOCLING_ERROR})")
        _AVAILABLE_CACHE = False
    return _AVAILABLE_CACHE


def docling_import_error() -> str:
    """直近の import 失敗理由 (未試行なら空文字)。ノートの注記に載せる用。"""
    return _DOCLING_ERROR


def _get_converter(*, ocr: bool, table_structure: bool):
    """DocumentConverter をオプションの組み合わせごとにプロセス内で使い回す (モデルロードが重い)。

    ocr/table_structure は変換品質に直結する (誤ってキャッシュを共有すると
    「OCRありで作った変換器」がOCRなし要求にも使われる、の逆もありうる) ので、
    オプションの組をキーにして別々に保持する。
    """
    key = (ocr, table_structure)
    converter = _CONVERTERS.get(key)
    if converter is None:
        with _CONVERTERS_LOCK:
            converter = _CONVERTERS.get(key)
            if converter is None:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                pipeline_options = PdfPipelineOptions(
                    do_ocr=ocr,
                    do_table_structure=table_structure,
                    artifacts_path=DOCLING_ARTIFACTS_PATH,
                )
                print(
                    f"[docling] 🔧 DocumentConverter を初期化中 "
                    f"(ocr={ocr}, table_structure={table_structure}) "
                    "— 初回はモデル取得で時間がかかります"
                )
                converter = DocumentConverter(
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
                )
                _CONVERTERS[key] = converter
    return converter


# ---------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------

def _resolve_cache_key(source: str, ocr: bool, table_structure: bool) -> str:
    """キャッシュキーを組み立てる。

    ローカルファイルは (パス, mtime, size) を含める — 同じパスのまま中身が
    更新されても mtime が変われば別キーになるので古い変換結果を返さない。
    URL はネットワーク越しに mtime を取りに行くコストが見合わないので
    従来どおり URL 文字列のみ (docling 側は毎回フェッチするので鮮度の問題は無い)。
    ocr/table_structure もキーに含める — 同じソースでもオプションが違えば
    出力が変わるため。

    判定は os.stat() 一発で行う (os.path.isfile() → os.stat() の2段にすると、
    間にファイルが消える/ロックされる TOCTOU で os.stat() 側が未捕捉の例外を
    投げうる。Phase 2 で Discord 添付を一時ファイルに落として即変換する経路が
    できると、この隙間が現実的なタイミングになる)。os.path.isfile() 相当の
    判定 (ディレクトリを除外する) も同じ stat 結果から行う。
    """
    try:
        st = os.stat(source)
        is_file = stat_module.S_ISREG(st.st_mode)
    except (OSError, TypeError, ValueError):
        is_file = False
    if is_file:
        location = f"{source}::{st.st_mtime_ns}::{st.st_size}"
    else:
        location = source
    return f"docling::{location}::ocr={ocr}::ts={table_structure}"


def _cache_path(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{digest}.md")


def _tables_cache_path(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{digest}.tables.json")


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


def _read_tables_cache(key: str) -> list:
    path = _tables_cache_path(key)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[docling] ⚠️ 表キャッシュ読み込み失敗 ({e}) — 表なしとして続行します")
        return []


def _write_tables_cache(key: str, tables: list) -> None:
    if not tables:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_tables_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(tables, f, ensure_ascii=False)
    except Exception as e:
        print(f"[docling] ⚠️ 表キャッシュ書き込み失敗 ({e}) — 続行します")


# ---------------------------------------------------------
# 各バックエンド
# ---------------------------------------------------------

def convert_with_docling(source: str, *, ocr: bool = False, table_structure: bool = True) -> dict:
    """docling で PDF/URL を Markdown 化する。失敗時は CaptureError。

    Returns:
        {"text": フルMarkdown, "tables": [{"markdown":..., "caption":..., "csv":...}, ...]}

    export_to_markdown() だけを呼んで結果を平文化すると、その瞬間に表の構造と
    キャプションが失われる (Phase 3-2 のキャプション付き埋め込みの材料が無くなる)。
    そのため DoclingDocument から tables を別途取り出して同梱する。
    1個の表の抽出に失敗しても本文 (markdown) は取れているので、その表だけ
    諦めて残りは返す (CLAUDE.md: 部分失敗で全体を無駄にしない)。
    """
    try:
        converter = _get_converter(ocr=ocr, table_structure=table_structure)
        result = converter.convert(source)
        document = result.document
        markdown = document.export_to_markdown()
        tables = []
        for table in getattr(document, "tables", []) or []:
            try:
                tables.append({
                    "markdown": table.export_to_markdown(document),
                    "caption": table.caption_text(document) or "",
                    "csv": table.export_to_dataframe(document).to_csv(index=False),
                })
            except Exception as e:
                print(f"[docling] ⚠️ 表の抽出に失敗 ({type(e).__name__}: {e}) — この表は省きます")
    except Exception as e:
        print(f"[docling] ❌ 変換失敗 ({type(e).__name__}: {e})")
        raise CaptureError(f"docling conversion failed: {e}") from e
    if not markdown or not markdown.strip():
        raise CaptureError("docling returned empty markdown")
    return {"text": markdown, "tables": tables}


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
                     use_cache: bool = True, ocr: bool = False,
                     table_structure: bool = True) -> dict:
    """論文本文を取得して Markdown/テキストで返す。

    Args:
        pdf_url        : docling に渡す PDF の URL またはローカルパス
        html_urls      : docling が使えない/失敗したときに順に試す HTML の URL 群
        max_chars      : 返すテキストの上限 (超過分は切り、truncated=True で知らせる)
        use_cache      : logs/paper_cache を使うか
        ocr            : docling の OCR を有効にするか (既定 off。数倍遅い)
        table_structure: docling の表構造認識を有効にするか (既定 on)

    Returns:
        {"text": str, "backend": "docling"|"html"|"cache"|...,
         "chars": int, "truncated": bool, "source": str, "warnings": [str, ...],
         "tables": [{"markdown":..., "caption":..., "csv":...}, ...]}
        tables は docling 経由でのみ埋まる (HTML/cache-miss 時は空リスト)。

    Raises:
        CaptureError: 全経路が失敗したとき
    """
    warnings = []
    cache_key = _resolve_cache_key(pdf_url, ocr, table_structure)

    if use_cache:
        cached = _read_cache(cache_key)
        if cached:
            return _pack(cached, "cache", pdf_url, max_chars, warnings,
                         _read_tables_cache(cache_key))

    if pdf_url and is_available():
        try:
            converted = convert_with_docling(pdf_url, ocr=ocr, table_structure=table_structure)
            markdown, tables = converted["text"], converted["tables"]
            if use_cache:
                _write_cache(cache_key, markdown)
                _write_tables_cache(cache_key, tables)
            return _pack(markdown, "docling", pdf_url, max_chars, warnings, tables)
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
            return _pack(text, "html", url, max_chars, warnings, [])
        except Exception as e:
            print(f"[docling] ⚠️ HTML 取得失敗 {url} ({type(e).__name__}: {e})")
            warnings.append(f"HTML 取得失敗 {url}: {e}")

    raise CaptureError(
        "本文を取得できませんでした (docling/HTML の全経路が失敗). "
        + " / ".join(warnings)
    )


def _pack(text: str, backend: str, source: str, max_chars: int, warnings: list,
          tables: list = None) -> dict:
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
        "tables": tables or [],
    }
