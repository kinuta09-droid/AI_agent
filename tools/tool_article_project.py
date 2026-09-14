# -*- coding: utf-8 -*-
"""
記事プロジェクト管理ノート (Article Project) モジュール。

目標駆動で記事を作る作業状態 (目標・採用資料・Q&Aログ・完成下書き) を、
Obsidianノート自体を正本として1プロジェクト=1ノートで管理する。
00_System/User_Memory.md のような「グローバルに1つだけ」の状態ノートとは異なり、
複数のプロジェクトを識別する必要があるため frontmatter(YAML) + セクション追記式の
専用フォーマットを使う (tool_create_note の Tpl_Permanent テンプレート適用・
Gemini MOC自動分類は、進行中の作業ノートには不要なノイズになるため使わない)。

完成した記事本体は管理ノートのセクションに埋め込まず、別ファイル (*_Draft.md) に
保存して管理ノートからは [[リンク]] のみで参照する (当初プラン「下書きは管理ノートと
分けて保存し、相互リンクする」の通り)。記事本文は見出し `##` を自由に含みうるため、
管理ノート内のセクション置換ロジック (_replace_section、次の `## ` を終端とみなす) に
記事本文をそのまま埋め込むと、本文中の `## 詳細` のような見出しが誤ってセクション終端と
解釈され、再保存のたびに本文の一部が「別セクション」としてファイルに残り重複する
(レビュー指摘)。記事を独立ファイルにすることでこの問題自体を回避する。

要件④(永続化・再開)で追加した機能:
  - qa_pairs全体(未回答含む)をfrontmatterに永続化し、Bot再起動後に質問リストを復元できる。
  - 手動編集検知: 書き込み直前にディスク上のmtimeを期待値と突き合わせ、ズレていれば
    上書きせず {base}_conflict.md / {base}_Draft_conflict.md へ退避する。
  - Q&A回答等の自由入力テキストに `## ` から始まる行が含まれるとセクション構造が分断される
    欠陥(_replace_section と同型)を、_append_to_section 側でゼロ幅スペース無害化して塞ぐ。

Discordコマンド側から Python 関数として直接呼ばれる想定のため、他の tool_*.py が
LLM(Planner)向けに徹底している「戻り値は常に文字列」という契約はここでは採らず、
dict/tuple/bool をそのまま返す。

Codexレビュー対応 (AP-101/AP-102、arms-article-project-persist-and-publish-001:0):
  - AP-101: save_final_draft の唯一の書き込み経路に sanitize_article_citations を通し、
    未採用の出典(捏造 or 表記揺れ)を検出したら frontmatter.unauthorized_citations に記録、
    promote_and_reflect はこれが残っている限り正式ノート化を拒否する。
  - AP-102: 手動編集競合を検出した際、退避(バックアップ)は作るが比較基準
    (updated/draft_updated)を更新していなかったため、同じ競合が無限に再検出され続けて
    いた。_rebase_conflict_baseline / draft_updated の即時更新でディスク上の現在値を
    新しい基準として採用する。ただしこれにより「保留中のAI生成版」を次回保存で
    無条件に上書きできてしまうため、Discord側 (commands/bot_commands_article_project.py の
    DraftConflictView) でAI版採用/手動編集版維持をボタンで明示選択させる。
"""
import os
import re
import datetime
import tempfile

import yaml

from lib.lib_shared import VAULT_ROOT
from tools.tool_obsidian import (
    _resolve_unique_path, _strip_section_or_warn, tool_fetch_context,
    PERSONAL_INFO_SECTION_HEADING,
)

PROJECTS_DIR = os.path.join(VAULT_ROOT, "00_System", "ArticleProjects")
MAIN_DIR = os.path.join(VAULT_ROOT, "30_Main")

# tool_obsidian._strip_section_or_warn は見出しが `## {PERSONAL_INFO_SECTION_HEADING}` に
# 完全一致する行しか検出できず、見出しに説明文・絵文字・括弧書きが付くと非破壊のまま
# (警告は出るが内容はそのまま残る)。記事は外部に出る可能性がある成果物のため、
# 「警告して続行」では個人情報の絶対除外という要件を満たせない。ここでは見出し行の
# どこかに PERSONAL_INFO_SECTION_HEADING という文字列が含まれていれば検出する、
# より広い正規表現で除去後を再チェックし、なお検出されるファイルは
# 参考資料として採用しない (処理を止めるのはそのファイルの採用であって、
# プロジェクト全体のフローではない)。CommonMark は見出し行の前に最大3スペースの
# インデントを許容するため、先頭の空白も許容する (レビュー指摘: インデント付き見出しの検出漏れ)。
_PERSONAL_INFO_HEADING_RE = re.compile(
    r'^[ \t]{0,3}#{1,6}[^\n]*' + re.escape(PERSONAL_INFO_SECTION_HEADING) + r'[^\n]*$',
    re.MULTILINE,
)

_STATUS_VALUES = ("goal_set", "qa", "draft_done", "completed")

# 記事本文中の出典wikilink。`[[名前]]` `[[名前#見出し]]` `[[名前|別名]]` のいずれも
# 出典名部分(`#`/`|`より前)だけを抽出する。前方の空白も飲み込み、除去後に二重スペースが
# 残らないようにする。
_CITATION_RE = re.compile(r'[ \t]*\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]')

_BODY_SECTIONS = ("知識収集ログ", "不足情報Q&A", "生成した記事（下書き）", "振り返り", "正式ノート")

_DRAFT_HEADING = "生成した記事（下書き）"
_REFLECTION_HEADING = "振り返り"
_PROMOTED_HEADING = "正式ノート"

# ディスクmtimeとfrontmatterの updated/draft_updated (秒精度) を突き合わせる際の許容誤差。
_CONFLICT_TOLERANCE_SEC = 2.0


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _slugify_goal(goal: str, max_len: int = 20) -> str:
    slug = goal.strip()[:max_len]
    slug = re.sub(r'[\\/:*?"<>|\s]+', '_', slug)
    return slug.strip('_') or "untitled"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """`---\\nYAML\\n---\\nbody` を (frontmatter_dict, body) に分ける。形式が崩れていれば空dict+全文をbodyとして返す。"""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', text, flags=re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, m.group(2)


def _join_frontmatter(frontmatter: dict, body: str) -> str:
    fm_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_text}---\n\n{body.lstrip(chr(10))}"


def _atomic_write(path: str, content: str) -> bool:
    """同じディレクトリの一時ファイルに書き込み、成功したら os.replace で置換する。

    以前は open(path, "w") で対象ファイルを直接切り詰めて書いており、書き込みの途中で
    失敗すると (ディスク容量不足・権限エラー等) 元のノートが壊れた状態のまま False が
    返っていた (レビュー指摘: 書き込みがアトミックでない)。os.replace は同一ファイル
    システム上であればアトミックな置換であり、一時ファイルへの書き込みが失敗しても
    元ファイルには一切触れない。
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def _is_conflicting(path: str, expected_updated: str | None) -> bool:
    """ディスク上のファイルが expected_updated(ISO文字列)より後に更新されているか判定する。

    比較できない(ファイル不在・パース不能)場合は「競合ではない」として安全側(＝通常の
    上書きを許可)に倒す。手動編集検知はあくまで保険であり、判定不能を理由に正常な
    保存フローを止めてしまう方が実害が大きい。
    """
    if not expected_updated or not os.path.exists(path):
        return False
    try:
        disk_mtime = os.path.getmtime(path)
        expected_ts = datetime.datetime.fromisoformat(expected_updated).timestamp()
    except (OSError, ValueError):
        return False
    return abs(disk_mtime - expected_ts) > _CONFLICT_TOLERANCE_SEC


def _backup_conflicting_version(path: str) -> str | None:
    """ディスク上の現在の内容(＝手動編集された版)を {base}_conflict{ext} として退避する。
    退避先のファイル名(ベース名のみ)を返す。読み込み自体に失敗、またはバックアップ書き込み
    自体が失敗(ディスク容量不足等)した場合はNone — 以前は書き込み結果を確認しておらず、
    バックアップが実際には存在しないのに成功したかのようなファイル名を返していた
    (レビュー指摘AP-102)。"""
    directory = os.path.dirname(path)
    base, ext = os.path.splitext(os.path.basename(path))
    conflict_filename, conflict_path = _resolve_unique_path(directory, f"{base}_conflict{ext}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    if not _atomic_write(conflict_path, content):
        return None
    return conflict_filename


def _rebase_conflict_baseline(path: str, timestamp_field: str) -> None:
    """自己参照する更新時刻(管理ノート自身のupdated)の競合を検出した際、ディスク上の
    現在の内容(手動編集版)は一切変えずに timestamp_field の値だけをディスク上の実際の
    mtimeへ合わせて書き直す。

    これを行わないと、_backup_conflicting_version でバックアップは作られるのに
    比較基準(expected_updated)が古い値のまま永久に残り、次回の保存試行が同じ競合を
    無限に再検出し続けてQ&A等の更新が一切進められなくなる(レビュー指摘AP-102)。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    frontmatter, body = _split_frontmatter(text)
    if not frontmatter:
        return
    frontmatter[timestamp_field] = datetime.datetime.fromtimestamp(
        os.path.getmtime(path)
    ).isoformat(timespec="seconds")
    _atomic_write(path, _join_frontmatter(frontmatter, body))


def _neutralize_heading_lines(text: str) -> str:
    """行頭の `#`(Markdown見出し、最大3スペースのインデント込み)をゼロ幅スペースで無害化する。

    _replace_section/_append_to_section は次の `^## ` 行をセクション終端の目印にしている
    ため、Q&A回答等の自由入力テキストに `## ` から始まる行が含まれると、管理ノートの
    セクション構造が意図せず分断される(save_final_draftが記事本体を別ファイルへ
    切り出した理由と同型の欠陥、レビュー指摘)。ゼロ幅スペースを見出し記号の直前に
    挿むことで、見た目をほぼ変えずに正規表現の完全一致だけを防ぐ。
    """
    return re.sub(r'^([ \t]{0,3})(#{1,6}[ \t])', '\\1\u200b\\2', text, flags=re.MULTILINE)


def _normalize_citation_name(name: str) -> str:
    name = name.strip()
    if name.lower().endswith('.md'):
        name = name[:-3]
    return name


def sanitize_article_citations(article: str, adopted_source_files: list[str]) -> tuple[str, list[str]]:
    """記事本文中の出典wikilink(`[[ファイル名]]`)を検証し、地の文からは取り除いた上で
    実際に引用された採用資料だけの一覧を末尾に付け直す。

    generate_final_article等のプロンプトは「資料由来の記述には出典[[ファイル名]]を付ける」
    と指示しているが、Geminiはこれを守っても (a) 採用していない資料名を出典として捏造する、
    (b) 生のwikilinkを地の文にそのまま埋め込み体験談としての読み味を損なう、という2つの
    問題を起こしうる(レビュー指摘AP-101)。プロンプト指示だけに依存せず、ここで機械的に
    検証・再構成する。

    戻り値: (出典を除去し末尾に一覧を付けた本文, 未採用だった出典名のリスト)。
    未採用出典が1件でもあれば、呼び出し側は完成確定・正式ノート化を止めること。
    """
    allowed = {_normalize_citation_name(f) for f in adopted_source_files}
    cited: list[str] = []
    unauthorized: list[str] = []

    def _strip(m: re.Match) -> str:
        name = _normalize_citation_name(m.group(1))
        if name not in allowed:
            unauthorized.append(name)
        elif name not in cited:
            cited.append(name)
        return ""

    body = _CITATION_RE.sub(_strip, article).rstrip()
    ordered_cited = [f for f in adopted_source_files if _normalize_citation_name(f) in cited]
    if ordered_cited:
        # 「採用資料」と呼ぶと"採用した全資料=実際に引用した資料"だと誤解を招くため、
        # 実際に本文で引用された分だけを指す見出しにする(レビュー指摘)。
        lines = "\n".join(f"- [[{_normalize_citation_name(f)}]]" for f in ordered_cited)
        body += f"\n\n---\n### 参照資料\n{lines}\n"
    return body, unauthorized


def _draft_filename(filename: str) -> str:
    """管理ノートのファイル名から、記事本体を保存する独立ファイル名を導出する。"""
    base = os.path.splitext(filename)[0]
    return f"{base}_Draft.md"


def create_project(goal: str, audience: str = "", thread_id: int | None = None, user_id: int | None = None) -> tuple[str | None, str | None]:
    """管理ノートを新規作成する。戻り値 (filename, error) — 成功時 error は None。"""
    if not goal or not goal.strip():
        return None, "goal is empty"

    os.makedirs(PROJECTS_DIR, exist_ok=True)
    date_prefix = datetime.datetime.now().strftime("%Y%m%d")
    raw_filename = f"{date_prefix}_{_slugify_goal(goal)}.md"
    filename, save_path = _resolve_unique_path(PROJECTS_DIR, raw_filename)

    now = _now_iso()
    frontmatter = {
        "type": "article_project",
        "status": "goal_set",
        "goal": goal.strip(),
        "audience": audience.strip(),
        "thread_id": thread_id,
        "user_id": user_id,
        "adopted_source_files": [],
        "qa_pairs": [],
        "draft_filename": None,
        "draft_updated": None,
        "unauthorized_citations": [],
        "promoted_filename": None,
        "reflection": None,
        "created": now,
        "updated": now,
    }
    body = "\n\n".join(f"## {heading}\n" for heading in _BODY_SECTIONS)

    if not _atomic_write(save_path, _join_frontmatter(frontmatter, body)):
        return None, "failed to write project note"

    return filename, None


def load_project(filename: str) -> dict | None:
    """frontmatter(dict) + body(str) を返す。存在しない/パース失敗はNone。"""
    path = os.path.join(PROJECTS_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    frontmatter, body = _split_frontmatter(text)
    if not frontmatter:
        return None
    return {"frontmatter": frontmatter, "body": body}


def load_draft_article(filename: str) -> str | None:
    """管理ノートに紐づく記事本体 (*_Draft.md) を読み込む。無ければNone。"""
    draft_path = os.path.join(PROJECTS_DIR, _draft_filename(filename))
    if not os.path.exists(draft_path):
        return None
    try:
        with open(draft_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def list_projects(statuses: list[str] | None = None) -> list[dict]:
    """PROJECTS_DIR配下の管理ノート(*_Draft.md/*_conflict*.mdを除く)を列挙する。

    戻り値: [{"filename": str, "frontmatter": dict}, ...]。statuses指定時はフィルタ。
    ファイル数が少ない前提のため、都度ディレクトリスキャンで十分(別インデックスは持たない)。
    """
    if not os.path.isdir(PROJECTS_DIR):
        return []
    results = []
    for entry in sorted(os.listdir(PROJECTS_DIR)):
        if not entry.endswith(".md"):
            continue
        if entry.endswith("_Draft.md") or "_conflict" in entry:
            continue
        project = load_project(entry)
        if project is None:
            continue
        frontmatter = project["frontmatter"]
        if statuses is not None and frontmatter.get("status") not in statuses:
            continue
        results.append({"filename": entry, "frontmatter": frontmatter})
    return results


def _write_project(filename: str, frontmatter: dict, body: str, expected_updated: str | None = None) -> bool:
    """管理ノートを1回のload/writeで書き込む。expected_updatedを渡すと、書き込み直前に
    ディスク上のmtimeと突き合わせ、ズレていれば(＝ユーザーが手動編集した)上書きせず
    {base}_conflict.md へ退避してFalseを返す。"""
    path = os.path.join(PROJECTS_DIR, filename)
    if _is_conflicting(path, expected_updated):
        if _backup_conflicting_version(path) is not None:
            # 手動編集版を新しい基準として採用する(レビュー指摘AP-102、詳細は
            # _rebase_conflict_baseline のdocstring)。これをしないと次回の
            # update_project_fields等が同じ競合を無限に再検出し続ける。
            _rebase_conflict_baseline(path, "updated")
        return False
    frontmatter["updated"] = _now_iso()
    return _atomic_write(path, _join_frontmatter(frontmatter, body))


def update_project_fields(filename: str, **fields) -> bool:
    """frontmatterの指定キーを更新して保存する (updatedは自動更新)。"""
    project = load_project(filename)
    if project is None:
        return False
    expected_updated = project["frontmatter"].get("updated")
    project["frontmatter"].update(fields)
    return _write_project(filename, project["frontmatter"], project["body"], expected_updated)


def sanitize_reference_material(filenames: list[str]) -> tuple[str, list[str]]:
    """採用ファイルの本文を取得し、個人情報セクションを機械的に除去する。

    ファイルごとに tool_fetch_context で本文を取得 → _strip_section_or_warn で標準見出しを
    除去 → 除去後もなお「パーソナル情報」を含む見出しが残っていれば(見出しの書式が
    崩れていて除去できなかった場合)、そのファイルは参考資料に含めず除外する。

    戻り値: (安全化された reference_material, 除外されたファイル名のリスト)。
    除外されたファイルがあれば呼び出し側でユーザーに通知すること。
    """
    safe_parts = []
    excluded = []
    for filename in filenames:
        raw = tool_fetch_context(filename)
        stripped = _strip_section_or_warn(raw, PERSONAL_INFO_SECTION_HEADING, "ArticleProject")
        if _PERSONAL_INFO_HEADING_RE.search(stripped):
            excluded.append(filename)
            continue
        safe_parts.append(stripped)
    return "\n\n".join(safe_parts), excluded


def _replace_section(body: str, heading: str, new_section_text: str) -> str:
    """`## heading` セクションの本文を new_section_text で丸ごと置き換える。無ければ末尾に新規作成する。

    new_section_text 自体に `##` 見出しを含めてはならない — このセクション終端の判定は
    次の `^## ` 行を目印にしているため、埋め込む側に `##` があると本文の途中で
    セクションが終わったと誤認識される (レビュー指摘、save_final_draft が記事本体を
    別ファイルへ切り出した理由と同型の制約)。短い管理用テキスト(採用資料一覧・
    Q&Aログ・記事へのリンク1行)専用であり、任意の長文Markdownを渡さないこと。
    """
    pattern = rf'(^## {re.escape(heading)}\s*\n)(.*?)(?=^## |\Z)'
    m = re.search(pattern, body, flags=re.MULTILINE | re.DOTALL)
    if m:
        return body[:m.start()] + m.group(1) + f"{new_section_text}\n\n" + body[m.end():]
    return body.rstrip('\n') + f"\n\n## {heading}\n{new_section_text}\n"


def _append_to_section(body: str, heading: str, text: str) -> str:
    """`## heading` セクション末尾にtextを追記した本文を返す(既存本文を保持したまま連結)。

    text はユーザーの自由入力(Q&A回答等)を想定しており、行頭の `## ` 等はセクション
    構造を壊さないよう自動的に無害化する(_neutralize_heading_lines、レビュー指摘)。
    """
    text = _neutralize_heading_lines(text)
    pattern = rf'^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)'
    m = re.search(pattern, body, flags=re.MULTILINE | re.DOTALL)
    existing = m.group(1).rstrip('\n') if m else ""
    combined = f"{existing}\n{text}"
    return _replace_section(body, heading, combined)


def append_project_section(filename: str, heading: str, text: str) -> bool:
    """`## heading` セクション末尾にtextを追記する。無ければ新規セクションを末尾に作成する。"""
    project = load_project(filename)
    if project is None:
        return False
    expected_updated = project["frontmatter"].get("updated")
    body = _append_to_section(project["body"], heading, text)
    return _write_project(filename, project["frontmatter"], body, expected_updated)


def save_adopted_sources(filename: str, adopted_source_files: list[str], log_text: str) -> bool:
    """知識収集フェーズの採用資料を記録する。status を qa に更新し「知識収集ログ」
    セクションへ追記する — save_final_draft と同じ理由で1回のload/writeにまとめ、
    status更新とログ追記のどちらかだけが失敗する中間状態を作らない
    (レビュー指摘: 資料採用時の保存結果が未確認だった)。

    呼び出し側は戻り値が False の場合、採用履歴の保存が確認できるまで次のフェーズへ
    進めず、同じ内容で再試行できるようにすること。
    """
    project = load_project(filename)
    if project is None:
        return False
    frontmatter = project["frontmatter"]
    expected_updated = frontmatter.get("updated")
    frontmatter["status"] = "qa"
    frontmatter["adopted_source_files"] = adopted_source_files
    body = _append_to_section(project["body"], "知識収集ログ", log_text)
    return _write_project(filename, frontmatter, body, expected_updated)


def record_qa_answer(filename: str, qa_pairs_snapshot: list[dict], question: str, answer: str) -> bool:
    """qa_pairs全体(frontmatter、未回答含む)の更新と「不足情報Q&A」への表示用ログ追記を
    1回のload/writeで行う。呼び出し側は qa_pairs_snapshot の該当エントリに answer を
    書き込んでから渡すこと。

    qa_pairs全体をfrontmatterに永続化することで、Bot再起動後も質問リスト(回答済み/
    未回答問わず)を復元できる — 以前は回答済みログのテキストしか残らず、未回答の質問文・
    出題順が完全にセッションメモリ依存だった(要件④の永続化の核心)。
    """
    project = load_project(filename)
    if project is None:
        return False
    frontmatter = project["frontmatter"]
    expected_updated = frontmatter.get("updated")
    frontmatter["qa_pairs"] = qa_pairs_snapshot
    body = _append_to_section(project["body"], "不足情報Q&A", f"Q: {question}\nA: {answer}")
    return _write_project(filename, frontmatter, body, expected_updated)


def save_final_draft(filename: str, article: str) -> tuple[bool, str | None]:
    """記事完成時の保存。記事本体を独立ファイル (*_Draft.md) にアトミック書き込みし、
    管理ノートには status(draft_done) と記事へのリンクだけを1回のload/writeで記録する。

    以前は記事本文そのものを管理ノートの「生成した記事（下書き）」セクションに埋め込んで
    いたが、本文中の `## 見出し` が _replace_section の終端判定を誤らせ、再保存のたびに
    本文の一部が別セクションとして残留・重複する欠陥があった (レビュー指摘)。
    記事を別ファイルに切り出すことで、管理ノート側のセクション置換ロジックには
    「[[リンク]]」という `##` を含まない短いテキストしか渡らなくなり、この問題が
    構造的に起きなくなる。記事ファイル自体の書き込みも _atomic_write で行うため、
    再試行しても同じ内容で上書きされるだけで重複しない (冪等)。

    戻り値: (成功したか, 競合時の退避ファイル名 or None)。下書きファイルがディスク上で
    手動編集されていた場合は上書きせず {base}_Draft_conflict.md へ退避し (False, filename)
    を返す — 管理ノート側の書き込みも行わない(statusだけ進んで本文が伴わない
    中間状態を避けるため、1回のload/writeで整合させる既存の思想を維持)。
    """
    project = load_project(filename)
    if project is None:
        return False, None

    frontmatter = project["frontmatter"]
    draft_filename = _draft_filename(filename)
    draft_path = os.path.join(PROJECTS_DIR, draft_filename)

    expected_draft_updated = frontmatter.get("draft_updated")
    if _is_conflicting(draft_path, expected_draft_updated):
        backup_filename = _backup_conflicting_version(draft_path)
        if backup_filename is not None:
            # 手動編集版のmtimeを新しい基準として管理ノートへ記録する。これをしないと
            # draft_updatedが古い値のまま残り続け、次回の保存試行が同じ競合を無限に
            # 再検出し続ける(レビュー指摘AP-102)。draft_updatedは管理ノート側の
            # frontmatterフィールドなので、更新は自己参照にならず _write_project の
            # 通常の(管理ノート自身に対する)競合チェックにそのまま乗る。
            new_draft_updated = datetime.datetime.fromtimestamp(
                os.path.getmtime(draft_path)
            ).isoformat(timespec="seconds")
            update_project_fields(filename, draft_updated=new_draft_updated)
        return False, backup_filename

    adopted = frontmatter.get("adopted_source_files") or []
    article, unauthorized_citations = sanitize_article_citations(article, adopted)

    if not _atomic_write(draft_path, article):
        return False, None

    expected_updated = frontmatter.get("updated")
    frontmatter["status"] = "draft_done"
    frontmatter["draft_filename"] = draft_filename
    frontmatter["draft_updated"] = _now_iso()
    frontmatter["unauthorized_citations"] = unauthorized_citations
    link_text = f"[[{os.path.splitext(draft_filename)[0]}]]"
    body = _replace_section(project["body"], _DRAFT_HEADING, link_text)
    ok = _write_project(filename, frontmatter, body, expected_updated)
    return ok, None


def _extract_article_title(article: str) -> str | None:
    """記事本文のH1行(`# タイトル`)からタイトルを抽出する。generate_final_articleの
    「タイトル（# ﾀｲﾄﾙ）」出力規約に依拠する(tool_paper_digest.build_note_articleと同型)。"""
    m = re.match(r'^#\s+(.+?)\s*$', article, flags=re.MULTILINE)
    return m.group(1).strip() if m else None


def _sanitize_title_for_filename(title: str, max_len: int = 60) -> str:
    """_slugify_goalと同じ禁止文字除去だがtruncateは緩め(記事タイトルはファイル名として
    読める方が正式ノートとして自然なため)。"""
    sanitized = re.sub(r'[\\/:*?"<>|]', '_', title.strip())[:max_len]
    return sanitized.strip('_ ') or "untitled"


def promote_and_reflect(filename: str, learning: str, next_action: str) -> tuple[str | None, str | None]:
    """完成確定+正式ノート化+振り返り記録を行う。戻り値 (正式ノートのfilename, error)。

    記事本体(*_Draft.md)を独立した正式ノートとして30_Mainへ書き起こし、管理ノートへの
    `up:`リンクを持たせる(記事→管理ノートの逆リンク。相互リンクの片方向問題への
    直接対応ではないが、新規実装として自然に満たされる副産物)。振り返り(学び・次に
    試すこと)はGeminiに生成させず、ユーザーの自由記述をそのまま記録する — 「使った
    知識」「追加した経験」は既存のadopted_source_files/qa_pairsから機械的に導出できる
    情報であり、資料にない体験を捏造しない既存方針と同じ理由でLLM生成を避ける。

    冪等性: frontmatterに既にpromoted_filenameがあれば再書き込みせずそれを返す
    (通常の再クリック・リトライはこれで冪等になる)。「30_Main書き込みは成功したが
    管理ノート側の記録が失敗した」という稀なケースでは、次回リトライ時にこのガードが
    効かず正式ノートが重複作成されうる — 真の原子性は物理的に別ファイル2つには
    適用できないため、頻発したら promoted_from マーカーでの再利用ロジックを追加する
    (ponytail: 現状はこの限界を許容する)。
    """
    project = load_project(filename)
    if project is None:
        return None, "project not found"
    frontmatter = project["frontmatter"]

    existing_promoted = frontmatter.get("promoted_filename")
    if existing_promoted:
        return existing_promoted, None

    unauthorized_citations = frontmatter.get("unauthorized_citations") or []
    if unauthorized_citations:
        # save_final_draft が検出した未採用出典が残ったまま正式ノート化を許すと、
        # 資料にない出典をハルシネーションで生成した記事がそのまま公開されうる
        # (レビュー指摘AP-101)。修正依頼で解消してから完成確定してもらう。
        names = "、".join(unauthorized_citations)
        return None, f"未採用の出典が検出されたため正式ノート化できません: {names}。修正依頼でこの出典を解消してからもう一度お試しください。"

    article = load_draft_article(filename)
    if article is None:
        return None, "draft article not found"

    title = _extract_article_title(article) or frontmatter.get("goal") or "記事"
    raw_main_filename = f"{_sanitize_title_for_filename(title)}.md"
    os.makedirs(MAIN_DIR, exist_ok=True)
    main_filename, main_path = _resolve_unique_path(MAIN_DIR, raw_main_filename)

    management_stem = os.path.splitext(filename)[0]
    adopted = frontmatter.get("adopted_source_files") or []
    reference_lines = "\n".join(f"- [[{os.path.splitext(f)[0]}]]" for f in adopted)
    reference_section = f"\n\n### 参照元\n{reference_lines}" if reference_lines else ""

    main_frontmatter = {
        "tags": ["type/permanent", "type/article"],
        "created": datetime.date.today().isoformat(),
        "status": "done",
        "up": f"[[{management_stem}]]",
        "promoted_from": filename,
    }
    main_body = (
        f"{article.rstrip()}\n\n"
        "## 🔗 リンク\n"
        f"### 管理ノート\n- [[{management_stem}]]"
        f"{reference_section}\n"
    )
    if not _atomic_write(main_path, _join_frontmatter(main_frontmatter, main_body)):
        return None, "failed to write main note"

    expected_updated = frontmatter.get("updated")
    frontmatter["status"] = "completed"
    frontmatter["promoted_filename"] = main_filename
    frontmatter["reflection"] = {
        "learning": learning.strip(),
        "next_action": next_action.strip(),
        "recorded": _now_iso(),
    }
    body = _replace_section(project["body"], _PROMOTED_HEADING, f"[[{os.path.splitext(main_filename)[0]}]]")
    body = _append_to_section(
        body, _REFLECTION_HEADING,
        f"学んだこと: {learning}\n次に試すこと: {next_action or '(未記入)'}",
    )
    if not _write_project(filename, frontmatter, body, expected_updated):
        # ponytail: 30_Main書き込み成功後の管理ノート更新失敗時、リトライで正式ノートが
        # 重複作成されうる。頻発したらpromoted_fromマーカーでの再利用ロジックを追加する。
        return None, "failed to update project note"

    return main_filename, None


_STATUS_SECTION_TITLES = {
    "completed": "✅ 完成・正式ノート化済み",
    "draft_done": "📝 下書き完成・推敲中",
    "qa": "❓ Q&A中",
    "goal_set": "🎯 目標設定のみ",
}


def build_dashboard_text(projects: list[dict]) -> str:
    """list_projects()の戻り値をステータス別にグルーピングしたMarkdownへ組み立てる
    (純関数、I/Oなし・テスト容易)。"""
    if not projects:
        return "## 🎯 記事プロジェクト一覧 (0件)\n\n進行中のプロジェクトはありません。"

    groups: dict[str, list[dict]] = {}
    for p in projects:
        status = p["frontmatter"].get("status") or "goal_set"
        groups.setdefault(status, []).append(p)

    lines = [f"## 🎯 記事プロジェクト一覧 ({len(projects)}件)\n"]

    for status in ("completed", "draft_done", "qa", "goal_set"):
        items = groups.get(status, [])
        if not items:
            continue
        title = _STATUS_SECTION_TITLES.get(status, status)
        lines.append(f"### {title} ({len(items)})")
        for p in items:
            fm = p["frontmatter"]
            goal = fm.get("goal") or "(目標未設定)"
            thread_id = fm.get("thread_id")
            thread_ref = f" スレッド: <#{thread_id}>" if thread_id else ""

            if status == "completed":
                promoted = fm.get("promoted_filename")
                link = f"[[{os.path.splitext(promoted)[0]}]]" if promoted else "(未リンク)"
                learning = (fm.get("reflection") or {}).get("learning", "")
                lines.append(f"- {link} — 目標: {goal} / 学び: {learning}")
            elif status == "draft_done":
                draft = fm.get("draft_filename")
                link = f"[[{os.path.splitext(draft)[0]}]]" if draft else "(下書き未保存)"
                lines.append(f"- {link} — 目標: {goal}{thread_ref}")
            elif status == "qa":
                qa_pairs = fm.get("qa_pairs") or []
                answered = sum(1 for qa in qa_pairs if qa.get("answer") is not None)
                lines.append(f"- 目標: {goal}（{answered}/{len(qa_pairs)}問回答済み）{thread_ref}")
            else:
                lines.append(f"- 目標: {goal}{thread_ref}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------
# X投稿連携 (/x-post): 昇格済み記事(status=completed)からX投稿文を生成し、
# 投稿成功後にfrontmatterへ結果を書き戻す。
# ---------------------------------------------------------

X_POST_MAX_CHARS = 280

# Xのt.co短縮URLは実長に関わらず常にこの重み付き文字数として数えられる(公式仕様)。
# URLを付ける場合、本文の生成上限はここから逆算して縮める。
X_URL_WEIGHTED_LENGTH = 23


def _generate_x_post_text(article_body: str, url: str | None = None) -> tuple[str | None, str | None]:
    """記事本文からX投稿文(280字以内)を1本生成する。280字超過時は1回だけ詰め直す。

    url が指定された場合、本文はその分(X_URL_WEIGHTED_LENGTH + 区切り1字)だけ
    短く生成し、末尾に改行区切りで付加した最終テキストを返す(投稿全体が280字
    相当に収まるようにするため。本文自体にURLを書かせるとX側のweighted length
    計算と二重に消費してしまう)。戻り値 (text, error)。"""
    from lib.lib_shared import client, MODEL_NAME

    body_limit = X_POST_MAX_CHARS - (X_URL_WEIGHTED_LENGTH + 1) if url else X_POST_MAX_CHARS
    url_rule = (
        "- URLは本文に書かない(別途こちらで末尾に付加するため)\n" if url else "- URLは一切含めない\n"
    )
    prompt = (
        "以下の記事本文から、X(旧Twitter)投稿用の文章を1つ作成してください。\n"
        "条件:\n"
        f"- 日本語で{body_limit}字以内(厳守)\n"
        f"{url_rule}"
        "- 記事の一番刺さる要点を1つだけ伝え、読者の興味を引く文にする\n"
        "- ハッシュタグは最大2個まで、本文の末尾に付けてよい\n"
        "- 本文のみを出力し、前置きや説明・囲み記号は一切付けない\n\n"
        f"--- 記事本文 ---\n{article_body[:4000]}\n"
    )
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        text = (response.text or "").strip()
    except Exception as e:
        return None, f"X投稿文の生成に失敗しました: {e}"

    if len(text) > body_limit:
        retry_prompt = (
            f"以下の文章を、意味を保ったまま{body_limit}字以内に短縮してください。"
            f"本文のみを出力してください。\n\n{text}"
        )
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=retry_prompt)
            text = (response.text or "").strip()
        except Exception as e:
            return None, f"{body_limit}字への詰め直しに失敗しました: {e}"
        if len(text) > body_limit:
            return None, f"{body_limit}字以内に収まりませんでした({len(text)}字)。手動で作成してください。"

    if url:
        text = f"{text}\n{url}"
    return text, None


def generate_x_post_draft(management_filename: str, url: str | None = None) -> tuple[dict | None, str | None]:
    """昇格済み(status=completed)の管理ノートから、X投稿用の下書きを生成する。

    対象記事は管理ノートの frontmatter.promoted_filename (30_Main直下のベース名) から
    読む — find_file_in_vault のパス区切り混入バグ(既知の教訓)を避けるため直接
    MAIN_DIR と結合する。url を指定すると本文末尾に付加した状態で返す
    (_generate_x_post_text 参照)。戻り値 (draft, error)、
    draft = {"text": str, "source_note": str, "char_count": int}。
    """
    project = load_project(management_filename)
    if project is None:
        return None, f"プロジェクトが見つかりません: {management_filename}"
    frontmatter = project["frontmatter"]
    if frontmatter.get("status") != "completed":
        return None, "昇格済み(status=completed)の記事のみX投稿文を生成できます。"
    promoted_filename = frontmatter.get("promoted_filename")
    if not promoted_filename:
        return None, "正式ノート(promoted_filename)が見つかりません。"

    main_path = os.path.join(MAIN_DIR, promoted_filename)
    try:
        with open(main_path, "r", encoding="utf-8") as f:
            main_text = f.read()
    except OSError:
        return None, f"正式ノートの読み込みに失敗しました: {promoted_filename}"
    _, article_body = _split_frontmatter(main_text)

    text, error = _generate_x_post_text(article_body, url=url)
    if error:
        return None, error
    return {"text": text, "source_note": promoted_filename, "char_count": len(text)}, None


def record_x_post_result(source_note: str, tweet_url: str, posted_at: str, text: str) -> bool:
    """投稿成功後、対象記事(30_Main直下)のfrontmatterに投稿結果を書き戻す。

    DB(lib/x_post_store.py)側が正本のため、ここはベストエフォート。失敗しても
    例外を投げず False を返すだけに留め、呼び出し側は obsidian_synced フラグを
    False のまま残して再投稿トリガーには使わない(既に posted 確定後のため)。
    """
    path = os.path.join(MAIN_DIR, source_note)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return False
    frontmatter, body = _split_frontmatter(raw)
    if not frontmatter:
        return False
    frontmatter["x_post_url"] = tweet_url
    frontmatter["x_post_at"] = posted_at
    frontmatter["x_post_text"] = text
    return _atomic_write(path, _join_frontmatter(frontmatter, body))
