# -*- coding: utf-8 -*-
"""
tools/tool_vault_export.py — Obsidian Vaultをトップレベルフォルダ単位でMarkdownへ書き出す。

NotebookLM等の外部ツールへVaultを「複数ソース」として投入するための書き出し専用スクリプト。
plans/notebooklm-gemini-obsidian-workflow.md Step7 (書籍の既読判定サブワークフロー) の前提。

NotebookLMの1ソース上限 (50万語 or 200MB、2026-08時点の公式ヘルプ) に収まるよう、
トップレベルフォルダ単位でファイルを分け、1フォルダが大きすぎる場合はノート単位で
さらにパート分割する (ノートをまたいでは分割しない)。
"""
import argparse
import datetime
import os
import re
import sys

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.lib_shared import VAULT_ROOT, EXCLUDE_DIRS
from lib.paths import LOGS_DIR

# tool_vault_index._INDEX_EXCLUDE_DIRS / tool_document_sidecar._SIDECAR_EXCLUDE_DIRS と
# 同じ判断 (テンプレートは要約対象として無意味)。
_EXPORT_EXCLUDE_DIRS = set(EXCLUDE_DIRS) | {"Templates"}
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# NotebookLMの1ソース上限は50万語。日本語の語数カウントの実際の挙動が不明なため
# 安全マージンを見て文字数ベースで区切る (.env等での調整は不要な程度の目安値)。
DEFAULT_MAX_CHARS = 400_000

_ROOT_GROUP = "_root"


def _walk_vault_md(vault_root):
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in _EXPORT_EXCLUDE_DIRS]
        for file in files:
            if not file.endswith(".md"):
                continue
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, vault_root).replace("\\", "/")
            yield abs_path, rel_path


def _read_body(abs_path):
    """ファイルを読み frontmatter を除去した本文を返す。読めなければ None。

    tool_vault_index._embed_updated_files() と同じ方針 (errors="replace" +
    OSError握りつぶし)。929件規模の一括走査で1件の破損/非UTF-8ファイルが
    全体を止めないようにする。
    """
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def _group_by_top_level(vault_root):
    """Vaultルート直下のフォルダ名 -> [(abs_path, rel_path), ...] の辞書を返す。

    ルート直下の .md (rel_path にスラッシュが無いもの) は _root グループにまとめる。
    """
    groups = {}
    for abs_path, rel_path in _walk_vault_md(vault_root):
        top = rel_path.split("/", 1)[0] if "/" in rel_path else _ROOT_GROUP
        groups.setdefault(top, []).append((abs_path, rel_path))
    return groups


def _split_into_parts(notes, max_chars):
    """notesを max_chars 以内のパートに分割する。ノートをまたいでは分割しない。

    戻り値: [(本文, ファイル数, 文字数), ...]
    """
    parts = []
    chunks, length, count = [], 0, 0
    for abs_path, rel_path in notes:
        body = _read_body(abs_path)
        if not body:  # 空ノート or 読み込み失敗 (None) のどちらもスキップ
            continue
        chunk = f"## {rel_path}\n\n{body}\n"
        if chunks and length + len(chunk) > max_chars:
            parts.append(("\n".join(chunks), count, length))
            chunks, length, count = [], 0, 0
        chunks.append(chunk)
        length += len(chunk)
        count += 1
    if chunks:
        parts.append(("\n".join(chunks), count, length))
    return parts


def export_vault(vault_root=None, output_dir=None, max_chars=DEFAULT_MAX_CHARS):
    """Vaultをトップレベルフォルダ単位 (+ 閾値超過時はパート分割) で書き出す。

    戻り値: [(出力パス, グループ名, ファイル数, 文字数), ...] (グループ名→パート順)
    """
    vault_root = vault_root or VAULT_ROOT
    if output_dir is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(LOGS_DIR, f"vault_export_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    results = []
    groups = _group_by_top_level(vault_root)
    for group_name in sorted(groups):
        parts = _split_into_parts(groups[group_name], max_chars)
        multi = len(parts) > 1
        for i, (body, file_count, char_count) in enumerate(parts, start=1):
            suffix = f"_part{i}" if multi else ""
            path = os.path.join(output_dir, f"{group_name}{suffix}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            results.append((path, group_name, file_count, char_count))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Obsidian Vault をトップレベルフォルダ単位でMarkdownへ書き出す"
    )
    parser.add_argument("--output-dir", default=None, help="出力先ディレクトリ (既定: logs/vault_export_<timestamp>/)")
    parser.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CHARS,
        help=f"1ファイルあたりの目安上限文字数 (既定: {DEFAULT_MAX_CHARS}。NotebookLMの1ソース50万語上限への安全マージン)",
    )
    args = parser.parse_args()

    results = export_vault(output_dir=args.output_dir, max_chars=args.max_chars)
    if not results:
        print("対象ファイルなし")
        return

    out_dir = os.path.dirname(results[0][0])
    print(f"書き出し完了: {out_dir}")
    for path, group_name, file_count, char_count in results:
        print(f"  {os.path.basename(path)}: {file_count}ノート / {char_count}文字")
    total_files = sum(r[2] for r in results)
    total_chars = sum(r[3] for r in results)
    print(f"合計: {len(results)}ファイル / {total_files}ノート / {total_chars}文字")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
