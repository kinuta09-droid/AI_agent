# -*- coding: utf-8 -*-
"""tests/test_note_promotion.py — ノート昇格パイプライン (Progressive Summarization)

検証する不変条件:
  [1] クラスタ検出: 類似度が閾値以上の3件が1クラスタにまとまる
  [2] クラスタ検出: min_cluster_size未満のグループはクラスタ扱いされない
  [3] クラスタ検出: 独立した2グループは2クラスタに分かれる (clique方式の正しさ)
  [4] クラスタ検出: target_dir 以外のチャンクは対象外
  [4b] クラスタ検出: 鎖状の連鎖 (A-B, B-C は閾値以上だが A-C は無関係) は
       3件クラスタに巻き込まれない (2026-08-22 実Vaultで発覚した回帰)
  [5] pendingフィルタ: 審議中クラスタとファイルが1件でも被れば新規提案しない
  [6] seenフィルタ: 却下済みクラスタは cooldown 期間内は再提案しない
  [7] seenフィルタ: cooldown 期間を過ぎれば再提案する
  [8] seenフィルタ: メンバーが変わり署名が変われば却下直後でも再提案される
  [9] ドラフト生成: 正常系は title/summary/body を返す
  [10] ドラフト生成: 不正なJSON応答は例外を投げず "Error: ..." を返す
  [11] 昇格: evergreenノート作成→source/promotionタグ→元ノートへpromoted-intoタグ→アーカイブ移動
  [12] 昇格: 一部ノートが事前に無くなっていても生存分だけで続行し、欠落を明記する
  [13] 昇格: 対象ノートが全滅していれば "Error: ..." を返しファイルに触れない
  [14] 昇格: アーカイブ先の同名衝突はタイムスタンプ接尾辞で退避する
  [15] 却下: ファイルを一切変更せず seen に記録するのみ
  [16] 昇格/却下: 未知のslotは "Error: ..." を返す (二重クリック相当)
  [17] run_note_promotion_job: クラスタ0件なら通知しない (沈黙バイアス)
  [18] run_note_promotion_job: 空きスロットを超えた分は通知されずpendingにも入らない
  [19] 統合: detect_fleeting_clusters→run_note_promotion_job→tool_promote_cluster を
       実際に繋いだ経路で昇格まで成功する (2026-08-22 実Vaultで発覚した回帰:
       detect_fleeting_clusters が返す filenames は "00_inbox/xxx.md" という
       target_dir込みのVault相対パスなのに、tool_promote_cluster 側がさらに
       target_dir を join して二重パス化し「ノートが見つからない」で昇格が
       全滅していた。単体テストが filenames に basename のみを与えていたため
       この不整合を検出できていなかった — 経路を跨ぐ形式の整合性は個々の
       単体テストでは保証できないので、実際に繋いだ経路を1本通す)
  [20] run_note_promotion_job: 本文が全ノート空ならGeminiを呼ばずスキップする
       (grill指摘: 空本文でも下書き生成すると、ファイル名だけから内容を
       捏造した下書きが本文要約の体で出力される実害が実機確認で見つかった)
  [21] _prioritize_by_reference: freq_counts が空ならクラスタ順序は変わらない
       (tools/tool_reference_promotion.py 未配線時の後方互換)
  [22] _prioritize_by_reference: 検索ログ高頻度ノートを含むクラスタが
       サイズに関わらず優先される (ロードマップ項目8: 参照実績を統合優先度に反映)
  [23] run_note_promotion_job: freq_lookup_fn を実際に通しても、優先クラスタが
       先に空きスロットへ入る (単体テストだけでなく実経路での確認)

本番Vault・logs/vault_index.db・logs/note_promotion_*.json には一切触れない
(tempfile 配下のDB/Vault/JSONパスを関数の引数として渡すだけで完全に隔離できる —
 このリポジトリが優先する「シグネチャ引数注入」方式。モジュール属性の退避は不要)。
"""
import datetime
import os
import sys
import tempfile

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tool_vault_index import _connect, SOURCE_VAULT  # noqa: E402
import tools.tool_note_promotion as promo  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# DB構築ヘルパー
# ---------------------------------------------------------

def _insert_chunk(conn, filepath, vec, source=SOURCE_VAULT):
    arr = np.asarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT INTO chunks(filepath, chunk_index, heading, text, embedding, dim, source) "
        "VALUES (?, 0, NULL, 'dummy', ?, ?, ?)",
        (filepath, arr.tobytes(), len(arr), source),
    )


def _make_db(db_path, entries):
    """entries: [(filepath, vec, source), ...]"""
    conn = _connect(db_path)
    for filepath, vec, source in entries:
        _insert_chunk(conn, filepath, vec, source)
    conn.commit()
    conn.close()


# 2グループの単位ベクトル (グループ内は高類似度、グループ間はほぼ無関係)
GROUP_A = [[1.0, 0.05, 0.0], [1.0, -0.05, 0.0], [0.95, 0.1, 0.0]]
GROUP_B = [[0.0, 1.0, 0.05], [0.0, 1.0, -0.05]]


# ---------------------------------------------------------
# [1]-[4] クラスタ検出
# ---------------------------------------------------------

def test_detect_clusters_groups_similar_notes():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        entries = [(f"00_inbox/a{i}.md", v, SOURCE_VAULT) for i, v in enumerate(GROUP_A)]
        _make_db(db, entries)
        clusters = promo.detect_fleeting_clusters(db_path=db)
        check("[1] 3件の類似ノートが1クラスタにまとまる", len(clusters) == 1, clusters)
        if clusters:
            check("[1] クラスタのファイル一覧が一致", clusters[0]["filenames"] == sorted(e[0] for e in entries))
    print("--- [1] done ---")


def test_detect_clusters_below_min_size_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        entries = [(f"00_inbox/a{i}.md", v, SOURCE_VAULT) for i, v in enumerate(GROUP_A[:2])]
        _make_db(db, entries)
        clusters = promo.detect_fleeting_clusters(db_path=db)
        check("[2] min_cluster_size未満はクラスタなし", clusters == [], clusters)
    print("--- [2] done ---")


def test_detect_clusters_splits_independent_groups():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        entries = (
            [(f"00_inbox/a{i}.md", v, SOURCE_VAULT) for i, v in enumerate(GROUP_A)]
            + [(f"00_inbox/b{i}.md", v, SOURCE_VAULT) for i, v in enumerate(GROUP_B)]
        )
        _make_db(db, entries)
        clusters = promo.detect_fleeting_clusters(db_path=db, min_cluster_size=2)
        check("[3] 独立した2グループが2クラスタに分かれる", len(clusters) == 2, clusters)
        if len(clusters) == 2:
            names = {frozenset(c["filenames"]) for c in clusters}
            expected = {frozenset(f"00_inbox/a{i}.md" for i in range(3)),
                        frozenset(f"00_inbox/b{i}.md" for i in range(2))}
            check("[3] グループの中身が正しい", names == expected, names)
    print("--- [3] done ---")


def test_detect_clusters_excludes_other_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        entries = (
            [(f"00_inbox/a{i}.md", v, SOURCE_VAULT) for i, v in enumerate(GROUP_A)]
            + [(f"30_Main/other.md", GROUP_A[0], SOURCE_VAULT)]
        )
        _make_db(db, entries)
        clusters = promo.detect_fleeting_clusters(db_path=db, target_dir="00_inbox")
        check("[4] 00_inbox以外は対象外", len(clusters) == 1 and "30_Main/other.md" not in clusters[0]["filenames"], clusters)
    print("--- [4] done ---")


def test_detect_clusters_does_not_chain_through_weak_link():
    """A-B, B-C は閾値以上だが A-C は無関係 (単連結クラスタリングなら3件とも
    巻き込まれてしまう。実Vaultで実際に発生した連鎖巻き込みの回帰チェック)。"""
    import math
    a = [1.0, 0.0]
    b = [math.cos(math.radians(30)), math.sin(math.radians(30))]  # cos(A,B)=0.866
    c = [math.cos(math.radians(60)), math.sin(math.radians(60))]  # cos(B,C)=0.866, cos(A,C)=0.5
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        entries = [("00_inbox/a.md", a, SOURCE_VAULT), ("00_inbox/b.md", b, SOURCE_VAULT),
                   ("00_inbox/c.md", c, SOURCE_VAULT)]
        _make_db(db, entries)
        clusters = promo.detect_fleeting_clusters(db_path=db, min_cluster_size=3,
                                                    similarity_threshold=0.75)
        check("[4b] 鎖状の3件は1クラスタに巻き込まれない", clusters == [], clusters)
    print("--- [4b] done ---")


# ---------------------------------------------------------
# [5]-[8] pending/seen フィルタ
# ---------------------------------------------------------

def test_filter_excludes_pending_overlap():
    clusters = [{"cluster_id": "c1", "filenames": ["a.md", "b.md", "c.md"]}]
    pending = {"0": {"filenames": ["b.md"]}}
    fresh = promo._filter_new_clusters(clusters, pending, {})
    check("[5] pendingと1件でも被れば除外", fresh == [], fresh)


def test_filter_excludes_seen_rejected_within_cooldown():
    now = datetime.datetime.now()
    clusters = [{"cluster_id": "c1", "filenames": ["a.md", "b.md", "c.md"]}]
    seen = {"c1": {"decision": "rejected", "at": now.isoformat()}}
    fresh = promo._filter_new_clusters(clusters, {}, seen, now=now)
    check("[6] cooldown中は再提案しない", fresh == [], fresh)


def test_filter_allows_seen_rejected_after_cooldown():
    now = datetime.datetime.now()
    old = now - datetime.timedelta(days=promo.REJECT_COOLDOWN_DAYS + 1)
    clusters = [{"cluster_id": "c1", "filenames": ["a.md", "b.md", "c.md"]}]
    seen = {"c1": {"decision": "rejected", "at": old.isoformat()}}
    fresh = promo._filter_new_clusters(clusters, {}, seen, now=now)
    check("[7] cooldown後は再提案する", fresh == clusters, fresh)


def test_filter_allows_new_cluster_after_membership_change():
    now = datetime.datetime.now()
    seen = {"old-signature": {"decision": "rejected", "at": now.isoformat()}}
    clusters = [{"cluster_id": "new-signature", "filenames": ["a.md", "b.md", "d.md"]}]
    fresh = promo._filter_new_clusters(clusters, {}, seen, now=now)
    check("[8] 署名が変われば却下直後でも再提案される", fresh == clusters, fresh)


# ---------------------------------------------------------
# [9]-[10] ドラフト生成
# ---------------------------------------------------------

def test_generate_promotion_draft_success():
    note_bodies = [{"filename": "a.md", "content": "メモ1"}, {"filename": "b.md", "content": "メモ2"}]
    gen_fn = lambda prompt: '{"title": "統合ノート", "summary": "要約", "body": "本文"}'
    draft = promo.generate_promotion_draft(note_bodies, gen_fn=gen_fn)
    check("[9] 正常系はdictを返す", isinstance(draft, dict), draft)
    if isinstance(draft, dict):
        check("[9] title/summary/bodyが揃う",
              draft == {"title": "統合ノート", "summary": "要約", "body": "本文"}, draft)


def test_generate_promotion_draft_invalid_json_returns_error():
    note_bodies = [{"filename": "a.md", "content": "メモ1"}]
    gen_fn = lambda prompt: "これはJSONではありません"
    draft = promo.generate_promotion_draft(note_bodies, gen_fn=gen_fn)
    check("[10] 不正応答はErrorを返す (例外を投げない)", isinstance(draft, str) and draft.startswith("Error"), draft)


# ---------------------------------------------------------
# [11]-[16] 昇格 / 却下
# ---------------------------------------------------------

_FLEETING_TPL = """---
created: 2026-01-01 09:00
tags:
  - type/fleeting
status: open
---

# 📝 {title}

- {title} についてのメモ
"""


def _make_vault_with_fleeting(root, filenames):
    """filenames は detect_fleeting_clusters と同じ形式 (例 "00_inbox/a.md"、target_dir込み)。"""
    for fn in filenames:
        path = os.path.join(root, fn)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_FLEETING_TPL.format(title=os.path.splitext(os.path.basename(fn))[0]))


def _fake_create_note(filename, content, summary=""):
    """tool_create_note の代わりに使う軽量スタブ。30_Main直下にtype/permanentノートを作る。"""
    safe = filename if filename.endswith(".md") else filename + ".md"
    save_dir = os.path.join(_fake_create_note.work_dir, "30_Main")
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, safe)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\ntags:\n  - type/permanent\n---\n# {filename}\n\n> {summary}\n\n{content}\n")
    return f"Success: Created [[{safe}]] with template."


def _make_pending_entry(filenames, cluster_id="c1"):
    return {
        "cluster_id": cluster_id,
        "filenames": filenames,
        "draft": {"title": "統合ノート", "summary": "要約", "body": "統合本文"},
    }


def test_promote_cluster_moves_files_and_tags():
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        _make_vault_with_fleeting(root, filenames)
        pending_path = os.path.join(root, "pending.json")
        seen_path = os.path.join(root, "seen.json")
        promo._save_pending({"0": _make_pending_entry(filenames)}, pending_path)
        _fake_create_note.work_dir = root

        result = promo.tool_promote_cluster(0, pending_path=pending_path, seen_path=seen_path,
                                             work_dir=root, create_note_fn=_fake_create_note)
        check("[11] 昇格結果に失敗接頭辞が無い", not result.startswith("Error"), result)

        evergreen_path = os.path.join(root, "30_Main", "統合ノート.md")
        check("[11] evergreenノートが作成される", os.path.exists(evergreen_path))
        if os.path.exists(evergreen_path):
            with open(evergreen_path, encoding="utf-8") as f:
                check("[11] source/promotionタグが付く", "source/promotion" in f.read())

        archive_dir = os.path.join(root, promo.ARCHIVE_SUBDIR, "統合ノート")
        for fn in filenames:
            basename = os.path.basename(fn)
            moved_path = os.path.join(archive_dir, basename)
            check(f"[11] {basename} がアーカイブへ移動 (二重パス化していないこと)", os.path.exists(moved_path))
            orig_path = os.path.join(root, fn)
            check(f"[11] {basename} は元の場所に残らない", not os.path.exists(orig_path))
            if os.path.exists(moved_path):
                with open(moved_path, encoding="utf-8") as f:
                    check(f"[11] {basename} にpromoted-intoタグが付く", "promoted-into/統合ノート" in f.read())

        pending_after = promo._load_pending(pending_path)
        check("[11] pendingからslotが除去される", "0" not in pending_after, pending_after)
        seen_after = promo._load_seen(seen_path)
        check("[11] seenにpromoted記録が残る", seen_after.get("c1", {}).get("decision") == "promoted", seen_after)
    print("--- [11] done ---")


def test_promote_cluster_partial_missing_files():
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        _make_vault_with_fleeting(root, filenames)
        os.remove(os.path.join(root, "00_inbox", "c.md"))  # 手動削除された想定
        pending_path = os.path.join(root, "pending.json")
        promo._save_pending({"0": _make_pending_entry(filenames)}, pending_path)
        _fake_create_note.work_dir = root

        result = promo.tool_promote_cluster(0, pending_path=pending_path,
                                             seen_path=os.path.join(root, "seen.json"),
                                             work_dir=root, create_note_fn=_fake_create_note)
        check("[12] 部分欠落でも成功する", not result.startswith("Error"), result)
        check("[12] 欠落ファイル名 (basename) が結果に明記される", "c.md" in result, result)
        archive_dir = os.path.join(root, promo.ARCHIVE_SUBDIR, "統合ノート")
        check("[12] 生存分のみアーカイブされる",
              os.path.exists(os.path.join(archive_dir, "a.md")) and os.path.exists(os.path.join(archive_dir, "b.md")))
    print("--- [12] done ---")


def test_promote_cluster_all_missing_returns_error():
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "00_inbox"), exist_ok=True)
        pending_path = os.path.join(root, "pending.json")
        promo._save_pending(
            {"0": _make_pending_entry(["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"])}, pending_path)

        result = promo.tool_promote_cluster(0, pending_path=pending_path,
                                             seen_path=os.path.join(root, "seen.json"),
                                             work_dir=root, create_note_fn=_fake_create_note)
        check("[13] 全滅時はErrorを返す", result.startswith("Error"), result)
        check("[13] 30_Mainにノートを作らない", not os.path.exists(os.path.join(root, "30_Main")))
    print("--- [13] done ---")


def test_promote_cluster_archive_name_collision_gets_suffix():
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        _make_vault_with_fleeting(root, filenames)
        archive_dir = os.path.join(root, promo.ARCHIVE_SUBDIR, "統合ノート")
        os.makedirs(archive_dir, exist_ok=True)
        with open(os.path.join(archive_dir, "a.md"), "w", encoding="utf-8") as f:
            f.write("既存の衝突ファイル")
        pending_path = os.path.join(root, "pending.json")
        promo._save_pending({"0": _make_pending_entry(filenames)}, pending_path)
        _fake_create_note.work_dir = root

        result = promo.tool_promote_cluster(0, pending_path=pending_path,
                                             seen_path=os.path.join(root, "seen.json"),
                                             work_dir=root, create_note_fn=_fake_create_note)
        check("[14] 衝突があっても成功する", not result.startswith("Error"), result)
        entries = os.listdir(archive_dir)
        suffixed = [e for e in entries if e.startswith("a_") and e != "a.md"]
        check("[14] タイムスタンプ接尾辞のファイルができる", len(suffixed) == 1, entries)
        with open(os.path.join(archive_dir, "a.md"), encoding="utf-8") as f:
            check("[14] 既存の衝突ファイルは上書きされない", f.read() == "既存の衝突ファイル")
    print("--- [14] done ---")


def test_reject_cluster_leaves_files_untouched():
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        _make_vault_with_fleeting(root, filenames)
        before = {}
        for fn in filenames:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                before[fn] = f.read()

        pending_path = os.path.join(root, "pending.json")
        seen_path = os.path.join(root, "seen.json")
        promo._save_pending({"0": _make_pending_entry(filenames)}, pending_path)

        result = promo.tool_reject_cluster(0, pending_path=pending_path, seen_path=seen_path)
        check("[15] 却下結果に失敗接頭辞が無い", not result.startswith("Error"), result)
        for fn in filenames:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                check(f"[15] {fn} は内容不変", f.read() == before[fn])
        seen_after = promo._load_seen(seen_path)
        check("[15] seenにrejected記録が残る", seen_after.get("c1", {}).get("decision") == "rejected", seen_after)
        pending_after = promo._load_pending(pending_path)
        check("[15] pendingからslotが除去される", "0" not in pending_after, pending_after)
    print("--- [15] done ---")


def test_unknown_slot_returns_error():
    with tempfile.TemporaryDirectory() as root:
        pending_path = os.path.join(root, "pending.json")
        seen_path = os.path.join(root, "seen.json")
        result_promote = promo.tool_promote_cluster(0, pending_path=pending_path, seen_path=seen_path, work_dir=root)
        result_reject = promo.tool_reject_cluster(0, pending_path=pending_path, seen_path=seen_path)
        check("[16] 未知slotの昇格はError", result_promote.startswith("Error"), result_promote)
        check("[16] 未知slotの却下はError", result_reject.startswith("Error"), result_reject)
    print("--- [16] done ---")


# ---------------------------------------------------------
# [17]-[18] run_note_promotion_job
# ---------------------------------------------------------

def test_run_job_silent_when_no_clusters():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")  # 作らない = クラスタ0件
        notified = []
        result = promo.run_note_promotion_job(
            db_path=db,
            pending_path=os.path.join(tmp, "pending.json"),
            seen_path=os.path.join(tmp, "seen.json"),
            notify_fn=lambda msg, components=None: notified.append(msg),
            freq_lookup_fn=lambda: {},
        )
        check("[17] クラスタ0件では通知しない", notified == [], notified)
        check("[17] 戻り値にクラスタなしと分かる文言", "クラスタなし" in result, result)
    print("--- [17] done ---")


def test_run_job_respects_free_slots():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        # NOTE_PROMO_SLOTS(5) を超える 6 クラスタ分のノートを用意する
        entries = []
        filenames_all = []
        for g in range(6):
            base = [1.0 if k == g else 0.0 for k in range(6)]
            for i in range(3):
                vec = list(base)
                vec[g] += 0.01 * i
                fn = f"00_inbox/g{g}_{i}.md"
                entries.append((fn, vec, SOURCE_VAULT))
                filenames_all.append(fn)
        _make_db(db, entries)
        _make_vault_with_fleeting(tmp, filenames_all)  # 本文を用意 (空本文ガードの回帰対策)

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        gen_fn = lambda prompt: '{"title": "T", "summary": "S", "body": "B"}'
        result = promo.run_note_promotion_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            gen_fn=gen_fn, notify_fn=lambda msg, components=None: notified.append(msg),
            freq_lookup_fn=lambda: {},
        )
        check("[18] 通知は空きスロット数(5)まで", len(notified) == promo.NOTE_PROMO_SLOTS, len(notified))
        pending_after = promo._load_pending(pending_path)
        check("[18] pendingも空きスロット数まで", len(pending_after) == promo.NOTE_PROMO_SLOTS, len(pending_after))
        check("[18] 戻り値に空き枠超過が明記される", "空き枠超過1件" in result, result)
    print("--- [18] done ---")


# ---------------------------------------------------------
# [19] 統合: detect_fleeting_clusters → run_note_promotion_job → tool_promote_cluster
# ---------------------------------------------------------

def test_end_to_end_detect_to_promote():
    """実際にVaultへFleetingノートを置き、DBへ埋め込みを入れ、検出→ジョブ実行→承認まで
    通しで動かす。filenames の形式 ("00_inbox/xxx.md" 込みか basename か) が経路の
    途中で食い違うとここで "ノートが見つからない" 系のErrorとして必ず露呈する。"""
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        _make_vault_with_fleeting(root, filenames)

        db = os.path.join(root, "index.db")
        entries = [(fn, v, SOURCE_VAULT) for fn, v in zip(filenames, GROUP_A)]
        _make_db(db, entries)

        pending_path = os.path.join(root, "pending.json")
        seen_path = os.path.join(root, "seen.json")
        gen_fn = lambda prompt: '{"title": "統合ノート", "summary": "要約", "body": "統合本文"}'
        job_result = promo.run_note_promotion_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=root,
            gen_fn=gen_fn, notify_fn=lambda msg, components=None: None,
            freq_lookup_fn=lambda: {},
        )
        check("[19] ジョブが1件通知したと報告する", "1件通知" in job_result, job_result)

        pending = promo._load_pending(pending_path)
        check("[19] pendingにfilenamesがtarget_dir込みで保存される",
              pending.get("0", {}).get("filenames") == sorted(filenames), pending)

        _fake_create_note.work_dir = root
        promote_result = promo.tool_promote_cluster(
            0, pending_path=pending_path, seen_path=seen_path,
            work_dir=root, create_note_fn=_fake_create_note)
        check("[19] 実経路を通しても昇格が失敗接頭辞にならない (2026-08-22回帰の直接検証)",
              not promote_result.startswith("Error"), promote_result)

        archive_dir = os.path.join(root, promo.ARCHIVE_SUBDIR, "統合ノート")
        for fn in filenames:
            basename = os.path.basename(fn)
            check(f"[19] {basename} が実際にアーカイブへ移動している",
                  os.path.exists(os.path.join(archive_dir, basename)))
            check(f"[19] {basename} は元の場所から消えている", not os.path.exists(os.path.join(root, fn)))
    print("--- [19] done ---")


def test_run_job_skips_draft_when_all_bodies_empty():
    """本文が全ノート空 (ファイル未配置等) なら Gemini を呼ばずスキップすること。
    呼ばれてしまうと、ファイル名だけから内容を捏造した下書きが本文要約の体で
    出力される (2026-08-22 実機確認で発覚したリスクの回帰チェック)。"""
    with tempfile.TemporaryDirectory() as root:
        filenames = ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]
        # 実ファイルは置かない = _read_note_body が空文字列を返す
        db = os.path.join(root, "index.db")
        entries = [(fn, v, SOURCE_VAULT) for fn, v in zip(filenames, GROUP_A)]
        _make_db(db, entries)

        gen_called = []
        gen_fn = lambda prompt: gen_called.append(prompt) or '{"title": "T", "summary": "S", "body": "B"}'
        result = promo.run_note_promotion_job(
            db_path=db, pending_path=os.path.join(root, "pending.json"),
            seen_path=os.path.join(root, "seen.json"), work_dir=root,
            gen_fn=gen_fn, notify_fn=lambda msg, components=None: None,
            freq_lookup_fn=lambda: {},
        )
        check("[20] 本文が全て空ならGeminiを呼ばない", gen_called == [], gen_called)
        check("[20] 戻り値がErrorになる (下書き生成扱いで失敗)", result.startswith("Error"), result)
    print("--- [20] done ---")


# ---------------------------------------------------------
# [21]-[23] freq_lookup_fn によるノート統合優先度反映 (ロードマップ項目8)
# ---------------------------------------------------------

def test_prioritize_by_reference_preserves_order_when_empty():
    clusters = [
        {"cluster_id": "big", "filenames": ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]},
        {"cluster_id": "small", "filenames": ["00_inbox/x.md", "00_inbox/y.md", "00_inbox/z.md"]},
    ]
    result = promo._prioritize_by_reference(clusters, {})
    check("[21] freq_counts空なら元の順序を維持 (後方互換)", result == clusters, result)


def test_prioritize_by_reference_promotes_hot_cluster():
    clusters = [
        {"cluster_id": "cold", "filenames": ["00_inbox/a.md", "00_inbox/b.md", "00_inbox/c.md"]},
        {"cluster_id": "hot", "filenames": ["00_inbox/x.md", "00_inbox/y.md"]},
    ]
    freq_counts = {"00_inbox/x.md": 5}  # 閾値(既定3)以上
    result = promo._prioritize_by_reference(clusters, freq_counts)
    check("[22] 高頻度ノートを含むクラスタが先頭に来る", result[0]["cluster_id"] == "hot", result)


def test_run_job_prioritizes_hot_cluster_end_to_end():
    """サイズ4クラスタとサイズ3クラスタがあるとき、freq_lookup_fn無しではサイズ4が
    slot0に入るが、サイズ3側に検索ログ高頻度ノートがあればそちらがslot0に入ること。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        group_sizes = {0: 3, 1: 4}  # group0=小 (優先させたい方), group1=大 (デフォルト首位)
        entries = []
        filenames_by_group = {}
        for g, size in group_sizes.items():
            base = [1.0 if k == g else 0.0 for k in range(2)]
            fns = []
            for i in range(size):
                vec = list(base)
                vec[g] += 0.01 * i
                fn = f"00_inbox/g{g}_{i}.md"
                entries.append((fn, vec, SOURCE_VAULT))
                fns.append(fn)
            filenames_by_group[g] = fns
        _make_db(db, entries)
        all_filenames = [fn for fns in filenames_by_group.values() for fn in fns]
        _make_vault_with_fleeting(tmp, all_filenames)

        gen_fn = lambda prompt: '{"title": "T", "summary": "S", "body": "B"}'

        # freq_lookup_fn無し (既定と同じ挙動): サイズの大きいgroup1がslot0に入る
        default_result = promo.run_note_promotion_job(
            db_path=db, pending_path=os.path.join(tmp, "pending_default.json"),
            seen_path=os.path.join(tmp, "seen_default.json"), work_dir=tmp,
            gen_fn=gen_fn, notify_fn=lambda msg, components=None: None,
            freq_lookup_fn=lambda: {},
        )
        pending_default = promo._load_pending(os.path.join(tmp, "pending_default.json"))
        check("[23] freq無しではサイズ4クラスタ(group1)がslot0",
              set(pending_default.get("0", {}).get("filenames", [])) == set(filenames_by_group[1]),
              pending_default)

        # freq_lookup_fnでgroup0の1ノートを高頻度扱いにする: group0(サイズ3)がslot0に来る
        hot_note = filenames_by_group[0][0]
        boosted_result = promo.run_note_promotion_job(
            db_path=db, pending_path=os.path.join(tmp, "pending_boosted.json"),
            seen_path=os.path.join(tmp, "seen_boosted.json"), work_dir=tmp,
            gen_fn=gen_fn, notify_fn=lambda msg, components=None: None,
            freq_lookup_fn=lambda: {hot_note: 5},
        )
        pending_boosted = promo._load_pending(os.path.join(tmp, "pending_boosted.json"))
        check("[23] freq有りではサイズ3クラスタ(group0)がslot0に優先される",
              set(pending_boosted.get("0", {}).get("filenames", [])) == set(filenames_by_group[0]),
              pending_boosted)
    print("--- [23] done ---")


if __name__ == "__main__":
    test_detect_clusters_groups_similar_notes()
    test_detect_clusters_below_min_size_returns_empty()
    test_detect_clusters_splits_independent_groups()
    test_detect_clusters_excludes_other_dirs()
    test_detect_clusters_does_not_chain_through_weak_link()
    test_filter_excludes_pending_overlap()
    test_filter_excludes_seen_rejected_within_cooldown()
    test_filter_allows_seen_rejected_after_cooldown()
    test_filter_allows_new_cluster_after_membership_change()
    test_generate_promotion_draft_success()
    test_generate_promotion_draft_invalid_json_returns_error()
    test_promote_cluster_moves_files_and_tags()
    test_promote_cluster_partial_missing_files()
    test_promote_cluster_all_missing_returns_error()
    test_promote_cluster_archive_name_collision_gets_suffix()
    test_reject_cluster_leaves_files_untouched()
    test_unknown_slot_returns_error()
    test_run_job_silent_when_no_clusters()
    test_run_job_respects_free_slots()
    test_end_to_end_detect_to_promote()
    test_run_job_skips_draft_when_all_bodies_empty()
    test_prioritize_by_reference_preserves_order_when_empty()
    test_prioritize_by_reference_promotes_hot_cluster()
    test_run_job_prioritizes_hot_cluster_end_to_end()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for f in _FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    sys.exit(0)
