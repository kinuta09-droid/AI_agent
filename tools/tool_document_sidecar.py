# -*- coding: utf-8 -*-
"""
tools/tool_document_sidecar.py — Vault 内の PDF/DOCX/PPTX を docling で Markdown 化し、
99_Assets/Converted/ にサイドカーノートとして配置する (Phase 3-0)。

plans/docling-integration.md Phase 3 の「方式A: サイドカー変換方式」の実装。
インデクサ本体 (tool_vault_index._walk_vault_md) は .md しか歩かないため、
Vault に PDF を置いても意味検索には永久に出てこない。この関数が生成した .md を
インデクサがそのまま拾う設計 — インデクサ本体は1行も変更しない (回帰リスク最小)。

なぜ Gemini 要約を挟まないか (tools/tool_document.py との違い):
  tool_import_document は「ユーザーが明示的に1本取り込みたい」動線で要約が主目的。
  こちらは Vault 全体を毎晩巡回するバッチなので、要約コストをファイル数に比例して
  払うと DAILY_GEMINI_CALL_LIMIT を圧迫する。ここでは docling の全文 Markdown を
  そのままインデックス対象にするだけ (Gemini 不使用 = 課金ゼロ、tool_paper_radar と同じ思想)。

冪等性: サイドカー .md の mtime が元ファイルの mtime 以降ならスキップする。
  lib.docling_client 自身の変換キャッシュ (mtime+size キー) とは独立の判定 —
  サイドカー側の存在確認だけなので、docling キャッシュとサイドカーの片方だけが
  消えた半端な状態からも自己修復する。
"""
import datetime
import os

from lib.docling_client import CaptureError, capture_document, is_available
from lib.lib_shared import EXCLUDE_DIRS, VAULT_ROOT
from tools.tool_document import check_document_caps

# インデクサ (tool_vault_index) がそのまま拾える場所。ここは変更しない前提で
# Phase 3-4 の eval フィクスチャにも直書きされている。
SIDECAR_ROOT_REL = "99_Assets/Converted"

_DOC_EXTENSIONS = (".pdf", ".docx", ".pptx")

# 出力先自身 ("Converted") と Templates は走査から除く。
# tool_vault_index._INDEX_EXCLUDE_DIRS と同じ判断 (テンプレートは検索を汚染する)。
_SIDECAR_EXCLUDE_DIRS = set(EXCLUDE_DIRS) | {"Templates", "Converted"}


def _walk_source_documents(vault_root: str):
    """Vault 内の PDF/DOCX/PPTX を (絶対パス, 相対パス) で列挙する。"""
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in _SIDECAR_EXCLUDE_DIRS]
        for file in files:
            if not file.lower().endswith(_DOC_EXTENSIONS):
                continue
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, vault_root).replace("\\", "/")
            yield abs_path, rel_path


def _sidecar_path(vault_root: str, rel_path: str) -> str:
    """元ファイルの相対パスから、拡張子を .md に変えたサイドカー保存先を作る。

    ディレクトリ構造を保つ (例: 30_Main/foo.pdf -> 99_Assets/Converted/30_Main/foo.pdf.md)。
    拡張子は**削らず** `.md` を継ぎ足す (元の拡張子を捨てて置き換えると、同名幹で
    拡張子違いのファイル (`foo.pdf` と `foo.docx` 等) がサイドカーパスで衝突し、
    片方がもう片方を無言で上書きする — mtime比較の相手も入れ替わるため、
    最悪交互に無駄な再変換を繰り返す)。拡張子込みで継ぎ足せば元パスへの
    単射になり、衝突しようがない。
    """
    md_rel = f"{rel_path}.md"
    return os.path.join(vault_root, *SIDECAR_ROOT_REL.split("/"), *md_rel.split("/"))


def _needs_conversion(source_abs: str, sidecar_abs: str) -> bool:
    """サイドカーが無い、または元ファイルの方が新しければ再変換が要る。"""
    if not os.path.exists(sidecar_abs):
        return True
    try:
        return os.path.getmtime(source_abs) > os.path.getmtime(sidecar_abs)
    except OSError:
        return True


def _render_sidecar_note(rel_path: str, capture: dict) -> str:
    """サイドカーノート本文を組み立てる純関数。

    先頭のフロントマター (source: docling) とコールアウトで「これは変換物」だと
    検索結果からでも分かるようにする (計画書の要求: 無言でオリジナル扱いしない)。
    """
    now = datetime.datetime.now()
    warnings = capture.get("warnings") or []
    warn_line = f"\n> 警告: {'; '.join(warnings)}" if warnings else ""
    header = (
        "---\n"
        "source: docling\n"
        f'original: "{rel_path}"\n'
        f"converted_at: {now.isoformat(timespec='seconds')}\n"
        "---\n\n"
        "> [!INFO] docling による自動変換 (元ファイルの写し)\n"
        f"> 元ファイル: [[{rel_path}]]\n"
        f"> 取得経路: docling ({capture.get('chars', 0)}文字)"
        f"{warn_line}\n\n"
    )
    return header + (capture.get("text", "") or "")


def sync_document_sidecars(vault_root: str = None) -> str:
    """Vault 内の PDF/DOCX/PPTX を docling で変換し、99_Assets/Converted/ 配下に
    サイドカー Markdown として書き出す。既存インデクサがそのまま拾う設計。

    Returns:
        サマリー文字列。docling 未導入なら劣化して0件処理のまま正常終了する
        (Phase 3 のゴール3: docling が無くても壊れない)。
    """
    vault_root = vault_root or VAULT_ROOT

    if not is_available():
        return "Document Conversion: docling 未導入のためスキップ (0件処理)"

    converted = 0
    skipped_unchanged = 0
    skipped_capped = 0
    failed = 0

    for source_abs, rel_path in _walk_source_documents(vault_root):
        sidecar_abs = _sidecar_path(vault_root, rel_path)
        if not _needs_conversion(source_abs, sidecar_abs):
            skipped_unchanged += 1
            continue

        cap_err = check_document_caps(source_abs)
        if cap_err:
            print(f"[DocSidecar] ⚠️ 上限超過のためスキップ: {rel_path} ({cap_err})")
            skipped_capped += 1
            continue

        try:
            capture = capture_document(source_abs)
        except CaptureError as e:
            print(f"[DocSidecar] ⚠️ 変換失敗: {rel_path} ({e})")
            failed += 1
            continue
        if not (capture.get("text") or "").strip():
            print(f"[DocSidecar] ⚠️ 変換結果が空: {rel_path}")
            failed += 1
            continue

        note = _render_sidecar_note(rel_path, capture)
        try:
            os.makedirs(os.path.dirname(sidecar_abs), exist_ok=True)
            with open(sidecar_abs, "w", encoding="utf-8") as f:
                f.write(note)
        except Exception as e:
            print(f"[DocSidecar] ⚠️ サイドカー書き込み失敗: {rel_path} ({e})")
            failed += 1
            continue
        converted += 1

    return (
        f"Document Conversion: {converted} 変換 / {skipped_unchanged} 未変更 / "
        f"{skipped_capped} 上限超過 / {failed} 失敗"
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(sync_document_sidecars())
