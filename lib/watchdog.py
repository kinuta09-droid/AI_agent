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

# --- 起動失敗 (crash loop) の検知 ---
# heartbeat が見るのは「上がったプロセスが止まったか」だけで、「そもそも上がって
# こない」は守備範囲外だった。実体が即 exit する場合、スーパーバイザの poll() が
# 5秒毎に再起動を繰り返すだけで、heartbeat 監視には永久に到達しない (stale 判定の
# 前に死亡検知で continue する)。結果、Bot が一度も起動しないまま無言でループする。
# そこで「起動直後の終了」を失敗として数え、間隔を空けつつ通知する。

# 起動からこの秒数未満で終了したら「起動失敗」とみなす。
# watchdog kill による終了は stale 判定 (300秒) を経ているので必ずこれより長く、
# 正常稼働からの kill を起動失敗と数えてしまうことはない。
FAST_EXIT_SECONDS = 60

# 再起動待ちの基準秒 / 上限秒 (連続失敗ごとに倍化)。
RESTART_DELAY = 5
MAX_RESTART_DELAY = 300

# 連続失敗がこの回数に達したら通知する (以降は再通知間隔ごとに繰り返す)。
ALERT_AFTER_FAILURES = 3
_ALERT_REPEAT_EVERY = 10


def is_fast_exit(uptime, threshold=FAST_EXIT_SECONDS):
    """起動から threshold 秒未満での終了か (= 起動失敗の疑い)。"""
    return uptime < threshold


def restart_delay(consecutive_failures,
                  base=RESTART_DELAY, cap=MAX_RESTART_DELAY):
    """連続失敗回数に応じた再起動待ち秒数 (指数バックオフ)。

    失敗が続く原因はたいてい待てば直らないもの (トークン失効・依存の破損) か、
    待てば直るもの (ブート直後でネットワーク未接続) のどちらか。前者で5秒毎に
    再起動を撃ち続けても API を叩き続けるだけなので、間隔を広げて上限で頭打ちに
    する。上限を無限にしないのは、後者が回復したとき自力で復帰させるため。
    """
    if consecutive_failures <= 1:
        return base
    return min(base * (2 ** (consecutive_failures - 1)), cap)


def should_alert(consecutive_failures, after=ALERT_AFTER_FAILURES,
                 repeat_every=_ALERT_REPEAT_EVERY):
    """この失敗回数で通知すべきか。

    1〜2回はブート直後のネットワーク未接続などで珍しくないため黙って再試行し、
    「明らかに自力で復帰していない」回数から通知する。以降は repeat_every 回
    ごとにだけ再通知して、復旧するまで鳴り続けるのを防ぐ。
    """
    if consecutive_failures < after:
        return False
    return (consecutive_failures - after) % repeat_every == 0


def beat(hb_file):
    """heartbeat を更新する。判定は mtime なので内容は診断用。

    親ディレクトリ (logs/) は gitignore されており誰も作らないため、ここで
    作成する。作らないと beat が永久に失敗し「健全なプロセスを watchdog が
    定期処刑し続ける」無限再起動ループになる (grill 2026-07-17 で検出)。

    送出側のループを絶対に壊さないため、失敗は種類を問わず握り潰す
    (heartbeat が止まれば watchdog が再起動してくれる、が正しい倒れ方)。
    """
    try:
        parent = os.path.dirname(hb_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(hb_file, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
    except Exception:
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


def _terminate_via_handle(pid):
    """PROCESS_TERMINATE だけを要求して強制終了する (Windows)。成功で True。

    os.kill が権限で失敗したときの代替。CPython の os.kill は内部で
    OpenProcess(PROCESS_ALL_ACCESS) を要求するため、タスクスケジューラが
    /rl highest で起動した高整合性プロセスを非管理者プロセスから終了させようと
    すると PROCESS_VM_WRITE 等が UIPI に阻まれ WinError 5 になる。
    終了に必要な権限は PROCESS_TERMINATE だけで、これは同一ユーザーであれば
    整合性レベルをまたいでも DACL で許可される。
    """
    if os.name != 'nt':
        return False
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def kill_process(pid):
    """実体プロセスを終了させる。成功で True。

    Windows の os.kill(pid, SIGTERM) は TerminateProcess 相当 (強制終了)。
    死亡済み PID には OSError [WinError 87] が飛ぶ (既知バグパターン) ので捕捉する。
    権限エラー (WinError 5) は「終了できない」ではなく「os.kill の要求権限が
    過剰」なだけのことがあるため、PROCESS_TERMINATE のみで開き直して再挑戦する
    (詳細は _terminate_via_handle)。これが無いと、タスクスケジューラ起動の
    スタックを scripts/arms_ctl.py から停止できない。
    強制終了では finally が走らず PID ファイルが残るが、pid_guard は
    「所有者死亡 = OS がハンドルを閉じる = 削除可能」で stale を回収できる設計
    なので、次回 acquire() が正しく引き継ぐ。
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except PermissionError:
        return _terminate_via_handle(pid)
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
