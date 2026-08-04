# -*- coding: utf-8 -*-
"""
lib/concurrency.py — プロセス内並列実行の設定と資源上限 (Phase C0)

Phase C1 の fan-out (engine_plan_execute が依存の無いステップを同時実行する) が
踏むことになる共有資源を、ここで一箇所に集める。

なぜ fan-out より先にこれを作るか:
  D1 Retriever により **Vault 検索1回あたり最大3回 Ollama を叩く** ようになった。
  6GB VRAM には bge-m3 (1.16GB) と qwen2.5-coder:7b (4.68GB) が同居できず、
  同時に叩くとロードで追い出し合う (PR #23 の実測: 16.2秒 → 6.0秒 の逆流)。
  「並列にしたら遅くなった」を後から切り分けるのは難しいので、上限を先に決める。

このモジュールが**守らないもの** (lib/cost_guard と同じ限界):
  ここにあるのは threading の同期プリミティブなので、**プロセス内でしか効かない**。
  bot_discord / agent_scheduler / main.py は別プロセスで同じ Ollama を共有するため、
  「定時ジョブの埋め込みバッチ」と「/agent の検索」の競合は防げない。
  それが要るようになったらファイルロック (lib/file_lock) へ格上げすること。

設定は import 時ではなく**呼び出し時**に読む (.env を書き換えて再起動するだけで
効かせるため)。os.environ への書き戻しはしない (CLAUDE.md のスコープ階層 LEVEL 2)。
"""
import os
import threading
from contextlib import contextmanager

from dotenv import load_dotenv

from lib.paths import ENV_PATH

# 単独 import されても .env を読めるようにする (load_dotenv は冪等)
load_dotenv(ENV_PATH)


# 既定値。ここを変えるときは tests/test_concurrency.py も一緒に直す。
DEFAULT_FANOUT_WORKERS = 3
DEFAULT_OLLAMA_MAX_CONCURRENCY = 1


def _env_flag(name: str, default: bool = True) -> bool:
    """機能フラグ。未設定なら default、"0"/"false"/"no"/"off" が OFF。

    tools/tool_vault_hybrid._enabled と同じ語彙にしてある (プロジェクト内で
    フラグの書き方が2種類あると、どちらが効くのか調べないと分からなくなる)。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """正の整数設定を読む。未設定/パース不能 → default、minimum 未満 → minimum。

    0 や負値で「無制限」にはしない: 上限を外したい人は大きい数を書く。
    「0 = 無制限」にすると、書き間違いの 0 が黙って上限撤廃になる。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def parallel_enabled() -> bool:
    """fan-out のキルスイッチ。AGENT_PARALLEL=0 で完全に逐次へ戻る。"""
    return _env_flag("AGENT_PARALLEL", default=True)


def fanout_workers() -> int:
    """1ウェーブで同時に走らせるステップ数の上限。"""
    return _env_int("AGENT_FANOUT_WORKERS", DEFAULT_FANOUT_WORKERS)


# --- Ollama 同時リクエスト上限 -------------------------------------------
# セマフォは「最初に必要になった時点の設定値」で作る。実行中に上限を変えられる
# ようにすると、走っているスロットの数と新しい上限が食い違って BoundedSemaphore が
# ValueError を投げる。設定変更はプロセス再起動で反映させる。
_ollama_sem = None
_ollama_sem_lock = threading.Lock()


def _get_ollama_semaphore() -> threading.BoundedSemaphore:
    global _ollama_sem
    if _ollama_sem is None:
        with _ollama_sem_lock:
            if _ollama_sem is None:
                limit = _env_int("OLLAMA_MAX_CONCURRENCY",
                                 DEFAULT_OLLAMA_MAX_CONCURRENCY)
                _ollama_sem = threading.BoundedSemaphore(limit)
    return _ollama_sem


@contextmanager
def ollama_slot():
    """Ollama への HTTP 呼び出しを囲む。既定では同時1本まで。

    埋め込み (tools/tool_vault_index._call_ollama_embed) と
    リランク/書き換え (tools/tool_vault_hybrid._call_ollama_llm) の**両方**が
    これを通ること。片方だけ囲うのは囲っていないのと大差ない
    (奪い合っているのはこの2つのモデルなので)。

    タイムアウトを持たせない理由: 待ち行列で待つ時間は「Ollama が別の要求を
    処理している時間」であって障害ではない。ここで諦めると、呼び出し側が
    リトライして更に列を伸ばす。呼び出し側の requests timeout が本来の防波堤。
    """
    sem = _get_ollama_semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _reset_for_tests() -> None:
    """セマフォを作り直す。テスト専用 (環境変数を差し替えてから呼ぶ)。"""
    global _ollama_sem
    with _ollama_sem_lock:
        _ollama_sem = None
