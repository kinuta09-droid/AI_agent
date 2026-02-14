# -*- coding: utf-8 -*-
"""
Obsidian Tools Module
Extracted from agent_hello.py
"""

import os
import sys
import json
import datetime
import time
import re
from lib_shared import (
    WORK_DIR, VAULT_ROOT, EXCLUDE_DIRS,
    find_file_in_vault, client, MODEL_NAME, retry
)

def tool_vault_search(keywords: str) -> str:
    """Vault内をキーワード検索する"""
    print(f"[Tool] 🧠 Searching Vault: {keywords}")
    kw_list = [k.strip() for k in keywords.split() if k.strip()]
    if not kw_list: return "No keywords."
    hits = []
    MAX_HITS = 10
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            if not file.endswith('.md'): continue
            try:
                with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                score = 0
                snippet = ""
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if any(k.lower() in line.lower() for k in kw_list):
                        score += 1
                        # Expand context: 5 lines before, 25 lines after
                        start = max(0, i - 5)
                        end = min(len(lines), i + 25)
                        snippet = "\n".join(lines[start:end])
                        break
                if score > 0:
                    hits.append(f"- File: [[{file}]]\n  Snippet:\n{snippet}\n...")
                    if len(hits) >= MAX_HITS: break
            except: pass
        if len(hits) >= MAX_HITS: break
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
        return f"Success: Updated [[{filename}]]"
    except Exception as e:
        return f"Error updating note: {e}"

def tool_merge_weekly_reports() -> str:
    """30_Main内の7日以内のレポートを週次レポートにまとめる"""
    print(f"[Tool] 🧹 Merging Weekly Reports...")
    target_dir = os.path.join(WORK_DIR, '30_Main')
    archive_dir = os.path.join(WORK_DIR, '50_Archive')

    today = datetime.datetime.now()
    week_str = today.strftime('%Y-W%U') 
    
    summary_filename = f"{week_str}_Weekly_Summary.md"
    summary_path = os.path.join(target_dir, summary_filename)
    archive_sub_dir = os.path.join(archive_dir, week_str)

    if not os.path.exists(target_dir):
        return "Error: 30_Main directory not found."

    merged_content = f"# Weekly Summary: {week_str}\n\nGenerated: {today.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    files_processed = []

    try:
        os.makedirs(archive_sub_dir, exist_ok=True)
        files = [f for f in os.listdir(target_dir) if f.endswith('.md')]
        if summary_filename in files:
            files.remove(summary_filename)

        if not files:
            return "No files to merge."

        for file in files:
            file_path = os.path.join(target_dir, file)
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                if (today - mtime).days > 7:
                    continue

                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                merged_content += f"## Source: [[{file}]] ({mtime.strftime('%Y-%m-%d')})\n\n{content}\n\n---\n\n"
                files_processed.append(file)
            except Exception as e:
                print(f"Error reading {file}: {e}")

        if not files_processed:
            return "No recent files found to merge."

        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(merged_content)

        for file in files_processed:
            src = os.path.join(target_dir, file)
            dst = os.path.join(archive_sub_dir, file)
            try:
                os.rename(src, dst)
            except Exception as e:
                print(f"Error moving {file}: {e}")

        return f"Success: Created [[{summary_filename}]]. Merged {len(files_processed)} files. Originals moved to 50_Archive/{week_str}."

    except Exception as e:
        return f"Error merging reports: {e}"
