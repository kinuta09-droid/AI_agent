# -*- coding: utf-8 -*-
"""
Paper Radar の外部API疎通スモーク (オプトイン)

既定では **SKIP して正常終了する**。実行するには:
    set ARMS_LIVE_TESTS=1 && py tests/test_paper_radar_live.py

なぜ分けるか:
  arXiv / HuggingFace / Semantic Scholar は無料・キー不要だが、
  (a) 実ネットワークに出る、(b) レスポンス形式は相手の都合で変わる、
  ため CI や日常のスイープに載せると「他人の障害でこちらが赤くなる」。
  一方で **形式が変わったことに気付く手段はここにしか無い** (ユニットテストは
  スタブなので永久に緑)。手動で叩けるようにしておく。

Gemini は呼ばない (課金ゼロ)。tool_paper_digest の要約はここでは検証しない。
"""
import datetime
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_paper_radar as radar
from lib.bot_errors import is_failure_result

_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _FAIL
    if ok:
        print(f"✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実ネットワークに出るため既定では実行しません "
              "(ARMS_LIVE_TESTS=1 で有効化)")
        return 0

    print("=== arXiv API ===")
    papers = []
    try:
        papers = radar.search_arxiv(radar.resolve_field("ai_agent"), max_results=10, days=14)
        check("arXiv 検索が結果を返す", bool(papers), f"{len(papers)}件")
        if papers:
            p = papers[0]
            check("必須フィールドが揃っている",
                  all(p.get(k) for k in ("arxiv_id", "title", "abstract", "pdf_url")),
                  f"{p.get('arxiv_id')} / {p.get('title', '')[:40]}")
            check("published_dt が datetime", isinstance(p.get("published_dt"), datetime.datetime),
                  str(p.get("published")))
    except Exception as e:
        check("arXiv 検索", False, f"{type(e).__name__}: {e}")

    print("\n=== HuggingFace Daily Papers ===")
    try:
        upvotes = radar.fetch_hf_upvotes(days=3)
        # 0件でも即失敗とはしない (その週の掲載が少ないこともある) が、
        # ID の綴りが arXiv 側と揃っているかは見る
        check("upvote マップを取得", isinstance(upvotes, dict), f"{len(upvotes)}件")
        if upvotes:
            sample = list(upvotes.items())[:3]
            check("キーが arXiv ID 形式",
                  all(radar.normalize_arxiv_id(k) == k for k, _ in sample), str(sample))
    except Exception as e:
        check("HF daily_papers", False, f"{type(e).__name__}: {e}")

    print("\n=== Semantic Scholar ===")
    try:
        # 被引用が確実に付いている既知の論文 (GPT-4 Technical Report)
        metrics = radar.fetch_citation_metrics(["2303.08774"])
        got = metrics.get("2303.08774", {})
        check("被引用数を取得", bool(got) and got.get("citations", 0) > 0, str(got))
    except Exception as e:
        check("Semantic Scholar batch", False, f"{type(e).__name__}: {e}")

    print("\n=== 統合 (保存はしない) ===")
    try:
        result = radar.run_paper_radar(field="ai_agent", top_k=3, days=14, save=False)
        # 判定は手書きの前方一致ではなく共通の判定器を通す (CLAUDE.md の教訓)
        check("run_paper_radar が失敗接頭辞を返さない",
              not is_failure_result(result), result.splitlines()[0] if result else "")
    except Exception as e:
        check("run_paper_radar", False, f"{type(e).__name__}: {e}")

    print("\n" + ("❌ FAILURES あり" if _FAIL else "✅ すべて疎通"))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
