# -*- coding: utf-8 -*-
"""
NotebookLM ツール — 根拠付きQ&Aエンジンとして NotebookLM を操作する最小ループ。

Flow:
  1. トピック名でノートブックを検索、無ければ作成 (find_or_create_notebook)
  2. URL または生テキストをソースとして投入し、処理完了まで待つ (add_source)
  3. 根拠(引用元)付きで質問する (ask_notebook)

NotebookLMは「ソースにない情報は書かない」設計 (捏造しない)。回答には
references (引用元の実テキスト) が付くため、そのまま出典として提示する。

事故防止の設計判断 (/grillで検出・修正):
  - ノートブックのタイトルには常に NOTEBOOK_TITLE_PREFIX を付ける。トピック名を
    LLM (Planner) が自由に組み立てるため、プレフィックス無しだとユーザーの
    既存ノートブック(このツールと無関係な実データ)とタイトルが偶然一致し、
    無関係な既存ノートブックへソースを追加・その全内容を巻き込んで質問して
    しまう事故になりうる。
  - add_source は同一ノートブック内の既存ソース(URLは末尾スラッシュ正規化して
    比較、テキストは内容ハッシュをタイトルに埋め込んで比較)を確認し、
    既に追加済みなら再送しない。同じtopicへの再利用呼び出しが増えるほど
    ソースが際限なく重複する事故を防ぐ。
  - sources は MAX_SOURCES 件まで。1件あたり最大180秒待つ設計のため、
    無制限だと1回の呼び出しが数分〜十数分ブロックしうる。

依存: notebooklm-mcp-cli (システムPython側にも導入済みである前提。
未導入/認証切れの場合は Error 文字列を返す — 他の tools/* と同じ規約)。

Usage:
  from tools.tool_notebooklm import run_notebooklm_research
  run_notebooklm_research("Relocation比較", ["https://...", "生テキスト..."], "医療制度を比較して")
"""
import hashlib
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

NOTEBOOK_TITLE_PREFIX = "[ARMS] "
MAX_SOURCES = 5


def _get_client():
    """notebooklm-mcp-cli のキャッシュ済み認証からクライアントを取得する。

    notebooklm_tools.mcp.tools._utils.get_client() を再利用する
    (スレッド安全なシングルトン + 再認証検知を自前実装しない)。
    非公開API依存だが、壊れた場合は呼び出し側の except で Error 文字列に落ちる。
    """
    from notebooklm_tools.mcp.tools._utils import get_client
    return get_client()


def find_or_create_notebook(topic: str) -> str:
    """トピック名(プレフィックス付き)と同名のノートブックがあれば再利用、無ければ新規作成してIDを返す。"""
    title = f"{NOTEBOOK_TITLE_PREFIX}{topic}"
    client = _get_client()
    for nb in client.list_notebooks():
        if nb.title == title:
            return nb.id
    nb = client.create_notebook(title=title)
    if not nb:
        raise RuntimeError(f"ノートブック作成に失敗しました: {title}")
    return nb.id


def _text_source_title(text: str) -> str:
    """テキストソースの内容に基づく安定したタイトル。同一内容の重複投入を検出するため。"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"Pasted Text {digest}"


def _find_existing_source_id(client, notebook_id: str, source: str, is_url: bool):
    """同一ノートブック内に既に追加済みの同じソースがあればそのIDを返す(無ければNone)。"""
    existing = client.get_notebook_sources_with_types(notebook_id)
    if is_url:
        normalized = source.rstrip("/")
        for s in existing:
            if (s.get("url") or "").rstrip("/") == normalized:
                return s.get("id")
    else:
        title = _text_source_title(source)
        for s in existing:
            if s.get("title") == title:
                return s.get("id")
    return None


def add_source(notebook_id: str, source: str) -> str:
    """URL または生テキストをソースとして追加し、処理完了まで待ってsource_idを返す。

    既に同じソースが追加済みなら再送せずそのIDを返す (重複蓄積の防止)。
    """
    client = _get_client()
    is_url = source.startswith("http://") or source.startswith("https://")

    existing_id = _find_existing_source_id(client, notebook_id, source, is_url)
    if existing_id:
        return existing_id

    if is_url:
        result = client.add_url_source(notebook_id, source, wait=True, wait_timeout=180.0)
    else:
        result = client.add_text_source(
            notebook_id, source, title=_text_source_title(source), wait=True, wait_timeout=180.0
        )
    if not result or not result.get("id"):
        raise RuntimeError(f"ソース追加に失敗しました: {source[:80]}")
    return result["id"]


def ask_notebook(notebook_id: str, question: str) -> dict:
    """ノートブック内の全ソースを対象に根拠付きで質問する。"""
    client = _get_client()
    result = client.query(notebook_id, question, timeout=120.0)
    if not result or not result.get("answer"):
        raise RuntimeError("NotebookLMから応答がありませんでした")
    return result


def _format_answer(topic: str, result: dict) -> str:
    lines = [f"[NotebookLM: {topic}]", result["answer"]]
    refs = result.get("references") or []
    if refs:
        lines.append("\n出典:")
        for r in refs[:5]:
            cited = (r.get("cited_text") or "").strip().replace("\n", " ")
            if cited:
                lines.append(f"- {cited[:150]}")
    return "\n".join(lines)


def run_notebooklm_research(topic: str, sources: list, question: str) -> str:
    """1コールでノートブック作成→ソース投入→根拠付き質問までを完結させる。"""
    if len(sources) > MAX_SOURCES:
        return f"Error: ソースは最大{MAX_SOURCES}件までです ({len(sources)}件指定されました)"

    added = 0
    try:
        notebook_id = find_or_create_notebook(topic)
        for src in sources:
            add_source(notebook_id, src)
            added += 1
        result = ask_notebook(notebook_id, question)
        return _format_answer(topic, result)
    except Exception as e:
        progress = (
            f" (ソース{added}/{len(sources)}件目まで追加成功、"
            f"ノートブック '{topic}' は作成/再利用済みなので同じ内容で再試行すれば続きから進む)"
            if sources else ""
        )
        return f"Error: {e}{progress}"
