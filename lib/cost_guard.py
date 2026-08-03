# -*- coding: utf-8 -*-
"""
API Cost Guard (Tier2-b)

日次のAPI呼び出し回数をファイル永続カウンタで追跡し、上限超過時に
GeminiBudgetExceeded を送出するセーフティネット。メータリングではないため
トークン計測・モデル別予算は扱わない。

設計メモ:
- bot_discord / agent_scheduler / main.py は別プロセスで同一APIキーを共有する
  ため、カウンタはインメモリ不可・ファイル永続必須。
- プロセス内の並行スレッド (asyncio.to_thread 経由の同時 /agent) は
  threading.Lock で直列化する。プロセス間の load→increment→save は
  非直列のため数件の lost-update が起きうるが、セーフティネット用途では
  ±数回の誤差は許容する (厳密な課金メータではない)。
- ガード自身の障害 (カウンタファイル破損・保存失敗・通知失敗) で
  本体のAPI呼び出しを止めてはならない。破損は count 0 扱いで続行する。
"""
import os
import math
import datetime
import threading

from dotenv import load_dotenv

from lib.paths import ENV_PATH, LOGS_DIR

# lib_shared より先に import されても上限値の env を読めるようにする (冪等)
load_dotenv(ENV_PATH)

# 未設定/パース不能 → デフォルト200。0以下 → 0 (ガード無効 = 無制限、カウントのみ継続)
DEFAULT_DAILY_LIMIT = 200
WARN_RATIO = 0.8

GEMINI_COUNTER_PATH = os.path.join(LOGS_DIR, "gemini_daily_calls.json")


class GeminiBudgetExceeded(Exception):
    """日次API呼び出し上限超過。lib_shared.retry() は再試行せず即座に再送出する。

    既存コードの広域 except Exception は str(e) をそのままユーザーへ表示する
    設計のため、__str__ は完成された日本語の文言にしておく。
    """

    def __init__(self, name: str, limit: int):
        self.name = name
        self.limit = limit
        super().__init__(
            f"⛔ 本日の{name}呼び出し上限({limit}回)に達しました。"
            f"明日0時にリセットされます。"
        )


def get_limit(env_var: str, default: int) -> int:
    """環境変数から日次上限を読む。未設定/パース不能→default、負値→0(無効)。"""
    raw = os.getenv(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)  # 負値は 0 (無効) に丸める


class DailyCounter:
    """日次呼び出しカウンタ (ファイル永続・日付ロールオーバー付き)。

    ファイル形式: {"date": "YYYY-MM-DD", "count": N,
                   "warned": bool, "limit_notified": bool}
    warned / limit_notified フラグをファイル側に置くことで、
    複数プロセス横断でも通知が1日1回に収まる。
    """

    def __init__(self, path: str, limit: int, name: str = "Gemini API",
                 warn_ratio: float = WARN_RATIO, notify: bool = True):
        self.path = path
        self.limit = limit
        self.name = name
        self.warn_ratio = warn_ratio
        self.notify = notify
        self._lock = threading.Lock()

    def _fresh(self, today: str) -> dict:
        return {"date": today, "count": 0, "warned": False, "limit_notified": False}

    def _load(self, today: str) -> dict:
        # lib_shared が本モジュールを import するため、循環回避で遅延 import
        from lib.lib_shared import load_json
        data = load_json(self.path, default={})
        if not isinstance(data, dict) or data.get("date") != today:
            # ファイル欠落/破損/日付ロールオーバー → 本日分としてリセット
            return self._fresh(today)
        data.setdefault("count", 0)
        data.setdefault("warned", False)
        data.setdefault("limit_notified", False)
        # bool は int のサブクラスなので明示除外 (count: true が 1 扱いで素通りする穴)
        if (not isinstance(data["count"], int) or isinstance(data["count"], bool)
                or data["count"] < 0):
            return self._fresh(today)
        return data

    def _save(self, data: dict) -> None:
        from lib.lib_shared import save_json
        try:
            save_json(self.path, data)
        except Exception as e:
            # ガードの保存失敗で本体を止めない (カウント精度は劣化するが続行)
            print(f"[cost_guard] ⚠️ counter save failed ({self.path}): {e}")

    def _notify(self, message: str) -> None:
        if not self.notify:
            return
        try:
            from lib.watchdog import notify_discord
            notify_discord(message)
        except Exception:
            pass  # 通知は観測用。失敗しても本体に影響させない

    def current(self) -> int:
        """現在の本日カウントを返す (参照のみ、加算しない)。"""
        today = datetime.date.today().isoformat()
        with self._lock:
            return self._load(today)["count"]

    def check_and_increment(self, n: int = 1) -> int:
        """上限チェック→加算→保存。超過時は加算せず GeminiBudgetExceeded を送出。

        limit <= 0 の場合は無制限 (キルスイッチ)。カウントだけは継続し、
        使用量データの蓄積は止めない。戻り値は加算後のカウント。
        """
        today = datetime.date.today().isoformat()
        pending_notice = None
        exceeded = False
        with self._lock:
            data = self._load(today)

            if self.limit > 0 and data["count"] + n > self.limit:
                exceeded = True
                if not data["limit_notified"]:
                    data["limit_notified"] = True
                    self._save(data)
                    pending_notice = (
                        f"⛔ {self.name}が日次上限({self.limit})に到達。"
                        f"以降の呼び出しは明日までブロックされます。"
                    )
            else:
                data["count"] += n
                if (self.limit > 0 and not data["warned"]
                        and data["count"] >= math.ceil(self.limit * self.warn_ratio)):
                    data["warned"] = True
                    pending_notice = (
                        f"⚠️ {self.name}使用量が本日上限の{int(self.warn_ratio * 100)}%に"
                        f"到達 ({data['count']}/{self.limit})"
                    )
                self._save(data)
                count = data["count"]

        # HTTP通知はロック外で行い、他スレッドのAPI呼び出しを塞がない
        if pending_notice:
            self._notify(pending_notice)
        if exceeded:
            raise GeminiBudgetExceeded(self.name, self.limit)
        return count


# --- グローバル日次カウンタ (Gemini API 共有client用シングルトン) ----------
GEMINI_COUNTER = DailyCounter(
    path=GEMINI_COUNTER_PATH,
    limit=get_limit("DAILY_GEMINI_CALL_LIMIT", DEFAULT_DAILY_LIMIT),
    name="Gemini API",
)

# --- Crypto Watcher 分析回数カウンタ (Phase 2) -----------------------------
# 15分ウォッチャーが起動するフル分析 (tool_analyze_crypto = Gemini+Tavily) の
# 日次上限。Gemini全体予算(200)とは別枠で先に止めることで、相場急変日に
# ウォッチャーが予算を食い潰して定時ジョブを道連れにするのを防ぐ。
WATCHER_COUNTER_PATH = os.path.join(LOGS_DIR, "crypto_watcher_daily.json")
WATCHER_COUNTER = DailyCounter(
    path=WATCHER_COUNTER_PATH,
    limit=get_limit("DAILY_WATCHER_ANALYSIS_LIMIT", 8),
    name="Crypto Watcher分析",
)


# --- Crypto Sweep 分析回数カウンタ (Phase 3) -------------------------------
# 4h足クローズ毎の全銘柄スイープ自体は Binance 公開APIのみで課金ゼロだが、
# BUY系候補が出た銘柄には tool_analyze_crypto (Gemini+Tavily) を起動する。
# 相場全体が同時に売られた日は候補が20銘柄同時に立ちうるため、ウォッチャーと
# 同様に別枠の日次上限で先に止める。
SWEEP_COUNTER_PATH = os.path.join(LOGS_DIR, "crypto_sweep_daily.json")
SWEEP_COUNTER = DailyCounter(
    path=SWEEP_COUNTER_PATH,
    limit=get_limit("DAILY_SWEEP_ANALYSIS_LIMIT", 6),
    name="Crypto Sweep分析",
)


# --- Google Search Grounding 呼び出し回数カウンタ ---------------------------
# tool_web_search_grounded (Gemini 3ファミリー共有の無料枠5,000プロンプト/月に
# 対する安全弁)。既定30/日は実際の利用見込み(1日数回)より大きく余裕を持たせてある。
GROUNDING_COUNTER_PATH = os.path.join(LOGS_DIR, "grounding_daily_calls.json")
GROUNDING_COUNTER = DailyCounter(
    path=GROUNDING_COUNTER_PATH,
    limit=get_limit("DAILY_GROUNDING_CALL_LIMIT", 30),
    name="Google Search Grounding",
)


def check_and_increment(n: int = 1) -> int:
    return GEMINI_COUNTER.check_and_increment(n)
