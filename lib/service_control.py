# -*- coding: utf-8 -*-
"""
lib/service_control.py — 常駐サービスの状態照会と再起動要求

なぜ独立モジュールなのか:
  サービス表 (script / PID / heartbeat のパス) は main.py のスーパーバイザにしか
  無かったが、同じ情報を Discord Bot (/status, /restart) と制御スクリプト
  (scripts/arms_ctl.py) からも参照したい。3箇所に写すとパスがずれた瞬間に
  「殺すべき PID を見失う」「heartbeat を見に行く先を間違える」が起きるため、
  定義をここ1箇所に集約して全員が import する。

  pid_guard / watchdog と同じく **標準ライブラリのみ** に依存する。discord や
  gemini を持ち込むと、スーパーバイザとユニットテストの両方が重くなる。

役割分担 (再起動の流れ):
    /restart or arms_ctl → request_restart(name)
        → logs/<name>.restart を書く (意図的な再起動である印)
        → 実体 PID を kill
    main.py スーパーバイザ → poll() が死亡を検知
        → consume_restart_flag(name) が True なら失敗カウントをリセット
        → 5秒後に新しいプロセスとして再生成 (= 新しいコードで上がり直す)

なぜフラグファイルが要るのか:
  スーパーバイザから見ると意図的な kill も異常終了も同じ「子の死亡」でしかない。
  そのまま数えると、起動から 60秒 (watchdog.FAST_EXIT_SECONDS) 以内の再起動が
  crash loop と誤判定され、指数バックオフと「🚨 起動失敗」通知が誤発火する。
  フラグは「これは人間が意図した終了だ」という1ビットの申し送りにすぎない。
"""
import os
import time

from lib import watchdog
from lib.pid_guard import is_process_alive, read_owner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")

# スーパーバイザ (main.py) 自身のロック。
# 子と違い、これを殺すと誰も再生成しない (タスクスケジューラは onlogon のため
# 次のログオンまで走らない) ので、Discord からの再起動対象には含めない。
LAUNCHER_PID_FILE = os.path.join(ROOT, "arms_launcher.pid")

# 常駐サービスの定義。main.py / bot / arms_ctl の全員がここを参照する。
# hb_file / pid_file は各サービス側の定義 (bot_discord.HEARTBEAT_FILE 等) と
# 一致していること。import で揃えないのは上記のとおり依存を持ち込まないため。
SERVICES = {
    "discord_bot": {
        "label": "Discord Bot",
        "script": "bot_discord.py",
        "hb_file": os.path.join(LOG_DIR, "bot_discord.hb"),
        "pid_file": os.path.join(ROOT, "bot_discord.pid"),
        "restart_flag": os.path.join(LOG_DIR, "discord_bot.restart"),
    },
    "scheduler": {
        "label": "Scheduler",
        "script": "agent_scheduler.py",
        "hb_file": os.path.join(LOG_DIR, "agent_scheduler.hb"),
        "pid_file": os.path.join(ROOT, "agent_scheduler.pid"),
        "restart_flag": os.path.join(LOG_DIR, "scheduler.restart"),
    },
}

# Discord コマンドの引数など、人間向けの短い名前から内部名への対応。
ALIASES = {"bot": "discord_bot", "discord": "discord_bot", "sched": "scheduler"}


def resolve(name):
    """別名を内部のサービス名に正規化する。未知の名前はそのまま返す。"""
    return ALIASES.get(name, name)


def _service(name):
    key = resolve(name)
    if key not in SERVICES:
        raise KeyError(f"unknown service: {name}")
    return key, SERVICES[key]


# ---------------------------------------------------------
# 状態照会
# ---------------------------------------------------------

def launcher_status():
    """スーパーバイザ (main.py) の状態を返す。"""
    pid = read_owner(LAUNCHER_PID_FILE)
    return {"name": "launcher", "label": "Launcher (main.py)",
            "pid": pid, "alive": is_process_alive(pid) if pid else False}


def launcher_alive():
    """スーパーバイザが生きているか。

    子を落として再生成させる操作は、これが True でないと「落としただけ」に
    なるため、再起動系の操作は必ず事前に確認すること。
    """
    return launcher_status()["alive"]


def status(name, now=None):
    """サービス1件の状態。

    hb_age は「heartbeat が最後に更新されてからの秒数」。プロセス起動時刻を
    知らない外部プロセスから呼ばれるため、watchdog.age_seconds の started_at には
    0 を渡して mtime のみを見る (起動猶予の判定はスーパーバイザの担当)。
    heartbeat ファイルが無い場合は None (= 未知) とし、巨大な秒数を報告しない。
    """
    key, svc = _service(name)
    if now is None:
        now = time.time()
    pid = read_owner(svc["pid_file"])
    alive = is_process_alive(pid) if pid else False
    hb_age = None
    if os.path.exists(svc["hb_file"]):
        hb_age = watchdog.age_seconds(svc["hb_file"], 0, now=now)

    if not alive:
        state = "stopped"
    elif hb_age is None or hb_age > watchdog.STALE_THRESHOLD:
        state = "stale"       # 生きているが heartbeat が止まっている = ハング疑い
    else:
        state = "running"

    return {"name": key, "label": svc["label"], "pid": pid,
            "alive": alive, "hb_age": hb_age, "state": state}


def all_status(now=None):
    """全サービスの状態 (SERVICES の定義順)。"""
    return [status(name, now=now) for name in SERVICES]


_STATE_ICON = {"running": "🟢", "stale": "🟡", "stopped": "🔴"}


def format_status_lines(now=None):
    """/status と `arms status` で共有する表示行を返す (list[str])。

    表示を1箇所にまとめるのは、Discord とコンソールで見え方が食い違うと
    「どちらが本当か」を確かめる手間が増えるため。
    """
    lines = []
    lch = launcher_status()
    if lch["alive"]:
        lines.append(f"🟢 {lch['label']}: running (PID {lch['pid']})")
    else:
        lines.append(f"🔴 {lch['label']}: not running "
                     f"— 子を再起動しても上がってきません")

    for st in all_status(now=now):
        icon = _STATE_ICON.get(st["state"], "❔")
        if st["alive"]:
            hb = "heartbeat 不明" if st["hb_age"] is None else f"heartbeat {int(st['hb_age'])}s前"
            note = " ⚠️ ハング疑い" if st["state"] == "stale" else ""
            lines.append(f"{icon} {st['label']}: {st['state']} "
                         f"(PID {st['pid']}, {hb}){note}")
        else:
            lines.append(f"{icon} {st['label']}: stopped")
    return lines


# ---------------------------------------------------------
# 再起動要求
# ---------------------------------------------------------

# 再起動フラグの有効期限。停止まで数秒しかかからないので短くてよい。
# 長くすると「立てたが落ちなかった」フラグが後日のクラッシュを覆い隠す。
FLAG_MAX_AGE = 300


def set_restart_flag(name):
    """意図的な再起動であることを示すフラグを置く。

    logs/ は gitignore 済みで誰も作らないことがあるため、ここで作成する
    (watchdog.beat と同じ理由)。
    """
    _key, svc = _service(name)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(svc["restart_flag"], 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
        return True
    except OSError:
        return False


def consume_restart_flag(name, max_age=FLAG_MAX_AGE, now=None):
    """フラグがあれば削除して True を返す (スーパーバイザが1回だけ消費する)。

    「読んで消す」を1関数にまとめているのは、消し忘れると次の異常終了まで
    「意図的」と誤認され続け、本物の crash loop を見逃すため。

    期限切れ (max_age 超) は削除するが False を返す。フラグを立てた直後に
    停止が失敗する経路 (Discord への応答送信エラー等) があり、置き去りの
    フラグが何時間も後の**本物のクラッシュ**を「意図的な再起動」と誤読して
    crash loop 検知を無効化してしまうため、鮮度で足切りする。
    """
    _key, svc = _service(name)
    path = svc["restart_flag"]
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(path)
    except OSError:
        return False          # フラグ無し = 通常の異常終了
    try:
        os.remove(path)
    except OSError:
        pass                  # 消せなくても判定は age で確定している
    return 0 <= age <= max_age


# 再起動を要求した側 (Discord) が「どこへ完了を報告すべきか」を書き置くファイル。
# Bot は自分を閉じて別プロセスとして上がり直すため、メモリ上の interaction は
# 引き継げない。次の起動時にこれを読んで完了メッセージを投稿する。
RESTART_NOTICE_FILE = os.path.join(LOG_DIR, "restart_notice.json")

# 書き置きの有効期限。PC 電源断などで何時間も後に起動した場合、
# 「再起動が完了しました」を今さら投稿しても混乱するだけなので捨てる。
NOTICE_MAX_AGE = 600


def write_restart_notice(channel_id, target, requested_at=None):
    """再起動完了の報告先を書き置く。成功で True。"""
    import json
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        payload = {
            "channel_id": int(channel_id),
            "target": target,
            "requested_at": time.time() if requested_at is None else requested_at,
        }
        with open(RESTART_NOTICE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return True
    except (OSError, TypeError, ValueError):
        return False


def pop_restart_notice(max_age=NOTICE_MAX_AGE, now=None):
    """書き置きを読んで削除する。無い/古い/壊れている場合は None。

    読んだら必ず消すのは、再接続で on_ready が再発火するたびに
    「再起動が完了しました」を投稿してしまわないため。
    """
    import json
    try:
        with open(RESTART_NOTICE_FILE, encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.remove(RESTART_NOTICE_FILE)
        except OSError:
            pass

    try:
        requested_at = float(payload["requested_at"])
        channel_id = int(payload["channel_id"])
    except (KeyError, TypeError, ValueError):
        return None

    elapsed = (time.time() if now is None else now) - requested_at
    if elapsed < 0 or elapsed > max_age:
        return None
    payload["elapsed"] = elapsed
    payload["channel_id"] = channel_id
    return payload


def request_restart(name):
    """サービスの再起動を要求する。戻り値: (成功したか, 人間向けメッセージ)。

    実体を殺すだけで、再生成はスーパーバイザに任せる (二重に起動役を作らない)。
    kill は watchdog.kill_process をそのまま使う — Windows で死亡済み PID に
    OSError [WinError 87] が飛ぶ既知の罠の対処がそちらに入っている。
    """
    key, svc = _service(name)
    label = svc["label"]

    if not launcher_alive():
        return False, (f"❌ {label}: スーパーバイザ (main.py) が動いていないため中止しました。"
                       f"落とすと誰も再起動しません。`arms.bat start` で起動してください。")

    pid = read_owner(svc["pid_file"])
    if pid is None or not is_process_alive(pid):
        # 既に死んでいる = スーパーバイザが数秒後に拾って再生成する。
        # フラグは残さない (次の異常終了を誤って「意図的」と読ませないため)。
        return True, f"ℹ️ {label}: 既に停止しています。スーパーバイザが再起動します。"

    set_restart_flag(key)
    if watchdog.kill_process(pid):
        return True, f"♻️ {label} (PID {pid}) を停止しました。約5秒後に新しいコードで起動します。"

    consume_restart_flag(key)   # 殺せなかったフラグは残さない
    return False, f"❌ {label} (PID {pid}) の停止に失敗しました。"


def stop_all():
    """スーパーバイザ → 子 の順に停止する (`arms stop` 用)。戻り値: list[str]。

    順序が重要: 先に子を殺すとスーパーバイザが即座に再生成してしまい、
    「止めたつもりが止まっていない」状態になる。
    """
    messages = []
    lch = launcher_status()
    if lch["alive"]:
        ok = watchdog.kill_process(lch["pid"])
        messages.append(f"{'🛑' if ok else '❌'} Launcher (PID {lch['pid']}) "
                        f"{'を停止しました' if ok else 'の停止に失敗しました'}")
    else:
        messages.append("ℹ️ Launcher は動いていません")

    for st in all_status():
        if st["alive"]:
            ok = watchdog.kill_process(st["pid"])
            messages.append(f"{'🛑' if ok else '❌'} {st['label']} (PID {st['pid']}) "
                            f"{'を停止しました' if ok else 'の停止に失敗しました'}")
        else:
            messages.append(f"ℹ️ {st['label']} は動いていません")

    # 停止操作の途中で置かれたフラグを残さない (次回起動時の誤読を防ぐ)
    for name in SERVICES:
        consume_restart_flag(name)
    return messages
