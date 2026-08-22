# -*- coding: utf-8 -*-
"""
tools/tool_contradiction_resolve.py — 矛盾の能動解決ワークフロー

Vault Insight Watcher (tools/tool_vault_watcher.py) が検知した矛盾を、
00_System/Vault_Contradictions.md への記録に加えて Discord の裁定ボタン
(A採用/B採用/両論併記) で即応できるようにする。

構成:
  queue_contradiction()        — check_vault_insights から検知直後に呼ばれる
  tool_resolve_contradiction() — Discordボタンから呼ぶ (裁定確定・両ノートへ反映)

tool_note_promotion.py と同じ「検出→pending.jsonにslot登録→個別メッセージで
ボタン通知→bot側の永続Viewがクリック処理」の型を踏襲する。
"""
import datetime
import json
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import LOGS_DIR
from lib.lib_shared import find_file_in_vault, save_json
from lib.file_lock import FileLock
from lib.lib_obsidian_utils import build_contradiction_names_and_links
from lib.bot_errors import is_failure_result
from tools.tool_vault_watcher import CONTRADICTIONS_NOTE
from tools.tool_obsidian import tool_update_note, _set_frontmatter_field

PENDING_PATH = os.path.join(LOGS_DIR, "contradiction_pending.json")
CONTRADICTION_SLOTS = 5

_VERDICTS = ("a", "b", "both")


# ---------------------------------------------------------
# pending (審議中) キャッシュ
# ---------------------------------------------------------

def _load_json_safe(path: str) -> dict:
    """壊れたキャッシュは常に「空」として扱う (tool_note_promotion と同じ安全弁)。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_pending(pending_path: str = PENDING_PATH) -> dict:
    return _load_json_safe(pending_path)


def _save_pending(data: dict, pending_path: str = PENDING_PATH) -> None:
    save_json(pending_path, data)


# ---------------------------------------------------------
# 検知直後のキュー登録 (Discord通知)
# ---------------------------------------------------------

def _default_notify(message: str, components=None) -> None:
    from agent_scheduler import send_discord_notification
    send_discord_notification(message, components=components)


def _notify_contradiction(slot: int, entry: dict, notify_fn) -> None:
    from agent_scheduler import _button_row
    message = (
        f"⚡ **矛盾を検知**: {entry['own_link']} ⇔ {entry['related_link']}\n"
        f"> {entry['insight']}\n\n"
        f"どちらを採用しますか?"
    )
    if len(message) > 1900:
        message = message[:1900] + "…"
    components = _button_row([
        (f"🅰️ {entry['own_name']} を採る", f"contra_resolve:{slot}:a"),
        (f"🅱️ {entry['related_name']} を採る", f"contra_resolve:{slot}:b"),
        ("🤝 両論併記", f"contra_resolve:{slot}:both"),
    ])
    notify_fn(message, components)


def queue_contradiction(relpath: str, title: str, result: dict,
                         pending_path: str = PENDING_PATH, notify_fn=None) -> None:
    """check_vault_insights から検知直後に呼ばれる。

    related_note はLLM自由記述で形式が保証されないため、Vault内の実ファイルに
    解決できないケースがあり得る。その場合はボタン通知をスキップし、
    Markdownログ (_default_write_contradiction) のみで現状維持する
    (fail-safe、既存動作を後退させない)。空きスロットが無い場合も同様。
    """
    notify_fn = notify_fn or _default_notify
    own_name, own_link, related_name, related_link = build_contradiction_names_and_links(
        relpath, result.get("related_note", ""))
    if not related_name or not find_file_in_vault(related_name):
        print(f"[ContradictionResolve] ⚠️ related_note を実ファイルに解決できず、"
              f"裁定ボタンをスキップします: {result.get('related_note', '')!r}")
        return

    timestamp = result.get("_timestamp") or datetime.datetime.now().isoformat(timespec="seconds")
    with FileLock(pending_path):
        pending = _load_pending(pending_path)
        used_slots = {int(k) for k in pending}
        free_slots = [i for i in range(CONTRADICTION_SLOTS) if i not in used_slots]
        if not free_slots:
            print("[ContradictionResolve] ⚠️ pendingスロットが満杯のため、裁定ボタンをスキップします")
            return
        slot = free_slots[0]
        entry = {
            "own_name": own_name,
            "related_name": related_name,
            "own_link": own_link,
            "related_link": related_link,
            "insight": result.get("insight", ""),
            "timestamp": timestamp,
        }
        # 通知が成功してから確定保存する。逆順 (先に保存→後で通知) だと、Discord
        # 送信失敗時に pending だけ埋まってボタンの無いスロットが永久にリークし、
        # 以降の矛盾が「スロット満杯」で拾われなくなる (tool_note_promotion の
        # 「全件成功してから一括保存」と同じ理由をここでも適用する)。
        _notify_contradiction(slot, entry, notify_fn)
        pending[str(slot)] = entry
        _save_pending(pending, pending_path)


# ---------------------------------------------------------
# 裁定確定 (Discordボタンから)
# ---------------------------------------------------------

def _apply_verdict(own_path: str, related_path: str, own_name: str, related_name: str,
                    verdict: str, resolved_at: str) -> None:
    """両ノートへ frontmatter + 追記で裁定結果を反映する。

    tool_update_note はツール層規約により例外を投げず "Error: ..." を返すだけ
    なので、戻り値を見ずに呼び捨てると本文追記の失敗が静かに握りつぶされる
    (Known Bug Patterns A と同型)。失敗があれば RuntimeError にして呼び出し元
    (tool_resolve_contradiction) の except に拾わせ、"Error: ..." として返す。
    """
    for path in (own_path, related_path):
        _set_frontmatter_field(path, "contradiction_status", "resolved")
        _set_frontmatter_field(path, "contradiction_resolved_at", resolved_at)

    if verdict == "both":
        _set_frontmatter_field(own_path, "contradiction_verdict", "both")
        _set_frontmatter_field(related_path, "contradiction_verdict", "both")
        results = [
            tool_update_note(own_name, f"→ 両論併記として整理: [[{related_name}]] も参照 ({resolved_at})"),
            tool_update_note(related_name, f"→ 両論併記として整理: [[{own_name}]] も参照 ({resolved_at})"),
        ]
    else:
        if verdict == "a":
            adopted_path, adopted_name = own_path, own_name
            superseded_path, superseded_name = related_path, related_name
        else:  # "b"
            adopted_path, adopted_name = related_path, related_name
            superseded_path, superseded_name = own_path, own_name

        _set_frontmatter_field(adopted_path, "contradiction_verdict", "adopted")
        _set_frontmatter_field(superseded_path, "contradiction_verdict", "superseded")
        results = [
            tool_update_note(superseded_name,
                              f"→ [[{adopted_name}]] の内容が優先されました ({resolved_at})"),
            tool_update_note(adopted_name,
                              f"→ [[{superseded_name}]] との矛盾を解消し、この内容を優先しました ({resolved_at})"),
        ]

    failed = [r for r in results if is_failure_result(r)]
    if failed:
        raise RuntimeError(f"本文追記に失敗しました: {failed}")


def _mark_contradiction_resolved(own_link: str, related_link: str, detected_at: str,
                                  verdict: str, resolved_at: str,
                                  contradictions_path: str = None) -> None:
    """Vault_Contradictions.md の該当行を [x] に書き換え、裁定結果を追記する。

    detected_at(検知時のtimestamp)+own_link+related_link の組で一意に特定する。
    複数一致 (秒精度の衝突) 時は最初の1件だけ書き換える (全置換はしない、fail-safe)。
    見つからなければ (手動で行が編集/削除された等) 何もせず静かに戻る — 裁定そのものは
    ノート側に反映済みなので、ここが失敗しても呼び出し元をError化しない。
    """
    contradictions_path = contradictions_path or CONTRADICTIONS_NOTE
    if not os.path.exists(contradictions_path):
        return
    with open(contradictions_path, encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(
        r'^- \[ \] resolved \| ' + re.escape(detected_at) + r' \| '
        + re.escape(own_link) + r' ⇔ ' + re.escape(related_link) + r'$',
        re.MULTILINE)
    verdict_label = {"a": "A採用", "b": "B採用", "both": "両論併記"}.get(verdict, verdict)
    replacement = (f'- [x] resolved | {detected_at} | {own_link} ⇔ {related_link}\n'
                   f'  → 裁定: {verdict_label} ({resolved_at})')

    new_content, n = pattern.subn(replacement, content, count=1)
    if n == 0:
        print(f"[ContradictionResolve] ⚠️ Vault_Contradictions.md内で該当行が見つかりません"
              f" (detected_at={detected_at})")
        return
    with open(contradictions_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def tool_resolve_contradiction(slot: int, verdict: str, pending_path: str = PENDING_PATH) -> str:
    """裁定ボタンから呼ぶ。verdict ∈ {"a", "b", "both"}。

    pending.pop の確定を FileLock で保護する (tool_promote_cluster と同じ理由:
    別slotをほぼ同時に裁定されたとき後勝ちの保存で一方が消えるのを防ぐ)。pop さえ
    確定してしまえば、以降の重い処理 (ファイルI/O) はこの slot を他プロセスが
    触ることは無いのでロック外で行ってよい。
    """
    if verdict not in _VERDICTS:
        return f"Error: 不正な裁定です: {verdict}"

    with FileLock(pending_path):
        pending = _load_pending(pending_path)
        entry = pending.pop(str(slot), None)
        if entry is not None:
            _save_pending(pending, pending_path)
    if entry is None:
        return "Error: この矛盾は既に処理済みか見つかりません"

    try:
        own_path = find_file_in_vault(entry["own_name"])
        related_path = find_file_in_vault(entry["related_name"])
        if not own_path or not related_path:
            missing = entry["own_name"] if not own_path else entry["related_name"]
            return f"Error: ノートが見つかりません: {missing}"

        resolved_at = datetime.datetime.now().isoformat(timespec="seconds")
        _apply_verdict(own_path, related_path, entry["own_name"], entry["related_name"],
                        verdict, resolved_at)
        _mark_contradiction_resolved(entry["own_link"], entry["related_link"],
                                      entry["timestamp"], verdict, resolved_at)
    except (OSError, RuntimeError) as e:
        return f"Error: ノートへの反映に失敗しました: {e}"

    verdict_label = {"a": entry["own_name"], "b": entry["related_name"], "both": "両論併記"}[verdict]
    return f"矛盾を解決しました: {verdict_label} ({entry['own_link']} ⇔ {entry['related_link']})"


if __name__ == "__main__":
    # ponytail: verdict分岐とMarkdown行の正規表現置換の最小セルフチェック。
    # フルテストは tests/test_contradiction_resolve.py 側。
    assert _VERDICTS == ("a", "b", "both")

    sample = (
        "# Vault Contradictions\n\n"
        "- [ ] resolved | 2026-08-22T10:00:00 | [[A]] ⇔ [[B]]\n"
        "  A と B が矛盾しています\n"
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Vault_Contradictions.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(sample)
        _mark_contradiction_resolved(
            "[[A]]", "[[B]]", "2026-08-22T10:00:00", "a", "2026-08-22T11:00:00",
            contradictions_path=path)
        with open(path, encoding="utf-8") as f:
            updated = f.read()
        assert "- [x] resolved | 2026-08-22T10:00:00 | [[A]] ⇔ [[B]]" in updated
        assert "→ 裁定: A採用 (2026-08-22T11:00:00)" in updated
        assert "  A と B が矛盾しています" in updated, "元の所見行は残ること"

    print("✅ tool_contradiction_resolve self-check passed")
