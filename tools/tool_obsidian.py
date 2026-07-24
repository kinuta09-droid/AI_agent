# -*- coding: utf-8 -*-
"""
Obsidian Tools Module
Extracted from agent_hello.py
"""

import os
import datetime
import time
import re
from lib.lib_shared import (
    WORK_DIR, VAULT_ROOT, EXCLUDE_DIRS,
    find_file_in_vault, client, MODEL_NAME, MODEL_NAME_HEAVY, retry
)
from lib.lib_obsidian_cli import run_obsidian_cli

def _keyword_hits(keywords: str, max_hits: int = 10) -> list:
    """キーワード検索のコア。Vault全件をスコアリングし上位 max_hits 件を返す。

    返値: [{"rel_path": "40_Report/foo.md", "filename": "foo.md",
            "score": <マッチ行数>, "snippet": <初回マッチ行の前5/後25行>}, ...]
    score はマッチした行の総数。降順ソート済み (同点は walk 順を保つ安定ソート)。
    max_hits が 0/None なら全件返す。

    walk 順の先着 max_hits 件で打ち切ってはならない: Vault は 600ファイル/3MB 規模で
    全件走査が安価な一方、打ち切るとフォルダ後方の高スコアファイルが
    ハイブリッド検索の RRF 候補にすら入らない。

    既知の限界: score に長さ正規化がないため長い文書が有利 (BM25化は別途検討)。
    ハイブリッド検索では semantic 側との RRF 融合がこの偏りを部分的に打ち消す。
    """
    kw_list = [k.strip() for k in keywords.split() if k.strip()]
    if not kw_list:
        return []
    hits = []
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            if not file.endswith('.md'): continue
            abs_path = os.path.join(root, file)
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                score = 0
                snippet = ""
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if any(k.lower() in line.lower() for k in kw_list):
                        score += 1
                        if not snippet:
                            # Expand context: 5 lines before, 25 lines after
                            start = max(0, i - 5)
                            end = min(len(lines), i + 25)
                            snippet = "\n".join(lines[start:end])
                if score > 0:
                    hits.append({
                        "rel_path": os.path.relpath(abs_path, VAULT_ROOT).replace("\\", "/"),
                        "filename": file,
                        "score": score,
                        "snippet": snippet,
                    })
            except Exception as e:
                print(f"⚠️ Vault scan skipped {file}: {e}")
    hits.sort(key=lambda h: -h["score"])
    return hits[:max_hits] if max_hits and max_hits > 0 else hits

def tool_vault_search(keywords: str) -> str:
    """Vault内をキーワード検索する"""
    print(f"[Tool] 🧠 Searching Vault: {keywords}")
    kw_list = [k.strip() for k in keywords.split() if k.strip()]
    if not kw_list: return "No keywords."
    hits = [f"- File: [[{h['filename']}]]\n  Snippet:\n{h['snippet']}\n..."
            for h in _keyword_hits(keywords)]
    return "\n\n".join(hits) if hits else "Vault内に見つかりませんでした。"

def _extract_and_integrate_sections(content: str, tpl: str) -> tuple[str, str]:
    """コンテンツから参照・ソースを抽出し、テンプレートに統合する"""
    ref_pattern = re.compile(r'^##\s*[^\n]*?(?:References|Reference|参照元|参考文献)[^\n]*?\n(.*?)(?=\n##|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE)
    ref_match = ref_pattern.search(content)
    if ref_match:
        ref_body = ref_match.group(1).strip()
        tpl_ref_marker = "### 参照元"
        if tpl_ref_marker in tpl:
            tpl = re.sub(r'### 参照元\s*\n-\s*\n', f'### 参照元\n{ref_body}\n', tpl)
            if ref_body not in tpl:
                 tpl = tpl.replace(tpl_ref_marker, f'{tpl_ref_marker}\n{ref_body}')
        content = ref_pattern.sub('', content).strip()

    src_pattern = re.compile(r'^##\s*[^\n]*?(?:Sources|Source|関連ノート|Sources/Citations)[^\n]*?\n(.*?)(?=\n##|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE)
    src_match = src_pattern.search(content)
    src_links = []
    
    if src_match:
        src_body = src_match.group(1).strip()
        src_links.append(src_body)
        content = src_pattern.sub('', content).strip()

    lineage_pattern = re.compile(r'^>\s*Source:\s*(\[\[.*?\]\])', re.MULTILINE | re.IGNORECASE)
    lineage_matches = lineage_pattern.findall(content)
    if lineage_matches:
        for match in lineage_matches:
             src_links.append(f"- {match}")
        content = lineage_pattern.sub('', content).strip()

    if src_links:
        src_body = "\n".join(src_links)
        tpl_src_marker = "### 関連ノート"
        if tpl_src_marker in tpl:
            tpl = re.sub(r'### 関連ノート\s*\n-\s*\[\[\s*\]\]\s*\n', f'### 関連ノート\n{src_body}\n', tpl)
            if src_body not in tpl:
                tpl = tpl.replace(tpl_src_marker, f'{tpl_src_marker}\n{src_body}')

    return content, tpl

def _get_moc_list() -> list[str]:
    """10_MOCsフォルダ内のMOCファイル名リストを取得する"""
    moc_dir = os.path.join(VAULT_ROOT, "10_MOCs")
    if not os.path.exists(moc_dir):
        return []
    files = [f for f in os.listdir(moc_dir) if f.endswith('.md')]
    return [os.path.splitext(f)[0] for f in files]

def _determine_parent_moc(content: str) -> str | None:
    """コンテンツ内容に基づいて最適な親MOCを判定する"""
    moc_list = _get_moc_list()
    if not moc_list:
        return None

    moc_str = ", ".join(moc_list)
    content_snippet = content[:2000]

    prompt = f"""以下のテキストの内容に最も適した親カテゴリ（MOC: Map of Content）をリストから1つ選んでください。

【選択肢リスト】
{moc_str}

【テキスト内容】
{content_snippet}

【ルール】
- 最も関連性の高いMOC名を1つだけ出力してください。
- リストに適切なものがなければ "None" と出力してください。
- 出力はMOC名のみ（例: "MOC_AI"）。余計な説明は不要。
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME, contents=prompt
        )
        result = response.text.strip()
        result = result.replace('"', '').replace("'", "").replace("`", "")
        
        if result in moc_list:
            return result
        return None
    except Exception as e:
        print(f"⚠️ MOC determination failed: {e}")
        return None

@retry(max_retries=3)
def tool_create_note(filename: str, content: str, summary: str = "") -> str:
    """Vault内に新規ノートを作成する。Templateを使用する。"""
    print(f"[Tool] 📝 Creating Note: {filename}")
    filename = filename.replace('\\', '/').split('/')[-1]
    
    if not filename.endswith('.md'): filename += '.md'
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)

    save_dir = os.path.join(WORK_DIR, '30_Main')
    save_path = os.path.join(save_dir, filename)
    template_path = os.path.join(WORK_DIR, '99_Assets', 'Templates', 'Tpl_Permanent.md')

    if os.path.exists(save_path):
        timestamp = datetime.datetime.now().strftime('%H%M%S')
        base_name = os.path.splitext(filename)[0]
        filename = f"{base_name}_{timestamp}.md"
        save_path = os.path.join(save_dir, filename)
        print(f"[Tool] ⚠️ File exists, creating with suffix: {filename}")

    try:
        os.makedirs(save_dir, exist_ok=True)
        note_content = content
        
        if os.path.exists(template_path):
            with open(template_path, 'r', encoding='utf-8') as f:
                tpl = f.read()
            
            content, tpl = _extract_and_integrate_sections(content, tpl)

            today_iso = datetime.datetime.now().strftime('%Y-%m-%d')
            title_text = os.path.splitext(filename)[0]
            tpl = tpl.replace('{{date}}', today_iso)
            tpl = tpl.replace('{{title}}', title_text)
            
            try:
                check_content = (summary + "\n" + content)[:3000]
                parent_moc = _determine_parent_moc(check_content)
                if parent_moc:
                    tpl = tpl.replace('up: "[[00_Inbox]]"', f'up: "[[{parent_moc}]]"')
                    print(f"[Tool] 🔗 Linked to MOC: [[{parent_moc}]]")
            except Exception as e:
                print(f"[Tool] ⚠️ MOC auto-link failed: {e}")
            
            summary_placeholder = "> このノートが言いたいことを1〜3行で書く。"
            if summary:
                 tpl = tpl.replace(summary_placeholder, f"> {summary}")
            else:
                 tpl = tpl.replace(summary_placeholder, "> (要約未入力)")

            content_placeholder = "- ここに詳細を書く"
            if content_placeholder in tpl:
                tpl = tpl.replace(content_placeholder, content)
            else:
                tpl += "\n\n" + content
            note_content = tpl

        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(note_content)
            
        # Trigger Obsidian to recognize the new file and process it
        try:
            vault_rel_path = f"30_Main/{filename}"
            run_obsidian_cli(["open", vault_rel_path])
        except Exception as e:
            print(f"[Tool] ⚠️ Failed to trigger Obsidian CLI open: {e}")
            
        return f"Success: Created [[{filename}]] with template."
    except Exception as e:
        return f"Error creating note: {e}"

def tool_read_note(filename: str) -> str:
    """Vault内のノートの内容を読み込む"""
    print(f"[Tool] 📖 Reading Note: {filename}")
    path = find_file_in_vault(filename)
    if not path: return f"Error: File {filename} not found."
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading note: {e}"

def tool_update_note(filename: str, append_text: str) -> str:
    """既存のノートに追記する"""
    print(f"[Tool] ✍️ Updating Note: {filename}")
    path = find_file_in_vault(filename)
    if not path: return f"Error: File {filename} not found."
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write("\n\n" + append_text)
            
        # Trigger Obsidian to recognize the updated file
        try:
            vault_rel_path = os.path.relpath(path, VAULT_ROOT).replace('\\', '/')
            run_obsidian_cli(["open", vault_rel_path])
        except Exception as e:
            print(f"[Tool] ⚠️ Failed to trigger Obsidian CLI open: {e}")
            
        return f"Success: Updated [[{filename}]]"
    except Exception as e:
        return f"Error updating note: {e}"

def tool_merge_weekly_reports() -> str:
    """30_Main の「完了した過去週」のレポートを、各ファイル自身の週へ振り分けて
    アーカイブする（週別サマリー生成 + 原本を 50_Archive/{週}/ へ移動）。

    設計（2026-07-25 根本見直し）:
    - 旧実装は `(today-mtime).days > 7` で7日超を恒久スキップしていたため、一度
      取りこぼしたファイルが二度と回収されず 30_Main に滞留した。年齢上限を撤廃し、
      各ファイルを mtime の ISO週へ振り分けることでバックログが自然にドレインする。
    - 進行中の「今週」(current_week) のファイルは作業中とみなし 30_Main に残す。
    - 既存の週サマリーは上書きせず追記（部分実行/catch-up 再実行で破壊しない）。
    - 移動先に同名が存在したらタイムスタンプ付きにフォールバック（Windows os.rename は
      dst 存在で失敗する。CLAUDE.md の既知パターンに準拠）。
    """
    from collections import defaultdict

    print(f"[Tool] 🧹 Merging Weekly Reports...")
    target_dir = os.path.join(WORK_DIR, '30_Main')
    archive_dir = os.path.join(WORK_DIR, '50_Archive')

    today = datetime.datetime.now()
    current_week = today.strftime('%Y-W%U')
    summary_dir = os.path.join(archive_dir, 'Summary')

    if not os.path.exists(target_dir):
        return "Error: 30_Main directory not found."

    try:
        os.makedirs(summary_dir, exist_ok=True)

        files = [
            f for f in os.listdir(target_dir)
            if f.endswith('.md') and not f.endswith('_Weekly_Summary.md')
        ]
        if not files:
            return "No files to merge."

        # 各ファイルを自分の ISO週へ振り分け（今週=進行中は残置）
        by_week = defaultdict(list)  # week_str -> [(filename, mtime, content), ...]
        for file in files:
            file_path = os.path.join(target_dir, file)
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                file_week = mtime.strftime('%Y-W%U')
                if file_week == current_week:
                    continue  # 進行中の今週は作業領域に残す

                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                by_week[file_week].append((file, mtime, content))
            except Exception as e:
                print(f"Error reading {file}: {e}")

        if not by_week:
            return f"No completed-week files to archive (current_week {current_week} は残置)."

        total_moved = 0
        weeks_done = []
        for week_str, entries in sorted(by_week.items()):
            archive_sub_dir = os.path.join(archive_dir, week_str)
            os.makedirs(archive_sub_dir, exist_ok=True)

            # 先に原本を移動し、実際に移動できたものだけをサマリーへ追記する。
            # （サマリー追記→移動の順だと移動失敗時に原本が 30_Main に残り、次回
            #  再読込で同じ内容がサマリーに二重追記される。移動を真実の源にする。）
            moved_entries = []
            for file, mtime, content in entries:
                src = os.path.join(target_dir, file)
                dst = os.path.join(archive_sub_dir, file)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(file)
                    dst = os.path.join(archive_sub_dir, f"{base}_{int(time.time())}{ext}")
                try:
                    os.rename(src, dst)
                    total_moved += 1
                    moved_entries.append((file, mtime, content))
                except Exception as e:
                    print(f"Error moving {file}: {e}")

            if not moved_entries:
                continue  # この週は1件も移動できなかった → 空サマリーを書かない

            # 既存サマリーには追記、無ければヘッダ付きで新規作成
            summary_path = os.path.join(summary_dir, f"{week_str}_Weekly_Summary.md")
            block = ""
            if not os.path.exists(summary_path):
                block += f"# Weekly Summary: {week_str}\n\n"
            block += f"<!-- Appended: {today.strftime('%Y-%m-%d %H:%M:%S')} -->\n\n"
            for file, mtime, content in moved_entries:
                block += (
                    f"## Source: [[{file}]] ({mtime.strftime('%Y-%m-%d')})\n\n"
                    f"{content}\n\n---\n\n"
                )
            with open(summary_path, 'a', encoding='utf-8') as f:
                f.write(block)

            weeks_done.append(week_str)

        return (
            f"Success: archived {total_moved} files into {len(weeks_done)} week(s): "
            f"{', '.join(weeks_done)}. current_week {current_week} は 30_Main に残置。"
        )

    except Exception as e:
        return f"Error merging reports: {e}"

def tool_fetch_context(filenames_comma_separated: str) -> str:
    """複数のファイル名をカンマ区切りで指定し、それらの内容をまとめて取得する（AIのコンテキスト用）"""
    print(f"[Tool] 📥 Fetching Context: {filenames_comma_separated}")
    filenames = [f.strip() for f in filenames_comma_separated.split(',') if f.strip()]
    if not filenames: return "No filenames provided."
    
    context_parts = []
    for filename in filenames:
        path = find_file_in_vault(filename)
        if not path:
            context_parts.append(f"--- File: {filename} (Not Found) ---")
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                # トークン節約のため長すぎるファイルは先頭のみ
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated for token limit)"
                context_parts.append(f"--- File: {filename} ---\n{content}")
        except Exception as e:
            context_parts.append(f"--- File: {filename} (Error: {e}) ---")
            
    return "\n\n".join(context_parts)

def _sanitize_vault_rel_path(rel_path: str) -> str:
    """Vault相対パスをセグメント単位でサニタイズする（`..` 除去・危険文字置換、`/` 区切りは維持）。"""
    segments = []
    for seg in rel_path.replace('\\', '/').split('/'):
        seg = seg.strip()
        if not seg or seg in ('.', '..'):
            continue
        segments.append(re.sub(r'[:*?"<>|]', '_', seg))
    return '/'.join(segments)


def tool_move_note(old_filename: str, new_dir_or_filename: str) -> str:
    """ノートを別のフォルダに移動、またはリネームする(リンク切れ防止)"""
    print(f"[Tool] 🚚 Moving Note: {old_filename} -> {new_dir_or_filename}")
    old_path = find_file_in_vault(old_filename)
    if not old_path: return f"Error: Source file {old_filename} not found."

    # 既存のパスからVault相対パスを計算
    src_rel = os.path.relpath(old_path, VAULT_ROOT).replace('\\', '/')

    # LLM由来の引数が CLI に渡るため、Vault外への脱出や危険文字を除去する
    dst_rel = _sanitize_vault_rel_path(new_dir_or_filename)
    if not dst_rel:
        return f"Error: Invalid destination path: {new_dir_or_filename}"
    # .md で終わる場合はリネーム（フルパス指定）、それ以外はフォルダ移動（元ファイル名を維持）
    if not dst_rel.endswith('.md'):
        dst_rel = dst_rel.rstrip('/') + '/' + os.path.basename(old_path)
    
    try:
        run_obsidian_cli(["move", src_rel, dst_rel])
        return f"Success: Moved/Renamed [[{old_filename}]] to {dst_rel}"
    except Exception as e:
        return f"Error moving note via CLI: {e}"

@retry(max_retries=3)
def tool_refactor_note(filename: str, instructions: str) -> str:
    """既存ノートを読み込み、AI自身が指定した指示(instructions)に基づいて内容を再構築・上書きする"""
    print(f"[Tool] 🏗️ Refactoring Note: {filename}")
    path = find_file_in_vault(filename)
    if not path: return f"Error: File {filename} not found."
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            current_content = f.read()
            
        prompt = f'''以下のMarkdownノートの内容を、指定された【修正指示】に従って再構築（リファクタリング）してください。
        
【修正指示】
{instructions}

【現在のノート内容】
{current_content}

【ルール】
- 応答は再構築されたノートの全文（Markdown形式）のみを出力してください。
- バッククォートによるコードブロック表記（```markdown ... ```）は除外し、生の中身だけを出力してください。
'''
        response = client.models.generate_content(
            model=MODEL_NAME_HEAVY, contents=prompt
        )
        new_content = response.text.strip()
        if new_content.startswith("```markdown"):
            new_content = new_content[11:]
        if new_content.startswith("```"):
            new_content = new_content[3:]
        if new_content.endswith("```"):
            new_content = new_content[:-3]
        new_content = new_content.strip()
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        # 変更をObsidianに認識させる
        try:
            vault_rel_path = os.path.relpath(path, VAULT_ROOT).replace('\\', '/')
            run_obsidian_cli(["open", vault_rel_path])
        except Exception as e:
            print(f"[Tool] ⚠️ Failed to trigger Obsidian CLI open: {e}")
        
        return f"Success: Refactored [[{filename}]]"
    except Exception as e:
        return f"Error refactoring note: {e}"
