# -*- coding: utf-8 -*-
"""
X投稿承認キューの状態管理ストア (JSON永続、固定5スロット)。

状態遷移: draft → pending_approval → posting → (posted | failed | needs_review)
「却下」は release_slot でスロットを解放し終端 (6状態には含めない)。
try_transition が現在stateとの一致を確認してから更新する CAS 方式のため、
ボタン連打・Bot再起動後の再クリックによる二重投稿はここで遮断される。
"""
import os
import threading
import uuid
import datetime

from lib.paths import LOGS_DIR

QUEUE_PATH = os.path.join(LOGS_DIR, "x_post_queue.json")
X_POST_SLOTS = 5

_lock = threading.Lock()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _fresh() -> dict:
    return {"slots": {str(i): None for i in range(X_POST_SLOTS)}}


def _load() -> dict:
    from lib.lib_shared import load_json
    data = load_json(QUEUE_PATH, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
        return _fresh()
    slots = data["slots"]
    for i in range(X_POST_SLOTS):
        slots.setdefault(str(i), None)
    data["slots"] = slots
    return data


def _save(data: dict) -> None:
    from lib.lib_shared import save_json
    save_json(QUEUE_PATH, data)


def allocate_slot(text: str, source_note: str):
    """空きスロットに下書き(status=draft)を登録する。空きが無ければ (None, None)。

    戻り値: (slot: int, post_id: str) または (None, None)
    """
    with _lock:
        data = _load()
        for i in range(X_POST_SLOTS):
            key = str(i)
            if data["slots"][key] is None:
                post_id = uuid.uuid4().hex
                now = _now()
                data["slots"][key] = {
                    "post_id": post_id, "status": "draft", "text": text,
                    "source_note": source_note, "created_at": now, "updated_at": now,
                    "tweet_url": None, "error_detail": None, "obsidian_synced": False,
                }
                _save(data)
                return i, post_id
        return None, None


def get_slot(slot: int) -> dict | None:
    with _lock:
        return _load()["slots"].get(str(slot))


def update_text(slot: int, new_text: str) -> bool:
    """pending_approval中のスロットのみ本文を上書きできる (再承認必須はボタン運用側で担保)。"""
    with _lock:
        data = _load()
        entry = data["slots"].get(str(slot))
        if entry is None or entry["status"] != "pending_approval":
            return False
        entry["text"] = new_text
        entry["updated_at"] = _now()
        _save(data)
        return True


def try_transition(slot: int, expected_status: str, new_status: str, **fields) -> bool:
    """現在のstatusがexpected_statusと一致する場合のみ new_status へ更新する (CAS)。

    二重投稿防止の要: ボタン連打・Bot再起動後の再クリックはここで弾かれる。
    fields で tweet_url/error_detail 等を同時に書き込める。
    """
    with _lock:
        data = _load()
        entry = data["slots"].get(str(slot))
        if entry is None or entry["status"] != expected_status:
            return False
        entry["status"] = new_status
        entry["updated_at"] = _now()
        entry.update(fields)
        _save(data)
        return True


def release_slot(slot: int) -> None:
    with _lock:
        data = _load()
        data["slots"][str(slot)] = None
        _save(data)


def mark_obsidian_synced(slot: int) -> None:
    with _lock:
        data = _load()
        entry = data["slots"].get(str(slot))
        if entry is not None:
            entry["obsidian_synced"] = True
            entry["updated_at"] = _now()
            _save(data)


def recover_stuck_posting() -> list:
    """起動時に呼ぶ。posting のまま残っているスロットを needs_review へ強制遷移し、対象slot番号を返す。

    Bot異常終了 (クラッシュ) 直前に投稿処理中だったスロットは成否不明のまま
    posting で固まる。再起動後にそのまま承認ボタンの再クリックを受け付けると
    二重投稿になりうるため、起動時に needs_review へ倒して自動再送を止める。
    """
    with _lock:
        data = _load()
        recovered = []
        for i in range(X_POST_SLOTS):
            entry = data["slots"][str(i)]
            if entry is not None and entry["status"] == "posting":
                entry["status"] = "needs_review"
                entry["error_detail"] = "Bot再起動により投稿成否不明 (要確認)"
                entry["updated_at"] = _now()
                recovered.append(i)
        if recovered:
            _save(data)
        return recovered
