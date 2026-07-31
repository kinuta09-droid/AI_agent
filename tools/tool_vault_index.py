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

import inspect
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
from lib.lib_obsidian_utils import format_source_ref
from lib.concurrency import ollama_slot

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

# tool_future_search.py:34 と同値 (プロジェクト慣例: 共有configモジュールはないため各ツールで定義)
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_EMBED_MODEL = "bge-m3"
EMBED_TIMEOUT = 60
EMBED_BATCH_SIZE = 32
# モデルを常駐させておく時間。ロードし直しがレイテンシの主因 (実測 +3.9秒/回) なので
# 長めに取ってある。実測値 (検索1回 5.7〜8.3秒) はこの常駐が前提。
#
# **代償**: リランカ (qwen2.5-coder:7b) が VRAM 4.09GB をこの時間ずっと占有する。
# このマシンの GPU は 6GB で、ComfyUI (lib/lib_comfy_client.py・tool_lora_factory)
# が画像生成に同じ GPU を使う。検索直後に /lora を回すと VRAM が足りず
# 画像生成が失敗・低速化しうる。競合したら OLLAMA_KEEP_ALIVE を短く
# (例 "60s") するか、VAULT_SEARCH_RERANK_BACKEND=gemini に戻す。
# 短くすると常駐が切れた後の1回だけ再ロード分 (数十秒) を払う。
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m")

def _accepts_prefer_cpu(fn) -> bool:
    """fn が prefer_cpu 引数を受け取れるか。判定できなければ False (安全側)。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False  # 組み込み等でシグネチャが取れない場合は渡さない
    if "prefer_cpu" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def query_embed_on_cpu() -> bool:
    """検索時のクエリ埋め込みを CPU で回すか (インデックス構築には効かない)。

    VAULT_QUERY_EMBED_DEVICE = cpu | gpu | auto (既定 auto)

    auto にしてある理由:
      この設定とリランクのバックエンドは **必ず連動していないといけない**。
      ローカルリランク + GPU埋め込み の組み合わせは、VRAM 6GB を
      bge-m3 とリランクモデルが奪い合って毎回入れ替わり、1問 8.3秒 → 15.3秒 になる
      (2026-07-28 実測)。片方だけ切り替えると、動きはするが黙って7秒遅くなる —
      誰も気付けない類の劣化なので、既定では自動で揃える。
      明示的に cpu / gpu と書けば上書きできる。
    """
    raw = os.getenv("VAULT_QUERY_EMBED_DEVICE", "auto").strip().lower()
    if raw == "cpu":
        return True
    if raw == "gpu":
        return False
    # auto: リランクがローカル(ollama)なら VRAM を譲る
    from tools.tool_vault_hybrid import RERANK_BACKEND_DEFAULT  # 遅延importで循環回避
    backend = (os.getenv("VAULT_SEARCH_RERANK_BACKEND") or RERANK_BACKEND_DEFAULT)
    return backend.strip().lower() == "ollama"

DB_PATH = os.path.join(_PROJECT_ROOT, "logs", "vault_index.db")

# chunks/files.source の値。Vault の .md は "vault"、エージェントの行動ログは
# tools/tool_activity_index.py が別の値 ("crypto" 等) で同じ DB に同居する。
#
# **この定数がある理由**: update_vault_index() は「Vault 走査で見つからなかった
# files 行」を削除する。source で閉じずに書くと、次の夜間ジョブが行動ログを
# 全消しする (しかもログに一行も残らない)。同居する以上、この indexer の
# 読み書きは必ず source='vault' に限定すること (Phase B の設計判断 D2)。
SOURCE_VAULT = "vault"

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
                       timeout: int = EMBED_TIMEOUT, prefer_cpu: bool = False):
    """Ollama /api/embed でテキスト群を埋め込む。

    Returns:
        成功時: 埋め込みベクトルのリスト (入力と同数)。
        失敗時: "[ERROR] ..." 文字列 (tool_future_search._call_ollama と同じソフトエラー規約)。
        呼び出し側は isinstance(result, str) でエラー判定すること。

    prefer_cpu の用途 (2026-07-28 実測):
      ローカルリランクを使うと、検索1回ごとに bge-m3 とリランクモデルが
      VRAM (6GB) を奪い合って入れ替わり、1問あたり約8秒をモデルのロードに
      払っていた (/api/ps で bge-m3 が VRAM から消えるのを確認)。
      **クエリ1本の埋め込みは CPU でも 2.3秒**なので、そちらを CPU に逃がすと
      リランクモデルが VRAM に居座れて 16.2秒 → 9.0秒 になる。

      **インデックス構築 (update_vault_index) では絶対に使わない**:
      あちらは数千チャンクをバッチで埋め込むスループット勝負で、
      CPU に落とすと夜間バッチが桁違いに遅くなる。
      検索時 (_semantic_hits) だけに効かせること。
    """
    payload = {"model": model, "input": texts}
    if prefer_cpu:
        payload["options"] = {"num_gpu": 0}
        # GPU を明け渡したまま常駐させる (毎回ロードし直すと CPU 化の意味が薄れる)
        payload["keep_alive"] = OLLAMA_KEEP_ALIVE
    try:
        # Phase C0: 埋め込みとリランクは同じ Ollama / 同じ 6GB VRAM を奪い合う。
        # fan-out (Phase C1) が同時に検索を走らせても、ここを直列に保つ。
        # 並列していないときは素通り (取得コストは Lock 1回分)。
        with ollama_slot():
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/embed",
                json=payload,
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


def _is_table_delimiter(line: str) -> bool:
    """Markdown 表の区切り行 (`|---|:---:|`) か。

    水平線 `---` を誤検知しないよう **`|` を必須**にしてある
    (`---` 単独は表ではなくセクション区切り)。
    """
    s = line.strip()
    return bool(s) and "|" in s and "-" in s and set(s) <= set("|-: \t")


def _table_start_index(lines: list):
    """行リストの中で「ヘッダ行 + 区切り行」が始まる位置。無ければ None。

    GFM の表はヘッダの直後に区切り行が来る、という一点だけで判定する。
    セル内容には何が入っていてもよい。
    """
    for i in range(len(lines) - 1):
        if "|" in lines[i] and _is_table_delimiter(lines[i + 1]):
            return i
    return None


def _split_table_block(text: str, max_chars: int):
    """表を含む段落を「行単位」で分割し、各片にヘッダ行を再掲する。

    **なぜ必要か** (2026-07-30):
      Markdown の表は空行を含まない1ブロックなので、_split_by_paragraphs から
      見ると「巨大な単一段落」に見える。従来はそのままハードスプリット
      (`para[:max_chars]`) に落ちていたため、**行の途中で切断され、2片目以降が
      ヘッダ行を失っていた**。検索でヒットしても `| 3 | 0.82 | 0.71 |` のように
      列名の無い数字の羅列になり、LLM には解釈できない。
      表が壊れていること自体はログにも出ないので、誰も気付けない。

    設計上の割り切り:
      1行が単体で max_chars を超える場合、**その行は割らずにそのまま出す**
      (max_chars を超えるチャンクが1つできる)。行の途中で切るのは、まさに今
      直そうとしている壊れ方そのものなので、長さの制約より行の完全性を優先する。
      埋め込みモデル (bge-m3) の入力長は 1200 字よりはるかに大きいので実害はない。

    Returns:
        [str, ...] 分割後の断片。表として解釈できなければ None
        (呼び出し側は従来のハードスプリットにフォールバックする)
    """
    lines = text.split("\n")
    start = _table_start_index(lines)
    if start is None:
        return None

    pieces = []
    preamble = "\n".join(lines[:start]).strip()
    if preamble:
        # 表の前にある地の文 (キャプション等) は表と切り離して1片にする。
        # ここを表側に混ぜるとヘッダ再掲のたびに重複して埋め込まれる。
        # 地の文が単体で上限を超えるときは従来どおり文字位置で割ってよい
        # (行の完全性を守る必要があるのは表だけ)。
        while len(preamble) > max_chars:
            pieces.append(preamble[:max_chars])
            preamble = preamble[max_chars:]
        if preamble.strip():
            pieces.append(preamble)

    header = "\n".join(lines[start:start + 2])
    rows = [ln for ln in lines[start + 2:] if ln.strip()]

    current_rows = []
    current_len = len(header)
    for row in rows:
        if current_rows and current_len + 1 + len(row) > max_chars:
            pieces.append(header + "\n" + "\n".join(current_rows))
            current_rows = []
            current_len = len(header)
        current_rows.append(row)
        current_len += 1 + len(row)
    if current_rows:
        pieces.append(header + "\n" + "\n".join(current_rows))
    return pieces


def _split_by_paragraphs(text: str, max_chars: int, overlap: int) -> list:
    """段落(\\n\\n)境界で貪欲に詰めながら max_chars 以下に分割する。

    直前チャンクの末尾 overlap 文字を次チャンクの先頭に引き継ぐ。
    単一段落が max_chars を超える場合はハードスプリットする。
    ただし **表だけは行単位で分割しヘッダを再掲する** (_split_table_block)。
    """
    paragraphs = text.split("\n\n")
    pieces = []
    current = ""
    for para in paragraphs:
        # 上限超えの段落が表なら、文字位置ではなく行単位で割る
        if len(para) > max_chars:
            table_pieces = _split_table_block(para, max_chars)
            if table_pieces is not None:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(table_pieces)
                continue

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
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS files (
            filepath    TEXT PRIMARY KEY,
            mtime       REAL NOT NULL,
            chunk_count INTEGER NOT NULL,
            source      TEXT NOT NULL DEFAULT '{SOURCE_VAULT}'
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath    TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading     TEXT,
            text        TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            dim         INTEGER NOT NULL,
            source      TEXT NOT NULL DEFAULT '{SOURCE_VAULT}'
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_filepath ON chunks(filepath);
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBに不足しているカラムを足す (冪等)。

    `CREATE TABLE IF NOT EXISTS` は既存テーブルを一切変更しないので、
    スキーマを増やしたときはここで明示的に ALTER する必要がある。

    追加する列は全て **additive** (NULL 可 or DEFAULT 付き) にする。既存行の
    embedding に触れないので 5000件規模の再埋め込みが発生しない
    (Phase B の設計判断 D1)。列を増やす以外の移行は行わないこと —
    PK の作り直しはテーブル再構築になり、その瞬間に埋め込みを失う。

    fingerprint は行動ログ側 (tool_activity_index) の変更検知に使う。
    Vault は mtime で差分を取れるが、行動ログのドキュメントは JSON から
    毎回組み立てるので mtime に相当するものが無い。本文のハッシュを持たせて
    「中身が変わったものだけ再埋め込み」を成立させる。
    """
    migrations = (
        ("files", "source", f"TEXT NOT NULL DEFAULT '{SOURCE_VAULT}'"),
        ("chunks", "source", f"TEXT NOT NULL DEFAULT '{SOURCE_VAULT}'"),
        ("files", "fingerprint", "TEXT"),
    )
    for table, column, decl in migrations:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            print(f"[VaultIndex] 🔧 migrate: {table}.{column} を追加しました")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
    conn.commit()


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
            # ここだけは source で閉じない: モデル/次元が変わると **全ソースの**
            # 埋め込みが等しく無効になるため。行動ログ側は行が消えれば
            # 「未登録」として次回の tool_activity_index 実行で作り直される
            # (差分判定が doc_id の有無なので自己修復する)。
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            _set_meta(conn, "embedding_model", OLLAMA_EMBED_MODEL)
            _set_meta(conn, "embedding_dim", str(dim))
            conn.commit()

        # source='vault' に限定する。ここを開けると、下の「消えたファイルの掃除」が
        # 行動ログの doc_id を「Vault に無いファイル」とみなして全消しする (D2)。
        stored_mtimes = dict(conn.execute(
            "SELECT filepath, mtime FROM files WHERE source=?", (SOURCE_VAULT,)).fetchall())

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

                conn.execute("DELETE FROM chunks WHERE filepath=? AND source=?",
                             (rel_path, SOURCE_VAULT))
                for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    arr = _normalize(np.asarray(vec, dtype=np.float32))
                    conn.execute(
                        "INSERT INTO chunks(filepath, chunk_index, heading, text, "
                        "embedding, dim, source) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (rel_path, idx, chunk["heading"], chunk["text"],
                         arr.tobytes(), dim, SOURCE_VAULT),
                    )
                conn.execute(
                    "INSERT INTO files(filepath, mtime, chunk_count, source) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(filepath) DO UPDATE SET mtime=excluded.mtime, "
                    "chunk_count=excluded.chunk_count, source=excluded.source",
                    (rel_path, mtime, len(chunks), SOURCE_VAULT),
                )
                conn.commit()
                updated += 1
            except Exception as e:
                print(f"[VaultIndex] ⚠️ Failed to index {rel_path}: {e}")
                failed += 1

        # 削除・移動されたファイルの行を除去。
        # stored_mtimes が source='vault' 限定なので差集合に行動ログは入らないが、
        # DELETE 側にも source を書く: 片方だけの防御は、後から片方だけ
        # 書き換えられたときに黙って穴が開く (D2)。
        removed = 0
        for rel_path in set(stored_mtimes) - seen:
            conn.execute("DELETE FROM chunks WHERE filepath=? AND source=?",
                         (rel_path, SOURCE_VAULT))
            conn.execute("DELETE FROM files WHERE filepath=? AND source=?",
                         (rel_path, SOURCE_VAULT))
            removed += 1
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM chunks WHERE source=?",
                             (SOURCE_VAULT,)).fetchone()[0]
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

    返値: [{"rel_path", "filename", "score", "snippet", "heading", "chunk_id",
            "source"}, ...] (成功)
          str (失敗: 未構築 / [ERROR] / 空index — 例外は投げない規約)
    ハイブリッド検索は返値が list かどうかでフォールバックを判定する。

    **source をまたいで検索する** (Phase B): Vault のノートと行動ログを
    同じ検索面から引くのが目的なので、ここでは絞り込まない。
    表示側 (lib_obsidian_utils.format_source_ref) が source を見て
    [[wikilink]] を出すかどうかを決める — 行動ログは Obsidian ノートでは
    ないので [[ ]] を出すと「Obsidianで開く」ボタンが空振りする。

    heading / chunk_id を返す理由 (Phase A2):
      呼び出し側が出典を [[ファイル名#見出し]] で表示できるようにするため。
      chunk_id ("30_Main/foo.md#3") はチャンクの一意キーで、リランクや
      評価スクリプトが「どのチャンクが選ばれたか」をログに残すのに使う。
      chunks.chunk_index / chunks.heading は既存カラムなので再インデックス不要。
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

    # クエリ埋め込みだけ CPU に逃がせるようにする (インデックス構築は GPU のまま)。
    # embed_fn を注入するテストは prefer_cpu を受け取らないので、シグネチャを見て
    # 対応している場合だけ渡す。
    #
    # try/except TypeError で握らない理由:
    #   embed_fn の**内部**で TypeError が出た場合まで拾ってしまい、
    #   (a) 本物のバグを「引数が合わなかった」と誤認して隠し、
    #   (b) リトライで埋め込みAPIを二度叩く。
    #   「引数を受け取れるか」は呼ぶ前に分かる情報なので、呼んで確かめない。
    if _accepts_prefer_cpu(embed_fn):
        result = embed_fn([str(query)], prefer_cpu=query_embed_on_cpu())
    else:
        result = embed_fn([str(query)])
    if isinstance(result, str):
        return result
    query_vec = _normalize(np.asarray(result[0], dtype=np.float32))

    conn = _connect(db_path)
    try:
        # source は末尾に足す: r[3] (embedding) など既存の位置参照を動かさない。
        # ORDER BY は「同点のときの順序」を行の物理配置から切り離すために要る
        # (下の argsort と対で意味を持つ — 片方だけでは決定的にならない)。
        rows = conn.execute(
            "SELECT filepath, heading, text, embedding, chunk_index, source "
            "FROM chunks ORDER BY filepath, chunk_index"
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
    # kind="stable" は必須。既定の quicksort は不安定なので、**同点のチャンクの
    # 順序が配列の中身に依存する**。実際 Phase B で行動ログ 178 行を同じテーブルに
    # 足したとき、無関係なはずの R03 (「使う人に合わせて賢くなるエージェント」) で
    # スコアが完全同値 (0.5807) の2ノートが入れ替わり、それがリランカに渡す
    # 候補の番号を1つずらして最終順位を 4位→7位 に落とした。
    # 行が増えただけで既存の順位が動くのは、それ自体が欠陥。
    # 上の ORDER BY と対で「行集合に依存しない決定的な順序」を作る。
    top_indices = np.argsort(-scores, kind="stable")[:top_k]

    hits = []
    for i in top_indices:
        filepath, heading, text, _, chunk_index, source = rows[int(i)]
        # サブ分割で見出し行が落ちたピースには見出しを補う (文脈をプランナーに渡す)
        snippet = text if (not heading or text.startswith("##")) else f"## {heading}\n{text}"
        hits.append({
            "rel_path": filepath,
            "filename": os.path.basename(filepath),
            "score": float(scores[int(i)]),
            "snippet": snippet,
            "heading": heading,
            "chunk_id": f"{filepath}#{chunk_index}",
            "source": source or SOURCE_VAULT,
        })
    return hits


def tool_vault_semantic_search(query: str, top_k: int = 5,
                               embed_fn=_call_ollama_embed,
                               db_path: str = DB_PATH) -> str:
    """Vault を意味ベースで検索する (embedding コサイン類似度)。

    出力形式は tool_vault_search と揃える (プランナー側の扱いを変えないため)。
    Vault のノートは見出しがあれば [[ファイル名#見出し]] — 回答の出典として
    Obsidian でクリックすれば該当セクションまで辿れる (Phase A2)。
    行動ログ (Phase B) は実ファイルではないので [[ ]] にしない
    — format_source_ref がその判断を持つ。
    """
    hits = _semantic_hits(query, top_k, embed_fn=embed_fn, db_path=db_path)
    if isinstance(hits, str):
        return hits
    return "\n\n".join(
        f"- File: {format_source_ref(h.get('source'), h['filename'], h.get('heading'))} "
        f"(score: {h['score']:.3f})\n  Snippet:\n{h['snippet']}\n..."
        for h in hits
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(update_vault_index())
