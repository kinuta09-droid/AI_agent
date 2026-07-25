# -*- coding: utf-8 -*-
"""CurateView / SetupView のエンゲージメント計測フックの回帰テスト

Discord へは接続せず、偽 Interaction でコールバックを直接叩く。
外部API (Tavily/Gemini/Obsidian書き込み) は全てスタブに差し替える。

このテストが存在する理由:
  2026-02-28 のリファクタで /curate が NameError のまま5ヶ月放置された。
  「起動テストをしていれば気付けた」ので、UI 経路をコードで踏むテストを置く。

実行: py tests/test_curate_hooks.py
"""
import os
import sys
import asyncio
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.engagement_log as el
import commands.bot_commands_curator as bcc

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# 偽 Interaction / 外部依存スタブ
# ---------------------------------------------------------

class _FakeResponse:
    def __init__(self, rec):
        self._rec = rec

    async def defer(self, *a, **kw):
        self._rec.append(("defer", None))

    async def send_message(self, content=None, **kw):
        self._rec.append(("send_message", content))

    async def edit_message(self, content=None, **kw):
        self._rec.append(("edit_message", content))


class _FakeFollowup:
    def __init__(self, rec):
        self._rec = rec

    async def send(self, content=None, embed=None, view=None, **kw):
        self._rec.append(("followup", content or (embed.title if embed else None)))


class FakeInteraction:
    def __init__(self, user_id=1, data=None):
        self.sent = []
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse(self.sent)
        self.followup = _FakeFollowup(self.sent)
        self.data = data or {}

    async def edit_original_response(self, **kw):
        self.sent.append(("edit_original", None))


SUGGESTIONS = {
    "topics": [
        {"title": "トピックA", "branch": "consistent", "description": "d1", "source_note": "N1"},
        {"title": "トピックB", "branch": "bridge", "description": "d2"},
        {"title": "トピックC", "branch": "entropy", "description": "d3", "reasoning": "r"},
    ],
    "ab_question": {"question": "Q?", "option_a": "選択肢A", "option_b": "選択肢B",
                    "type": "normal"},
}


def _install_stubs(log_path: str):
    """外部APIを潰し、計測だけ一時ログへ向ける。"""
    orig = {
        "log_event": el.log_event,
        "log_impressions": el.log_impressions,
    }
    bcc.log_event = lambda *a, **kw: orig["log_event"](*a, log_path=log_path, **kw)
    bcc.log_impressions = lambda items, source, meta=None: orig["log_impressions"](
        items, source, meta=meta, log_path=log_path)

    bcc.search_and_save_topic = lambda *a, **kw: "Success: Created [[stub.md]]"
    bcc.update_user_profile = lambda *a, **kw: "Success"
    bcc.read_main_context = lambda *a, **kw: "ctx"
    bcc.fetch_external_trends = lambda *a, **kw: ""
    bcc.get_curation_suggestions = lambda *a, **kw: SUGGESTIONS


def _events(log_path, event=None):
    evs = el.read_events(log_path=log_path)
    return [e for e in evs if event is None or e["event"] == event]


# ---------------------------------------------------------
# 1. 提示 → クリック
# ---------------------------------------------------------

def test_impression_and_click():
    print("\n[1] CurateView: 提示 → クリック")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        _install_stubs(log_path)

        async def run():
            view = bcc.CurateView(SUGGESTIONS, user_id=1, context="ctx")
            interaction = FakeInteraction(user_id=1)
            await view._make_topic_callback(2)(interaction)  # 3つ目 (entropy) を選択
            return view, interaction

        view, interaction = asyncio.run(run())

        imps = _events(log_path, "impression")
        clicks = _events(log_path, "click")
        check("提示3件が impression になる", len(imps) == 3, str(len(imps)))
        check("クリックが1件記録される", len(clicks) == 1, str(len(clicks)))
        check("click と impression が同一 group_id",
              clicks and clicks[0]["group_id"] == view.group_id == imps[0]["group_id"],
              f"{clicks[0]['group_id'] if clicks else None} / {view.group_id}")
        check("選ばれたのは entropy の トピックC",
              clicks[0]["item"] == "トピックC" and clicks[0]["meta"]["branch"] == "entropy",
              str(clicks[0]))
        check("未選択2件が負例として残る",
              {e["item"] for e in imps} - {"トピックC"} == {"トピックA", "トピックB"})
        check("調査結果がユーザーに返る",
              any(k == "followup" for k, _ in interaction.sent), str(interaction.sent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 2. 再提案 (拒否)
# ---------------------------------------------------------

def test_reject():
    print("\n[2] CurateView: 再提案で reject が残る")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        _install_stubs(log_path)

        async def run():
            view = bcc.CurateView(SUGGESTIONS, user_id=1, context="ctx")
            interaction = FakeInteraction(user_id=1)
            await view._refinement_callback(interaction)
            return view

        view = asyncio.run(run())

        rejects = _events(log_path, "reject")
        imps = _events(log_path, "impression")
        check("拒否3件が記録される", len(rejects) == 3, str(len(rejects)))
        check("reject は提示と同じ group_id",
              all(r["group_id"] == view.group_id for r in rejects),
              str([r["group_id"] for r in rejects]))
        check("branch が残る",
              {r["meta"]["branch"] for r in rejects} == {"consistent", "bridge", "entropy"},
              str([r["meta"] for r in rejects]))
        check("再提案後の新 View も impression を出す (3+3)",
              len(imps) == 6, str(len(imps)))
        check("新 View は別の group_id",
              len({e["group_id"] for e in imps}) == 2,
              str({e["group_id"] for e in imps}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 3. A/B 回答
# ---------------------------------------------------------

def test_ab_answer():
    print("\n[3] CurateView: A/B 回答")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        _install_stubs(log_path)

        async def run():
            view = bcc.CurateView(SUGGESTIONS, user_id=1, context="ctx")
            await view._make_ab_callback("option_b")(FakeInteraction(user_id=1))
            return view

        view = asyncio.run(run())
        ans = _events(log_path, "ab_answer")
        check("A/B 回答が1件記録される", len(ans) == 1, str(len(ans)))
        check("選んだ側の文言が item になる", ans[0]["item"] == "選択肢B", str(ans[0]))
        check("質問文が meta に残る", ans[0]["meta"]["question"] == "Q?", str(ans[0]["meta"]))
        check("同一 group_id で提示と紐づく", ans[0]["group_id"] == view.group_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 4. 他人のボタン操作は記録しない
# ---------------------------------------------------------

def test_other_user_not_logged():
    print("\n[4] 他ユーザーの操作は計測しない")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        _install_stubs(log_path)

        async def run():
            view = bcc.CurateView(SUGGESTIONS, user_id=1, context="ctx")
            other = FakeInteraction(user_id=999)  # 別人
            await view._make_topic_callback(0)(other)
            await view._refinement_callback(other)
            await view._make_ab_callback("option_a")(other)
            return other

        other = asyncio.run(run())
        check("click は記録されない", len(_events(log_path, "click")) == 0)
        check("reject は記録されない", len(_events(log_path, "reject")) == 0)
        check("ab_answer は記録されない", len(_events(log_path, "ab_answer")) == 0)
        check("拒否メッセージが返る",
              any("操作できません" in str(c) for _, c in other.sent), str(other.sent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 5. SetupView (2026-07-25 に復元したコマンド)
# ---------------------------------------------------------

def test_setup_view():
    print("\n[5] SetupView: 提示 → 複数選択")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        _install_stubs(log_path)
        suggestions = {"topics": [
            {"title": f"トピック{i}", "description": f"d{i}", "branch": "deepen"}
            for i in range(12)
        ]}

        async def run():
            view = bcc.SetupView(suggestions, user_id=1)
            interaction = FakeInteraction(user_id=1, data={"values": ["0", "3"]})
            await view._select_callback(interaction)
            return view

        view = asyncio.run(run())
        imps = _events(log_path, "impression")
        clicks = _events(log_path, "click")
        check("提示12件が impression になる", len(imps) == 12, str(len(imps)))
        check("選択2件が click になる", len(clicks) == 2, str(len(clicks)))
        check("選んだのは 0番目と3番目",
              {c["item"] for c in clicks} == {"トピック0", "トピック3"},
              str([c["item"] for c in clicks]))
        check("click は提示と同じ group_id",
              all(c["group_id"] == view.group_id for c in clicks))
        check("source は setup", all(e["source"] == "setup" for e in imps + clicks))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 6. /curate /setup /topic のコマンド登録 (登録漏れ再発防止)
# ---------------------------------------------------------

def test_commands_registered():
    print("\n[6] スラッシュコマンドの登録")
    import bot_discord

    names = {c.name for c in bot_discord.tree.get_commands()}
    # 2026-02-28 のリファクタで消え、2026-07-25 に復元した7件
    restored = {"curate", "setup", "topic", "calendar", "add-event", "tasks", "remind"}
    missing = restored - names
    check("復元した7コマンドが登録されている", not missing, f"missing: {missing}")
    check("ハンドラが commands/ 側に存在する",
          callable(bcc.cmd_setup) and callable(bcc.cmd_topic))


if __name__ == "__main__":
    print("=== test_curate_hooks ===")
    test_impression_and_click()
    test_reject()
    test_ab_answer()
    test_other_user_not_logged()
    test_setup_view()
    test_commands_registered()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
