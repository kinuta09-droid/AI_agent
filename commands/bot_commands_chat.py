# -*- coding: utf-8 -*-
"""チャット/ボイスサブシステム (旧 bot_discord.py 本体から分離)。"""

import os
import re
import time
import asyncio
import datetime
import tempfile
import threading
import wave

import discord

from engine_plan_execute import run_plan_and_execute
from lib.bot_errors import log_command_error
from lib.lib_shared import client as gemini_client, MODEL_NAME
from lib.discord_response import (
    send_long_message as _send_long_message,
    make_discord_file as _make_discord_file,
    create_obsidian_view as _create_obsidian_view,
)
from tools.tool_calendar import tool_get_tasks, tool_get_calendar
from tools.tool_web import tool_fetch_daily_news
from tools.tool_crypto import tool_analyze_crypto, tool_scan_crypto_market
from tools.tool_curator import (
    read_main_context,
    get_curation_suggestions,
    fetch_external_trends,
)

try:
    import discord.ext.voice_recv as voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False

try:
    from tools.tool_bitnet import quick_filter_discord, gemini_fallback_response
    BITNET_AVAILABLE = True
except ImportError:
    BITNET_AVAILABLE = False

# 長期記憶 (optional — import失敗でチャットを壊さない)
try:
    from tools.tool_user_memory import get_memory_block as _get_memory_block
except ImportError:
    def _get_memory_block(*args, **kwargs):
        return ""

from commands import bot_commands_curator
from commands import bot_commands_agent

# bot_discord から注入されるヘルパー (Discord 応答系は lib.discord_response を直接 import 済み。
# _openai_client は bot_discord.py 側で閉じた state のため引き続き注入で受け取る)
_log_to_daily_note = None
_openai_client = None


def init_helpers(log_func, openai_client):
    global _log_to_daily_note, _openai_client
    _log_to_daily_note = log_func
    _openai_client = openai_client


# ---------------------------------------------------------
# Chat Session State
# ---------------------------------------------------------

# ユーザーID -> セッション情報 のマップ
_chat_sessions: dict[int, dict] = {}


def _gemini_chat_with_history(history: list) -> str:
    """会話履歴を含めて Gemini に送信する（直近10件 + 長期記憶）。"""
    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
    system_msg = f"今日は{today_str}です。日本語で簡潔に応答してください。"
    memory_block = _get_memory_block()
    if memory_block:
        system_msg = f"{system_msg}\n\n{memory_block}"

    contents = []
    for msg in history[-10:]:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME, contents=contents,
            config={"system_instruction": system_msg},
        )
        return (response.text or "").strip() or "(応答なし)"
    except Exception as e:
        # GeminiBudgetExceeded (日次上限) もここに落ちる: BitNetローカル
        # フォールバックが発動するのが意図した縮退モード (変更不要)。
        print(f"[Discord] Gemini API error: {e}")
        if BITNET_AVAILABLE:
            last_msg = history[-1]["content"] if history else ""
            return gemini_fallback_response(last_msg)
        return f"Gemini API Error: {e}"


# ---------------------------------------------------------
# Voice Session (STT via Whisper)
# ---------------------------------------------------------

class VoiceSession:
    """ボイスチャンネルの音声を収集し、Whisper STT → Gemini に送信する。"""

    SILENCE_SECONDS = 2.0   # 無音判定しきい値（秒）— 文末の間を待つ
    MIN_AUDIO_BYTES = 96000  # 最低音声長（~0.5秒 @ 48kHz stereo 16bit）
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2  # 16-bit

    def __init__(self, text_channel, user_id, session):
        self.text_channel = text_channel
        self.user_id = user_id
        self.session = session
        self._buffer = bytearray()
        self._buf_lock = threading.Lock()
        self._last_audio_time = 0.0
        self._checker_task = None
        self._processing = False

    def on_audio(self, user, data):
        """BasicSink callback — 音声受信スレッドから呼ばれる。(user, VoiceData)"""
        if user is None or user.id != self.user_id:
            return
        with self._buf_lock:
            self._buffer.extend(data.pcm)
            buf_len = len(self._buffer)
        self._last_audio_time = time.monotonic()
        # 初回音声受信時にログ出力
        if buf_len == len(data.pcm):
            print(f"[Voice] Audio receiving started from user {user.id}")

    async def start(self):
        """無音チェッカーを開始する。"""
        self._checker_task = asyncio.create_task(self._silence_checker())

    async def stop(self):
        """無音チェッカーを停止しバッファをクリアする。"""
        if self._checker_task:
            self._checker_task.cancel()
            try:
                await self._checker_task
            except asyncio.CancelledError:
                pass
        with self._buf_lock:
            self._buffer.clear()

    async def _silence_checker(self):
        """定期的にバッファを監視し、無音になったら STT を実行する。"""
        while True:
            await asyncio.sleep(0.5)
            if self._processing:
                continue
            if self._last_audio_time == 0.0:
                continue
            elapsed = time.monotonic() - self._last_audio_time
            with self._buf_lock:
                buf_size = len(self._buffer)
            if elapsed >= self.SILENCE_SECONDS and buf_size > self.MIN_AUDIO_BYTES:
                self._processing = True
                print(f"[Voice] Silence detected, processing {buf_size} bytes of audio")
                with self._buf_lock:
                    audio_data = bytes(self._buffer)
                    self._buffer.clear()
                self._last_audio_time = 0.0
                try:
                    text = await asyncio.to_thread(self._transcribe, audio_data)
                    if text and text.strip():
                        await self._handle_transcription(text.strip())
                except Exception as e:
                    print(f"[Voice] STT error: {e}")
                finally:
                    self._processing = False
            elif elapsed >= self.SILENCE_SECONDS and 0 < buf_size <= self.MIN_AUDIO_BYTES:
                # 短すぎる音声はノイズと判断して破棄
                with self._buf_lock:
                    self._buffer.clear()
                self._last_audio_time = 0.0

    @staticmethod
    def _to_mono_16k(pcm_data: bytes) -> bytes:
        """ステレオ 48kHz PCM → モノラル 16kHz PCM に変換する。"""
        import struct
        # ステレオ 16bit → モノラル (L+R)/2
        samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
        mono = []
        for i in range(0, len(samples), 2):
            if i + 1 < len(samples):
                mono.append((samples[i] + samples[i + 1]) // 2)
            else:
                mono.append(samples[i])
        # 48kHz → 16kHz (1/3 ダウンサンプル)
        downsampled = mono[::3]
        return struct.pack(f"<{len(downsampled)}h", *downsampled)

    def _transcribe(self, pcm_data: bytes) -> str:
        """PCM → モノラル16kHz WAV → Whisper API で文字起こし。"""
        tmp_path = None
        try:
            # ステレオ 48kHz → モノラル 16kHz
            mono_data = VoiceSession._to_mono_16k(pcm_data)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)       # モノラル
                wf.setsampwidth(2)       # 16bit
                wf.setframerate(16000)   # 16kHz
                wf.writeframes(mono_data)

            # 直近の会話履歴をプロンプトに含めて文脈認識を向上
            context_hints = "AIエージェント、ニュース、仮想通貨、ビットコイン、カレンダー、タスク、予定、教えて、お願いします。"
            recent = self.session.get("history", [])[-3:]
            if recent:
                last_texts = "。".join(m["content"][:50] for m in recent)
                context_hints = f"{last_texts}。{context_hints}"

            with open(tmp_path, "rb") as audio_file:
                result = _openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ja",
                    temperature=0.0,
                    prompt=context_hints[:200],
                )
            return result.text
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def _handle_transcription(self, text: str):
        """認識テキストを Gemini に送信し、テキストチャンネルに応答する。"""
        await self.text_channel.send(f"🎤 **認識**: {text}")

        self.session["history"].append({"role": "user", "content": text})

        async with self.text_channel.typing():
            result = await asyncio.to_thread(
                _gemini_chat_with_history, self.session["history"]
            )

        self.session["history"].append({"role": "model", "content": result})

        await asyncio.to_thread(_log_to_daily_note, "chat", f"[voice] {text}", result)

        if len(result) <= 2000:
            await self.text_channel.send(result)
        else:
            await self.text_channel.send(result[:1900] + "\n\n*... (truncated)*")


# ---------------------------------------------------------
# Chat Intent Parser
# ---------------------------------------------------------

INTENT_PATTERNS = {
    "curate": r"^(curate|キュレート|トピック)\s*(.*)",
    "tasks": r"^(tasks?|タスク|todo)",
    "calendar": r"^(calendar|カレンダー|予定|schedule)\s*(.*)",
    "news": r"^(news|ニュース)",
    "crypto": r"^(crypto|btc|仮想通貨)\s*(.*)",
    "scan": r"^(scan|スキャン|市場)",
    "agent": r"^(agent|エージェント|plan)\s*(.*)",
}


def _parse_chat_intent(message: str) -> tuple:
    """メッセージからインテント（ツール名）とパラメータを抽出する。"""
    message_stripped = message.strip()
    for intent, pattern in INTENT_PATTERNS.items():
        match = re.match(pattern, message_stripped, re.IGNORECASE)
        if match:
            query = match.group(2).strip() if match.lastindex and match.lastindex >= 2 else ""
            return intent, {"query": query}
    return "chat", {}


async def _dispatch_curate(interaction: discord.Interaction, query: str):
    """Curate workflow via /chat command (と /curate から共用)。"""
    try:
        context = query if query else await asyncio.to_thread(read_main_context)
        trends = await asyncio.to_thread(fetch_external_trends)
        suggestions = await asyncio.to_thread(
            get_curation_suggestions, context, "normal",
            None, 0, trends,
        )
        topics = suggestions.get("topics", [])
        if not topics:
            await interaction.followup.send("トピック提案の生成に失敗しました。")
            return
        # commands/ 分割時にこの2つは bot_commands_curator へ移動したが、
        # ここの参照が残されて NameError になっていた (2026-07-25 修正)。
        embed = bot_commands_curator._build_curate_embed(suggestions)
        view = bot_commands_curator.CurateView(
            suggestions, interaction.user.id, context=context
        )
        await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(log_command_error("/chat curate", e))


async def _dispatch_agent(interaction: discord.Interaction, goal: str):
    """Plan-and-Execute workflow via /chat command."""
    if not goal:
        await interaction.followup.send("エージェントへの指示を入力してください。例: `/chat agent AIの最新トレンドを調べて`")
        return

    loop = asyncio.get_running_loop()

    async def discord_pe_callback(step_data):
        # 描画は /agent (commands/bot_commands_agent.py) と共用する。
        # 同じエンジンの同じイベントを2箇所で描いていたため、
        # fan-out のウェーブ表示を片方だけ足すと必ず食い違う。
        content = bot_commands_agent.render_progress(step_data)
        if not content:
            return
        try:
            await interaction.edit_original_response(content=content)
        except Exception as e:
            print(f"[Discord] Warning: Failed to update status: {e}")

    def sync_callback_wrapper(step_data):
        asyncio.run_coroutine_threadsafe(discord_pe_callback(step_data), loop)

    try:
        result = await asyncio.to_thread(run_plan_and_execute, goal, step_callback=sync_callback_wrapper)
    except Exception as e:
        result = log_command_error(f"/chat agent {goal}", e)

    await asyncio.to_thread(_log_to_daily_note, "command", f"/chat agent {goal}", result)

    # /agent (commands/bot_commands_agent.py) と同じエンジン・同じ戻り値契約なので
    # 見出しの出し分けも同じ関数に寄せる。入口ごとに書くと片方だけ ✅ のまま残る。
    header = bot_commands_agent.mission_header(result)

    try:
        await _send_long_message(interaction, f"{header}\n\n{result}", title="Agent Result")
    except discord.NotFound:
        try:
            if len(result) > 1900:
                file = _make_discord_file(result, "Agent_Result.md")
                await interaction.channel.send(
                    content=f"{interaction.user.mention} {header}", file=file
                )
            else:
                await interaction.channel.send(f"{interaction.user.mention} {header}\n\n{result}")
        except Exception as e:
            print(f"[Discord] Failed to send fallback: {e}")

    await bot_commands_agent.send_feedback_prompt(interaction.channel, goal, result)


# ---------------------------------------------------------
# /chat コマンド本体 (@tree.command デコレータは bot_discord.py 側の薄い
# ラッパーに付く。commands/bot_commands_agent.py の cmd_agent と同じ形)
# ---------------------------------------------------------

async def cmd_chat(interaction: discord.Interaction, message: str = "on"):
    user_id = interaction.user.id

    # --- Session Control ---
    msg_lower = message.strip().lower()

    if msg_lower == "off":
        if user_id in _chat_sessions:
            session = _chat_sessions[user_id]
            turns = len(session["history"]) // 2
            # ボイス切断
            voice_ses = session.get("voice_session")
            if voice_ses:
                await voice_ses.stop()
            vc = session.get("voice_client")
            if vc and vc.is_connected():
                await vc.disconnect()
            del _chat_sessions[user_id]
            voice_tag = " + 🎙️ ボイス切断" if vc else ""
            await interaction.response.send_message(
                f"👋 チャットセッションを終了しました（{turns}ターン{voice_tag}）"
            )
        else:
            await interaction.response.send_message("⚠️ アクティブなセッションがありません")
        return

    if msg_lower == "on":
        _chat_sessions[user_id] = {
            "channel_id": interaction.channel_id,
            "history": [],
            "started_at": datetime.datetime.now(),
        }
        await interaction.response.send_message(
            "💬 **チャットセッションを開始しました**\n"
            "このチャンネルでメッセージを送ると AI が応答します。\n"
            "終了するには `/chat off` と入力してください。\n"
            "ツール呼び出し (`tasks`, `calendar`, `news` 等) もそのまま使えます。"
        )
        return

    if msg_lower == "voice":
        # --- Voice Session ---
        if not VOICE_RECV_AVAILABLE:
            await interaction.response.send_message(
                "⚠️ `discord-ext-voice-recv` が未インストールです。"
            )
            return
        if _openai_client is None:
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` が `.env` に設定されていません（Whisper STT に必要）。"
            )
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ まずボイスチャンネルに参加してください。"
            )
            return

        vc_channel = interaction.user.voice.channel

        # 既にボイスセッション中なら拒否
        if user_id in _chat_sessions and _chat_sessions[user_id].get("voice_client"):
            await interaction.response.send_message("⚠️ 既にボイスセッション中です。")
            return

        await interaction.response.defer()

        try:
            vc = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            await interaction.followup.send(
                log_command_error("ボイスチャンネル接続", e, "❌ ボイスチャンネル接続エラー")
            )
            return

        # セッション作成 (既存テキストセッションがあれば拡張、なければ新規)
        if user_id not in _chat_sessions:
            _chat_sessions[user_id] = {
                "channel_id": interaction.channel_id,
                "history": [],
                "started_at": datetime.datetime.now(),
            }
        session = _chat_sessions[user_id]
        session["voice_client"] = vc
        session["voice_enabled"] = True

        # VoiceSession を作成して音声受信開始
        vs = VoiceSession(interaction.channel, user_id, session)
        session["voice_session"] = vs
        await vs.start()

        sink = voice_recv.BasicSink(vs.on_audio)
        vc.listen(sink)

        await interaction.followup.send(
            f"🎙️ **ボイスチャンネル「{vc_channel.name}」に接続しました**\n"
            "音声認識が有効です。話しかけると AI が応答します。\n"
            "終了するには `/chat off` と入力してください。"
        )
        return

    # --- Single message mode (with intent dispatch) ---
    is_deferred = False
    try:
        await interaction.response.defer()
        is_deferred = True
    except discord.NotFound:
        print("[Chat] Warning: Interaction timed out before defer. Using channel send for fallback.")
    except Exception as e:
        print(f"[Chat] Unexpected error during defer: {e}")

    # 1. Intent 解析
    intent, params = _parse_chat_intent(message)

    # 2. Tool Dispatch
    if intent == "curate":
        if is_deferred:
            await _dispatch_curate(interaction, params.get("query", ""))
        else:
            try:
                await interaction.channel.send(f"{interaction.user.mention} ⚠️ インタラクションタイムアウトのため、キュレートを開始できませんでした。")
            except Exception:
                pass
        await asyncio.to_thread(_log_to_daily_note, "chat", message, "[curate] Suggestions presented")
        return
    elif intent == "tasks":
        result = await asyncio.to_thread(tool_get_tasks, "today")
    elif intent == "calendar":
        date_str = params.get("query", "") or "today"
        result = await asyncio.to_thread(tool_get_calendar, date_str)
    elif intent == "news":
        result = await asyncio.to_thread(tool_fetch_daily_news)
    elif intent == "crypto":
        symbol = params.get("query", "").upper() or "BTC/USDT"
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        result = await asyncio.to_thread(tool_analyze_crypto, symbol)
    elif intent == "scan":
        result = await asyncio.to_thread(tool_scan_crypto_market)
    elif intent == "agent":
        if is_deferred:
            await _dispatch_agent(interaction, params.get("query", ""))
        else:
            try:
                await interaction.channel.send(f"{interaction.user.mention} ⚠️ インタラクションタイムアウトのため、エージェントを起動できませんでした。")
            except Exception:
                pass
        return
    else:
        # Default: Gemini chat
        today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        sys_msg = f"今日は{today_str}です。日本語で簡潔に応答してください。"
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=MODEL_NAME, contents=message,
                config={"system_instruction": sys_msg},
            )
            result = (response.text or "").strip() or "(応答なし)"
        except Exception as e:
            result = log_command_error("/chat (Gemini)", e, "Gemini API Error")

    # Obsidian にログ
    await asyncio.to_thread(_log_to_daily_note, "chat", message, result)

    # Discord に応答
    title_map = {
        "tasks": "✅ Tasks", "calendar": "📅 Calendar", "news": "📰 News",
        "crypto": "💰 Crypto", "scan": "📊 Market Scan", "chat": "💬 Chat",
    }
    title = title_map.get(intent, "Chat")
    if is_deferred:
        await _send_long_message(interaction, result, title=title)
    else:
        try:
            mention = f"{interaction.user.mention} **{title}** (Interaction Timed Out)"
            if len(result) > 1900:
                fb_file = _make_discord_file(result, f"{title.replace(' ', '_')}.md")
                await interaction.channel.send(content=mention, file=fb_file)
            else:
                await interaction.channel.send(f"{mention}\n\n{result}")
        except Exception as e:
            print(f"[Discord] ❌ Failed to send fallback message in cmd_chat: {e}")


# ---------------------------------------------------------
# on_message からのチャットセッション継続処理
# ---------------------------------------------------------

async def handle_chat_message(message: discord.Message):
    """アクティブなチャットセッション中のメッセージに応答する。

    bot_discord.py の on_message から、許可ユーザー判定・スレッド分岐の
    後に呼ばれる。セッション対象外のユーザー/チャンネルなら何もしない。
    """
    user_id = message.author.id
    if user_id not in _chat_sessions:
        return

    # 空メッセージはスキップ（画像・埋め込みのみの場合など）
    if not message.content or not message.content.strip():
        return

    session = _chat_sessions[user_id]

    # 同じチャンネルのみ応答
    if message.channel.id != session["channel_id"]:
        return

    # セッション内インテント解析（ツール呼び出しも可能）
    intent, params = _parse_chat_intent(message.content)

    if intent != "chat":
        # ツール呼び出し: session 履歴には残さず直接実行
        if intent == "tasks":
            result = await asyncio.to_thread(tool_get_tasks, "today")
        elif intent == "calendar":
            result = await asyncio.to_thread(tool_get_calendar, params.get("query", "") or "today")
        elif intent == "news":
            result = await asyncio.to_thread(tool_fetch_daily_news)
        elif intent == "crypto":
            symbol = params.get("query", "").upper() or "BTC/USDT"
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            result = await asyncio.to_thread(tool_analyze_crypto, symbol)
        elif intent == "scan":
            result = await asyncio.to_thread(tool_scan_crypto_market)
        else:
            result = None

        if result:
            await message.reply(result[:2000])
            return

    # BitNet pre-filter: 挨拶/スパムはGemini API呼び出しなしで即時応答
    if BITNET_AVAILABLE:
        _filter = quick_filter_discord(message.content)
        if _filter["action"] == "reply":
            await message.reply(_filter["reply"])
            return
        elif _filter["action"] == "skip":
            return

    # 通常の会話: 履歴に追加して Gemini に送信
    session["history"].append({"role": "user", "content": message.content})

    async with message.channel.typing():
        result = await asyncio.to_thread(
            _gemini_chat_with_history, session["history"]
        )

    session["history"].append({"role": "model", "content": result})

    # Obsidian にログ
    await asyncio.to_thread(_log_to_daily_note, "chat", message.content, result)

    # Discord に応答
    view = _create_obsidian_view(result)
    if len(result) <= 2000:
        await message.reply(result, view=view)
    else:
        await message.reply(result[:1900] + "\n\n*... (truncated)*", view=view)
