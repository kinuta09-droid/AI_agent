# -*- coding: utf-8 -*-
"""
Vault ハイブリッド検索 (Phase ④)

tool_obsidian._keyword_hits (キーワード) と tool_vault_index._semantic_hits (意味) を
RRF (Reciprocal Rank Fusion, k=60) でマージする。semantic index 未構築 / Ollama 停止 /
numpy 欠落時はキーワード検索のみに自動フォールバックする。

意図的に tool_vault_index を遅延 import する: あちらはモジュールトップで numpy を
import するため、同居させると numpy 欠落環境で検索ツールが全滅する。本モジュールは
常に import 可能なのでレジストリ entry に optional を付けない。
"""

from tools.tool_obsidian import _keyword_hits

RRF_K = 60
NOT_FOUND = "Vault内に見つかりませんでした。"


def tool_vault_hybrid_search(query: str, top_k: int = 5,
                             keyword_fn=_keyword_hits,
                             semantic_fn=None) -> str:
    """Vault内をキーワード+意味検索のハイブリッド (RRF) で検索する。

    出力形式は tool_vault_search / tool_vault_semantic_search と揃える
    (`- File: [[name]]\\n  Snippet:` — プランナー側の扱いを変えないため)。
    keyword_fn / semantic_fn はテスト注入用 (embed_fn 注入と同じパターン)。
    """
    if not query or not str(query).strip():
        return "No query."
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 5
    print(f"[Tool] 🔎 Hybrid searching Vault: {query}")

    kw_hits = keyword_fn(str(query))

    # semantic側: 遅延import。失敗 or soft-error文字列ならキーワードのみモード
    fallback_note = ""
    sem_hits = []
    try:
        if semantic_fn is None:
            from tools.tool_vault_index import _semantic_hits as semantic_fn
        # チャンク単位のヒットをファイル単位に集約するため多めに取る
        sem_result = semantic_fn(str(query), top_k * 2)
    except ImportError as e:
        sem_result = f"[ERROR] semantic search unavailable: {e}"
    if isinstance(sem_result, str):
        reason = sem_result.split("\n")[0]
        fallback_note = f"⚠️ 意味検索は利用できないためキーワード検索のみ ({reason})\n\n"
    else:
        sem_hits = sem_result

    # ランク付け: keyword=score降順(同点はwalk順), semantic=スコア順のままファイル毎に最良のみ
    kw_ranked = sorted(kw_hits, key=lambda h: -h["score"])
    sem_ranked = []
    seen = set()
    for h in sem_hits:
        if h["rel_path"] not in seen:
            seen.add(h["rel_path"])
            sem_ranked.append(h)

    # RRF: score(d) = Σ 1/(k + rank)
    fused = {}  # rel_path -> {"rrf", "sources", "kw", "sem"}
    for source, ranked in (("keyword", kw_ranked), ("semantic", sem_ranked)):
        for rank, h in enumerate(ranked, start=1):
            d = fused.setdefault(h["rel_path"], {"rrf": 0.0, "sources": [], "kw": None, "sem": None})
            d["rrf"] += 1.0 / (RRF_K + rank)
            d["sources"].append(source)
            d["kw" if source == "keyword" else "sem"] = h

    if not fused:
        return fallback_note + NOT_FOUND

    top = sorted(fused.items(), key=lambda kv: (-kv[1]["rrf"], kv[0]))[:top_k]
    lines = []
    for rel_path, d in top:
        best = d["sem"] or d["kw"]  # snippetはsemantic優先 (見出し文脈付きで質が高い)
        sources = "+".join(d["sources"])
        lines.append(f"- File: [[{best['filename']}]] (rrf: {d['rrf']:.4f}, {sources})\n"
                     f"  Snippet:\n{best['snippet']}\n...")
    return fallback_note + "\n\n".join(lines)
