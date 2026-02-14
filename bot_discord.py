# -*- coding: utf-8 -*-
"""
Antigravity Discord Command Center
Discord Bot を通じて agent_hello.py のツールを操作し、
全ログを Obsidian デイリーノートに集約する。
"""

import os
import sys
import re
import io
import asyncio
from lib_obsidian_utils import get_obsidian_uri, get_obsidian_uri_from_link
import datetime
import signal
import time
import tempfile
import wave
import threading
import subprocess

try:
    import discord.ext.voice_recv as voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False

try:
    import openai as openai_lib
    OPENAI_AVAILABLE = True
except ImportError:
    openai_lib = None
    OPENAI_AVAILABLE = False

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# PID Guard: 重複起動防止
# ---------------------------------------------------------
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_discord.pid")


def _kill_existing_bot():
    """PID ファイルに記録された旧プロセスを停止する。"""
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, 'r') as f:
            old_pid = int(f.read().strip())
        if old_pid == os.getpid():
            return
        os.kill(old_pid, signal.SIGTERM)
        print(f"[Discord] 🛑 Killed previous bot process (PID {old_pid})")
    except (ProcessLookupError, ValueError, OSError):
        pass  # プロセスが既に終了済みまたは無効な PID
    except PermissionError:
        print(f"[Discord] ⚠️ Could not kill PID {old_pid} (permission denied)")
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def _write_pid():
    """自身の PID を記録する。"""
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def _cleanup_pid():
    """終了時に PID ファイルを削除する。"""
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

# agent_hello.py と同じディレクトリなので直接 import
from agent_hello import (
    tool_fetch_daily_news,
    tool_analyze_crypto,
    tool_scan_crypto_market,
    tool_audit_predictions,
    run_plan_and_execute,
    extract_patterns,
    generate_performance_dashboard,
    tool_generate_briefing,
)

from lib_shared import (
    client as gemini_client,
    MODEL_NAME,
    VAULT_ROOT,
    DAILY_NOTE_DIR_NAME,
)

from tool_curator import (
    read_main_context,
    get_curation_suggestions,
    search_and_save_topic,
    update_user_profile,
    fetch_external_trends,
)

from tool_calendar import (
    tool_get_calendar,
    tool_add_event,
    tool_create_task,
    tool_get_tasks,
    tool_sync_to_daily_note,
)

import discord
from discord import app_commands
from dotenv import load_dotenv

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

load_dotenv(r'C:\AI_agent\.env')

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

_openai_client = (
    openai_lib.OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_AVAILABLE and OPENAI_API_KEY
    and not OPENAI_API_KEY.startswith("sk-YOUR")
    else None
)

if not DISCORD_TOKEN:
    print("Error: DISCORD_BOT_TOKEN not found in .env")
    sys.exit(1)

# ---------------------------------------------------------
# Bot Setup
# ---------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---------------------------------------------------------
# Chat Session State
# ---------------------------------------------------------

# ユーザーID -> セッション情報 のマップ
_chat_sessions: dict[int, dict] = {}


def _gemini_chat_with_history(history: list) -> str:
    """会話履歴を含めて Gemini に送信する（直近10件）。"""
    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
    system_msg = f"今日は{today_str}です。日本語で簡潔に応答してください。"

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
# Helpers
# ---------------------------------------------------------

def _log_to_daily_note(log_type: str, user_input: str, ai_output: str = ""):
    """Obsidian 50_Archive/Chatlog に Discord ログを保存する。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.datetime.now().strftime("%H:%M")
    
    # Save to 50_Archive/Chatlog
    log_dir = os.path.join(VAULT_ROOT, "50_Archive", "Chatlog")
    daily_path = os.path.join(log_dir, f"{today}.md")

    if log_type == "chat":
        entry = (
            f"\n## 💬 Discord Chat ({time_str})\n"
            f"**User**: {user_input}\n"
            f"**AI**: {ai_output}\n"
            f"#log/mobile\n"
        )
    elif log_type == "action":
        entry = (
            f"\n## 📱 Action Log ({time_str})\n"
            f"{user_input}\n"
            f"#log/action\n"
        )
    elif log_type == "command":
        entry = (
            f"\n## 🤖 Discord Command ({time_str})\n"
            f"**Command**: {user_input}\n"
            f"**Result**: {ai_output[:200]}{'...' if len(ai_output) > 200 else ''}\n"
            f"#log/mobile\n"
        )
    else:
        entry = f"\n## 📝 Log ({time_str})\n{user_input}\n"

    # デイリーノートが存在しない場合は基本ヘッダーで作成
    if not os.path.exists(daily_path):
        os.makedirs(os.path.dirname(daily_path), exist_ok=True)
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write(f"# {today}\n")

    with open(daily_path, "a", encoding="utf-8") as f:
        f.write(entry)


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
    """Curate workflow via /chat command."""
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
        embed = _build_curate_embed(suggestions)
        view = CurateView(suggestions, interaction.user.id, context=context)
        await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}")


async def _dispatch_agent(interaction: discord.Interaction, goal: str):
    """Plan-and-Execute workflow via /chat command."""
    if not goal:
        await interaction.followup.send("エージェントへの指示を入力してください。例: `/chat agent AIの最新トレンドを調べて`")
        return

    loop = asyncio.get_running_loop()

    async def discord_pe_callback(step_data):
        phase = step_data.get("phase", "")
        try:
            if phase == "plan" and step_data.get("status") == "planning":
                await interaction.edit_original_response(content="🧠 **計画を策定中...**")
            elif phase == "plan" and step_data.get("status") == "ready":
                plan = step_data.get("plan", [])
                lines = [f"{p['step']}. {p['task']}" for p in plan]
                await interaction.edit_original_response(
                    content=f"📋 **実行計画:**\n" + "\n".join(lines) + "\n\n▶️ 実行開始..."
                )
            elif phase == "execute" and step_data.get("status") == "running":
                await interaction.edit_original_response(
                    content=f"▶️ **実行中:** Step {step_data.get('step', '?')}/{step_data.get('total', '?')} - {step_data.get('task', '')}..."
                )
            elif phase == "execute" and step_data.get("status") in ("done", "error"):
                icon = "✅" if step_data["status"] == "done" else "❌"
                await interaction.edit_original_response(
                    content=f"{icon} Step {step_data.get('step', '?')}/{step_data.get('total', '?')} - {step_data.get('task', '')}\n\n⏳ 次のステップへ..."
                )
            elif phase == "critique":
                await interaction.edit_original_response(content="🔍 **結果を評価中...**")
            elif phase == "revision":
                await interaction.edit_original_response(
                    content=f"🔄 **再計画中... (修正 {step_data.get('attempt', 1)}回目)**"
                )
        except Exception as e:
            print(f"[Discord] Warning: Failed to update status: {e}")

    def sync_callback_wrapper(step_data):
        asyncio.run_coroutine_threadsafe(discord_pe_callback(step_data), loop)

    try:
        result = await asyncio.to_thread(run_plan_and_execute, goal, step_callback=sync_callback_wrapper)
    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", f"/chat agent {goal}", result)

    try:
        await _send_long_message(interaction, f"✅ **Mission Complete**\n\n{result}", title="Agent Result")
    except discord.NotFound:
        try:
            if len(result) > 1900:
                file = discord.File(io.StringIO(result), filename="Agent_Result.md")
                await interaction.channel.send(
                    content=f"{interaction.user.mention} ✅ **Mission Complete**", file=file
                )
            else:
                await interaction.channel.send(f"{interaction.user.mention} ✅ **Mission Complete**\n\n{result}")
        except Exception as e:
            print(f"[Discord] Failed to send fallback: {e}")


# ---------------------------------------------------------
# Obsidian Helper Views
# ---------------------------------------------------------

class OpenInObsidianView(discord.ui.View):
    def __init__(self, links: dict[str, str]):
        """
        links: {filename: uri} の辞書
        """
        super().__init__(timeout=None)
        
        # リンク数に応じてボタンを追加 (最大5個まで)
        # 5個を超える場合はドロップダウン (TODO: 実装簡略化のため一旦ボタンのみ、溢れたら無視)
        count = 0
        for name, uri in links.items():
            if count >= 5:
                break
            # ラベルが長すぎる場合は切り詰め
            label = f"📂 {name}"[:80]
            
            # URLではなく、コールバックでローカルコマンド(os.startfile)を実行する
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            
            # 変数をキャプチャするためにデフォルト引数を使用
            async def callback(interaction: discord.Interaction, uri=uri, name=name):
                try:
                    if uri.startswith("obsidian://"):
                        # Windows only: ローカルでURIを開く
                        os.startfile(uri)
                        await interaction.response.send_message(f"🚀 Opening **{name}** in Obsidian...", ephemeral=True, delete_after=3)
                    else:
                        await interaction.response.send_message("❌ Invalid URI scheme.", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(f"❌ Failed to open: {e}", ephemeral=True)
            
            btn.callback = callback
            self.add_item(btn)
            count += 1

def _create_obsidian_view(content: str) -> discord.ui.View | None:
    """コンテンツ内のパス([[...]]含む)を検出し、Obsidianを開くボタンViewを作成する"""
    unique_links = {}

    # 1. (Path: ...) の検出（絶対パス）
    for match in re.finditer(r"\(Path:\s*([A-Z]:\\[^)]+)\)", content):
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
            name = os.path.basename(path)
            unique_links[name] = uri

    # 2. [[...]] の検出（相対パス/ファイル名）
    for match in re.finditer(r"\[\[(.*?)\]\]", content):
        link_text = match.group(1).strip()
        # パイプ | で表示名が指定されている場合がある [[file|label]] -> fileを採用
        if "|" in link_text:
            link_text = link_text.split("|")[0]
            
        uri = get_obsidian_uri_from_link(link_text)
        if uri:
            # 拡張子がない場合は補完して表示名にする
            name = link_text if "." in link_text else f"{link_text}.md"
            unique_links[name] = uri
            
    # 3. LoRA出力パスの検出
    match = re.search(r"出力:\s*([A-Z]:\\[^\n]+)", content)
    if match:
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
             name = "LoRA Dataset"
             unique_links[name] = uri

    if unique_links:
        return OpenInObsidianView(unique_links)
    return None


def _format_discord_content(content: str) -> str:
    """Discord表示用にメッセージを整形する（[[...]]削除など）"""
    # Obsidianリンク [[...]] を太字 **...** に置換して見やすくする
    display_content = re.sub(r"\[\[(.*?)\]\]", r"**\1**", content)
    # (Path: ...) も表示上は邪魔なので削除する
    display_content = re.sub(r"\(Path:.*?\)", "", display_content).strip()
    return display_content


async def _send_long_message(interaction: discord.Interaction, content: str,
                              title: str = "Result", color: int = 0x00ff00):
    """2000文字を超える場合は Embed + ファイル添付で送信する。Obsidianボタンも付与。"""
    
    
    display_content = _format_discord_content(content)
    view = _create_obsidian_view(content)
    
    kwargs = {}
    if view:
        kwargs["view"] = view

    if len(display_content) <= 2000:
        await interaction.followup.send(display_content, **kwargs)
    else:
        # サマリー（最初の1500文字）を Embed で、全文を .md ファイルで添付
        summary = display_content[:1500] + "\n\n*... (full report attached)*"
        embed = discord.Embed(title=title, description=summary, color=color)
        file = discord.File(io.StringIO(content), filename=f"{title}.md")
        await interaction.followup.send(embed=embed, file=file, **kwargs)


async def _send_with_fallback(interaction: discord.Interaction, result: str,
                              title: str = "Result", emoji: str = "📋",
                              embed: discord.Embed = None, file: discord.File = None):
    """interaction.followup + NotFound 時のチャンネル直接送信フォールバック。"""
    try:
        if embed or file:
            view = _create_obsidian_view(result)
            kwargs = {}
            if view:
                kwargs["view"] = view
            await interaction.followup.send(embed=embed, file=file, **kwargs)
        else:
            await _send_long_message(interaction, result, title=title)
    except discord.NotFound:
        try:
            mention = f"{interaction.user.mention} {emoji} **{title}** (Interaction Timed Out)"
            if len(result) > 1900:
                fb_file = discord.File(io.StringIO(result), filename=f"{title.replace(' ', '_')}.md")
                await interaction.channel.send(content=mention, file=fb_file)
            else:
                await interaction.channel.send(f"{mention}\n\n{result}")
        except Exception as e:
            print(f"[Discord] ❌ Failed to send fallback message: {e}")
    except Exception as e:
        print(f"[Discord] ❌ Failed to send result: {e}")


# ---------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------

@tree.command(name="chat", description="AIチャット (on: セッション開始, off: 終了, またはメッセージ)")
@app_commands.describe(message="on/off またはメッセージを入力")
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
            await interaction.followup.send(f"❌ ボイスチャンネル接続エラー: {e}")
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
    await interaction.response.defer()

    # 1. Intent 解析
    intent, params = _parse_chat_intent(message)

    # 2. Tool Dispatch
    if intent == "curate":
        await _dispatch_curate(interaction, params.get("query", ""))
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
        await _dispatch_agent(interaction, params.get("query", ""))
        return
    else:
        # Default: Gemini chat
        today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        sys_msg = f"今日は{today_str}です。日本語で簡潔に応答してください。"
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME, contents=message,
                config={"system_instruction": sys_msg},
            )
            result = (response.text or "").strip() or "(応答なし)"
        except Exception as e:
            result = f"Gemini API Error: {e}"

    # Obsidian にログ
    await asyncio.to_thread(_log_to_daily_note, "chat", message, result)

    # Discord に応答
    title_map = {
        "tasks": "✅ Tasks", "calendar": "📅 Calendar", "news": "📰 News",
        "crypto": "💰 Crypto", "scan": "📊 Market Scan", "chat": "💬 Chat",
    }
    await _send_long_message(interaction, result, title=title_map.get(intent, "Chat"))


@tree.command(name="news", description="今日のニュースを収集・保存")
async def cmd_news(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_fetch_daily_news)
    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", "/news", result)
    await _send_with_fallback(interaction, result, title="Daily News", emoji="📰")


@tree.command(name="briefing", description="デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）")
async def cmd_briefing(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_generate_briefing)
    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", "/briefing", result)
    await _send_with_fallback(interaction, result, title="Daily Briefing", emoji="📋")


@tree.command(name="crypto", description="仮想通貨のクオンツ分析")
@app_commands.describe(symbol="通貨ペア（例: BTC/USDT, ETH/USDT）")
async def cmd_crypto(interaction: discord.Interaction, symbol: str = "BTC/USDT"):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_analyze_crypto, symbol)
    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", f"/crypto {symbol}", result)

    display_content = _format_discord_content(result)
    embed = discord.Embed(
        title=f"Crypto Analysis: {symbol}",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0xf0b90b  # Binance yellow
    )
    clean = symbol.replace('/', '_')
    file = discord.File(
        io.StringIO(result),
        filename=f"Crypto_{clean}_{datetime.datetime.now().strftime('%Y-%m-%d')}.md"
    ) if len(result) > 1500 else None
    await _send_with_fallback(interaction, result, title="Crypto Analysis", emoji="🪙", embed=embed, file=file)


@tree.command(name="scan", description="主要20銘柄の一括スキャン")
async def cmd_scan(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_scan_crypto_market)
    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", "/scan", result)

    display_content = _format_discord_content(result)
    embed = discord.Embed(
        title="Market Scan",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0x00bfff
    )
    file = discord.File(
        io.StringIO(result),
        filename=f"Market_Scan_{datetime.datetime.now().strftime('%Y-%m-%d')}.md"
    ) if len(result) > 1500 else None
    await _send_with_fallback(interaction, result, title="Market Scan", emoji="🔭", embed=embed, file=file)



@tree.command(name="agent", description="AIエージェントに自律的なタスクを依頼（検索・ファイル操作など）")
@app_commands.describe(goal="エージェントへの指示（例: 〇〇について調べてまとめて）")
async def cmd_agent(interaction: discord.Interaction, goal: str):
    await interaction.response.defer()

    # Plan-and-Execute 用コールバック
    async def discord_pe_callback(step_data):
        phase = step_data.get("phase", "")
        try:
            if phase == "plan" and step_data.get("status") == "planning":
                await interaction.edit_original_response(
                    content="🧠 **計画を策定中...**"
                )
            elif phase == "plan" and step_data.get("status") == "ready":
                plan = step_data.get("plan", [])
                lines = [f"{p['step']}. {p['task']}" for p in plan]
                plan_text = "\n".join(lines)
                await interaction.edit_original_response(
                    content=f"📋 **実行計画:**\n{plan_text}\n\n▶️ 実行開始..."
                )
            elif phase == "execute" and step_data.get("status") == "running":
                step_num = step_data.get("step", "?")
                total = step_data.get("total", "?")
                task = step_data.get("task", "")
                await interaction.edit_original_response(
                    content=f"▶️ **実行中:** Step {step_num}/{total} - {task}..."
                )
            elif phase == "execute" and step_data.get("status") in ("done", "error"):
                step_num = step_data.get("step", "?")
                total = step_data.get("total", "?")
                task = step_data.get("task", "")
                icon = "✅" if step_data["status"] == "done" else "❌"
                await interaction.edit_original_response(
                    content=f"{icon} Step {step_num}/{total} - {task}\n\n⏳ 次のステップへ..."
                )
            elif phase == "critique":
                await interaction.edit_original_response(
                    content="🔍 **結果を評価中...**"
                )
            elif phase == "revision":
                attempt = step_data.get("attempt", 1)
                await interaction.edit_original_response(
                    content=f"🔄 **再計画中... (修正 {attempt}回目)**"
                )
        except Exception as e:
            print(f"[Discord] ⚠️ Failed to update status: {e}")

    try:
        loop = asyncio.get_running_loop()

        def sync_callback_wrapper(step_data):
            asyncio.run_coroutine_threadsafe(discord_pe_callback(step_data), loop)

        result = await asyncio.to_thread(run_plan_and_execute, goal, step_callback=sync_callback_wrapper)

    except Exception as e:
        result = f"Error: {e}"

    await asyncio.to_thread(_log_to_daily_note, "command", f"/agent {goal}", result)
    await _send_with_fallback(interaction, f"✅ **Mission Complete**\n\n{result}", title="Agent Result", emoji="✅")

# ---------------------------------------------------------
# The Explorer: Interactive Views
# ---------------------------------------------------------

_BRANCH_LABEL = {
    "deepen": "🔬 深化", "expand": "🌐 水平展開", "jump": "🚀 飛躍",
    "consistent": "🔬 安定", "bridge": "🌐 橋渡し", "entropy": "🚀 飛躍",
}
_BRANCH_ICON = {
    "deepen": "🔬", "expand": "🌐", "jump": "🚀",
    "consistent": "🔬", "bridge": "🌐", "entropy": "🚀",
}


def _build_curate_embed(suggestions: dict, reject_count: int = 0) -> discord.Embed:
    """トピック提案の Embed を生成する（再利用可能）。"""
    topics = suggestions.get("topics", [])
    ab = suggestions.get("ab_question", {})

    title = "🌳 The Explorer -- 知識の枝を伸ばそう"
    if reject_count > 0:
        title = f"🌳 The Explorer -- 再提案 #{reject_count}"

    embed = discord.Embed(
        title=title,
        description="気になるトピックのボタンを押すと、AIが調査してナレッジノートを作成します。",
        color=0x7B68EE,
    )
    for i, topic in enumerate(topics[:3], 1):
        branch = topic.get("branch", "")
        label = _BRANCH_LABEL.get(branch, "📚")
        source = topic.get("source_note", "")
        source_text = f" ← [[{source}]]" if source else ""
        reasoning = topic.get("reasoning", "")
        reasoning_text = f"\n💡 *{reasoning[:100]}*" if (branch == "entropy" and reasoning) else ""
        embed.add_field(
            name=f"{i}. [{label}] {topic['title']}",
            value=f"{topic.get('description', '')}{source_text}{reasoning_text}",
            inline=False,
        )

    if ab:
        embed.add_field(
            name=f"💭 {ab.get('question', 'あなたの好みは？')}",
            value=f"**A**: {ab.get('option_a', '?')}\n**B**: {ab.get('option_b', '?')}",
            inline=False,
        )

    return embed


class CurateView(discord.ui.View):
    """Interactive view for /curate with Deep Refinement Loop."""

    def __init__(self, suggestions: dict, user_id: int,
                 context: str = "", rejected_topics: list = None,
                 reject_count: int = 0):
        super().__init__(timeout=300)
        self.suggestions = suggestions
        self.user_id = user_id
        self.context = context
        self.rejected_topics = rejected_topics or []
        self.reject_count = reject_count
        self.topics = suggestions.get("topics", [])
        self.ab_question = suggestions.get("ab_question", {})

        # トピックボタン（Row 0）
        for i, topic in enumerate(self.topics[:3]):
            branch = topic.get("branch", "")
            icon = _BRANCH_ICON.get(branch, "📚")
            btn = discord.ui.Button(
                label=f"{icon} {topic['title']}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"topic_{i}",
                row=0,
            )
            btn.callback = self._make_topic_callback(i)
            self.add_item(btn)

        # A/B ボタン（Row 1）
        if self.ab_question:
            btn_a = discord.ui.Button(
                label=f"A: {self.ab_question.get('option_a', '?')}"[:80],
                style=discord.ButtonStyle.success,
                custom_id="ab_a",
                row=1,
            )
            btn_a.callback = self._make_ab_callback("option_a")
            self.add_item(btn_a)

            btn_b = discord.ui.Button(
                label=f"B: {self.ab_question.get('option_b', '?')}"[:80],
                style=discord.ButtonStyle.success,
                custom_id="ab_b",
                row=1,
            )
            btn_b.callback = self._make_ab_callback("option_b")
            self.add_item(btn_b)

        # 再提案ボタン（Row 2）— 旧「スキップ」を置き換え
        skip_label = "🔄 別の提案を見る" if reject_count == 0 else f"🔄 もっと別の提案 ({reject_count}回目)"
        btn_skip = discord.ui.Button(
            label=skip_label[:80],
            style=discord.ButtonStyle.secondary,
            custom_id="skip",
            row=2,
        )
        btn_skip.callback = self._refinement_callback
        self.add_item(btn_skip)

        # 終了ボタン（Row 2）
        btn_end = discord.ui.Button(
            label="終了",
            style=discord.ButtonStyle.danger,
            custom_id="end",
            row=2,
        )
        btn_end.callback = self._end_callback
        self.add_item(btn_end)

    def _make_topic_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "このボタンは操作できません。", ephemeral=True
                )
                return

            topic = self.topics[index]
            await interaction.response.defer()

            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(view=self)

            branch = topic.get("branch", "")
            icon = _BRANCH_ICON.get(branch, "🔍")
            await interaction.followup.send(
                f"{icon} **{topic['title']}** を調査中..."
            )
            result = await asyncio.to_thread(
                search_and_save_topic,
                topic["title"],
                topic.get("search_query", ""),
                topic.get("source_note", ""),
                topic.get("branch", ""),
                topic.get("reasoning", ""),
            )
            await asyncio.to_thread(
                _log_to_daily_note, "command", f"/curate -> {topic['title']}", result
            )
            view = _create_obsidian_view(result)
            display_result = _format_discord_content(result)
            
            kwargs = {}
            if view:
                kwargs["view"] = view
            
            await interaction.followup.send(f"✅ {display_result}", **kwargs)

        return callback

    def _make_ab_callback(self, option_key: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "このボタンは操作できません。", ephemeral=True
                )
                return

            answer = self.ab_question.get(option_key, "?")
            question = self.ab_question.get("question", "?")

            await interaction.response.defer(ephemeral=True)
            await asyncio.to_thread(update_user_profile, question, answer)
            await interaction.followup.send(
                f"✅ 好みを記録しました: {answer}", ephemeral=True
            )

        return callback

    async def _refinement_callback(self, interaction: discord.Interaction):
        """Deep Refinement Loop: 拒否データを蓄積して再提案する。"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このボタンは操作できません。", ephemeral=True
            )
            return

        # 現在のトピックを拒否リストに追加
        new_rejected = self.rejected_topics.copy()
        for t in self.topics:
            new_rejected.append(t.get("title", ""))
        new_reject_count = self.reject_count + 1

        # ボタンを無効化 + 「考え中」表示
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="🤔 **別の切り口を考え中...**", view=self
        )

        try:
            # コンテキストがなければ再取得
            ctx = self.context
            if not ctx:
                ctx = await asyncio.to_thread(read_main_context)

            # 外部トレンド再取得 + 拒否データ付きで再生成
            trends = await asyncio.to_thread(fetch_external_trends)
            new_suggestions = await asyncio.to_thread(
                get_curation_suggestions, ctx, "normal",
                new_rejected, new_reject_count, trends,
            )

            new_topics = new_suggestions.get("topics", [])
            if not new_topics:
                await interaction.followup.send("これ以上の提案を生成できませんでした。")
                return

            # 新しい Embed + View を生成して送信
            new_embed = _build_curate_embed(new_suggestions, new_reject_count)
            new_view = CurateView(
                new_suggestions, self.user_id,
                context=ctx,
                rejected_topics=new_rejected,
                reject_count=new_reject_count,
            )
            await interaction.followup.send(embed=new_embed, view=new_view)

        except Exception as e:
            await interaction.followup.send(f"再提案の生成に失敗しました: {e}")

    async def _end_callback(self, interaction: discord.Interaction):
        """セッション終了。"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このボタンは操作できません。", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"👋 探索を終了しました（{self.reject_count}回再提案）", view=self
        )


class SetupView(discord.ui.View):
    """Onboarding view for /setup: dropdown with multiple topic selection."""

    def __init__(self, suggestions: dict, user_id: int):
        super().__init__(timeout=600)
        self.suggestions = suggestions
        self.user_id = user_id
        self.topics = suggestions.get("topics", [])

        options = []
        for i, topic in enumerate(self.topics[:25]):
            options.append(discord.SelectOption(
                label=topic["title"][:100],
                description=topic.get("description", "")[:100],
                value=str(i),
            ))

        select = discord.ui.Select(
            placeholder="興味のあるトピックを選んでください...",
            min_values=1,
            max_values=min(10, len(options)),
            options=options,
        )
        select.callback = self._select_callback
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このメニューは操作できません。", ephemeral=True
            )
            return

        selected_indices = [int(v) for v in interaction.data["values"]]
        selected_topics = [
            self.topics[i] for i in selected_indices if i < len(self.topics)
        ]

        await interaction.response.defer()

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)

        names = ", ".join([t["title"] for t in selected_topics])
        await interaction.followup.send(
            f"📚 {len(selected_topics)}件のトピックを調査します: {names}\n"
            f"しばらくお待ちください..."
        )

        results = []
        for topic in selected_topics:
            result = await asyncio.to_thread(
                search_and_save_topic,
                topic["title"],
                topic.get("search_query", ""),
                topic.get("source_note", ""),
                topic.get("branch", ""),
            )
            results.append(f"- {topic['title']}: {result}")
            await asyncio.sleep(1)

        summary = "\n".join(results)
        await asyncio.to_thread(
            _log_to_daily_note, "command", f"/setup -> {names}", summary
        )
        await _send_long_message(
            interaction,
            f"✅ **セットアップ完了**\n\n{summary}",
            title="Setup Results",
        )


# ---------------------------------------------------------
# The Curator: Slash Commands
# ---------------------------------------------------------

@tree.command(name="curate", description="AIがあなたの興味に基づいてトピックを提案します")
async def cmd_curate(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        context = await asyncio.to_thread(read_main_context)
        trends = await asyncio.to_thread(fetch_external_trends)
        suggestions = await asyncio.to_thread(
            get_curation_suggestions, context, "normal",
            None, 0, trends,
        )

        topics = suggestions.get("topics", [])
        if not topics:
            await interaction.followup.send("トピック提案の生成に失敗しました。")
            return

        embed = _build_curate_embed(suggestions)
        view = CurateView(suggestions, interaction.user.id, context=context)
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        await interaction.followup.send(f"Error: {e}")

    await asyncio.to_thread(_log_to_daily_note, "command", "/curate", "Suggestions presented")


@tree.command(
    name="setup",
    description="初回セットアップ: 興味のあるトピックを一括選択してナレッジベースを構築",
)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        context = await asyncio.to_thread(read_main_context)
        suggestions = await asyncio.to_thread(
            get_curation_suggestions, context, "setup"
        )

        topics = suggestions.get("topics", [])
        if not topics:
            await interaction.followup.send("トピック提案の生成に失敗しました。")
            return

        embed = discord.Embed(
            title="🚀 Antigravity Setup -- ナレッジベース初期構築",
            description=(
                f"あなたの興味に基づいて **{len(topics)}件** のトピックを生成しました。\n"
                "下のドロップダウンから気になるトピックを選んでください（最大10件）。\n"
                "選択したトピックについてAIが調査し、ナレッジノートを自動作成します。"
            ),
            color=0x2ECC71,
        )

        view = SetupView(suggestions, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        await interaction.followup.send(f"Error: {e}")


@tree.command(name="topic", description="興味のあるトピックを直接指定して調査・記録します")
@app_commands.describe(query="調べたいキーワード（例: 古代ローマの建築技術）")
async def cmd_topic(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    try:
        # 1. User_Profile.md に明示的な興味として記録
        await asyncio.to_thread(
            update_user_profile, "Explicit Interest", query
        )

        await interaction.followup.send(
            f"📌 **{query}** を興味リストに追加しました。調査を開始します..."
        )

        # 2. Tavily検索 → Gemini要約 → Knowledge_Base に保存
        result = await asyncio.to_thread(search_and_save_topic, query)

        await asyncio.to_thread(
            _log_to_daily_note, "command", f"/topic {query}", result
        )
        view = _create_obsidian_view(result)
        display_result = _format_discord_content(result)
        
        kwargs = {}
        if view:
            kwargs["view"] = view

        await interaction.followup.send(f"✅ {display_result}", **kwargs)

    except Exception as e:
        await interaction.followup.send(f"Error: {e}")


# ---------------------------------------------------------
# Calendar / Task Commands
# ---------------------------------------------------------

@tree.command(name="calendar", description="Google Calendarから予定を取得します")
@app_commands.describe(date="日付（today, tomorrow, 2026-02-15 など）")
async def cmd_calendar(interaction: discord.Interaction, date: str = "today"):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_get_calendar, date)
    except Exception as e:
        result = f"Error: {e}"

    embed = discord.Embed(
        title="📅 Calendar",
        description=result[:2000],
        color=0x4285F4,
    )
    await interaction.followup.send(embed=embed)


@tree.command(name="add-event", description="Google Calendarに予定を追加します")
@app_commands.describe(
    title="予定のタイトル",
    start_time="開始時刻（例: 明日 14:00, 2026-02-15 10:00）",
    end_time="終了時刻（省略時は1時間後）",
)
async def cmd_add_event(interaction: discord.Interaction,
                        title: str, start_time: str, end_time: str = ""):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_add_event, title, start_time, end_time)
    except Exception as e:
        result = f"Error: {e}"

    await interaction.followup.send(result)


@tree.command(name="tasks", description="Todoistのタスク一覧を表示します")
@app_commands.describe(filter="フィルター（today, overdue, week, all）")
async def cmd_tasks(interaction: discord.Interaction, filter: str = "today"):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_get_tasks, filter)
    except Exception as e:
        result = f"Error: {e}"

    embed = discord.Embed(
        title="✅ Tasks",
        description=result[:2000],
        color=0xE44332,
    )
    await interaction.followup.send(embed=embed)


@tree.command(name="remind", description="Todoistにリマインダー（タスク）を追加します")
@app_commands.describe(
    content="リマインド内容",
    due_date="期限（例: 明日, 2026-02-15, 毎日 21:00）",
)
async def cmd_remind(interaction: discord.Interaction,
                     content: str, due_date: str = ""):
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_create_task, content, due_date, 1)
    except Exception as e:
        result = f"Error: {e}"

    await interaction.followup.send(result)


# ---------------------------------------------------------
# Agent Stats Command
# ---------------------------------------------------------

@tree.command(name="agent-stats", description="AIエージェントの実行統計と学習状況を表示")
async def cmd_agent_stats(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        patterns = await asyncio.to_thread(extract_patterns)

        if patterns.get("message"):
            await interaction.followup.send(f"📊 {patterns['message']}")
            return

        total = patterns["total"]
        rate = patterns["success_rate"]
        avg_dur = patterns["avg_duration"]
        tool_rel = patterns.get("tool_reliability", {})
        insights = patterns.get("insights", "")

        # ツール信頼性ランキング（上位5件）
        sorted_tools = sorted(tool_rel.items(), key=lambda x: x[1], reverse=True)[:5]
        tool_lines = "\n".join(f"  {t}: **{r:.0%}**" for t, r in sorted_tools)

        embed = discord.Embed(
            title="📊 Agent Performance Stats",
            color=0x3498DB,
        )
        embed.add_field(
            name="全体統計",
            value=f"総実行: **{total}**回\n成功率: **{rate:.0%}**\n平均時間: **{avg_dur:.1f}秒**",
            inline=False,
        )
        if tool_lines:
            embed.add_field(
                name="ツール信頼性 TOP5",
                value=tool_lines,
                inline=False,
            )
        if insights:
            embed.add_field(
                name="AI分析",
                value=insights[:1000],
                inline=False,
            )

        await interaction.followup.send(embed=embed)

        # Dashboard も更新
        await asyncio.to_thread(generate_performance_dashboard)

    except Exception as e:
        await interaction.followup.send(f"Error: {e}")


# ---------------------------------------------------------
# Webhook Listener (HTTP Shortcuts → Obsidian)
# ---------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    # 自分自身のメッセージは無視
    if message.author == bot.user:
        return

    # Webhook からのメッセージを検知してデイリーノートに記録
    if message.webhook_id is not None:
        if LOG_CHANNEL_ID and str(message.channel.id) == LOG_CHANNEL_ID:
            await asyncio.to_thread(
                _log_to_daily_note, "action", message.content
            )
            await message.add_reaction("✅")
            print(f"  📱 Webhook log saved: {message.content[:50]}")
        return

    # --- Chat Session Handler ---
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


# ---------------------------------------------------------
# Events
# ---------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[Discord] ✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[Discord] 🏰 Connected Guilds: {[g.id for g in bot.guilds]}")
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[Discord] 🔄 Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"[Discord] 🔄 Synced {len(synced)} slash command(s) globally (may take up to 1h)")
    except Exception as e:
        print(f"[Discord] ❌ Failed to sync commands: {e}")
    print("[Discord] 🚀 Bot is ready!")


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def run_bot():
    """main.py から呼び出し可能なエントリーポイント"""
    # Imported from main.py -> Spawn fresh process to avoid Session Closed error (global client reuse)
    if __name__ != "__main__":
        print(f"[Discord] 🚀 Spawning independent bot process via subprocess...")
        try:
            subprocess.run([sys.executable, os.path.abspath(__file__)], check=False)
        except KeyboardInterrupt:
            print("\n[Discord] 🛑 Broker stopped.")
        return

    # Standalone execution (or via subprocess)
    _kill_existing_bot()
    _write_pid()
    print("[Discord] 🤖 Starting Antigravity Discord Bot...")
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        _cleanup_pid()


if __name__ == "__main__":
    run_bot()
