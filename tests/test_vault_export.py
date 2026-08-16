# -*- coding: utf-8 -*-
"""
tool_vault_export.export_vault() のユニットテスト。

一時Vaultを作り vault_root/output_dir を関数引数で渡すため、実Vault・logs/には
一切触れない (モジュール変数の差し替えより安全 — Category C の資源汚染教訓を参照)。

実行: py tests/test_vault_export.py
"""
import os
import sys
import shutil
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools import tool_vault_export

_FAIL = False


def _check(name, ok):
    global _FAIL
    print(f"[{'OK' if ok else 'NG'}] {name}")
    if not ok:
        _FAIL = True


def _write(root, rel_path, content):
    abs_path = os.path.join(root, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)


def test_export_excludes_strips_frontmatter_and_groups_by_folder():
    vault = tempfile.mkdtemp(prefix="vault_export_src_")
    out_dir = tempfile.mkdtemp(prefix="vault_export_out_")
    try:
        _write(vault, "00_home.md", "ルート直下ノート")
        _write(vault, "01_Daily/note_a.md", "本文A")
        _write(vault, "30_Main/note_b.md", "---\ntitle: B\n---\n本文B")
        _write(vault, "Trash/deleted.md", "捨てられたノート")
        _write(vault, "Templates/template.md", "テンプレート本文")
        _write(vault, "30_Main/empty.md", "---\ntitle: empty\n---\n")

        results = tool_vault_export.export_vault(vault_root=vault, output_dir=out_dir)
        by_name = {os.path.basename(p): (g, fc, cc) for p, g, fc, cc in results}

        _check("出力は3ファイル (_root/01_Daily/30_Main、単一パートずつ)", len(results) == 3)
        _check("ルート直下ノートは _root.md へ", "_root.md" in by_name)
        _check("01_Daily は独立ファイル", "01_Daily.md" in by_name)
        _check("30_Main は独立ファイル (空ノートは除く1件のみ)", by_name.get("30_Main.md", (None, None))[1] == 1)

        with open(os.path.join(out_dir, "30_Main.md"), "r", encoding="utf-8") as f:
            main_content = f.read()
        _check("見出しに相対パスが入る", "## 30_Main/note_b.md" in main_content)
        _check("frontmatterが除去されている", "title: B" not in main_content)
        _check("frontmatter除去後の本文は残る", "本文B" in main_content)

        combined_all = ""
        for path, *_ in results:
            with open(path, "r", encoding="utf-8") as f:
                combined_all += f.read()
        _check("Trash配下が含まれない", "捨てられたノート" not in combined_all)
        _check("Templates配下が含まれない", "テンプレート本文" not in combined_all)
    finally:
        shutil.rmtree(vault, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def test_export_splits_large_folder_into_parts_without_breaking_notes():
    vault = tempfile.mkdtemp(prefix="vault_export_src_")
    out_dir = tempfile.mkdtemp(prefix="vault_export_out_")
    try:
        # 各ノート本文は50文字程度。max_chars=80 なら1ノートずつでも
        # 見出し込みで超過するため、ノート単位で別パートに分かれるはず。
        for i in range(4):
            _write(vault, f"50_Archive/note_{i}.md", "あ" * 50)

        results = tool_vault_export.export_vault(vault_root=vault, output_dir=out_dir, max_chars=80)
        parts = sorted(os.path.basename(p) for p, *_ in results)

        _check("50_Archiveが複数パートに分割される", len(parts) > 1)
        _check("パートファイル名にpart番号が付く", all("50_Archive_part" in p for p in parts))

        total_files = sum(fc for _, _, fc, _ in results)
        _check("全4ノートが分割後も欠けずに揃う", total_files == 4)

        for path, _, file_count, _ in results:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            heading_count = content.count("## 50_Archive/note_")
            _check(f"{os.path.basename(path)}: 見出し数とファイル数カウントが一致 (ノートが分断されていない)",
                   heading_count == file_count)
    finally:
        shutil.rmtree(vault, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    test_export_excludes_strips_frontmatter_and_groups_by_folder()
    test_export_splits_large_folder_into_parts_without_breaking_notes()
    sys.exit(1 if _FAIL else 0)
