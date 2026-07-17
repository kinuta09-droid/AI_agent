# -*- coding: utf-8 -*-
"""
lib/watchdog.py — ハング検知 (heartbeat 鮮度判定)

main.py のスーパーバイザは proc.poll() で「死亡」は検知できるが、
「ハング」(プロセスは生きているが機能停止) は検知できない。
本モジュールは heartbeat ファイルの鮮度でハングを判定する部品を提供し、
判定後の再起動は既存スーパーバイザに委ねる (二重監視を作らない)。

役割分担:
    bot_discord / agent_scheduler … 定期的に beat() で heartbeat を更新
    main.py スーパーバイザ        … is_stale() で鮮度チェック →
                                    stale なら kill_process(実体PID) →
                                    ランチャー親が exit → 既存 poll() が再起動

なぜ mtime 基準か:
  内容のパース失敗や書き込み途中の読み取りを考えなくてよい。ファイル内容は
  診断用 (人間が見たとき用) の time.time() 文字列にすぎない。

なぜ max(mtime, started_at) 基準か:
  (1) 前回稼働の古い .hb が残っていても、新プロセスの起動直後に誤killしない。
  (2) 重量級 import 中 (beat 開始前) は started_at 起点の猶予として働く。
  つまり「起動猶予」を別パラメータにせず threshold ひとつで表現できる。

依存は標準ライブラリのみ (notify_discord 内の requests は遅延 import)。
pid_guard と同じ理由で、単体テストから import できることを優先している。
"""
import os
import signal
import time

# beat 間隔 30秒の10回分。Discord 再接続や一時的な高負荷を余裕で吸収する。
STALE_THRESHOLD = 300

# beat 送出側 (bot / scheduler) の推奨間隔。
BEAT_INTERVAL = 30


def beat(hb_file):
    """heartbeat を更新する。判定は mtime なので内容は診断用。

    送出側のループを絶対に壊さないため、書き込み失敗は握り潰す
    (heartbeat が止まれば watchdog が再起動してくれる、が正しい倒れ方)。
    """
    try:
        with open(hb_file, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
    except OSError:
        pass


def age_seconds(hb_file, started_at, now=None):
    """最後の生存証明からの経過秒数。

    生存証明 = max(heartbeat の mtime, プロセス起動時刻)。
    ファイルが無い/読めない場合は started_at 起点。
    """
    if now is None:
        now = time.time()
    try:
        last = os.path.getmtime(hb_file)
    except OSError:
        last = started_at
    return now - max(last, started_at)


def is_stale(hb_file, started_at, threshold=STALE_THRESHOLD, now=None):
    """heartbeat が threshold 秒以上更新されていなければ True (= ハング疑い)。"""
    return age_seconds(hb_file, started_at, now=now) > threshold


def kill_process(pid):
    """実体プロセスを終了させる。成功で True。

    Windows の os.kill(pid, SIGTERM) は TerminateProcess 相当 (強制終了)。
    死亡済み PID には OSError [WinError 87] が飛ぶ (既知バグパターン) ので捕捉する。
    強制終了では finally が走らず PID ファイルが残るが、pid_guard は
    「所有者死亡 = OS がハンドルを閉じる = 削除可能」で stale を回収できる設計
    なので、次回 acquire() が正しく引き継ぐ。
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False


def notify_discord(message):
    """Watchdog 発動をログチャンネルへ通知する (観測用・失敗しても本体に影響なし)。"""
    token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID")
    if not token or not channel_id:
        return
    try:
        import requests
        requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": message},
            timeout=10,
        )
    except Exception:
        pass
