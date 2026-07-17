# -*- coding: utf-8 -*-
"""
learning_db の耐久化・ローテーション・採番のユニットテスト。

一時ディレクトリを作り engine_plan_execute の LEARNING_DB_PATH /
LEARNING_DB_ARCHIVE_PATH を差し替えて実行するため、実 learning_db.json や
archive には一切触れない。差し替えは _temp_db() が finally で必ず復元する
(runner 依存にすると pytest 実行時に復元が走らず後続テストを汚染するため)。

実行: py tests/test_learning_db_rotation.py
"""
import os
import sys
import json
import shutil
import tempfile
from contextlib import contextmanager

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as eng
from lib.lib_shared import save_json


@contextmanager
def _temp_db(initial: dict = None):
    """一時DB環境を作り、engine のパス定数を差し替えて必ず復元する。

    yield されるのは (db_path, archive_path)。initial があれば db_path に書く。
    """
    root = tempfile.mkdtemp(prefix="ldb_test_")
    db_path = os.path.join(root, "learning_db.json")
    archive_path = os.path.join(root, "learning_db_archive.jsonl")
    orig_db = eng.LEARNING_DB_PATH
    orig_archive = eng.LEARNING_DB_ARCHIVE_PATH
    try:
        if initial is not None:
            with open(db_path, 'w', encoding='utf-8') as f:
                json.dump(initial, f, ensure_ascii=False, indent=2)
        eng.LEARNING_DB_PATH = db_path
        eng.LEARNING_DB_ARCHIVE_PATH = archive_path
        yield db_path, archive_path
    finally:
        eng.LEARNING_DB_PATH = orig_db
        eng.LEARNING_DB_ARCHIVE_PATH = orig_archive
        shutil.rmtree(root, ignore_errors=True)


def _make_db(n: int, seq=None) -> dict:
    """outcome=success の n 件を持つ DB を作る。"""
    execs = [
        {"id": f"exec_20260101_{i:03d}", "outcome": "success",
         "tools_used": ["tool_x"], "plan_steps": 2}
        for i in range(1, n + 1)
    ]
    db = {"executions": execs}
    if seq is not None:
        db["seq"] = seq
    return db


def _log_once(query="テストクエリ", success=True):
    """log_execution を最小引数で1回呼ぶ。"""
    results = [{"step": 1, "tool": "tool_x", "success": success,
                "observation": "" if success else "Error: boom"}]
    eng.log_execution(query, plan=[{"step": 1}], results=results,
                      critique={"status": "ok"}, duration_seconds=1.0)


# --------------------------------------------------------------------------
# Step 3: seq 採番の移行互換
# --------------------------------------------------------------------------

def test_seq_migration_matches_legacy_output():
    """seq 無しの98件DB → 次IDが _099 (旧 len()+1 実装と一致)。"""
    with _temp_db(_make_db(98)):  # seq 無し
        _log_once()
        db = eng._load_learning_db()
        newest = db["executions"][-1]["id"]
        assert newest.endswith("_099"), f"期待 _099, 実際 {newest}"
        assert db["seq"] == 99
    print("✅ test_seq_migration_matches_legacy_output")


def test_ids_do_not_rewind_after_trim():
    """cap到達後トリムしてもIDが巻き戻らない (旧実装ではFAILする回帰)。"""
    with _temp_db(_make_db(200, seq=200)):  # 満杯 + seq=200
        _log_once()  # 201件目 → trim で200件に戻る
        db = eng._load_learning_db()
        newest = db["executions"][-1]["id"]
        # seq ベースなら _201。旧 len()+1 実装は trim 後 len=200 なので _201 に
        # 見えるが、次のログでまた _201 を再発行して衝突する。それを次で確認。
        assert db["seq"] == 201, f"seq が巻き戻った: {db['seq']}"
        _log_once()  # さらに1件
        db2 = eng._load_learning_db()
        ids = [e["id"] for e in db2["executions"]]
        assert len(ids) == len(set(ids)), f"ID衝突: {[i for i in ids if ids.count(i) > 1]}"
        assert db2["seq"] == 202
    print("✅ test_ids_do_not_rewind_after_trim")


def test_legacy_len_based_would_collide():
    """旧 len(executions)+1 採番を再現するとトリム後にID衝突することを実証。"""
    # 旧採番を手で再現: cap=3 で満杯、trim するたび len が 3 に戻る
    cap = 3
    execs = []
    seq_ids = []
    for _ in range(5):
        legacy_id = f"exec_D_{len(execs) + 1:03d}"  # 旧採番
        execs.append({"id": legacy_id})
        if len(execs) > cap:
            execs = execs[-cap:]  # trim
        seq_ids.append(legacy_id)
    # trim で len が頭打ちになるため _004 が複数回発行される
    assert len(seq_ids) != len(set(seq_ids)), "旧採番でも衝突しなかった=前提崩壊"
    print("✅ test_legacy_len_based_would_collide (旧採番の欠陥を実証)")


# --------------------------------------------------------------------------
# Step 4: ローテーション + アーカイブ
# --------------------------------------------------------------------------

def test_no_trim_below_cap():
    """98件・cap200 で1件も削られず archive も作られない (実データ相当)。"""
    with _temp_db(_make_db(98)) as (db_path, archive_path):
        _log_once()
        db = eng._load_learning_db()
        assert len(db["executions"]) == 99, f"件数 {len(db['executions'])}"
        assert not os.path.exists(archive_path), "archive が誤って作られた"
    print("✅ test_no_trim_below_cap")


def test_trim_at_cap_writes_overflow_to_archive():
    """201件目のログで200件保持 + 溢れた最古1件が archive JSONL に1行。"""
    with _temp_db(_make_db(200, seq=200)) as (db_path, archive_path):
        oldest_id = eng._load_learning_db()["executions"][0]["id"]
        _log_once()
        db = eng._load_learning_db()
        assert len(db["executions"]) == 200, f"件数 {len(db['executions'])}"
        assert os.path.exists(archive_path), "archive が作られていない"
        with open(archive_path, encoding='utf-8') as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(lines) == 1, f"archive 行数 {len(lines)}"
        archived = json.loads(lines[0])
        assert archived["id"] == oldest_id, "退避されたのが最古エントリでない"
        # 退避された最古IDは現DBに残っていない
        assert oldest_id not in [e["id"] for e in db["executions"]]
    print("✅ test_trim_at_cap_writes_overflow_to_archive")


def test_archive_failure_keeps_all_entries():
    """archive 書き込み失敗時はトリムせず全件保持 (捨てるより太らせる)。"""
    with _temp_db(_make_db(200, seq=200)) as (db_path, archive_path):
        # archive_path をディレクトリにして open('a') を失敗させる
        os.makedirs(archive_path)
        _log_once()
        db = eng._load_learning_db()
        assert len(db["executions"]) == 201, \
            f"archive失敗なのにトリムされた: {len(db['executions'])}"
    print("✅ test_archive_failure_keeps_all_entries")


def test_rotate_executions_pure():
    """_rotate_executions は純粋関数で db を書き換えない。"""
    db = _make_db(5)
    kept, overflow = eng._rotate_executions(db, max_entries=3)
    assert len(kept) == 3 and len(overflow) == 2
    assert overflow[0]["id"].endswith("_001"), "溢れは古い方から"
    assert len(db["executions"]) == 5, "元 db が書き換えられた"
    # cap 以下なら overflow 空
    kept2, overflow2 = eng._rotate_executions(_make_db(3), max_entries=3)
    assert overflow2 == [] and len(kept2) == 3
    print("✅ test_rotate_executions_pure")


# --------------------------------------------------------------------------
# Step 2: 破損検疫
# --------------------------------------------------------------------------

def test_corrupt_db_quarantined_and_preserved():
    """破損DB → .corrupt-* へ退避 + 空DB再開 + 退避ファイルに元の中身が残る。"""
    with _temp_db() as (db_path, archive_path):
        with open(db_path, 'w', encoding='utf-8') as f:
            f.write('{"executions": [ THIS IS BROKEN JSON')
        db = eng._load_learning_db()
        assert db == {"executions": []}, "空DBで再開していない"
        assert not os.path.exists(db_path), "破損ファイルが退避されていない"
        corrupt = [p for p in os.listdir(os.path.dirname(db_path))
                   if ".corrupt-" in p]
        assert len(corrupt) == 1, f"退避ファイル数 {len(corrupt)}"
        with open(os.path.join(os.path.dirname(db_path), corrupt[0]),
                  encoding='utf-8') as f:
            assert "BROKEN JSON" in f.read(), "退避ファイルに元の中身が無い"
    print("✅ test_corrupt_db_quarantined_and_preserved")


def test_missing_db_returns_empty_without_quarantine():
    """存在しないDBは破損ではない → 退避せず空DBを返す。"""
    with _temp_db() as (db_path, archive_path):
        assert not os.path.exists(db_path)
        db = eng._load_learning_db()
        assert db == {"executions": []}
        corrupt = [p for p in os.listdir(os.path.dirname(db_path))
                   if ".corrupt-" in p]
        assert corrupt == [], "存在しないだけなのに退避された"
    print("✅ test_missing_db_returns_empty_without_quarantine")


def test_next_log_after_corruption_does_not_double_wipe():
    """破損退避後の初回ログが空DBから正しく積み増す (全消失しない)。"""
    with _temp_db() as (db_path, archive_path):
        with open(db_path, 'w', encoding='utf-8') as f:
            f.write("not json at all")
        _log_once()
        db = eng._load_learning_db()
        assert len(db["executions"]) == 1, "退避後の再開で1件積めていない"
    print("✅ test_next_log_after_corruption_does_not_double_wipe")


# --------------------------------------------------------------------------
# Step 1: save_json 原子性
# --------------------------------------------------------------------------

def test_save_json_atomic_on_failure():
    """dump 中に例外が出ても元ファイルが無傷で残る (tmp 差し替え方式)。"""
    root = tempfile.mkdtemp(prefix="atomic_test_")
    try:
        path = os.path.join(root, "data.json")
        save_json(path, {"good": 1})  # 正常な元ファイル

        class Unserializable:
            pass

        try:
            save_json(path, {"bad": Unserializable()})  # dump が TypeError
        except TypeError:
            pass
        else:
            raise AssertionError("シリアライズ不能でも例外が出なかった")

        # 元ファイルが無傷か
        with open(path, encoding='utf-8') as f:
            assert json.load(f) == {"good": 1}, "元ファイルが壊れた"
        # tmp ファイルが残っていない
        leftovers = [p for p in os.listdir(root) if ".tmp-" in p]
        assert leftovers == [], f"tmp が残留: {leftovers}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("✅ test_save_json_atomic_on_failure")


# --------------------------------------------------------------------------
# Step 5: 信頼性窓
# --------------------------------------------------------------------------

def test_reliability_window_excludes_old_failures():
    """RELIABILITY_WINDOW 件より古い失敗は low_reliability 警告に出ない。"""
    # 窓の外に tool_bad の失敗を4件、窓の内は成功で埋める
    old_fail = [
        {"id": f"exec_A_{i:03d}", "outcome": "failed", "query": "旧クエリ",
         "tools_used": ["tool_bad"], "plan_steps": 1, "failure_reason": "x"}
        for i in range(4)
    ]
    recent_ok = [
        {"id": f"exec_B_{i:03d}", "outcome": "success",
         "tools_used": ["tool_good"], "plan_steps": 1}
        for i in range(eng.RELIABILITY_WINDOW)
    ]
    with _temp_db({"executions": old_fail + recent_ok, "seq": 999}):
        prompt = eng.get_enhanced_prompt("なにか")
        assert "tool_bad" not in prompt, "窓外の失敗ツールが警告に混入"
    print("✅ test_reliability_window_excludes_old_failures")


def test_reliability_window_flags_recent_failures():
    """窓の内に失敗が集中していれば正しく警告する (窓化で見逃さない)。"""
    recent_fail = [
        {"id": f"exec_C_{i:03d}", "outcome": "failed", "query": "最近クエリ",
         "tools_used": ["tool_flaky"], "plan_steps": 1, "failure_reason": "boom"}
        for i in range(5)
    ]
    with _temp_db({"executions": recent_fail, "seq": 5}):
        prompt = eng.get_enhanced_prompt("なにか")
        assert "tool_flaky" in prompt, "窓内の失敗ツールが警告されない"
    print("✅ test_reliability_window_flags_recent_failures")


if __name__ == "__main__":
    tests = [
        test_seq_migration_matches_legacy_output,
        test_ids_do_not_rewind_after_trim,
        test_legacy_len_based_would_collide,
        test_no_trim_below_cap,
        test_trim_at_cap_writes_overflow_to_archive,
        test_archive_failure_keeps_all_entries,
        test_rotate_executions_pure,
        test_corrupt_db_quarantined_and_preserved,
        test_missing_db_returns_empty_without_quarantine,
        test_next_log_after_corruption_does_not_double_wipe,
        test_save_json_atomic_on_failure,
        test_reliability_window_excludes_old_failures,
        test_reliability_window_flags_recent_failures,
    ]
    for t in tests:
        t()
    print(f"\n🎉 ALL {len(tests)} LEARNING_DB ROTATION TESTS PASSED")
