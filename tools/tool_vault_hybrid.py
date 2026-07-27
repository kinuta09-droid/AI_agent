# -*- coding: utf-8 -*-
"""
Vault ハイブリッド検索 (Phase ④ → Phase A2 拡張)

tool_obsidian._keyword_hits (キーワード) と tool_vault_index._semantic_hits (意味) を
RRF (Reciprocal Rank Fusion, k=60) でマージする。semantic index 未構築 / Ollama 停止 /
numpy 欠落時はキーワード検索のみに自動フォールバックする。

Phase A2 で3段重ねた:
  1. multi-query 書き換え — 1問を Light Gemini で複数の言い換えに分解し、
     全変種のランクリストを RRF に流す。質問の語彙とノートの語彙のズレを吸収する。
     特にキーワード側は「日本語の一文まるごと」を部分文字列照合するため、
     書き換えが無いとほぼ必ず 0 件になり、実質セマンティック単独になっていた。
  2. LLM リランク — RRF は「順位」しか見ず中身を読まない。上位候補を Light Gemini に
     渡し「本当に質問に答えているのはどれか」で並べ替える。
  3. 出典 (chunk_id 貫通) — 出力を [[ファイル名#見出し]] にする。これが無いと
     回答が正しいかをユーザーが検証できない。

いずれも **失敗したら黙って元の挙動に戻る** (例外を投げない) が、
戻った理由は必ず戻り値と stdout の両方に残す。無言で劣化すると
「壊れていても誰も気付けない」状態になる (CLAUDE.md /curate の教訓)。

意図的に tool_vault_index を遅延 import する: あちらはモジュールトップで numpy を
import するため、同居させると numpy 欠落環境で検索ツールが全滅する。本モジュールは
常に import 可能なのでレジストリ entry に optional を付けない。

環境変数 (未設定＝ON。切りたいときだけ .env に 0 を書く):
  VAULT_SEARCH_MULTIQUERY=0  — クエリ書き換えを止める
  VAULT_SEARCH_RERANK=0      — LLM リランクを止める
"""

import json
import os
import re

from lib.lib_obsidian_utils import format_wikilink
from tools.tool_obsidian import _keyword_hits

RRF_K = 60
NOT_FOUND = "Vault内に見つかりませんでした。"

# 原クエリに追加する言い換えの本数 (計 1 + MULTIQUERY_VARIANTS 本を検索する)
MULTIQUERY_VARIANTS = 2
# リランクに渡す RRF 上位件数。多すぎるとプロンプトが膨れ Light の精度が落ちる
RERANK_CANDIDATES = 20
# リランクプロンプトに載せる 1 候補あたりのスニペット長
RERANK_SNIPPET_CHARS = 600


# ---------------------------------------------------------
# 設定・LLM 呼び出し
# ---------------------------------------------------------

def _enabled(name: str, override=None) -> bool:
    """機能フラグ。override が bool ならそれを優先、None なら環境変数を見る。

    環境変数は未設定なら ON、"0"/"false"/"no"/"off" のみ OFF。
    import 時ではなく呼び出し時に読む: .env 差し替え後の再起動だけで効かせるため。

    override があるのは scripts/eval_retrieval.py の A/B 計測用。
    os.environ を書き換えて切り替える方式は採らない (CLAUDE.md の
    スコープ階層 LEVEL 2: プロセス環境の書き換えは子プロセスまで汚す)。
    """
    if override is not None:
        return bool(override)
    raw = os.getenv(name)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*|```")
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_json_array(raw) -> list:
    """LLM 出力から JSON 配列を取り出す。取れなければ空リスト。

    lib_shared.safe_json_loads を使わない理由:
      あれはトップレベル配列を {"plan": [...]} に包んで返す (Planner の出力形式に
      合わせた仕様)。この2箇所が欲しいのは素の配列なので、"plan" というキー名に
      依存すると Planner 側の都合でここが黙って壊れる。
    """
    if not isinstance(raw, str):
        return []
    text = _JSON_FENCE_RE.sub("", raw).strip()
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _call_light_llm(prompt: str) -> str:
    """Light Gemini を1回呼んで生テキストを返す。

    lib_shared は import コストが重く (genai/tavily クライアント初期化) 、
    フラグ OFF のときに払う理由がないので関数内で遅延 import する。
    共有 client を使うのは cost_guard (日次上限) を必ず通すため — 別経路を作らない。
    """
    from lib.lib_shared import client, MODEL_NAME
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return (response.text or "").strip()


# ---------------------------------------------------------
# 施策1: multi-query 書き換え
# ---------------------------------------------------------

_EXPAND_PROMPT = """あなたは個人のObsidianノート(日本語)を検索するためのクエリを作る係です。

【元の質問】
{query}

このノート群から関連ノートを見つけるための**検索クエリ**を{n}個作ってください。

規則:
- 質問文をそのまま繰り返さない。ノート本文に実際に書かれていそうな語を使う
- 1クエリは半角スペース区切りの単語2〜5個。文章にしない
- 表記ゆれを意図的にばらす (英語表記・カタカナ・略語・固有名詞・年月など)
- {n}個は互いに違う切り口にする (同義語の言い換えだけを並べない)

出力は説明なしのJSON配列のみ:
["クエリ1", "クエリ2"]"""


def _expand_queries(query: str, llm_fn=None, n: int = MULTIQUERY_VARIANTS,
                    enabled=None):
    """原クエリ + 言い換えのリストを返す。

    Returns:
        (queries, note) — queries[0] は必ず原クエリ。note は "" か理由文字列。

    どんな失敗 (LLM例外・予算超過・パース不能・空配列) でも例外は投げず
    [query] に戻る。

    note を出す条件が「設定でOFF」と「予期しないフォールバック」で違う理由:
      note は Planner とユーザーが読む戻り値に混ざるので、毎回出すと文脈を汚す。
      OFF は本人が .env でそう設定した既知の状態なので stdout (ログ) だけに残し、
      フォールバックは「ONにしたつもりが効いていない」＝気付けないと困る事故なので
      戻り値にも残す。
    """
    query = str(query).strip()
    if not _enabled("VAULT_SEARCH_MULTIQUERY", enabled):
        print("[Hybrid] クエリ書き換えは無効 (VAULT_SEARCH_MULTIQUERY=0)")
        return [query], ""

    try:
        raw = (llm_fn or _call_light_llm)(_EXPAND_PROMPT.format(query=query, n=n))
        variants = [str(v).strip() for v in _parse_json_array(raw)]
    except Exception as e:
        print(f"[Hybrid] ⚠️ クエリ書き換えに失敗、原クエリのみで検索します: {e}")
        return [query], f"クエリ書き換え: フォールバック ({type(e).__name__}: {e})"

    queries = [query]
    for v in variants:
        if v and v not in queries:
            queries.append(v)
        if len(queries) > n:
            break
    if len(queries) == 1:
        print("[Hybrid] ⚠️ クエリ書き換えが有効な変種を返しませんでした、原クエリのみで検索します")
        return queries, "クエリ書き換え: フォールバック (有効な変種が0件)"
    return queries, ""


# ---------------------------------------------------------
# 施策2: LLM リランク
# ---------------------------------------------------------

_RERANK_PROMPT = """あなたは検索結果の適合性を判定する係です。

【ユーザーの質問】
{query}

【候補】
{candidates}

質問に**実際に答えている**候補を、関連が強い順に並べて番号のJSON配列で返してください。

規則:
- 質問の語が出てくるだけで内容が答えになっていない候補は選ばない
- 明らかに無関係な候補は含めない (全部並べる必要はない)
- 番号は候補に実在するものだけを使う。説明・コードブロックは書かない

出力例: [3, 1, 7]"""


def _format_rerank_candidates(candidates: list) -> str:
    lines = []
    for i, h in enumerate(candidates, start=1):
        link = format_wikilink(h["filename"], h.get("heading"))
        body = str(h.get("snippet", ""))[:RERANK_SNIPPET_CHARS]
        lines.append(f"[{i}] {link}\n{body}")
    return "\n\n".join(lines)


def _llm_rerank(query: str, candidates: list, llm_fn=None, enabled=None):
    """RRF 上位候補を LLM の判断で並べ替える。

    Returns:
        (ordered, note) — ordered は candidates の並べ替え。note は "" か理由文字列。

    **足切りではなく並べ替え**である点が重要: LLM が挙げなかった候補は捨てず、
    RRF 順のまま末尾に連結する。LLM は再現性が無いので、選から漏れた=無関係とは
    断定できない。捨ててよいのは top_k による打ち切りだけ。
    """
    if not candidates:
        return candidates, ""
    if not _enabled("VAULT_SEARCH_RERANK", enabled):
        # OFF は既知の設定なのでログのみ。note は事故 (フォールバック) 専用
        # — _expand_queries の docstring 参照。
        print("[Hybrid] LLMリランクは無効 (VAULT_SEARCH_RERANK=0)")
        return candidates, ""

    try:
        prompt = _RERANK_PROMPT.format(
            query=str(query).strip(), candidates=_format_rerank_candidates(candidates))
        order = _parse_json_array((llm_fn or _call_light_llm)(prompt))
    except Exception as e:
        print(f"[Hybrid] ⚠️ LLMリランクに失敗、RRF順のまま返します: {e}")
        return candidates, f"LLMリランク: フォールバック ({type(e).__name__}: {e})"

    picked = []
    seen = set()
    for raw in order:
        try:
            idx = int(raw) - 1  # プロンプトは1始まり
        except (TypeError, ValueError):
            continue  # "3位" のような非数値は無視 (落とさない)
        if 0 <= idx < len(candidates) and idx not in seen:
            seen.add(idx)
            candidates[idx]["rerank_pos"] = len(picked) + 1
            picked.append(candidates[idx])

    if not picked:
        print("[Hybrid] ⚠️ LLMリランクが有効な番号を返しませんでした、RRF順のまま返します")
        return candidates, "LLMリランク: フォールバック (有効な番号が0件)"

    rest = [h for i, h in enumerate(candidates) if i not in seen]
    return picked + rest, ""


# ---------------------------------------------------------
# RRF 融合
# ---------------------------------------------------------

def _resolve_semantic_fn(semantic_fn):
    """semantic 側の呼び出し関数を1回だけ解決する。

    numpy 欠落・モジュール破損はここで確定するので、クエリ変種ごとに
    import を再試行して同じ失敗を N 回繰り返すことがない。
    """
    if semantic_fn is not None:
        return semantic_fn, ""
    try:
        from tools.tool_vault_index import _semantic_hits
        return _semantic_hits, ""
    except Exception as e:
        return None, f"[ERROR] semantic search unavailable: {e}"


def _rank_lists(queries: list, keyword_fn, semantic_fn, per_query_k: int):
    """各クエリ変種について (source, query_index, ランク済みヒット列) を列挙する。

    semantic 側の soft-error / 例外は keyword を道連れにしない
    (ImportError=numpy欠落、sqlite3.DatabaseError=index破損、
     ValueError=次元不一致blob のいずれでも継続する)。
    Returns: (rank_lists, semantic_error) — semantic_error は最初の失敗理由。
    """
    rank_lists = []
    semantic_error = ""
    for qi, q in enumerate(queries, start=1):
        rank_lists.append(("keyword", qi, sorted(keyword_fn(q), key=lambda h: -h["score"])))

        if semantic_fn is None:
            continue
        try:
            # チャンク単位のヒットをファイル単位に集約するため多めに取る
            sem_result = semantic_fn(q, per_query_k)
        except Exception as e:
            sem_result = f"[ERROR] semantic search unavailable: {e}"
        if isinstance(sem_result, str):
            if not semantic_error:
                semantic_error = sem_result.split("\n")[0]
            semantic_fn = None  # 以降のクエリでも同じ失敗を繰り返さない
            continue

        deduped = []
        seen = set()
        for h in sem_result:
            if h["rel_path"] not in seen:
                seen.add(h["rel_path"])
                deduped.append(h)
        rank_lists.append(("semantic", qi, deduped))
    return rank_lists, semantic_error


def _fuse(rank_lists: list) -> list:
    """RRF: score(d) = Σ 1/(k + rank)。ファイル(rel_path)単位に融合する。

    融合キーが rel_path なのは、キーワード側とセマンティック側が出会える唯一の
    キーだから (キーワード検索はチャンクを知らない)。出典の見出しは、そのファイルに
    ついて最良だった semantic チャンクのものを採用する。
    """
    fused = {}
    for source, qi, ranked in rank_lists:
        for rank, h in enumerate(ranked, start=1):
            d = fused.setdefault(h["rel_path"], {
                "rel_path": h["rel_path"], "filename": h["filename"],
                "rrf": 0.0, "sources": set(), "query_ids": set(),
                "heading": None, "chunk_id": None, "snippet": None,
                "_kw_snippet": None, "rerank_pos": None,
            })
            d["rrf"] += 1.0 / (RRF_K + rank)
            d["sources"].add(source)
            d["query_ids"].add(qi)
            if source == "semantic":
                # semantic の最良チャンク (最初に来たもの) を出典・スニペットに採用
                if d["snippet"] is None:
                    d["snippet"] = h["snippet"]
                    d["heading"] = h.get("heading")
                    d["chunk_id"] = h.get("chunk_id")
            elif d["_kw_snippet"] is None:
                d["_kw_snippet"] = h["snippet"]

    hits = []
    for d in fused.values():
        # snippet は semantic 優先 (見出し文脈付きで質が高い)
        if d["snippet"] is None:
            d["snippet"] = d["_kw_snippet"] or ""
        d.pop("_kw_snippet")
        d["sources"] = sorted(d["sources"])
        d["query_ids"] = sorted(d["query_ids"])
        hits.append(d)
    hits.sort(key=lambda d: (-d["rrf"], d["rel_path"]))
    return hits


# ---------------------------------------------------------
# 構造化検索 (評価スクリプトとツール本体の共通コア)
# ---------------------------------------------------------

def _hybrid_hits(query: str, top_k: int = 5, keyword_fn=_keyword_hits,
                 semantic_fn=None, llm_fn=None, multiquery=None, rerank=None):
    """ハイブリッド検索の構造化結果を返す。

    Returns:
        (hits, notes, queries) — hits は上位 top_k 件の dict 列
        (rel_path / filename / heading / chunk_id / snippet / rrf / sources /
         query_ids / rerank_pos)、notes は劣化・フォールバックの理由行、
        queries は実際に検索した全クエリ変種。

    multiquery / rerank に True/False を渡すと .env より優先される
    (None なら .env)。scripts/eval_retrieval.py の A/B 計測用。

    文字列整形と分けてあるのは scripts/eval_retrieval.py が Recall/MRR を
    採点するのに rel_path の並びだけを必要とするため。
    """
    queries, expand_note = _expand_queries(query, llm_fn=llm_fn, enabled=multiquery)
    notes = [expand_note] if expand_note else []

    semantic_fn, resolve_error = _resolve_semantic_fn(semantic_fn)
    rank_lists, semantic_error = _rank_lists(
        queries, keyword_fn, semantic_fn, per_query_k=top_k * 2)

    sem_error = resolve_error or semantic_error
    if sem_error:
        notes.insert(0, f"⚠️ 意味検索は利用できないためキーワード検索のみ ({sem_error})")

    hits = _fuse(rank_lists)
    if not hits:
        return [], notes, queries

    candidates, tail = hits[:RERANK_CANDIDATES], hits[RERANK_CANDIDATES:]
    reranked, rerank_note = _llm_rerank(query, candidates, llm_fn=llm_fn, enabled=rerank)
    if rerank_note:
        notes.append(rerank_note)
    return (reranked + tail)[:top_k], notes, queries


# ---------------------------------------------------------
# Planner 向けツール
# ---------------------------------------------------------

def _format_hit(h: dict) -> str:
    link = format_wikilink(h["filename"], h.get("heading"))
    detail = [f"rrf: {h['rrf']:.4f}", "+".join(h["sources"])]
    if h.get("query_ids"):
        detail.append("q" + "/".join(str(i) for i in h["query_ids"]))
    if h.get("rerank_pos"):
        detail.append(f"rerank:{h['rerank_pos']}")
    return f"- File: {link} ({', '.join(detail)})\n  Snippet:\n{h['snippet']}\n..."


def tool_vault_hybrid_search(query: str, top_k: int = 5,
                             keyword_fn=_keyword_hits,
                             semantic_fn=None, llm_fn=None,
                             multiquery=None, rerank=None) -> str:
    """Vault内をキーワード+意味検索のハイブリッド (RRF) で検索する。

    出力形式は tool_vault_search / tool_vault_semantic_search と揃える
    (`- File: [[name#heading]]\\n  Snippet:` — プランナー側の扱いを変えないため)。
    keyword_fn / semantic_fn / llm_fn はテスト注入用 (embed_fn 注入と同じパターン)。
    multiquery / rerank は計測・デバッグ用の上書き (None なら .env)。
    レジストリの usage は `(query, top_k=5)` のままなので Planner には見えない。
    """
    if not query or not str(query).strip():
        return "No query."
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 5
    print(f"[Tool] 🔎 Hybrid searching Vault: {query}")

    hits, notes, queries = _hybrid_hits(
        query, top_k, keyword_fn=keyword_fn, semantic_fn=semantic_fn, llm_fn=llm_fn,
        multiquery=multiquery, rerank=rerank)

    if len(queries) > 1:
        # 何で引いた結果なのかを常に残す。ヒットが的外れなときに
        # 「書き換えが暴走した」のか「Vaultに無い」のかを切り分ける唯一の手がかり。
        notes.append("🔎 検索クエリ: " + " / ".join(f'"{q}"' for q in queries))
    header = ("\n".join(notes) + "\n\n") if notes else ""

    if not hits:
        return header + NOT_FOUND
    return header + "\n\n".join(_format_hit(h) for h in hits)
