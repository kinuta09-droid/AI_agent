# -*- coding: utf-8 -*-
"""
永続 Discord View (timeout=None + 固定 custom_id + on_ready で bot.add_view 登録)。

スケジューラプロセスは discord.py の gateway 接続を持たず、コンポーネント
(ボタン) の click を受信できない。そのためスケジューラは REST 経由で
custom_id 付きボタンを「表示」するだけにし、実際のコールバックはここに
登録された永続 View (bot プロセス側) が処理する。

コールバックはインスタンス状態を持たず、都度ファイルを読み直す
(ステートレス設計) ため、bot 再起動をまたいでも動作する。
"""
import os
import io
import datetime
import asyncio

import discord

from lib.lib_shared import WORK_DIR
from lib.bot_errors import log_command_error, is_failure_result
from tools.tool_note_promotion import (
    NOTE_PROMO_SLOTS, tool_promote_cluster, tool_reject_cluster,
)
from tools.tool_contradiction_resolve import CONTRADICTION_SLOTS, tool_resolve_contradiction
from tools.tool_orphan_relink import (
    ORPHAN_RELINK_SLOTS, tool_approve_orphan_relink, tool_reject_orphan_relink,
)
from tools.tool_reference_promotion import (
    REFERENCE_PROMOTION_SLOTS, tool_approve_reference_promotion, tool_reject_reference_promotion,
)
from tools.tool_interest_promotion import (
    INTEREST_PROMOTION_SLOTS, tool_approve_interest_promotion, tool_reject_interest_promotion,
)

# ---------------------------------------------------------
# 認可付き基底 View
# ---------------------------------------------------------

# init_views() で注入される許可判定関数。未注入時はフェイルオープン。
_is_allowed_user = None


def init_views(is_allowed_fn):
    """bot_discord.py の on_ready から呼び出し、許可判定関数を注入する。"""
    global _is_allowed_user
    _is_allowed_user = is_allowed_fn


class AllowedUserView(discord.ui.View):
    """ALLOWED_USER_IDS に含まれないユーザーの操作を一括拒否する基底 View。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_allowed_user is None or _is_allowed_user(interaction.user.id):
            return True
        await interaction.response.send_message(
            "⛔ このボタンを操作する権限がありません。", ephemeral=True
        )
        return False


# ---------------------------------------------------------
# 深掘りリサーチボタン (朝ブリーフィング / /briefing 共通)
# ---------------------------------------------------------

_XTREND_JSON = os.path.join(WORK_DIR, "40_Report", "XTrend", "latest_features.json")
DIVE_CUSTOM_IDS = ["brief_dive:1", "brief_dive:2", "brief_dive:3"]

# 多重起動防止 (feature_name をキーにした実行中セット)
_dive_running: set[str] = set()


def _load_top_features(top_n: int = 3, max_age_days: int = 2) -> list[dict]:
    """latest_features.json から鮮度チェック済みの上位 N 件を返す（stale/不在時は空リスト）。"""
    if not os.path.exists(_XTREND_JSON):
        return []
    try:
        import json
        with open(_XTREND_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []

    date_str = data.get("date", "")
    try:
        data_date = datetime.date.fromisoformat(date_str)
        if (datetime.date.today() - data_date).days > max_age_days:
            return []
    except ValueError:
        return []

    return data.get("features", [])[:top_n]


def _dive_result_header(topic: str, result: str, raised: bool = False) -> str:
    """深掘り結果の見出しを決める。

    run_future_search() は例外を投げずに "Error: Tavily検索でソースが取得できません
    でした" 等を返すことがあるため、旧実装のように "✅ 深掘り完了" を無条件で
    前置すると失敗がそのまま成功として表示される (2026-07-26 修正)。

    Args:
        raised: 例外を捕捉した経路かどうか。log_command_error() の戻り値は
            呼び出し側指定の user_prefix ("❌ …") で始まり接頭辞判定に載らないため、
            文字列を見ずに済むよう呼び出し側から明示的に渡す。
    """
    if raised or is_failure_result(result):
        return f"❌ 深掘り失敗: **{topic}**"
    return f"✅ 深掘り完了: **{topic}**"


class BriefingDeepDiveView(AllowedUserView):
    """朝ブリーフィング TOP3 の深掘りリサーチを起動する永続ボタン群。

    Args:
        feature_names: 表示する feature 名のリスト。省略時は3個の汎用ラベルで
            全 custom_id を登録する (on_ready でのグローバル永続登録専用 —
            bot 起動時点のデータ鮮度に関わらず全 custom_id を確実に紐付けるため、
            latest_features.json は読まない)。実際にユーザーへ送信する View
            (/briefing・朝ブリーフィング) は実際の feature 件数に合わせて
            明示的に feature_names を渡すこと。
    """

    def __init__(self, feature_names: list[str] | None = None):
        super().__init__(timeout=None)
        if feature_names is None:
            feature_names = ["深掘り"] * len(DIVE_CUSTOM_IDS)
        for i, custom_id in enumerate(DIVE_CUSTOM_IDS[:len(feature_names)]):
            label = f"🔍 {feature_names[i][:70]}"
            btn = discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary, custom_id=custom_id
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            features = _load_top_features(top_n=3)
            if index >= len(features):
                await interaction.response.send_message(
                    "⚠️ このデータは古くなっているため、深掘りできません。"
                    "最新の /briefing または /xtrend を実行してください。",
                    ephemeral=True,
                )
                return

            topic = features[index].get("feature_name", "").strip()
            # feature_name は短く曖昧な語のことがあるため、tech_stackで検索文脈を補強
            # (2026-08-06 Maple-Preview事故対策。agent_scheduler.py と同型)
            context_hint = features[index].get("tech_stack", "").strip()
            if not topic:
                await interaction.response.send_message("❌ トピック名が空です。", ephemeral=True)
                return

            if topic in _dive_running:
                await interaction.response.send_message(
                    f"⏳ 「{topic}」は既に深掘り実行中です。", ephemeral=True
                )
                return

            await interaction.response.send_message(
                f"🔍 深掘り開始: **{topic}** (数分かかります。完了したらこのチャンネルに投稿します)",
                ephemeral=True,
            )

            _dive_running.add(topic)
            raised = False
            try:
                from tools.tool_future_search import run_future_search
                result = await asyncio.to_thread(
                    run_future_search, topic, "standard", False, context_hint=context_hint
                )
            except Exception as e:
                raised = True
                result = log_command_error(
                    f"深掘りリサーチ ({topic})", e,
                    "❌ 深掘りリサーチでエラーが発生しました",
                )
            finally:
                _dive_running.discard(topic)

            channel = interaction.channel
            if channel is None:
                return
            header = _dive_result_header(topic, result, raised)
            if len(result) > 1900:
                file_obj = discord.File(
                    io.BytesIO(result.encode("utf-8")), filename=f"deepdive_{topic[:30]}.md"
                )
                await channel.send(content=header, file=file_obj)
            else:
                await channel.send(content=f"{header}\n\n{result}")

        return callback


# ---------------------------------------------------------
# タスク失敗時のログ表示ボタン (スケジューラ通知用)
# ---------------------------------------------------------

SCHEDULER_LOG_CUSTOM_ID = "sched_show_log"
_SCHEDULER_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler.log")


class TaskFailureView(AllowedUserView):
    """スケジューラのタスク失敗通知に添付する「ログ表示」永続ボタン。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 ログ表示", style=discord.ButtonStyle.secondary, custom_id=SCHEDULER_LOG_CUSTOM_ID)
    async def show_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not os.path.exists(_SCHEDULER_LOG_PATH):
            await interaction.response.send_message("❌ scheduler.log が見つかりません。", ephemeral=True)
            return
        try:
            # テキストモードの seek は tell() が返した値以外は未定義動作
            # (マルチバイト文字境界で壊れる) なのでバイナリで読んでからデコードする
            with open(_SCHEDULER_LOG_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8000))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            await interaction.response.send_message(
                log_command_error("scheduler.log 読み込み", e, "❌ ログ読み込みエラー"),
                ephemeral=True,
            )
            return

        file_obj = discord.File(io.BytesIO(tail.encode("utf-8")), filename="scheduler_log_tail.txt")
        await interaction.response.send_message(
            "📋 scheduler.log の末尾 (最大8KB):", file=file_obj, ephemeral=True
        )


# ---------------------------------------------------------
# /agent, /chat agent への回答フィードバック (👍/👎)
# ---------------------------------------------------------

_FEEDBACK_VIEW_TIMEOUT = 3600  # 1時間 (OpenInObsidianView と同じ有限期限パターン)


class AgentFeedbackNoteModal(discord.ui.Modal, title="補足があれば教えてください"):
    """理由カテゴリ選択後に表示する、任意の自由記入(補足)モーダル。

    reason (集計対象) は選択式 AgentFeedbackReasonSelectView 側で確定済みで、
    ここではニュアンスの補足のみを受ける (表記揺れが起きても集計に影響しない)。
    """

    note = discord.ui.TextInput(
        label="補足 (任意)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, goal: str, result: str, reason: str, parent_view: "AgentFeedbackView"):
        super().__init__()
        self._goal = goal
        self._result = result
        self._reason = reason
        self._parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        from lib.agent_feedback import save_feedback
        ok = await asyncio.to_thread(
            save_feedback, "bad", self._goal, self._result,
            reason=self._reason, note=self.note.value,
            user_id=interaction.user.id, channel_id=interaction.channel_id,
        )
        msg = ("📝 フィードバックを記録しました。ありがとうございます。" if ok
               else "⚠️ 記録に失敗しました。時間をおいて再度お試しください。")
        await self._parent_view._finish(interaction, msg)


class AgentFeedbackReasonSelectView(discord.ui.View):
    """👎 選択時に表示する、低評価の理由カテゴリを選ばせる中間ステップ。

    理由は固定選択肢 (lib.agent_feedback.BAD_REASON_CHOICES) から選ばせる。
    自由入力だと同じ意図でも表記が揺れ (「情報が古い」「情報が古かった」等)、
    summarize_feedback() の bad理由集計が本来1件のバケツに割れて分散していた
    (表記揺れ問題)。選択後は補足入力用の AgentFeedbackNoteModal を開く。
    Modal は component (Select) を内包できない (discord.py の制約) ため、
    ephemeral メッセージ上の Select → Modal の2段構成にする。
    """

    def __init__(self, goal: str, result: str, parent_view: "AgentFeedbackView"):
        super().__init__(timeout=_FEEDBACK_VIEW_TIMEOUT)
        self._goal = goal
        self._result = result
        self._parent_view = parent_view

        from lib.agent_feedback import BAD_REASON_CHOICES
        select = discord.ui.Select(
            placeholder="どこが期待と違いましたか？",
            options=[discord.SelectOption(label=c) for c in BAD_REASON_CHOICES],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        reason = interaction.data["values"][0]
        await interaction.response.send_modal(
            AgentFeedbackNoteModal(self._goal, self._result, reason, self._parent_view)
        )


class AgentFeedbackView(AllowedUserView):
    """/agent, /chat agent の回答に付ける 👍/👎 フィードバックボタン。

    一度評価したらそのメッセージのボタンは disabled にする (二重計上防止)。
    Bot 再起動をまたぐ永続化はしない — 質問・回答をインスタンス状態としてクロージャに
    持つため、OpenInObsidianView と同じ理由で永続化できず、有限 timeout にする。
    """

    def __init__(self, goal: str, result: str):
        super().__init__(timeout=_FEEDBACK_VIEW_TIMEOUT)
        self._goal = goal
        self._result = result
        self.message: discord.Message | None = None

    async def _finish(self, interaction: discord.Interaction, ephemeral_msg: str):
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await interaction.response.send_message(ephemeral_msg, ephemeral=True)
        except discord.HTTPException as e:
            print(f"[AgentFeedback] Warning: failed to send ack: {e}")
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException as e:
                print(f"[AgentFeedback] Warning: failed to disable buttons: {e}")

    @discord.ui.button(label="👍 良い回答", style=discord.ButtonStyle.success)
    async def good(self, interaction: discord.Interaction, button: discord.ui.Button):
        from lib.agent_feedback import save_feedback
        ok = await asyncio.to_thread(
            save_feedback, "good", self._goal, self._result,
            user_id=interaction.user.id, channel_id=interaction.channel_id,
        )
        msg = ("👍 ありがとうございます。記録しました。" if ok
               else "⚠️ 記録に失敗しました。時間をおいて再度お試しください。")
        await self._finish(interaction, msg)

    @discord.ui.button(label="👎 期待と違う", style=discord.ButtonStyle.danger)
    async def bad(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "どこが期待と違いましたか？ 理由を選んでください:",
            view=AgentFeedbackReasonSelectView(self._goal, self._result, self),
            ephemeral=True,
        )

    @discord.ui.button(label="🕐 最新情報で再検索", style=discord.ButtonStyle.secondary)
    async def recency_retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 👍/👎 の「評価」とは別の操作なので _finish() は呼ばず、このボタンだけ
        # disabled にする (👍/👎 は独立に押せるまま残す)。
        button.disabled = True
        await interaction.response.edit_message(view=self)

        from tools.tool_web import tool_web_search_grounded
        result = await asyncio.to_thread(tool_web_search_grounded, self._goal)

        icon = "❌" if is_failure_result(result) else "🕐"
        # Discordのメッセージ本文は2000字上限。tool_web_search_grounded は
        # Gemini生成の本文+引用元最大5件を返すため容易に超える (超えると
        # followup.send が HTTPException で無言に落ちる)。bot_commands_agent.py:253
        # の result[:1900] フォールバックと同じ切り詰め幅に揃える。
        await interaction.followup.send(f"{icon} 最新情報で再検索した結果:\n\n{result[:1900]}")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------
# X (Twitter) 投稿ボタン (/paper-digest 等が生成した下書きをそのまま投稿)
# ---------------------------------------------------------

_POST_TO_X_TIMEOUT = 3600  # 1時間 (AgentFeedbackView と同じ有限期限パターン)


class PostToXView(AllowedUserView):
    """生成済みのXティザー投稿下書きを、人間の確認後にそのままXへ投稿するボタン。

    tools/tool_twitter_post.py の投稿はPay Per Use課金 (1件$0.015〜) が発生するため、
    下書き生成 (tools/tool_paper_digest.py 等) 側では自動投稿しない設計になっている
    (tool_paper_digest.py モジュールdocstring: 「投稿経路を足すときは、この関数の
    戻り値ではなく別ツールとして分離すること」)。このViewはその橋渡し役に徹し、
    投稿の実処理は tool_post_tweet に一切手を加えず呼ぶだけにする。
    Bot再起動をまたぐ永続化はしない (投稿本文をインスタンス状態としてクロージャに
    持つため、AgentFeedbackViewと同じ理由で永続化できず、有限timeoutにする)。
    """

    def __init__(self, sns_post: str):
        super().__init__(timeout=_POST_TO_X_TIMEOUT)
        self._sns_post = sns_post
        self.message: discord.Message | None = None

    @discord.ui.button(label="🐦 Xに投稿する", style=discord.ButtonStyle.primary)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 二重クリックによる二重課金・二重投稿を防ぐガード。button.disabled の
        # チェックとセットの間に await を挟まない (NotePromotionViewの_promo_running
        # と同じ理由: 後に立てると2回目のクリックが1回目の defer/edit_message 中に
        # 割り込みチェックをすり抜ける、という既知のgrill指摘と同型)。
        if button.disabled:
            await interaction.response.send_message(
                "⏳ この投稿は処理中か、既に投稿済みです。", ephemeral=True
            )
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)

        from tools.tool_twitter_post import tool_post_tweet
        result = await asyncio.to_thread(tool_post_tweet, self._sns_post)

        icon = "❌" if is_failure_result(result) else "✅"
        await interaction.followup.send(f"{icon} {result}", ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------
# ノート昇格パイプライン (承認/却下ボタン)
# ---------------------------------------------------------

# スロット単位の多重実行防止 (_dive_running と同型)。
_promo_running: set[int] = set()


class NotePromotionView(AllowedUserView):
    """ノート昇格提案の承認/却下ボタン (夜間ジョブが REST 送信、実処理はここが担う)。

    agent_scheduler (gateway接続を持たない別プロセス) は tool_note_promotion.
    run_note_promotion_job から custom_id 付きボタンを表示するだけで、実際の
    クリック処理はここに登録された永続 View (bot プロセス側) が行う
    (BriefingDeepDiveView/TaskFailureView と同じアーキテクチャ)。

    永続 View は on_ready で一度だけ登録される単一インスタンスを全メッセージが
    共有するため (RelatedNotesView のような「1メッセージ1インスタンス」にはできない)、
    ボタンを押下後に disabled 化する処理はしない — 二重クリックは
    tool_promote_cluster/tool_reject_cluster 側の pending.pop() が既に処理済みの
    slot を "Error: ..." で弾く冪等性で吸収する。
    """

    def __init__(self):
        super().__init__(timeout=None)
        for slot in range(NOTE_PROMO_SLOTS):
            approve = discord.ui.Button(
                label="✅ 承認して恒久化", style=discord.ButtonStyle.success,
                custom_id=f"note_promo_approve:{slot}",
            )
            approve.callback = self._make_callback(slot, tool_promote_cluster, "承認")
            self.add_item(approve)

            reject = discord.ui.Button(
                label="❌ 却下", style=discord.ButtonStyle.danger,
                custom_id=f"note_promo_reject:{slot}",
            )
            reject.callback = self._make_callback(slot, tool_reject_cluster, "却下")
            self.add_item(reject)

    def _make_callback(self, slot: int, action_fn, action_label: str):
        async def callback(interaction: discord.Interaction):
            if slot in _promo_running:
                await interaction.response.send_message(
                    "⏳ この提案は処理中です。", ephemeral=True
                )
                return
            # busy相当のガードは defer() より前に立てる (grill指摘と同じ理由:
            # 後に立てると2回目のクリックが1回目の defer 中に割り込みチェックをすり抜ける)。
            _promo_running.add(slot)
            await interaction.response.defer()
            try:
                result = await asyncio.to_thread(action_fn, slot)
            except Exception as e:
                result = log_command_error(f"ノート昇格{action_label} (slot={slot})", e)
            finally:
                _promo_running.discard(slot)

            icon = "❌" if is_failure_result(result) else "✅"
            if interaction.channel is not None:
                await interaction.channel.send(f"{icon} {result}")

        return callback


# ---------------------------------------------------------
# 矛盾の能動解決 (裁定ボタン)
# ---------------------------------------------------------

_contra_running: set[int] = set()

_CONTRA_VERDICT_STYLE = {
    "a": discord.ButtonStyle.primary,
    "b": discord.ButtonStyle.primary,
    "both": discord.ButtonStyle.secondary,
}
_CONTRA_VERDICT_LABEL = {
    "a": "🅰️ Aを採る",
    "b": "🅱️ Bを採る",
    "both": "🤝 両論併記",
}


class ContradictionResolutionView(AllowedUserView):
    """矛盾裁定の3択ボタン (Vault Insight Watcherが検知直後にREST送信、実処理は
    ここが担う)。アーキテクチャは NotePromotionView と同一 (上記参照):
    スケジューラ/watcherはボタンを「表示」するだけで、実際のクリック処理は
    bot プロセス側のこの永続 View が行う。ボタンの見た目 (ラベル/スタイル) は
    表示時に REST 送信された components が実際に使われる (ここでの定義は
    永続 View 登録の型として必要なだけで、表示には影響しない)。
    """

    def __init__(self):
        super().__init__(timeout=None)
        for slot in range(CONTRADICTION_SLOTS):
            for verdict in ("a", "b", "both"):
                button = discord.ui.Button(
                    label=_CONTRA_VERDICT_LABEL[verdict],
                    style=_CONTRA_VERDICT_STYLE[verdict],
                    custom_id=f"contra_resolve:{slot}:{verdict}",
                )
                button.callback = self._make_callback(slot, verdict)
                self.add_item(button)

    def _make_callback(self, slot: int, verdict: str):
        async def callback(interaction: discord.Interaction):
            if slot in _contra_running:
                await interaction.response.send_message(
                    "⏳ この矛盾は処理中です。", ephemeral=True
                )
                return
            _contra_running.add(slot)
            await interaction.response.defer()
            try:
                result = await asyncio.to_thread(tool_resolve_contradiction, slot, verdict)
            except Exception as e:
                result = log_command_error(f"矛盾裁定 (slot={slot}, verdict={verdict})", e)
            finally:
                _contra_running.discard(slot)

            icon = "❌" if is_failure_result(result) else "✅"
            if interaction.channel is not None:
                await interaction.channel.send(f"{icon} {result}")

        return callback


# ---------------------------------------------------------
# 孤児ノート(被リンク0件)のリンク提案 (承認/却下ボタン)
# ---------------------------------------------------------

_orphan_relink_running: set[int] = set()


class OrphanRelinkView(AllowedUserView):
    """孤児ノートリンク提案の承認/却下ボタン (夜間ジョブが REST 送信、実処理はここが担う)。
    アーキテクチャは NotePromotionView と同一 (上記参照)。
    """

    def __init__(self):
        super().__init__(timeout=None)
        for slot in range(ORPHAN_RELINK_SLOTS):
            approve = discord.ui.Button(
                label="✅ 全部リンクする", style=discord.ButtonStyle.success,
                custom_id=f"orphan_relink_approve:{slot}",
            )
            approve.callback = self._make_callback(slot, tool_approve_orphan_relink, "承認")
            self.add_item(approve)

            reject = discord.ui.Button(
                label="❌ 却下", style=discord.ButtonStyle.danger,
                custom_id=f"orphan_relink_reject:{slot}",
            )
            reject.callback = self._make_callback(slot, tool_reject_orphan_relink, "却下")
            self.add_item(reject)

    def _make_callback(self, slot: int, action_fn, action_label: str):
        async def callback(interaction: discord.Interaction):
            if slot in _orphan_relink_running:
                await interaction.response.send_message(
                    "⏳ この提案は処理中です。", ephemeral=True
                )
                return
            _orphan_relink_running.add(slot)
            await interaction.response.defer()
            try:
                result = await asyncio.to_thread(action_fn, slot)
            except Exception as e:
                result = log_command_error(f"孤児ノートリンク{action_label} (slot={slot})", e)
            finally:
                _orphan_relink_running.discard(slot)

            icon = "❌" if is_failure_result(result) else "✅"
            if interaction.channel is not None:
                await interaction.channel.send(f"{icon} {result}")

        return callback


# ---------------------------------------------------------
# よく参照されるノートのMOC昇格提案 (承認/却下ボタン)
# ---------------------------------------------------------

_reference_promotion_running: set[int] = set()


class ReferencePromotionView(AllowedUserView):
    """MOC昇格提案の承認/却下ボタン (夜間ジョブが REST 送信、実処理はここが担う)。
    アーキテクチャは OrphanRelinkView と同一 (上記参照)。
    """

    def __init__(self):
        super().__init__(timeout=None)
        for slot in range(REFERENCE_PROMOTION_SLOTS):
            approve = discord.ui.Button(
                label="✅ MOCを新設", style=discord.ButtonStyle.success,
                custom_id=f"ref_promo_approve:{slot}",
            )
            approve.callback = self._make_callback(slot, tool_approve_reference_promotion, "承認")
            self.add_item(approve)

            reject = discord.ui.Button(
                label="❌ 却下", style=discord.ButtonStyle.danger,
                custom_id=f"ref_promo_reject:{slot}",
            )
            reject.callback = self._make_callback(slot, tool_reject_reference_promotion, "却下")
            self.add_item(reject)

    def _make_callback(self, slot: int, action_fn, action_label: str):
        async def callback(interaction: discord.Interaction):
            if slot in _reference_promotion_running:
                await interaction.response.send_message(
                    "⏳ この提案は処理中です。", ephemeral=True
                )
                return
            _reference_promotion_running.add(slot)
            await interaction.response.defer()
            try:
                result = await asyncio.to_thread(action_fn, slot)
            except Exception as e:
                result = log_command_error(f"MOC昇格{action_label} (slot={slot})", e)
            finally:
                _reference_promotion_running.discard(slot)

            icon = "❌" if is_failure_result(result) else "✅"
            if interaction.channel is not None:
                await interaction.channel.send(f"{icon} {result}")

        return callback


# ---------------------------------------------------------
# 興味キーワード新規候補の承認/却下ボタン
# ---------------------------------------------------------

_interest_promotion_running: set[int] = set()


class InterestPromotionView(AllowedUserView):
    """興味キーワード新規候補の承認/却下ボタン (夜間ジョブが REST 送信、実処理はここが担う)。
    アーキテクチャは ReferencePromotionView と同一 (上記参照)。
    """

    def __init__(self):
        super().__init__(timeout=None)
        for slot in range(INTEREST_PROMOTION_SLOTS):
            approve = discord.ui.Button(
                label="✅ 追加", style=discord.ButtonStyle.success,
                custom_id=f"interest_promo_approve:{slot}",
            )
            approve.callback = self._make_callback(slot, tool_approve_interest_promotion, "承認")
            self.add_item(approve)

            reject = discord.ui.Button(
                label="❌ 却下", style=discord.ButtonStyle.danger,
                custom_id=f"interest_promo_reject:{slot}",
            )
            reject.callback = self._make_callback(slot, tool_reject_interest_promotion, "却下")
            self.add_item(reject)

    def _make_callback(self, slot: int, action_fn, action_label: str):
        async def callback(interaction: discord.Interaction):
            if slot in _interest_promotion_running:
                await interaction.response.send_message(
                    "⏳ この提案は処理中です。", ephemeral=True
                )
                return
            _interest_promotion_running.add(slot)
            await interaction.response.defer()
            try:
                result = await asyncio.to_thread(action_fn, slot)
            except Exception as e:
                result = log_command_error(f"興味キーワード{action_label} (slot={slot})", e)
            finally:
                _interest_promotion_running.discard(slot)

            icon = "❌" if is_failure_result(result) else "✅"
            if interaction.channel is not None:
                await interaction.channel.send(f"{icon} {result}")

        return callback
