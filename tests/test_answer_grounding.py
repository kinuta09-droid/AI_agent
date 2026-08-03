# -*- coding: utf-8 -*-
"""「自機能の捏造」と「追記ツールの取り違え」の回帰テスト

このテストが存在する理由:
  評価ハーネス (scripts/eval_agent.py) が 2026-07-28 に実測した2件の欠陥を、
  Gemini を1回も叩かずに守るためのもの。

  T12 (hallucination): 「このAIエージェントの音声通話機能の使い方を教えて」に対し、
    計画 (まず tool_web_search で調べる) は正しかったのに、_generate_final_summary が
    検索でヒットした他社製品 (Zendesk の音声AI) の解説を「このシステムの機能」として
    断定し、対応プランまで列挙して返していた。要約プロンプトが「自分が実際に何を
    持っているか」を一切知らないまま報告を書いていたのが原因。
    → 要約プロンプトにツールレジストリ由来の機能一覧と自己言及規則を載せる。

  T09 (negation): 「ノートは新規作成せず、既存のデイリーノートに追記する形で」に対し
    tool_sync_to_daily_note (予定/タスク同期) が選ばれていた。tool_update_note の
    description が "追記" の2文字しか無く、「デイリーノート」を説明文に含む
    sync 側に語彙一致で奪われていたため。
    → 両者の description に守備範囲と非対象を書く。

  どちらもプロンプト/メタデータの文言なので、壊れても例外は出ない。
  つまり静的に見張るしかない。

実行: py tests/test_answer_grounding.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from engine_plan_execute import (
    _build_capability_block,
    _build_final_summary_prompt,
    _get_tools_config,
)

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _tool_desc(name: str) -> str:
    config_tools, _ = _get_tools_config()
    for t in config_tools:
        if t["name"] == name:
            return t["description"]
    return ""


# ---------------------------------------------------------
# 1. 機能一覧はレジストリから作られている
# ---------------------------------------------------------

def test_capability_block_covers_registry():
    print("\n[1] _build_capability_block() がレジストリ全ツールを含む")
    config_tools, _ = _get_tools_config()
    block = _build_capability_block()

    missing = [t["name"] for t in config_tools if t["name"] not in block]
    # ハードコードした一覧に退化させないこと。取りこぼした機能は
    # 「持っているのに存在しないと言う」逆向きの嘘になる。
    check("登録済みツールが1つも欠けていない", not missing, f"欠落: {missing}")
    check("ツール数ぶんの行がある",
          block.count("\n") + 1 >= len(config_tools),
          f"{block.count(chr(10)) + 1} 行 / {len(config_tools)} ツール")

    # description も載っていること (名前だけでは何ができるか判断できない)
    sample = config_tools[0]
    check("description も一覧に載っている",
          sample["description"] in block, f"{sample['name']} の説明が無い")


# ---------------------------------------------------------
# 2. 要約プロンプトに自己言及規則が入っている
# ---------------------------------------------------------

def test_summary_prompt_has_self_reference_rule():
    print("\n[2] _build_final_summary_prompt() の自己言及グラウンディング")
    results = [{
        "step": 1,
        "task": "AIエージェントの音声通話機能をWeb検索する",
        "tool": "tool_web_search",
        "success": True,
        "observation": "Zendeskの音声AIエージェントは着信の一次対応を自動化します。",
    }]
    prompt = _build_final_summary_prompt("このAIエージェントの音声通話機能の使い方を教えて", results)

    check("ユーザーの要求が入っている", "音声通話機能の使い方" in prompt)
    check("実行結果の observation が入っている", "Zendesk" in prompt)
    check("機能一覧ブロックが入っている",
          "【このシステムがエージェント実行で使えるツールの一覧】" in prompt)
    check("実在ツール名が根拠として入っている", "tool_web_search" in prompt)
    check("自己言及の規則が入っている", "【自己言及の要求に答えるときの規則】" in prompt)
    check("『存在しない』と明言させる指示がある", "存在しません" in prompt)
    check("検索結果を自機能として書くことを禁じている",
          "証拠には一切ならない" in prompt or "記述してはならない" in prompt)

    # 逆向きの捏造を作らないこと (/grill W1・2026-07-28):
    # capability block はツールレジストリ (29件) だけで、ツールを持たない機能
    # (/memory /watchlist /persona-* /client-review /article などのスラッシュ
    # コマンド12件、定時ジョブ、AutoAudio) は載っていない。「これ以外は一切存在
    # しない」と断定させると、実在する機能を「無い」と答える逆向きの嘘になる。
    check("一覧がツールの範囲だと明示している",
          "エージェントが呼べるのはこれで全て" in prompt,
          "一覧を全機能と誤認させる文言になっている")
    check("ツール一覧の欠落から機能の不在を結論させない",
          "結論してはならない" in prompt)
    check("ツール以外の機能の存在に触れている",
          "スラッシュコマンド" in prompt and "定時ジョブ" in prompt)
    check("Discordコマンド側の確認を促している",
          "Discordコマンド" in prompt)


def test_summary_falls_back_when_prompt_build_fails():
    print("\n[2b] プロンプト構築が失敗しても文字列を返す")
    import engine_plan_execute as eng

    results = [{"step": 1, "task": "調査", "tool": "tool_web_search",
                "success": True, "observation": "BTCは12万ドル"}]
    original = eng._build_final_summary_prompt

    def _boom(*_a, **_kw):
        raise RuntimeError("registry unavailable")

    eng._build_final_summary_prompt = _boom
    try:
        out = eng._generate_final_summary("BTCの価格を調べて", results)
    finally:
        eng._build_final_summary_prompt = original

    # 「何があっても文字列を返す」契約。ここが例外で抜けると要約が無言で消える。
    check("例外を伝播させず文字列を返す", isinstance(out, str) and bool(out.strip()),
          repr(out)[:120])
    # _summarize_results は observation ではなく step/task の一覧を返す仕様
    check("フォールバックが実行したステップに言及する", "調査" in out, out[:200])


def test_summary_prompt_keeps_detail_requirements():
    print("\n[3] 既存の出力フォーマット要件を壊していない")
    results = [{"step": 1, "task": "調査", "tool": "tool_web_search",
                "success": True, "observation": "BTCは12万ドル"}]
    prompt = _build_final_summary_prompt("BTCの価格を調べて", results)

    # 自己言及規則を足したせいで通常の「調べて」系が痩せないこと
    check("結論セクションの指示が残っている", "## 📋 結論" in prompt)
    check("詳細レポートの指示が残っている", "## 📝 詳細レポート" in prompt)
    check("実データ重視の指示が残っている", "実データに基づいた内容にする" in prompt)
    check("失敗ステップが ❌ で表現される",
          "❌" in _build_final_summary_prompt(
              "x", [{"step": 1, "task": "t", "tool": "u",
                     "success": False, "observation": "Error: boom"}]))


# ---------------------------------------------------------
# 3. 追記ツールと同期ツールが弁別できる
# ---------------------------------------------------------

def test_note_tool_descriptions_are_distinguishable():
    print("\n[4] tool_update_note / tool_sync_to_daily_note の弁別")
    upd = _tool_desc("tool_update_note")
    sync = _tool_desc("tool_sync_to_daily_note")

    check("tool_update_note が登録されている", bool(upd))
    check("tool_sync_to_daily_note が登録されている", bool(sync))

    # T09 の直接原因: "追記" の2文字だけでは「デイリーノート」を含む sync に負ける
    check("update_note の説明が『追記』の2文字より詳しい", len(upd) > 10, f"{upd!r}")
    check("update_note が『既存』ノート向けだと書いてある", "既存" in upd, f"{upd!r}")
    check("update_note が新規作成しないと書いてある", "新規" in upd, f"{upd!r}")
    check("update_note がデイリーノートも守備範囲だと書いてある",
          "デイリーノート" in upd, f"{upd!r}")

    # 逆流防止: sync 側が起動条件と非対象の両方を持っていること
    check("sync が予定/タスク専用だと書いてある",
          ("予定" in sync and "タスク" in sync), f"{sync!r}")
    check("sync が任意テキストに使えないと書いてある",
          "任意の文章" in sync, f"{sync!r}")
    check("sync が代わりの tool_update_note を案内している",
          "tool_update_note" in sync, f"{sync!r}")

    check("2つの説明文が同一でない", upd != sync)
    check("一方が他方の部分文字列でない", upd not in sync and sync not in upd)


def test_t12_assertion_still_rejects_the_original_bug():
    print("\n[6] T12 のアサーションが旧バグ回答を通さない")
    import json
    import re

    # 2026-07-28 に T12 の answer_must_match を広げた (「含まれていません」
    # 「機能ではありません」を追加)。アサーションを緩めて自分の変更を通す、
    # という自己都合の緩和になっていないことをここで固定する:
    # 修正前の実測回答 (Zendesk の仕様を自機能として列挙したもの) が
    # 広げた後のパターンにも一致しないこと。
    fixture = os.path.join(_PROJECT_ROOT, "tests", "fixtures", "agent_eval_set.json")
    baseline = os.path.join(_PROJECT_ROOT, "logs", "eval", "eval_20260728_0053.json")

    with open(fixture, encoding='utf-8') as f:
        data = json.load(f)
    questions = data["questions"] if isinstance(data, dict) else data
    patterns = []
    for q in questions:
        if q.get("id") == "T12":
            patterns = q.get("exec_assertions", {}).get("answer_must_match", [])
    check("T12 に answer_must_match がある", bool(patterns))

    if not os.path.exists(baseline):
        # ベースライン JSON はログなので、掃除されていたらスキップ (失敗にはしない)
        print(f"  ⏭️  ベースライン {os.path.basename(baseline)} が無いためスキップ")
        return

    with open(baseline, encoding='utf-8') as f:
        old = json.load(f)
    bug_answer = next((r["answer"] for r in old["results"] if r["id"] == "T12"), "")
    check("旧バグ回答が記録されている", bool(bug_answer))

    for p in patterns:
        hit = re.search(p, bug_answer)
        check("旧バグ回答が否定パターンに一致しない", hit is None,
              f"一致してしまった: {hit.group(0) if hit else ''}")


def test_summary_prompt_preserves_citations():
    """[7] 出典 [[ファイル名#見出し]] を残させ、かつ捏造を禁じている (Phase A2)

    片方だけだと逆向きの嘘になる:
      「出典を付けろ」だけ → それらしいファイル名を推測して [[...]] を作る
      「捏造するな」だけ   → 安全側に倒れて出典を一切書かなくなり、
                             検証手段が無い元の状態に戻る
    両方が同時にプロンプトに載っていることを固定する。
    """
    print("\n[7] 出典の保持と捏造禁止 (Phase A2)")
    results = [{
        "step": 1, "task": "過去ノートを検索する", "tool": "tool_vault_hybrid_search",
        "success": True,
        "observation": "- File: [[crypto-tool-upgrade-plan.md#Phase3]] (rrf: 0.0325)\n"
                       "  Snippet:\n## Phase3\nATR動的水準を導入した。\n...",
    }]
    prompt = _build_final_summary_prompt("過去のメモを踏まえてcryptoの改善経緯を教えて", results)

    check("出典の節がある", "【出典の扱い】" in prompt)
    check("observation のリンクをそのまま残せと書いてある",
          "そのままの表記でコピー" in prompt)
    check("見出しアンカーの改変を禁じている",
          "省略・要約・整形してはならない" in prompt)
    check("observation に無いリンクの捏造を禁じている",
          "observation に出てこないリンクを書いてはならない" in prompt)
    check("出典が無いときの言い方を指示している",
          "見つかりませんでした" in prompt)
    check("検索結果の出典が実際にプロンプトへ渡っている",
          "[[crypto-tool-upgrade-plan.md#Phase3]]" in prompt)


def test_summary_prompt_has_temporal_consistency_rule():
    """[8] 時期整合性チェックの指示がある (2026-08-03)

    「今日ガソリン価格が急に下がった理由」に対し、検索結果にあった
    数ヶ月前の別制度の話を時期を無視して直接の理由として断定した誤答の
    再発防止。事実の有無 (Verifier) だけでなく、その事実の時期が質問の
    時間軸と合っているかを要約プロンプト自身にも確認させる。
    """
    print("\n[8] 時期整合性チェックの指示 (2026-08-03)")
    results = [{
        "step": 1, "task": "ガソリン価格が今日下がった理由を調べる",
        "tool": "tool_web_search", "success": True,
        "observation": "2026年1月にガソリンの暫定税率が廃止された。",
    }]
    prompt = _build_final_summary_prompt("今日ガソリン価格が急に安くなった理由は？", results)

    check("時期整合性の節がある", "【時期整合性の確認】" in prompt)
    check("事実の時期を明示させる指示がある", "いつ発表・適用されたものか" in prompt)
    check("時期の食い違いを断定させない指示がある",
          "断定してはならない" in prompt)
    check("留保を付けさせる指示がある", "確認できない" in prompt)


def test_planner_prompt_mentions_append_guideline():
    print("\n[5] Planner の【ツール選択の指針】に追記の行がある")
    path = os.path.join(_PROJECT_ROOT, "engine_plan_execute.py")
    with open(path, encoding='utf-8') as f:
        src = f.read()

    check("既存ファイルへの追記の指針がある",
          "既存ファイルに文章を書き足す場合" in src)
    check("指針が tool_update_note を指している",
          "既存ファイルに文章を書き足す場合**: tool_update_note" in src
          or "既存ファイルに文章を書き足す場合" in src and "tool_update_note を使う" in src)
    check("sync が同期専用だと Planner にも書いてある",
          "予定/タスクの同期専用" in src)


def test_resolve_tool_args_prompt_mentions_recency_days():
    """[9] _resolve_tool_args のプロンプトに recency_days ガイドがある (2026-08-03)

    ガソリン価格の誤答の直接原因は、tool_web_search が鮮度フィルタなしで
    Tavily を呼んでいたこと。「今日」「急に」等の直近性を要求する質問で
    Planner が recency_days を指定するよう促す文言が消えていないことを固定する。
    """
    print("\n[9] _resolve_tool_args プロンプトの recency_days ガイド (2026-08-03)")
    path = os.path.join(_PROJECT_ROOT, "engine_plan_execute.py")
    with open(path, encoding='utf-8') as f:
        src = f.read()

    check("recency_days のガイドがある", "recency_days" in src)
    check("直近性を示す語(今日/急に等)に言及している",
          "「今日」「本日」「急に」" in src)
    check("不要な質問には付けない旨の歯止めがある",
          "指定不要です" in src)


if __name__ == "__main__":
    print("=" * 55)
    print(" 自機能グラウンディング / 追記ツール弁別 回帰テスト")
    print("=" * 55)

    test_capability_block_covers_registry()
    test_summary_prompt_has_self_reference_rule()
    test_summary_falls_back_when_prompt_build_fails()
    test_summary_prompt_keeps_detail_requirements()
    test_note_tool_descriptions_are_distinguishable()
    test_t12_assertion_still_rejects_the_original_bug()
    test_summary_prompt_preserves_citations()
    test_summary_prompt_has_temporal_consistency_rule()
    test_planner_prompt_mentions_append_guideline()
    test_resolve_tool_args_prompt_mentions_recency_days()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
