# -*- coding: utf-8 -*-
"""
Vault Semantic Index — Obsidian Vault のセマンティック検索 (RAG)

Ollama (bge-m3) でローカル埋め込みを生成し、SQLite に永続化する。
検索は numpy のブルートフォース内積 (正規化済みベクトルのコサイン類似度)。
Vault 規模 (~600ファイル・数千チャンク) では ANN ライブラリは不要。

構成:
  _call_ollama_embed()          — Ollama /api/embed へのバッチ埋め込み呼び出し
  _chunk_markdown()             — Markdown を見出し単位でチャンク分割 (純粋関数)
  update_vault_index()          — mtime 差分による増分インデックス更新 (夜間バッチ)
  tool_vault_semantic_search()  — プランナーから呼ばれる意味検索ツール

初回セットアップ (ユーザー手動、1回のみ):
  ollama pull bge-m3
"""

import os
import re
import sys
import sqlite3

import numpy as np
import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.lib_shared import VAULT_ROOT, EXCLUDE_DIRS

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

# tool_future_search.py:34 と同値 (プロジェクト慣例: 共有configモジュールはないため各ツールで定義)
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_EMBED_MODEL = "bge-m3"
EMBED_TIMEOUT = 60
EMBED_BATCH_SIZE = 32

DB_PATH = os.path.join(_PROJECT_ROOT, "logs", "vault_index.db")

# テンプレートは検索結果を汚染するためインデックス対象から除外する
# (共有の EXCLUDE_DIRS は変更せず、このモジュール専用の合成セットを使う)
_INDEX_EXCLUDE_DIRS = set(EXCLUDE_DIRS) | {"Templates"}

CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP = 150
CHUNK_MIN_CHARS = 20


# ---------------------------------------------------------
# Ollama Embedding
# ---------------------------------------------------------

def _call_ollama_embed(texts: list, model: str = OLLAMA_EMBED_MODEL,
                       timeout: int = EMBED_TIMEOUT):
    """Ollama /api/embed でテキスト群を埋め込む。

    Returns:
        成功時: 埋め込みベクトルのリスト (入力と同数)。
        失敗時: "[ERROR] ..." 文字列 (tool_future_search._call_ollama と同じソフトエラー規約)。
        呼び出し側は isinstance(result, str) でエラー判定すること。
    """
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": model, "input": texts},
            timeout=timeout,
        )
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            return "[ERROR] Ollama returned no/mismatched embeddings."
        return embeddings
    except requests.exceptions.ConnectionError:
        return "[ERROR] Ollama not running. Start with: ollama serve"
    except requests.exceptions.Timeout:
        return f"[ERROR] Ollama embed timeout after {timeout}s"
    except Exception as e:
        return f"[ERROR] Ollama embed call failed: {e}"


# ---------------------------------------------------------
# Chunking (pure function — no I/O)
# ---------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_H2_SPLIT_RE = re.compile(r"^(?=##\s)", re.MULTILINE)
_H2_HEADING_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def _chunk_markdown(text: str, max_chars: int = CHUNK_MAX_CHARS,
                    overlap: int = CHUNK_OVERLAP) -> list:
    """Markdown を見出し(##)単位でチャンク分割する。

    - フロントマターは除去 (埋め込みノイズ)
    - ## 見出しがないノートは全体を1チャンク
    - max_chars 超のセクションは段落境界でオーバーラップ付きサブ分割
    - CHUNK_MIN_CHARS 未満のゴミチャンクは除去

    Returns:
        [{"heading": str | None, "text": str}, ...]
    """
    # CRLF 正規化 (Windows 編集ノートでフロントマター除去・見出し分割が失敗しないように)
    text = text.replace("\r\n", "\n")
    # overlap >= max_chars だとハードスプリットが無限ループするため防御クランプ
    overlap = min(overlap, max_chars // 2)
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    if not body:
        return []

    sections = _H2_SPLIT_RE.split(body)
    chunks = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        m = _H2_HEADING_RE.match(section)
        heading = m.group(1).strip() if m else None

        if len(section) <= max_chars:
            pieces = [section]
        else:
            pieces = _split_by_paragraphs(section, max_chars, overlap)

        for piece in pieces:
            piece = piece.strip()
            if len(piece) < CHUNK_MIN_CHARS:
                continue
            chunks.append({"heading": heading, "text": piece})
    return chunks


def _split_by_paragraphs(text: str, max_chars: int, overlap: int) -> list:
    """段落(\\n\\n)境界で貪欲に詰めながら max_chars 以下に分割する。

    直前チャンクの末尾 overlap 文字を次チャンクの先頭に引き継ぐ。
    単一段落が max_chars を超える場合はハードスプリットする。
    """
    paragraphs = text.split("\n\n")
    pieces = []
    current = ""
    for para in paragraphs:
        # 単一段落が上限を超える場合はハードスプリット
        while len(para) > max_chars:
            head, para = para[:max_chars], para[max_chars - overlap:]
            if current:
                pieces.append(current)
                current = ""
            pieces.append(head)

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = current[-overlap:] + "\n\n" + para if overlap > 0 else para
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces


# ---------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    """DB接続を開き、スキーマを保証する。"""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            filepath    TEXT PRIMARY KEY,
            mtime       REAL NOT NULL,
            chunk_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath    TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading     TEXT,
            text        TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            dim         INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_filepath ON chunks(filepath);
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    return conn


def _get_meta(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _walk_vault_md(vault_root: str):
    """Vault 内の .md ファイルを (絶対パス, 相対パス) で列挙する。"""
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in _INDEX_EXCLUDE_DIRS]
        for file in files:
            if not file.endswith(".md"):
                continue
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, vault_root).replace("\\", "/")
            yield abs_path, rel_path


# ---------------------------------------------------------
# Incremental indexer (nightly batch)
# ---------------------------------------------------------

def update_vault_index(embed_fn=_call_ollama_embed, db_path: str = DB_PATH,
                       vault_root: str = None) -> str:
    """Vault を走査し、変更のあったファイルだけ再チャンク・再埋め込みする。

    embed_fn / db_path / vault_root は注入可能 (テスト用)。

    Returns:
        サマリー文字列、または "[ERROR] ..." (Ollama 不通時など)。
    """
    vault_root = vault_root or VAULT_ROOT

    # Ollama 疎通確認: ダメなら Vault 走査すらせず即座に返す
    probe = embed_fn(["ping"])
    if isinstance(probe, str):
        return probe
    dim = len(probe[0])

    conn = _connect(db_path)
    try:
        # モデル/次元が変わっていたらフルリインデックス
        stored_model = _get_meta(conn, "embedding_model")
        stored_dim = _get_meta(conn, "embedding_dim")
        if stored_model != OLLAMA_EMBED_MODEL or stored_dim != str(dim):
            if stored_model is not None:
                print(f"[VaultIndex] ⚠️ Embedding model/dim changed "
                      f"({stored_model}/{stored_dim} -> {OLLAMA_EMBED_MODEL}/{dim}). Full reindex.")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            _set_meta(conn, "embedding_model", OLLAMA_EMBED_MODEL)
            _set_meta(conn, "embedding_dim", str(dim))
            conn.commit()

        stored_mtimes = dict(conn.execute("SELECT filepath, mtime FROM files").fetchall())

        updated = 0
        failed = 0
        seen = set()

        for abs_path, rel_path in _walk_vault_md(vault_root):
            seen.add(rel_path)
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue
            if stored_mtimes.get(rel_path) == mtime:
                continue  # 未変更 — 読み込みも埋め込みもしない

            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                chunks = _chunk_markdown(content)

                # チャンクテキストは分割方式上、見出し行 (## ...) を先頭に含む
                # ためそのまま埋め込む (サブ分割された後続ピースには含まれないが許容)
                embed_texts = [c["text"] for c in chunks]
                vectors = []
                embed_error = False
                for i in range(0, len(embed_texts), EMBED_BATCH_SIZE):
                    batch = embed_texts[i:i + EMBED_BATCH_SIZE]
                    result = embed_fn(batch)
                    if isinstance(result, str):
                        # このファイルだけスキップ (旧データ温存 → 次回リトライ)、走査は継続
                        print(f"[VaultIndex] ⚠️ Embed failed for {rel_path}: {result}")
                        embed_error = True
                        break
                    vectors.extend(result)
                if embed_error:
                    failed += 1
                    continue

                conn.execute("DELETE FROM chunks WHERE filepath=?", (rel_path,))
                for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    arr = _normalize(np.asarray(vec, dtype=np.float32))
                    conn.execute(
                        "INSERT INTO chunks(filepath, chunk_index, heading, text, embedding, dim) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (rel_path, idx, chunk["heading"], chunk["text"],
                         arr.tobytes(), dim),
                    )
                conn.execute(
                    "INSERT INTO files(filepath, mtime, chunk_count) VALUES(?, ?, ?) "
                    "ON CONFLICT(filepath) DO UPDATE SET mtime=excluded.mtime, "
                    "chunk_count=excluded.chunk_count",
                    (rel_path, mtime, len(chunks)),
                )
                conn.commit()
                updated += 1
            except Exception as e:
                print(f"[VaultIndex] ⚠️ Failed to index {rel_path}: {e}")
                failed += 1

        # 削除・移動されたファイルの行を除去
        removed = 0
        for rel_path in set(stored_mtimes) - seen:
            conn.execute("DELETE FROM chunks WHERE filepath=?", (rel_path,))
            conn.execute("DELETE FROM files WHERE filepath=?", (rel_path,))
            removed += 1
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return (f"Vault index: {updated} files updated, {removed} removed, "
                f"{failed} failed, {total} chunks total.")
    finally:
        conn.close()


# ---------------------------------------------------------
# Semantic search tool (planner-facing)
# ---------------------------------------------------------

def _semantic_hits(query: str, top_k: int = 5,
                   embed_fn=_call_ollama_embed,
                   db_path: str = DB_PATH):
    """意味検索のコア。構造化ヒット(スコア降順) or soft-error 文字列を返す。

    返値: [{"rel_path", "filename", "score", "snippet"}, ...] (成功)
          str (失敗: 未構築 / [ERROR] / 空index — 例外は投げない規約)
    ハイブリッド検索は返値が list かどうかでフォールバックを判定する。
    """
    if not query or not str(query).strip():
        return "No query."
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 5

    if not os.path.exists(db_path):
        return ("Vault semantic index が未構築です。夜間ジョブ (03:15) の実行を待つか、"
                "update_vault_index() を手動実行してください。")

    result = embed_fn([str(query)])
    if isinstance(result, str):
        return result
    query_vec = _normalize(np.asarray(result[0], dtype=np.float32))

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT filepath, heading, text, embedding FROM chunks"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return ("Vault semantic index が空です。夜間ジョブ (03:15) の実行を待つか、"
                "update_vault_index() を手動実行してください。")

    matrix = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    if matrix.shape[1] != query_vec.shape[0]:
        return ("[ERROR] インデックスの埋め込み次元がクエリと一致しません。"
                "update_vault_index() で再構築してください。")
    scores = matrix @ query_vec
    top_indices = np.argsort(-scores)[:top_k]

    hits = []
    for i in top_indices:
        filepath, heading, text, _ = rows[int(i)]
        # サブ分割で見出し行が落ちたピースには見出しを補う (文脈をプランナーに渡す)
        snippet = text if (not heading or text.startswith("##")) else f"## {heading}\n{text}"
        hits.append({
            "rel_path": filepath,
            "filename": os.path.basename(filepath),
            "score": float(scores[int(i)]),
            "snippet": snippet,
        })
    return hits


def tool_vault_semantic_search(query: str, top_k: int = 5,
                               embed_fn=_call_ollama_embed,
                               db_path: str = DB_PATH) -> str:
    """Vault を意味ベースで検索する (embedding コサイン類似度)。

    出力形式は tool_vault_search と揃える (プランナー側の扱いを変えないため)。
    """
    hits = _semantic_hits(query, top_k, embed_fn=embed_fn, db_path=db_path)
    if isinstance(hits, str):
        return hits
    return "\n\n".join(
        f"- File: [[{h['filename']}]] (score: {h['score']:.3f})\n  Snippet:\n{h['snippet']}\n..."
        for h in hits
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(update_vault_index())
