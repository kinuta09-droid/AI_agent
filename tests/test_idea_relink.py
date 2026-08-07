# -*- coding: utf-8 -*-
"""
アイデアの関連ノート自動検出・紐づけ機能の回帰テスト。

対象:
  - tools/tool_obsidian.py: _add_tag_to_note / _append_related_note_links /
    tool_confirm_related_notes / tool_list_permanent_ideas
  - commands/bot_commands_idea.py: permanent_idea_autocomplete / cmd_idea_relink /
    RelatedNotesView

方針: 実Vault・実Gemini I/Oは一切禁止。ファイルI/Oを伴うテストはtempfileの
一時ディレクトリに偽ノートを書いて検証する (実Vaultは絶対に触らない)。
find_file_in_vault / WORK_DIR はmonkeypatchで一時ディレクトリを指すようにする。

実行: py tests/test_idea_relink.py
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_obsidian as obs_mod
import commands.bot_commands_idea as idea_mod


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _make_note(path: str, tags=None, related=None):
    """簡易テンプレ (frontmatter tags: + ### 関連ノート) を持つ偽ノートを書く。"""
    tags = tags if tags is not None else ["type/permanent"]
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    related_lines = "".join(f"- {r}\n" for r in related) if related else "- [[ ]]\n"
    content = f"---\ntags:\n{tag_lines}---\n\n# Note\n\n### 関連ノート\n{related_lines}"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


# ---------------------------------------------------------
# _add_tag_to_note
# ---------------------------------------------------------

def test_add_tag_to_note_appends_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "note.md")
        _make_note(path)

        obs_mod._add_tag_to_note(path, "idea/foo")
        content1 = _read(path)
        assert content1.count("  - idea/foo\n") == 1, content1

        obs_mod._add_tag_to_note(path, "idea/foo")
        content2 = _read(path)
        assert content2 == content1, "同じタグの2回目追記で内容が変わった (冪等性違反)"
    print("✅ [1] _add_tag_to_note: 追記1回・2回目は冪等")


# ---------------------------------------------------------
# _append_related_note_links
# ---------------------------------------------------------

def test_append_related_note_links_replaces_placeholder_and_dedups():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "note.md")
        _make_note(path, related=None)  # プレースホルダのみ

        obs_mod._append_related_note_links(path, ["[[A]]"])
        content = _read(path)
        assert "- [[ ]]" not in content, content
        assert "- [[A]]" in content, content

        obs_mod._append_related_note_links(path, ["[[A]]"])  # 重複追記
        content2 = _read(path)
        assert content2.count("[[A]]") == 1, f"同じリンクが重複追記された: {content2}"

        obs_mod._append_related_note_links(path, ["[[B]]"])  # 別リンクは追記される
        content3 = _read(path)
        assert "[[A]]" in content3 and "[[B]]" in content3, content3
    print("✅ [2] _append_related_note_links: プレースホルダ置換+重複排除+新規追記")


# ---------------------------------------------------------
# tool_confirm_related_notes
# ---------------------------------------------------------

def _fake_find_file_in_vault(vault_map):
    def _find(filename):
        if not filename.endswith(".md"):
            filename += ".md"
        return vault_map.get(filename)
    return _find


def test_tool_confirm_related_notes_links_idea_and_candidates():
    with tempfile.TemporaryDirectory() as d:
        idea_path = os.path.join(d, "アイデア本体.md")
        cand1_path = os.path.join(d, "候補1.md")
        cand2_path = os.path.join(d, "候補2.md")
        _make_note(idea_path)
        _make_note(cand1_path)
        _make_note(cand2_path)

        vault_map = {
            "アイデア本体.md": idea_path,
            "候補1.md": cand1_path,
            "候補2.md": cand2_path,
        }
        orig = obs_mod.find_file_in_vault
        try:
            obs_mod.find_file_in_vault = _fake_find_file_in_vault(vault_map)
            result = obs_mod.tool_confirm_related_notes("アイデア本体.md", ["候補1.md", "候補2.md"])
        finally:
            obs_mod.find_file_in_vault = orig

        assert result.startswith("Success:"), result
        idea_content = _read(idea_path)
        assert "idea/アイデア本体" in idea_content, idea_content
        assert "[[候補1]]" in idea_content and "[[候補2]]" in idea_content, idea_content
        cand1_content = _read(cand1_path)
        assert "idea/アイデア本体" in cand1_content, f"candidateにタグが付いていない: {cand1_content}"
    print("✅ [3] tool_confirm_related_notes: idea本体+candidate双方にタグ付与、idea側にリンク追記")


def test_tool_confirm_related_notes_missing_candidate_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        idea_path = os.path.join(d, "アイデア本体.md")
        _make_note(idea_path)
        vault_map = {"アイデア本体.md": idea_path}  # candidateは見つからない
        orig = obs_mod.find_file_in_vault
        try:
            obs_mod.find_file_in_vault = _fake_find_file_in_vault(vault_map)
            result = obs_mod.tool_confirm_related_notes("アイデア本体.md", ["存在しない候補.md"])
        finally:
            obs_mod.find_file_in_vault = orig
        assert result.startswith("Success: Linked 0 related note(s)"), result
    print("✅ [4] tool_confirm_related_notes: 見つからないcandidateはスキップしログのみ")


def test_tool_confirm_related_notes_idea_not_found():
    orig = obs_mod.find_file_in_vault
    try:
        obs_mod.find_file_in_vault = lambda filename: None
        result = obs_mod.tool_confirm_related_notes("no_such.md", ["x.md"])
    finally:
        obs_mod.find_file_in_vault = orig
    assert result == "Error: Idea note no_such.md not found.", result
    print("✅ [5] tool_confirm_related_notes: idea_filenameが見つからなければError文字列")


# ---------------------------------------------------------
# tool_list_permanent_ideas
# ---------------------------------------------------------

def test_tool_list_permanent_ideas_filters_by_type():
    with tempfile.TemporaryDirectory() as d:
        main_dir = os.path.join(d, "30_Main")
        os.makedirs(main_dir)
        perm_path = os.path.join(main_dir, "perm.md")
        other_path = os.path.join(main_dir, "other.md")
        with open(perm_path, 'w', encoding='utf-8') as f:
            f.write("---\ntags:\n  - type/permanent\n---\n# Perm\n")
        with open(other_path, 'w', encoding='utf-8') as f:
            f.write("---\ntags:\n  - type/fleeting\n---\n# Other\n")

        orig = obs_mod.WORK_DIR
        try:
            obs_mod.WORK_DIR = d
            result = obs_mod.tool_list_permanent_ideas()
        finally:
            obs_mod.WORK_DIR = orig

        titles = [r["title"] for r in result]
        assert titles == ["perm"], f"type/permanent以外が混ざった: {titles}"
    print("✅ [6] tool_list_permanent_ideas: type/permanentを含むノートだけを返す")


# ---------------------------------------------------------
# permanent_idea_autocomplete
# ---------------------------------------------------------

async def _run_permanent_idea_autocomplete_filters():
    orig = idea_mod.tool_list_permanent_ideas
    try:
        idea_mod.tool_list_permanent_ideas = lambda *a, **k: [
            {"title": "音声作品まとめ", "mtime": 2}, {"title": "催眠もの企画", "mtime": 1}
        ]
        choices = await idea_mod.permanent_idea_autocomplete(_make_fake_interaction(1), "音声")
    finally:
        idea_mod.tool_list_permanent_ideas = orig

    assert len(choices) == 1, f"部分一致フィルタが効いていない: {choices}"
    assert choices[0].value == "音声作品まとめ", choices[0].value  # prefixなし (idea_autocompleteと違う点)


def test_permanent_idea_autocomplete_filters():
    asyncio.run(_run_permanent_idea_autocomplete_filters())
    print("✅ [7] permanent_idea_autocomplete: 部分一致でフィルタしprefix無しでvalueを返す")


# ---------------------------------------------------------
# cmd_idea_relink
# ---------------------------------------------------------

async def _run_cmd_idea_relink_extracts_summary_and_notifies():
    captured = {}
    orig_read, orig_notify = idea_mod.tool_read_note, idea_mod._notify_related_notes
    try:
        idea_mod.tool_read_note = lambda title: (
            "> [!SUMMARY] 要約・核心\n> テスト要約文\n\n## 1. 詳細\n本文…"
        )

        async def _fake_notify(target, idea_filename, query):
            captured["target"] = target
            captured["idea_filename"] = idea_filename
            captured["query"] = query

        idea_mod._notify_related_notes = _fake_notify

        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea_relink(interaction, "既存アイデア")
    finally:
        idea_mod.tool_read_note, idea_mod._notify_related_notes = orig_read, orig_notify

    assert captured.get("idea_filename") == "既存アイデア", captured
    assert captured.get("query") == "テスト要約文", captured


def test_cmd_idea_relink_extracts_summary_and_notifies():
    asyncio.run(_run_cmd_idea_relink_extracts_summary_and_notifies())
    print("✅ [8] cmd_idea_relink: SUMMARYコールアウトを抽出し_notify_related_notesに渡す")


async def _run_cmd_idea_relink_read_failure_skips_notify():
    called = {"n": 0}
    orig_read, orig_notify = idea_mod.tool_read_note, idea_mod._notify_related_notes
    try:
        idea_mod.tool_read_note = lambda title: "Error: note not found."

        async def _fake_notify(*a, **k):
            called["n"] += 1

        idea_mod._notify_related_notes = _fake_notify
        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea_relink(interaction, "no such")
    finally:
        idea_mod.tool_read_note, idea_mod._notify_related_notes = orig_read, orig_notify

    sent = interaction.followup.send.call_args[0][0]
    assert sent.startswith("❌"), sent
    assert called["n"] == 0, "tool_read_note失敗なのに関連ノート検索が呼ばれた"


def test_cmd_idea_relink_read_failure_skips_notify():
    asyncio.run(_run_cmd_idea_relink_read_failure_skips_notify())
    print("✅ [9] cmd_idea_relink: tool_read_note失敗時は❌を返し_notify_related_notesを呼ばない")


# ---------------------------------------------------------
# RelatedNotesView
# ---------------------------------------------------------

async def _run_related_notes_view_defaults_to_all_selected():
    """Select操作前は全候補が選択済み扱い (SelectOptionのdefault=Trueと揃える)。"""
    view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md", "候補2.md"])
    assert view.selected == ["候補1.md", "候補2.md"], view.selected


def test_related_notes_view_defaults_to_all_selected():
    asyncio.run(_run_related_notes_view_defaults_to_all_selected())
    print("✅ [10a] RelatedNotesView: 未操作なら全候補が選択済み扱い")


async def _run_related_notes_select_callback_updates_view_selected():
    view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md", "候補2.md"])
    select = next(c for c in view.children if isinstance(c, idea_mod.RelatedNotesSelect))
    select._values = ["候補1.md"]  # discord.pyのSelect.valuesはinteraction.data由来だがテストでは直接設定
    interaction = _make_fake_interaction(1)
    await select.callback(interaction)
    assert view.selected == ["候補1.md"], view.selected


def test_related_notes_select_callback_updates_view_selected():
    asyncio.run(_run_related_notes_select_callback_updates_view_selected())
    print("✅ [10b] RelatedNotesSelect.callback: 選択結果をview.selectedへ反映する")


async def _run_related_notes_view_confirm_success_icon():
    orig = idea_mod.tool_confirm_related_notes
    try:
        idea_mod.tool_confirm_related_notes = lambda idea_filename, candidates: (
            "Success: Linked 2 related note(s) to [[T]] with tag #idea/T."
        )
        view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md", "候補2.md"])
        interaction = _make_fake_interaction(1)
        await view.confirm.callback(interaction)
    finally:
        idea_mod.tool_confirm_related_notes = orig

    sent = interaction.followup.send.call_args[0][0]
    assert sent.startswith("✅"), sent


def test_related_notes_view_confirm_success_icon():
    asyncio.run(_run_related_notes_view_confirm_success_icon())
    print("✅ [10] RelatedNotesView.confirm: 成功時は✅ (✅ハードコード禁止規約)")


async def _run_related_notes_view_confirm_calls_once_with_selected_subset_and_failure_icon():
    calls = {"n": 0, "args": None}

    def _confirm(idea_filename, candidates):
        calls["n"] += 1
        calls["args"] = (idea_filename, candidates)
        return "Error: Idea note not found."

    orig = idea_mod.tool_confirm_related_notes
    try:
        idea_mod.tool_confirm_related_notes = _confirm
        view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md", "候補2.md"])
        view.selected = ["候補1.md"]  # ユーザーが候補2のチェックを外した状態を模す
        interaction = _make_fake_interaction(1)
        await view.confirm.callback(interaction)
    finally:
        idea_mod.tool_confirm_related_notes = orig

    assert calls["n"] == 1, f"tool_confirm_related_notesが1回だけ呼ばれていない: {calls['n']}"
    assert calls["args"] == ("アイデア.md", ["候補1.md"]), f"外した候補2が含まれてしまった: {calls['args']}"
    sent = interaction.followup.send.call_args[0][0]
    assert sent.startswith("❌"), sent


def test_related_notes_view_confirm_calls_once_with_selected_subset_and_failure_icon():
    asyncio.run(_run_related_notes_view_confirm_calls_once_with_selected_subset_and_failure_icon())
    print("✅ [11] RelatedNotesView.confirm: 選択したサブセットだけでtool_confirm_related_notesを1回呼び失敗時は❌")


async def _run_related_notes_view_confirm_empty_selection_skips():
    called = {"n": 0}

    def _counting(*a, **k):
        called["n"] += 1
        return "Success: x"

    orig = idea_mod.tool_confirm_related_notes
    try:
        idea_mod.tool_confirm_related_notes = _counting
        view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md"])
        view.selected = []  # 全チェックを外した状態
        interaction = _make_fake_interaction(1)
        await view.confirm.callback(interaction)
    finally:
        idea_mod.tool_confirm_related_notes = orig

    assert called["n"] == 0, "選択なしなのにtool_confirm_related_notesが呼ばれた"
    sent = interaction.followup.send.call_args[0][0]
    assert "スキップ" in sent, sent


def test_related_notes_view_confirm_empty_selection_skips():
    asyncio.run(_run_related_notes_view_confirm_empty_selection_skips())
    print("✅ [12] RelatedNotesView.confirm: 選択なしで確定するとtool_confirm_related_notesを呼ばずスキップ")


async def _run_related_notes_view_confirm_double_click_does_not_double_process():
    """WrapUpViewで一度grillが検出した連打バグ (test_idea_brainstorm.py [1]) と同型の回帰。
    busyフラグをdefer()より前に立てないと、2回目のクリックが1回目のdefer中に割り込んで
    tool_confirm_related_notesが二重に走ってしまう。
    """
    calls = {"n": 0}

    def _slow_confirm(idea_filename, candidates):
        calls["n"] += 1
        return "Success: ok"

    async def _slow_defer():
        await asyncio.sleep(0.05)

    orig = idea_mod.tool_confirm_related_notes
    try:
        idea_mod.tool_confirm_related_notes = _slow_confirm
        view = idea_mod.RelatedNotesView("アイデア.md", ["候補1.md"])
        i1, i2 = _make_fake_interaction(1), _make_fake_interaction(1)
        i1.response.defer.side_effect = _slow_defer
        i2.response.defer.side_effect = _slow_defer

        await asyncio.gather(view.confirm.callback(i1), view.confirm.callback(i2))
    finally:
        idea_mod.tool_confirm_related_notes = orig

    assert calls["n"] == 1, f"busyガードが効かず二重にtool_confirm_related_notesが走った (n={calls['n']})"


def test_related_notes_view_confirm_double_click_does_not_double_process():
    asyncio.run(_run_related_notes_view_confirm_double_click_does_not_double_process())
    print("✅ [13] RelatedNotesView.confirm: 連打してもtool_confirm_related_notesは1回だけ")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
