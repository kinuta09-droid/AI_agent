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
from lib.lib_obsidian_utils import get_obsidian_uri, get_obsidian_uri_from_link
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


def _check_existing_bot():
    """PID ファイルを確認し、既にボットが動作しているかチェックする。"""
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, 'r', encoding='utf-8') as f:
            old_pid = int(f.read().strip())
        if old_pid == os.getpid():
            return

        # Windows でプロセスが存在するか確認
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, old_pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            print(f"[Discord] ⚠️ Another bot instance is already running (PID {old_pid}). Exiting.")
            # 生存中インスタンスの PID ファイルには触れずに終了する
            sys.exit(0) # 正常終了として扱う（ループ防止）
        # プロセスが存在しない = stale な PID ファイルなので削除
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    except ValueError:
        # 破損した PID ファイルは削除（後続の _write_pid で再作成）
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    except OSError:
        pass


def _write_pid():
    """自身の PID を記録する。"""
    with open(PID_FILE, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))


def _cleanup_pid():
    """終了時に PID ファイルを削除する。"""
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

# ---------------------------------------------------------
# Modular Imports
# ---------------------------------------------------------

from lib.lib_shared import (
    client as gemini_client,
    MODEL_NAME,
    VAULT_ROOT,
    DAILY_NOTE_DIR_NAME,
)

from engine_plan_execute import (
    run_plan_and_execute,
    extract_patterns,
    generate_performance_dashboard,
)

from tools.tool_web import (
    tool_fetch_daily_news,
)

from tools.tool_crypto import (
    tool_analyze_crypto,
    tool_scan_crypto_market,
    tool_audit_predictions,
)

from tools.tool_curator import (
    read_main_context,
    get_curation_suggestions,
    search_and_save_topic,
    update_user_profile,
    fetch_external_trends,
)

from tools.tool_obsidian import (
    tool_vault_search,
    tool_create_note,
    tool_read_note,
    tool_update_note,
    tool_merge_weekly_reports,
)

from tools.tool_briefing_manager import (
    tool_generate_briefing,
    add_provider,
)

from tools.tool_gmail_manager import (
    get_gmail_summary as tool_gmail_check,
)

from tools.tool_calendar import (
    tool_get_calendar,
    tool_add_event,
    tool_create_task,
    tool_get_tasks,
    tool_sync_to_daily_note,
)

try:
    from tools.tool_bitnet import quick_filter_discord, gemini_fallback_response
    BITNET_AVAILABLE = True
except ImportError:
    BITNET_AVAILABLE = False

# 新機能: 記事生成 UI
from commands import bot_commands_article
from commands import bot_commands_curator
from commands import bot_commands_calendar
from commands import bot_commands_tools
from commands import bot_commands_agent
from commands import bot_commands_client
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


def _parse_allowed_user_ids(raw: str) -> set:
    """カンマ区切りのユーザーID文字列を set[int] にパースする（数字以外は無視）。"""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


ALLOWED_USER_IDS = _parse_allowed_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
if not ALLOWED_USER_IDS:
    # フェイルオープン: env 設定ミスで bot が完全停止するのを避けるため、警告のみ出して全許可
    print("[Discord] ⚠️ ALLOWED_USER_IDS が未設定です。全ユーザーがコマンドを実行できます。")


def _is_allowed_user(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

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


class GatedCommandTree(app_commands.CommandTree):
    """ALLOWED_USER_IDS に含まれないユーザーの全スラッシュコマンドを一括拒否する。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_allowed_user(interaction.user.id):
            return True
        try:
            await interaction.response.send_message(
                "⛔ このボットのコマンドを実行する権限がありません。", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False


tree = GatedCommandTree(bot)

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
                file = _make_discord_file(result, "Agent_Result.md")
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

    # 4. Gmail URL の検出 → 外部リンクボタン
    gmail_links = {}
    for match in re.finditer(r"\(Gmail(\d*):\s*(https://[^)]+)\)", content):
        num = match.group(1) or "1"
        url = match.group(2).strip()
        gmail_links[f"Gmail {num}"] = url

    if unique_links or gmail_links:
        view = OpenInObsidianView(unique_links)
        for label, url in gmail_links.items():
            if len(view.children) < 5:
                view.add_item(discord.ui.Button(
                    label=label, url=url, style=discord.ButtonStyle.link
                ))
        return view
    return None


def _format_discord_content(content: str) -> str:
    """Discord表示用にメッセージを整形する（[[...]]削除など）"""
    # Obsidianリンク [[...]] を太字 **...** に置換して見やすくする
    display_content = re.sub(r"\[\[(.*?)\]\]", r"**\1**", content)
    # (Path: ...) も表示上は邪魔なので削除する
    display_content = re.sub(r"\(Path:.*?\)", "", display_content)
    display_content = re.sub(r"\(Gmail\d*:.*?\)", "", display_content).strip()
    return display_content


def _make_discord_file(content: str, filename: str) -> discord.File:
    """テキストコンテンツから discord.File を生成する。
    BytesIO(UTF-8) を使うことで StringIO による TypeError を防ぐ。
    """
    return discord.File(io.BytesIO(content.encode('utf-8')), filename=filename)


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
        file = _make_discord_file(content, f"{title}.md")
        await interaction.followup.send(embed=embed, file=file, **kwargs)


async def _send_with_fallback(interaction: discord.Interaction, result: str,
                              title: str = "Result", emoji: str = "📋",
                              embed: discord.Embed = None, file: discord.File = None):
    """interaction.followup + NotFound 時のチャンネル直接送信フォールバック。"""
    try:
        if embed or file:
            view = _create_obsidian_view(result)
            send_kwargs = {}
            if embed is not None:
                send_kwargs["embed"] = embed
            if file is not None:
                send_kwargs["file"] = file
            if view:
                send_kwargs["view"] = view
            await interaction.followup.send(**send_kwargs)
        else:
            await _send_long_message(interaction, result, title=title)
    except discord.NotFound:
        try:
            mention = f"{interaction.user.mention} {emoji} **{title}** (Interaction Timed Out)"
            if len(result) > 1900:
                fb_file = _make_discord_file(result, f"{title.replace(' ', '_')}.md")
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
    intent, params = _parse_chat_intent(message.content)

    # 2. Tool Dispatch
    if intent == "curate":
        await _dispatch_curate(interaction, params.get("query", ""))
        await asyncio.to_thread(_log_to_daily_note, "chat", message.content, "[curate] Suggestions presented")
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
                model=MODEL_NAME, contents=message.content,
                config={"system_instruction": sys_msg},
            )
            result = (response.text or "").strip() or "(応答なし)"
        except Exception as e:
            result = f"Gemini API Error: {e}"

    # Obsidian にログ
    await asyncio.to_thread(_log_to_daily_note, "chat", message.content, result)

    # Discord に応答
    title_map = {
        "tasks": "✅ Tasks", "calendar": "📅 Calendar", "news": "📰 News",
        "crypto": "💰 Crypto", "scan": "📊 Market Scan", "chat": "💬 Chat",
    }
    await _send_long_message(interaction, result, title=title_map.get(intent, "Chat"))


@tree.command(name="news", description="今日のニュースを収集・保存")
async def cmd_news(interaction: discord.Interaction):
    await bot_commands_tools.cmd_news(interaction)


@tree.command(name="briefing", description="デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）")
async def cmd_briefing(interaction: discord.Interaction):
    await bot_commands_tools.cmd_briefing(interaction)


@tree.command(name="gmail_check", description="直近の重要メール要約")
@app_commands.describe(hours="何時間前まで遡るか（デフォルト: 12）")
async def cmd_gmail_check(interaction: discord.Interaction, hours: int = 12):
    await bot_commands_tools.cmd_gmail_check(interaction, hours)


@tree.command(name="crypto", description="仮想通貨のクオンツ分析")
@app_commands.describe(symbol="通貨ペア（例: BTC/USDT, ETH/USDT）")
async def cmd_crypto(interaction: discord.Interaction, symbol: str = "BTC/USDT"):
    await bot_commands_tools.cmd_crypto(interaction, symbol)


@tree.command(name="scan", description="主要20銘柄の一括スキャン")
async def cmd_scan(interaction: discord.Interaction):
    await bot_commands_tools.cmd_scan(interaction)


@tree.command(name="xtrend", description="X(Twitter)のAIトレンドを収集・分析してObsidianに保存")
@app_commands.describe(min_likes="最低いいね数 (デフォルト: 500)")
async def cmd_xtrend(interaction: discord.Interaction, min_likes: int = 500):
    await bot_commands_tools.cmd_xtrend(interaction, min_likes)


@tree.command(name="xtrend-prompt", description="XTrend分析済み機能のClaude Code実装指示書を生成（先に/xtrendを実行）")
@app_commands.describe(index="機能番号 1始まり (デフォルト: 1)")
async def cmd_xtrend_prompt(interaction: discord.Interaction, index: int = 1):
    await bot_commands_tools.cmd_xtrend_prompt(interaction, index)


@tree.command(name="browse", description="URLを開いてAIで情報抽出（JS動的ページ・SPAに対応）")
@app_commands.describe(
    url="取得するURL",
    task="AIへの抽出指示 (例: このページの料金プランを教えて)"
)
async def cmd_browse(interaction: discord.Interaction, url: str, task: str = "このページの主要な情報を要約して"):
    await bot_commands_tools.cmd_browse(interaction, url, task)


@tree.command(name="future", description="英語圏10エージェント並列リサーチ → 日本語レポート+辛口診断をObsidianに保存")
@app_commands.describe(topic="調査トピック (英語推奨, 例: AI agents 2026)")
async def cmd_future(interaction: discord.Interaction, topic: str):
    await bot_commands_tools.cmd_future(interaction, topic)


@tree.command(name="agent", description="AIエージェントに自律的なタスクを依頼（検索・ファイル操作など）")
@app_commands.describe(goal="エージェントへの指示（例: 〇〇について調べてまとめて）")
async def cmd_agent(interaction: discord.Interaction, goal: str):
    await bot_commands_agent.cmd_agent(interaction, goal)


@tree.command(name="agent-stats", description="AIエージェントの実行統計と学習状況を表示")
async def cmd_agent_stats(interaction: discord.Interaction):
    await bot_commands_agent.cmd_agent_stats(interaction)


@tree.command(name="client-review", description="成果物を指定ペルソナ（顧客視点）で5軸評価する。")
@app_commands.describe(
    deliverable="評価対象の成果物テキスト、または [[ファイル名]] の形式でノートリンクを指定",
    job="当初の依頼内容（何を目的とした成果物か）",
    persona="ペルソナID（未指定時は other）例: writing, client_A"
)
async def cmd_client_review(interaction: discord.Interaction, deliverable: str, job: str, persona: str = "other"):
    await bot_commands_client.cmd_client_review(interaction, deliverable, job, persona)


@tree.command(name="persona-add", description="新しいクライアントペルソナを登録する。")
@app_commands.describe(
    client_id="一意のID（半角英数）", 
    label="表示名",
    industry="ベースとなる業種",
    extra_rules="追加のルール（改行やセミコロン区切り）",
    manual_path="マニュアルファイルのパス（TXT等）"
)
async def cmd_persona_add(interaction: discord.Interaction, client_id: str, label: str, industry: str, extra_rules: str = "", manual_path: str = ""):
    await bot_commands_client.cmd_persona_add(interaction, client_id, label, industry, extra_rules, manual_path)


@tree.command(name="persona-list", description="登録済みのペルソナ一覧を表示する。")
async def cmd_persona_list(interaction: discord.Interaction):
    await bot_commands_client.cmd_persona_list(interaction)


@tree.command(name="article", description="対話形式で自分らしい体験談記事を作成する")
async def cmd_article(interaction: discord.Interaction):
    await bot_commands_article.cmd_article(interaction)
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

    # 許可ユーザー以外のメッセージは無視（webhook 経路は上のチャンネルIDゲートで制御済み）
    if not _is_allowed_user(message.author.id):
        return

    # --- 記事生成スレッドのメッセージ処理 ---
    if isinstance(message.channel, discord.Thread):
        await bot_commands_article.handle_article_reply(message)

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


# ---------------------------------------------------------
# Events
# ---------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[Discord] ✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[Discord] 🏰 Connected Guilds: {[g.id for g in bot.guilds]}")

    # bot_commands_curator のヘルパー初期化
    bot_commands_curator.init_helpers(
        _log_to_daily_note, _create_obsidian_view, _format_discord_content,
        _send_long_message, _send_with_fallback
    )
    # bot_commands_tools のヘルパー初期化
    bot_commands_tools.init_helpers(
        _log_to_daily_note, _format_discord_content, _send_with_fallback
    )
    # bot_commands_agent のヘルパー初期化
    bot_commands_agent.init_helpers(
        _log_to_daily_note, _send_long_message, _send_with_fallback
    )
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
    _check_existing_bot()
    _write_pid()
    print("[Discord] 🤖 Starting Antigravity Discord Bot...")
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        _cleanup_pid()


if __name__ == "__main__":
    run_bot()
