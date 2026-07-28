# -*- coding: utf-8 -*-
"""行動ログのセマンティックインデクサ (Phase B)

`lib/activity_corpus.py` が組み立てたドキュメントを bge-m3 で埋め込み、
Vault と**同じ SQLite** (`logs/vault_index.db`) に `source` 違いで同居させる。
検索面を1つに保つのが目的なので、別DB・別ツールには分けない。

  py tools/tool_activity_index.py     # 手動実行 (通常は 03:25 の夜間ジョブ)

**手動実行の注意 (VRAM)**:
  このマシンの GPU は 6GB で、リランカ (qwen2.5-coder:7b・4.68GB) と
  bge-m3 (1.16GB) は同居できない。検索を打った直後は OLLAMA_KEEP_ALIVE
  (既定10分) の間リランカが VRAM に居座っているため、そこへこのバッチを
  ぶつけると互いに追い出し合って両方遅くなる。**遅くなるだけで動く**ので
  誰も気付けない。定時実行が 03:25 なのはこのため (03:15 の Vault Index の
  直後で、検索が走らない時間帯)。

規約:
  - 例外を投げない。失敗は "[ERROR] ..." 文字列で返す
    (tool_vault_index.update_vault_index と同じソフトエラー規約)
  - 埋め込みは **GPU バッチのまま**。`prefer_cpu` を渡してはいけない
    (CPU 化は検索時のクエリ1本だけの最適化で、数百文書のバッチに使うと
     桁違いに遅くなる)。tests/test_rerank_backend.py が ast で静的検査する
"""

import hashlib
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

from lib.activity_corpus import (
    ACTIVITY_SOURCES, SOURCE_AGENT_EXEC, SOURCE_AGENT_SKILL, SOURCE_CRYPTO,
    render_execution, render_execution_rollups, render_skill_rollups,
    render_trade, render_trade_rollups,
)
from lib.file_lock import FileLock, FileLockTimeout
from lib.lib_shared import (
    LEARNING_DB_ARCHIVE_PATH, LEARNING_DB_PATH, TRADE_LOG_PATH,
)
from tools.tool_vault_index import (
    DB_PATH, EMBED_BATCH_SIZE, OLLAMA_EMBED_MODEL,
    _call_ollama_embed, _chunk_markdown, _connect, _get_meta, _normalize,
    _set_meta,
)

SKILL_OBS_PATH = os.path.join(_PROJECT_ROOT, "logs", "skill_observations.jsonl")


# ---------------------------------------------------------
# 入力の読み込み (ソースごとに独立して失敗する)
# ---------------------------------------------------------

def _read_jsonl(path: str) -> list:
    """1行1JSON を読む。壊れた行は飛ばす (途中の1行で全部を失わない)。"""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_trades() -> list:
    """trade_log.json を読む。

    FileLock を通すのは、bot(/crypto)・scheduler(22:30監査)・watcher(15分毎) が
    read-modify-write するファイルだから。読むだけでも、書き込みの途中
    (save_json の os.replace 直前) を掴まないようにロックを取る。
    """
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    with FileLock(TRADE_LOG_PATH):
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    return data if isinstance(data, list) else []


def load_executions() -> list:
    """learning_db.json と、ローテーションで溢れた archive を合わせて読む。

    archive を無視すると、200件を超えた瞬間に「昔の失敗」が検索面から
    静かに消える。学習DBのローテーションは記録の削除ではなく退避なので、
    検索面でも同じ扱いにする。
    """
    records = []
    if os.path.exists(LEARNING_DB_PATH):
        with open(LEARNING_DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
        if isinstance(db, dict):
            records.extend(db.get("executions") or [])
    records.extend(_read_jsonl(LEARNING_DB_ARCHIVE_PATH))
    return records


def load_observations() -> list:
    return _read_jsonl(SKILL_OBS_PATH)


# 各ソースの (ローダ, レンダラ群)。source ごとに独立させてあるのは、
# 1つのファイルが読めなかったときに **そのソースだけ** 更新をスキップし、
# 他のソースの掃除に巻き込まないため (D2 と同じ「掃除の巻き添え」対策)。
_SOURCE_SPECS = (
    (SOURCE_CRYPTO, load_trades,
     (lambda recs: [render_trade(r) for r in recs], render_trade_rollups)),
    (SOURCE_AGENT_EXEC, load_executions,
     (lambda recs: [render_execution(r) for r in recs], render_execution_rollups)),
    (SOURCE_AGENT_SKILL, load_observations,
     (render_skill_rollups,)),
)


def collect_documents() -> tuple:
    """全ソースのドキュメントを組み立てる。

    Returns:
        (docs_by_source, errors) — docs_by_source は {source: [doc, ...]}。
        読み込みに失敗した source は **キーごと入らない** (空リストではない)。
        「0件だった」と「読めなかった」を呼び出し側が区別できないと、
        読めなかったソースを「全部消えた」と解釈して掃除してしまう。
    """
    docs_by_source = {}
    errors = []
    for source, loader, renderers in _SOURCE_SPECS:
        try:
            records = loader()
        except (OSError, ValueError, FileLockTimeout) as e:
            errors.append(f"{source}: 読み込み失敗 ({type(e).__name__}: {e})")
            continue
        docs = []
        for render in renderers:
            docs.extend(render(records))
        docs_by_source[source] = docs
    return docs_by_source, errors


# ---------------------------------------------------------
# インデックス更新
# ---------------------------------------------------------

def fingerprint(text: str) -> str:
    """本文のハッシュ。中身が変わったものだけ再埋め込みするための鍵。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def update_activity_index(embed_fn=_call_ollama_embed, db_path: str = DB_PATH,
                          collect_fn=collect_documents) -> str:
    """行動ログを走査し、本文が変わったドキュメントだけ再埋め込みする。

    Returns:
        サマリー文字列、または "[ERROR] ..."。
    """
    # Ollama 疎通確認。ダメならファイルも読まずに返す (update_vault_index と同型)
    probe = embed_fn(["ping"])
    if isinstance(probe, str):
        return probe
    dim = len(probe[0])

    docs_by_source, errors = collect_fn()

    conn = _connect(db_path)
    try:
        # 埋め込みモデル/次元は Vault 側と共有する。食い違ったまま書くと
        # chunks に次元の違うベクトルが混ざり、_semantic_hits の vstack が
        # 落ちる (= 検索が全滅する)。ここでは直さず、直せる側に案内して止める。
        stored_dim = _get_meta(conn, "embedding_dim")
        stored_model = _get_meta(conn, "embedding_model")
        if stored_dim is None:
            _set_meta(conn, "embedding_model", OLLAMA_EMBED_MODEL)
            _set_meta(conn, "embedding_dim", str(dim))
            conn.commit()
        elif stored_dim != str(dim) or stored_model != OLLAMA_EMBED_MODEL:
            return (f"[ERROR] 埋め込みモデル/次元が Vault インデックスと一致しません "
                    f"({stored_model}/{stored_dim} vs {OLLAMA_EMBED_MODEL}/{dim})。"
                    f"先に update_vault_index() を実行して再構築してください。")

        stored = dict(conn.execute(
            "SELECT filepath, fingerprint FROM files WHERE source IN "
            f"({','.join('?' * len(ACTIVITY_SOURCES))})", ACTIVITY_SOURCES).fetchall())

        updated = skipped = failed = removed = 0

        for source, docs in docs_by_source.items():
            for doc in docs:
                fp = fingerprint(doc["text"])
                if stored.get(doc["doc_id"]) == fp:
                    skipped += 1
                    continue
                try:
                    chunks = _chunk_markdown(doc["text"])
                    if not chunks:
                        continue
                    vectors = []
                    embed_error = None
                    texts = [c["text"] for c in chunks]
                    for i in range(0, len(texts), EMBED_BATCH_SIZE):
                        # prefer_cpu は渡さない — バッチは GPU のまま (冒頭の注意参照)
                        result = embed_fn(texts[i:i + EMBED_BATCH_SIZE])
                        if isinstance(result, str):
                            embed_error = result
                            break
                        vectors.extend(result)
                    if embed_error:
                        # この1件だけ諦める。旧データを残すので次回リトライされる
                        print(f"[ActivityIndex] ⚠️ Embed failed for "
                              f"{doc['doc_id']}: {embed_error}")
                        failed += 1
                        continue

                    _write_doc(conn, doc, chunks, vectors, dim, fp)
                    conn.commit()
                    updated += 1
                except Exception as e:
                    print(f"[ActivityIndex] ⚠️ Failed to index {doc['doc_id']}: {e}")
                    failed += 1

        # 消えたドキュメントの掃除。**読み込みに失敗した source は触らない**:
        # docs_by_source にキーが無い = 実態が分からない状態なので、
        # 「Vault に無いから消す」と同じ理屈で全消しすると復旧不能になる。
        for source in docs_by_source:
            alive = {d["doc_id"] for d in docs_by_source[source]}
            existing = [row[0] for row in conn.execute(
                "SELECT filepath FROM files WHERE source=?", (source,)).fetchall()]
            for doc_id in set(existing) - alive:
                conn.execute("DELETE FROM chunks WHERE filepath=? AND source=?",
                             (doc_id, source))
                conn.execute("DELETE FROM files WHERE filepath=? AND source=?",
                             (doc_id, source))
                removed += 1
        conn.commit()

        total = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source IN "
            f"({','.join('?' * len(ACTIVITY_SOURCES))})", ACTIVITY_SOURCES
        ).fetchone()[0]
    finally:
        conn.close()

    summary = (f"Activity index: {updated} docs updated, {skipped} unchanged, "
               f"{removed} removed, {failed} failed, {total} chunks total.")
    if errors:
        # 読めなかったソースは必ず戻り値に出す。ログだけに出すと
        # 「更新0件＝変化なし」と読まれて、壊れていることに気付けない。
        summary += " ⚠️ 読み込めなかったソース: " + " / ".join(errors)
    return summary


def _write_doc(conn, doc: dict, chunks: list, vectors: list, dim: int, fp: str):
    """1ドキュメント分の chunks/files 行を置き換える。"""
    doc_id, source = doc["doc_id"], doc["source"]
    conn.execute("DELETE FROM chunks WHERE filepath=? AND source=?", (doc_id, source))
    for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
        arr = _normalize(np.asarray(vec, dtype=np.float32))
        conn.execute(
            "INSERT INTO chunks(filepath, chunk_index, heading, text, embedding, "
            "dim, source) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (doc_id, idx, chunk["heading"] or doc["title"], chunk["text"],
             arr.tobytes(), dim, source),
        )
    # mtime は行動ログでは意味を持たない (JSON から毎回組み立てるため)。
    # 変更検知は fingerprint が担うので 0.0 を入れる。
    conn.execute(
        "INSERT INTO files(filepath, mtime, chunk_count, source, fingerprint) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(filepath) DO UPDATE SET mtime=excluded.mtime, "
        "chunk_count=excluded.chunk_count, source=excluded.source, "
        "fingerprint=excluded.fingerprint",
        (doc_id, 0.0, len(chunks), source, fp),
    )


# ---------------------------------------------------------
# キーワード検索 (RRF の非対称を消すため — 設計判断 D4)
# ---------------------------------------------------------

def _activity_keyword_hits(keywords: str, max_hits: int = 10,
                           db_path: str = DB_PATH) -> list:
    """行動ログのチャンク本文をキーワードで走査する。

    tools/tool_obsidian._keyword_hits と同じ返値の形にする
    (rel_path / filename / score / snippet)。score はマッチした行数。

    **なぜ必要か (D4)**: RRF はランクリストの本数に素直なので、
    Vault のノートが keyword+semantic の2本から加点されるのに対し、
    行動ログが semantic の1本しか持たないと構造的に順位が沈む。

    **もう一つの理由**: PR #23 で検索の依存が Ollama に一本化された
    (埋め込みもリランクも)。Ollama が落ちると意味検索もリランクも死ぬので、
    この経路が無いと行動ログコーパスが丸ごと無言で消える。

    Vault 側と違いファイルを walk しない: 本文は既に SQLite にあるので、
    数百行の SELECT で足りる (ディスクを二度読む理由がない)。
    """
    kw_list = [k.strip() for k in str(keywords or "").split() if k.strip()]
    if not kw_list or not os.path.exists(db_path):
        return []

    try:
        conn = _connect(db_path)
    except Exception:
        return []  # 検索は落とさない (キーワード側が消えるだけ)
    try:
        rows = conn.execute(
            "SELECT filepath, heading, text, source FROM chunks WHERE source IN "
            f"({','.join('?' * len(ACTIVITY_SOURCES))})", ACTIVITY_SOURCES).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    lowered = [k.lower() for k in kw_list]
    by_doc = {}
    for filepath, heading, text, source in rows:
        score = 0
        snippet = ""
        for line in str(text).split("\n"):
            low = line.lower()
            if any(k in low for k in lowered):
                score += 1
                if not snippet:
                    snippet = text
        if not score:
            continue
        # ドキュメントは短いのでチャンク単位ではなく doc 単位に畳み、
        # スコアは足し合わせる (_keyword_hits がファイル単位に合計するのと同じ)。
        # 融合キーが rel_path なので、畳まないと同じ doc が複数ランクに並んで
        # RRF で自分自身を押し上げる。
        current = by_doc.get(filepath)
        if current is None:
            by_doc[filepath] = {
                "rel_path": filepath,
                "filename": os.path.basename(filepath),
                "score": score,
                "snippet": snippet,
                "heading": heading,
                "source": source,
            }
        else:
            current["score"] += score

    hits = sorted(by_doc.values(), key=lambda h: -h["score"])
    return hits[:max_hits] if max_hits and max_hits > 0 else hits


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(update_activity_index())
