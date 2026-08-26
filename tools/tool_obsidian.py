# -*- coding: utf-8 -*-
"""
Obsidian Tools Module
Extracted from agent_hello.py
"""

import os
import datetime
import re
from lib.lib_shared import (
    WORK_DIR, VAULT_ROOT, EXCLUDE_DIRS,
    find_file_in_vault, client, MODEL_NAME, MODEL_NAME_HEAVY, retry
)
from lib.lib_obsidian_cli import run_obsidian_cli
from lib.lib_obsidian_utils import read_frontmatter_field

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

def _resolve_unique_path(save_dir: str, filename: str) -> tuple[str, str]:
    """save_dir 内で同名衝突が無いファイル名・パスを返す(上書きせずタイムスタンプ接尾辞で退避)。"""
    save_path = os.path.join(save_dir, filename)
    if os.path.exists(save_path):
        timestamp = datetime.datetime.now().strftime('%H%M%S')
        base_name = os.path.splitext(filename)[0]
        filename = f"{base_name}_{timestamp}.md"
        save_path = os.path.join(save_dir, filename)
        print(f"[Tool] ⚠️ File exists, creating with suffix: {filename}")
    return filename, save_path


@retry(max_retries=3)
def tool_create_note(filename: str, content: str, summary: str = "") -> str:
    """Vault内に新規ノートを作成する。Templateを使用する。"""
    print(f"[Tool] 📝 Creating Note: {filename}")
    filename = filename.replace('\\', '/').split('/')[-1]

    if not filename.endswith('.md'): filename += '.md'
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)

    save_dir = os.path.join(WORK_DIR, '30_Main')
    template_path = os.path.join(WORK_DIR, '99_Assets', 'Templates', 'Tpl_Permanent.md')
    filename, save_path = _resolve_unique_path(save_dir, filename)

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


def tool_save_idea(text: str) -> str:
    """雑なアイデアを00_inboxにFleeting Note (status: open) として即保存する。"""
    print(f"[Tool] 💡 Saving Idea: {text[:24]}")
    title = " ".join(text.split())[:24] or "idea"
    filename = re.sub(r'[\\/:*?"<>|]', '_', title) + '.md'

    save_dir = os.path.join(WORK_DIR, '00_inbox')
    template_path = os.path.join(WORK_DIR, '99_Assets', 'Templates', 'Tpl_Fleeting.md')

    try:
        os.makedirs(save_dir, exist_ok=True)
        filename, save_path = _resolve_unique_path(save_dir, filename)

        note_content = text
        if os.path.exists(template_path):
            with open(template_path, 'r', encoding='utf-8') as f:
                tpl = f.read()
            now = datetime.datetime.now()
            tpl = tpl.replace('{{date}}', now.strftime('%Y-%m-%d'))
            tpl = tpl.replace('{{time}}', now.strftime('%H:%M'))
            tpl = tpl.replace('{{title}}', os.path.splitext(filename)[0])
            note_content = tpl.rstrip('\n') + '\n' + text + '\n'

        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(note_content)

        try:
            run_obsidian_cli(["open", f"00_inbox/{filename}"])
        except Exception as e:
            print(f"[Tool] ⚠️ Failed to trigger Obsidian CLI open: {e}")

        return f"Success: Created [[{filename}]] in 00_inbox."
    except Exception as e:
        return f"Error saving idea: {e}"


def tool_list_open_ideas(limit: int = 25) -> list[dict]:
    """00_inbox内の status: open (または未設定) なFleeting Noteを mtime 降順で返す。"""
    inbox_dir = os.path.join(WORK_DIR, '00_inbox')
    if not os.path.isdir(inbox_dir):
        return []
    items = []
    for f in os.listdir(inbox_dir):
        if not f.endswith('.md'):
            continue
        path = os.path.join(inbox_dir, f)
        if not os.path.isfile(path):
            continue
        status = read_frontmatter_field(path, 'status')
        if status is not None and status != 'open':
            continue
        items.append({"title": os.path.splitext(f)[0], "mtime": os.path.getmtime(path)})
    items.sort(key=lambda x: -x["mtime"])
    return items[:limit] if limit else items


_TAGS_BLOCK_RE = re.compile(r'^tags:\n((?:  - .*\n)*)', re.MULTILINE)
_RELATED_NOTES_BLOCK_RE = re.compile(r'(### 関連ノート\n)((?:- .*\n?)*)')


def _add_tag_to_note(path: str, tag: str) -> None:
    """frontmatterのtagsブロックにtagを追記する（既にあれば何もしない=冪等）。"""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    m = _TAGS_BLOCK_RE.search(content)
    if not m:
        return  # tags: ブロックが無いノートは対象外（Fleeting Note等の想定外フォーマット）

    existing_lines = m.group(1)
    if any(line.strip() == f'- {tag}' for line in existing_lines.splitlines()):
        return  # 既に付与済み

    new_block = existing_lines + f'  - {tag}\n'
    content = content[:m.start(1)] + new_block + content[m.end(1):]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _append_related_note_links(path: str, links: list[str]) -> None:
    """### 関連ノート セクションへwikilinkを追記する（プレースホルダ置換 or 重複しない追記）。"""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    m = _RELATED_NOTES_BLOCK_RE.search(content)
    if not m:
        return  # 想定外フォーマットのノートは対象外

    existing_body = m.group(2)
    existing_lines = [l for l in existing_body.splitlines() if l.strip() and l.strip() != '- [[ ]]']
    for link in links:
        line = f'- {link}'
        if line not in existing_lines:
            existing_lines.append(line)

    new_body = "".join(f'{l}\n' for l in existing_lines) if existing_lines else '- [[ ]]\n'
    content = content[:m.start(2)] + new_body + content[m.end(2):]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


_FRONTMATTER_RE = re.compile(r'\A---\n(.*?)\n---\n', re.DOTALL)
_CLIP_SOURCE_RE = re.compile(r'^source:\s*\S', re.MULTILINE)


def _set_frontmatter_field(path: str, field: str, value: str) -> None:
    """frontmatterの{field}: 行を{value}に置換する。無ければ末尾に追加する (冪等)。

    _add_tag_to_note と同じ正規表現ベースの流儀 (YAMLフルパースはしない)。
    frontmatterブロックが無いノートは対象外 (tag_untagged_clips と同じ扱い)。
    """
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    m = _FRONTMATTER_RE.match(content)
    if not m:
        return

    frontmatter = m.group(1)
    field_re = re.compile(rf'^{re.escape(field)}:.*$', re.MULTILINE)
    line = f'{field}: {value}'
    if field_re.search(frontmatter):
        new_frontmatter = field_re.sub(line, frontmatter, count=1)
    else:
        new_frontmatter = frontmatter + f'\n{line}'

    content = content[:m.start(1)] + new_frontmatter + content[m.end(1):]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _append_list_item_to_section(path: str, section_heading: str, item: str) -> bool:
    """Markdownの `## {section_heading}` 見出し直下の連続する `- ` リストの末尾に
    `- {item}` を追記する。見出しが見つからない・ファイルが読めない場合は一切
    書き込まず False を返す (_set_frontmatter_field と同じ「壊れた形式は無視」安全弁)。

    見出し行の直後に連続する `- ` 行だけをブロックとしてマッチさせるため、
    次の `## ` 見出しや別セクションの内容には影響しない。
    """
    line = str(item).strip().replace('\n', ' ')
    if not line:
        return False

    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return False

    section_re = re.compile(
        rf'(^## {re.escape(section_heading)}.*\n(?:- .*\n)*)', re.MULTILINE)
    m = section_re.search(content)
    if not m:
        return False

    content = content[:m.start(1)] + m.group(1) + f'- {line}\n' + content[m.end(1):]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True


def tag_untagged_clips(limit: int = 20) -> str:
    """00_inbox内の未処理Clipノート(Obsidian Web Clipper由来、frontmatterにsource:を持つ)へ
    BitNetでトピックタグ、GeminiでMOC候補タグ(moc/<name>)を付与する。
    処理済みは 'clip/processed' タグで判定(状態ファイル無し・毎回全件走査で冪等性確保)。
    """
    from tools.tool_bitnet import generate_note_tags

    inbox_dir = os.path.join(WORK_DIR, '00_inbox')
    if not os.path.isdir(inbox_dir):
        return "Error: 00_inbox not found."

    processed_count = 0
    for f in os.listdir(inbox_dir):
        if processed_count >= limit:
            break
        if not f.endswith('.md'):
            continue
        path = os.path.join(inbox_dir, f)
        if not os.path.isfile(path):
            continue

        try:
            with open(path, 'r', encoding='utf-8') as fh:
                content = fh.read()
        except Exception as e:
            print(f"[Tool] ⚠️ tag_untagged_clips: failed to read {f}: {e}")
            continue

        fm_match = _FRONTMATTER_RE.match(content)
        if not fm_match:
            continue
        frontmatter = fm_match.group(1)
        if not _CLIP_SOURCE_RE.search(frontmatter):
            continue  # source: が無い = Clipper由来ではない通常のFleeting Note

        tags_match = _TAGS_BLOCK_RE.search(content)
        if tags_match and any(
            line.strip() == '- clip/processed' for line in tags_match.group(1).splitlines()
        ):
            continue  # 処理済み
        if not tags_match:
            # tags: がYAMLブロック形式で存在しないノートは _add_tag_to_note が
            # 何を書いても黙って無視する = clip/processed も永遠に付かず、この
            # ノートだけ毎日 Gemini(有料)を呼び続けるコストリークになる。
            # BitNet/Gemini を呼ぶ前にここで弾き、無駄な呼び出しを避ける。
            print(f"[Tool] ⚠️ tag_untagged_clips: {f} has no YAML-block tags:, skipping (fix Clipper template)")
            continue

        body = content[fm_match.end():]  # frontmatterを除いた本文だけをLLMに渡す
        title = os.path.splitext(f)[0]
        try:
            tags = generate_note_tags(title, body[:200])
            for tag in tags:
                _add_tag_to_note(path, tag.replace(' ', '-'))
        except Exception as e:
            print(f"[Tool] ⚠️ tag_untagged_clips: generate_note_tags failed for {f}: {e}")

        try:
            parent_moc = _determine_parent_moc(body[:3000])
            if parent_moc:
                _add_tag_to_note(path, f"moc/{parent_moc}")
        except Exception as e:
            print(f"[Tool] ⚠️ tag_untagged_clips: MOC determination failed for {f}: {e}")

        # ponytail: clip/processed は「成功」ではなく「試行済み」を意味する簡略化。
        # BitNet/Gemini が両方落ちている間に来たクリップは空タグのまま残る(自動リトライ無し)。
        # アップグレード: 手動タグ削除以外のリカバリが要るとわかったら、失敗時は processed を付けない設計に変える。
        _add_tag_to_note(path, "clip/processed")
        processed_count += 1

    if processed_count > 0:
        from tools.tool_vault_index import update_vault_index
        try:
            update_vault_index()
        except Exception as e:
            print(f"[Tool] ⚠️ tag_untagged_clips: update_vault_index failed: {e}")

    return f"Tagged {processed_count} clip note(s)."


def tool_confirm_related_notes(idea_filename: str, candidate_filenames: list[str]) -> str:
    """関連ノート通知でチェック式メニューから選ばれた候補を確定する処理。
    candidate_filenames は通知した全候補ではなく、ユーザーが選択したサブセット。

    アイデア自身と各候補に同じ階層タグ idea/<タイトル> を付け、アイデア側の
    ### 関連ノート にwikilinkを追記する。連番ID方式はやめ、既に一意な
    アイデアのタイトル（_resolve_unique_path が保証）をそのままタグ名に流用する。
    """
    print(f"[Tool] 🔗 Confirming related notes for: {idea_filename} <- {candidate_filenames}")
    idea_path = find_file_in_vault(idea_filename)
    if not idea_path:
        return f"Error: Idea note {idea_filename} not found."

    idea_title = os.path.splitext(os.path.basename(idea_path))[0]
    tag = f"idea/{idea_title}"

    try:
        _add_tag_to_note(idea_path, tag)
        _append_related_note_links(idea_path, [f'[[{os.path.splitext(c)[0]}]]' for c in candidate_filenames])

        linked = []
        for candidate in candidate_filenames:
            candidate_path = find_file_in_vault(candidate)
            if not candidate_path:
                print(f"[Tool] ⚠️ Related note not found, skipped: {candidate}")
                continue
            _add_tag_to_note(candidate_path, tag)
            linked.append(candidate)

        return f"Success: Linked {len(linked)} related note(s) to [[{idea_title}]] with tag #{tag}."
    except Exception as e:
        return f"Error confirming related notes: {e}"


def tool_list_permanent_ideas(limit: int = 25) -> list[dict]:
    """30_Main内の type/permanent なノートを mtime 降順で返す（/idea-relink のautocomplete用）。"""
    main_dir = os.path.join(WORK_DIR, '30_Main')
    if not os.path.isdir(main_dir):
        return []
    items = []
    for f in os.listdir(main_dir):
        if not f.endswith('.md'):
            continue
        path = os.path.join(main_dir, f)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                head = fh.read(500)
        except Exception:
            continue
        if 'type/permanent' not in head:
            continue
        items.append({"title": os.path.splitext(f)[0], "mtime": os.path.getmtime(path)})
    items.sort(key=lambda x: -x["mtime"])
    return items[:limit] if limit else items


def tool_delete_idea(title: str) -> str:
    """00_inboxのFleeting Noteを削除する(壁打ち完了後の後処理用)。

    find_file_in_vault (vault全域を os.walk で探す) は使わない — 要約後に保存する本ノートが
    元アイデアと同じ題名になった場合、列挙順次第でそちらを消してしまう恐れがあるため、
    00_inbox 直下のパスだけを見る (grill 指摘)。
    """
    filename = title if title.endswith('.md') else title + '.md'
    path = os.path.join(WORK_DIR, '00_inbox', filename)
    if not os.path.exists(path):
        return f"Error: Idea note {title} not found."
    try:
        os.remove(path)
        return f"Success: Deleted [[{title}]]."
    except Exception as e:
        return f"Error deleting idea: {e}"


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
