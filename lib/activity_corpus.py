# -*- coding: utf-8 -*-
"""行動ログ → 検索用ドキュメントのレンダラ (Phase B)

エージェント自身の行動履歴 (crypto の予測ログ・Plan&Execute の実行ログ・
ツールの成否観測) を、Vault のノートと同じ意味検索の土俵に載せるための
**純粋関数** 群。I/O は一切しない (読み込みと埋め込みは tool_activity_index)。

なぜ生の JSON をそのまま埋め込まないか:
  bge-m3 に `{"outcome": "partial", "tools_used": ["tool_vault_search"]}` を
  食わせても、「失敗の傾向」のような自然文クエリとは語彙が噛み合わない。
  日本語の文にしてから埋め込む。

なぜソースごとに粒度を変えるか:
  skill_observations は132行あるが中身は互いにほぼ同一で、1行=1文書にすると
  近似重複が top-k を丸ごと埋める。一方 learning_db の1実行は `query` に
  自然文の依頼、`failure_reason` に具体的なエラーを持つので記録単位で価値がある。
  → 「まとめて初めて意味が出るもの」だけロールアップにする。

  | source        | 粒度                                    |
  |---------------|-----------------------------------------|
  | crypto        | 1トレード=1文書 + signal_type別ロールアップ |
  | agent_exec    | 1実行=1文書 + 失敗理由別ロールアップ       |
  | agent_skill   | ツール別ロールアップのみ                   |

出力の契約 (tool_activity_index が消費する):
  {"doc_id": str, "source": str, "title": str, "text": str}

  doc_id は `_activity/<source>/<key>` 形式の**仮想パス**。chunks.filepath に
  そのまま入り、RRF の融合キー・評価スクリプトの採点キーになる。
  実在するファイルパスと衝突しないよう `_activity/` を先頭に付ける
  (Vault 側の rel_path は必ず実フォルダ名で始まる)。
"""

import collections
import re

SOURCE_CRYPTO = "crypto"
SOURCE_AGENT_EXEC = "agent_exec"
SOURCE_AGENT_SKILL = "agent_skill"

ACTIVITY_SOURCES = (SOURCE_CRYPTO, SOURCE_AGENT_EXEC, SOURCE_AGENT_SKILL)

# 仮想パスの接頭辞。Vault の rel_path (例 "30_Main/foo.md") と衝突させないため
# アンダースコア始まりにしてある。
DOC_PREFIX = "_activity"

# 評価ハーネス (scripts/eval_agent.py) が意図的に失敗させたツールの目印。
# 個別の実行ドキュメントには残す (起きたことは事実なので) が、
# **ロールアップからは除く**: 「同じ失敗を繰り返していないか?」という問いに
# 自分のテスト用フィクスチャを答えとして返すのは、実態と食い違う報告になる。
# ただし黙って捨てず、除外件数をロールアップ本文に必ず書く。
EVAL_FIXTURE_MARK = "[eval]"


def doc_id(source: str, key: str) -> str:
    """`_activity/<source>/<key>` を組み立てる。

    key に含まれるスラッシュは潰す (BTC/USDT のような銘柄名がそのまま来る)。
    潰さないと doc_id の階層が銘柄によって変わり、前方一致での絞り込みが壊れる。
    """
    safe = str(key).replace("/", "-").replace("\\", "-").strip()
    return f"{DOC_PREFIX}/{source}/{safe}"


def _date(ts) -> str:
    """ISO タイムスタンプの日付部分。取れなければ空文字。"""
    return str(ts or "")[:10]


def _fmt_num(value, digits: int = 8) -> str:
    """価格を読みやすく整形する。

    round() で丸めない: BTC (10万台) と POL (0.08) が同じ桁数では、
    片方が潰れるか片方がゼロ埋めになる。有効数字で切って末尾の0を落とす。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == 0:
        return "0"
    text = f"{f:.{digits}g}"
    return text


# ---------------------------------------------------------
# crypto — 予測ログ (trade_log.json)
# ---------------------------------------------------------

# status の表示名。OPEN のまま決着していないトレードを「負け」と数えないため、
# 勝敗の集計とは別に「未決済」を独立した状態として持つ。
_TRADE_STATUS_LABEL = {
    "OPEN": "未決済",
    "WIN": "勝ち (目標到達)",
    "LOSS": "負け (損切り)",
    "EXPIRED": "期限切れ (どちらにも到達せず)",
}


def render_trade(rec: dict) -> dict:
    """1トレード (予測1件) を1文書にする。"""
    symbol = rec.get("symbol", "?")
    signal = rec.get("signal", "?")
    stype = rec.get("signal_type", "?")
    status = str(rec.get("status", "OPEN")).upper()
    date = _date(rec.get("timestamp"))
    title = f"{symbol} {signal} ({stype}) {date}"

    # 見出しに source 名 ("crypto予測ログ") を入れない: 表示側の
    # format_source_ref がラベルとして必ず前置するので二重になる。
    # ただし検索語としては欲しいので、本文の1行目に残す。
    lines = [
        f"## {title}",
        "記録の種類: crypto 予測ログ (シグナル1件分のトレード記録)",
        f"銘柄: {symbol} / シグナル: {signal} / シグナル種別: {stype}",
        f"記録種別: {rec.get('record_kind', '?')} / "
        f"結果: {_TRADE_STATUS_LABEL.get(status, status)}",
        f"エントリー価格: {_fmt_num(rec.get('entry_price'))} / "
        f"損切り: {_fmt_num(rec.get('stop_loss'))} / "
        f"目標: {_fmt_num(rec.get('target_price'))} / "
        f"Kelly比率: {rec.get('kelly_pct')}%",
    ]

    mode = rec.get("levels_mode")
    if mode:
        lines.append(
            f"水準の決め方: {mode} (ATR値 {_fmt_num(rec.get('atr_value'))}, "
            f"ATR使用率 {rec.get('atr_used_pct')}%, SL倍率 {rec.get('sl_atr_mult')})")

    reason = str(rec.get("reason") or "").strip()
    if reason:
        lines.append(f"シグナルの根拠: {reason}")
    note = str(rec.get("audit_note") or "").strip()
    if note:
        lines.append(f"監査メモ: {note}")

    return {
        "doc_id": doc_id(SOURCE_CRYPTO, f"{date}-{rec.get('id', 'unknown')}"),
        "source": SOURCE_CRYPTO,
        "title": title,
        "text": "\n".join(lines),
    }


def render_trade_rollups(records: list) -> list:
    """signal_type 別の実績まとめ。

    「BREAKOUT で負けたときの共通点は?」のような問いに答えるのはこちら
    (個別トレード文書は1件ずつしか語れない)。

    **決着していないトレードを勝敗に混ぜない**: 全件 OPEN の状態で
    「勝率0%」と書くと、負けたという嘘になる。決着件数が0なら
    「算出できない」と明示する — 数字を出さないことより、
    出せない理由を書かないことの方が危険。
    """
    by_type = collections.defaultdict(list)
    for rec in records:
        by_type[str(rec.get("signal_type") or "unknown")].append(rec)

    docs = []
    for stype, recs in sorted(by_type.items()):
        statuses = collections.Counter(
            str(r.get("status", "OPEN")).upper() for r in recs)
        wins, losses = statuses.get("WIN", 0), statuses.get("LOSS", 0)
        decided = wins + losses
        dates = sorted(_date(r.get("timestamp")) for r in recs if r.get("timestamp"))
        symbols = sorted({str(r.get("symbol", "?")) for r in recs})

        lines = [
            f"## シグナル種別 {stype} の実績まとめ",
            "記録の種類: crypto 予測ログの集計 (シグナル種別ごとの成績)",
            f"対象 {len(recs)} 件"
            + (f" ({dates[0]} 〜 {dates[-1]})" if dates else ""),
            "結果内訳: " + " / ".join(
                f"{_TRADE_STATUS_LABEL.get(k, k)} {v}件"
                for k, v in sorted(statuses.items())),
        ]
        if decided:
            lines.append(
                f"勝率: {wins}/{decided} = {wins / decided * 100:.0f}% "
                f"(決着した {decided} 件のみで算出)")
        else:
            lines.append(
                "勝率: 決着したトレードが1件も無いため算出できない "
                "(全件が未決済のため、勝ちとも負けとも言えない)")
        lines.append("対象銘柄: " + ", ".join(symbols))

        reasons = [str(r.get("reason") or "").strip() for r in recs]
        reasons = [r for r in reasons if r]
        if reasons:
            lines.append("シグナルの根拠に現れた記述:")
            for text in sorted(set(reasons))[:5]:
                lines.append(f"- {text}")

        docs.append({
            "doc_id": doc_id(SOURCE_CRYPTO, f"rollup-{stype}"),
            "source": SOURCE_CRYPTO,
            "title": f"シグナル種別 {stype} の実績まとめ",
            "text": "\n".join(lines),
        })
    return docs


# ---------------------------------------------------------
# agent_exec — Plan&Execute の実行ログ (learning_db.json)
# ---------------------------------------------------------

_OUTCOME_LABEL = {
    "success": "成功",
    "partial": "部分失敗 (一部のステップだけ成功)",
    "failed": "失敗",
}


def render_execution(rec: dict) -> dict:
    """1実行を1文書にする。"""
    exec_id = rec.get("id", "unknown")
    outcome = str(rec.get("outcome", "unknown"))
    date = _date(rec.get("timestamp"))
    query = str(rec.get("query") or "").strip()
    title = f"{exec_id} {_OUTCOME_LABEL.get(outcome, outcome)} {date}"

    lines = [
        f"## {title}",
        "記録の種類: エージェントの実行ログ (Plan&Execute 1回分)",
        f"ユーザーの依頼: {query}",
        f"結果: {_OUTCOME_LABEL.get(outcome, outcome)} "
        f"(計画 {rec.get('plan_steps')} ステップ / "
        f"実行 {rec.get('steps_count')} ステップ / "
        f"{rec.get('duration_seconds')} 秒)",
        "使用したツール: " + (", ".join(rec.get("tools_used") or []) or "なし"),
        f"Critic の判定: {rec.get('critique_status', '?')}",
    ]
    reason = str(rec.get("failure_reason") or "").strip()
    if reason:
        lines.append(
            f"失敗したステップ: {rec.get('failed_step')} / 失敗理由: {reason}")

    return {
        "doc_id": doc_id(SOURCE_AGENT_EXEC, exec_id),
        "source": SOURCE_AGENT_EXEC,
        "title": title,
        "text": "\n".join(lines),
    }


# 失敗理由をグルーピングするための正規化。可変部分 (引数名・パス・ID) を
# 落とさないと、同じ原因の失敗が全部バラバラのグループになる。
_ARG_RE = re.compile(r"'[^']*'")
_NUM_RE = re.compile(r"\d+")
# 関数名も伏せる。`tool_vault_search() got an unexpected keyword argument` と
# `get_gmail_summary() got an unexpected keyword argument` は **同じ原因**
# (Planner がシグネチャに無い引数を作る) であって別の失敗ではない。
# 実測ではこれが最頻の実障害パターンで、関数名を残すと5件が5グループに割れて
# 「1回ずつ起きた無関係な事故」に見える。関数名は本文の「関係したツール」と
# 「実際のエラーメッセージ」に必ず残すので、署名から落としても情報は消えない。
_CALLABLE_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\(\)")


def _failure_signature(reason: str) -> str:
    """失敗理由から可変部分を伏せた署名を作る (グルーピング用・表示用ではない)。"""
    text = _ARG_RE.sub("'…'", str(reason or "").strip())
    text = _CALLABLE_RE.sub("<ツール>()", text)
    text = _NUM_RE.sub("N", text)
    return text[:120]


# ロールアップを作る最小件数。1件しか無いものを「繰り返している失敗」という
# 見出しで出すと、見出しが実態と食い違う (CLAUDE.md が繰り返し戒めている類型)。
# 単発の失敗は個別の実行ドキュメントが既に持っているので、情報も失われない。
FAILURE_ROLLUP_MIN = 2


def render_execution_rollups(records: list) -> list:
    """失敗理由の署名ごとに「繰り返している失敗」をまとめる。

    ロードマップの「同じ失敗を繰り返していないか?」に直接答える文書。
    評価ハーネスが注入した合成失敗 (EVAL_FIXTURE_MARK) は除外するが、
    除外した件数は本文に書く — 黙って減らすと、集計を見た人が
    「失敗が少ない」と誤読する。

    2件以上のグループしか出さない (FAILURE_ROLLUP_MIN)。
    """
    failures = [r for r in records
                if str(r.get("outcome")) in ("failed", "partial")
                and str(r.get("failure_reason") or "").strip()]
    excluded = [r for r in failures
                if EVAL_FIXTURE_MARK in str(r.get("failure_reason"))]
    real = [r for r in failures if EVAL_FIXTURE_MARK not in str(r.get("failure_reason"))]

    by_sig = collections.defaultdict(list)
    for rec in real:
        by_sig[_failure_signature(rec.get("failure_reason"))].append(rec)

    groups = [(sig, recs) for sig, recs in by_sig.items()
              if len(recs) >= FAILURE_ROLLUP_MIN]

    docs = []
    for index, (sig, recs) in enumerate(
            sorted(groups, key=lambda kv: (-len(kv[1]), kv[0]))):
        dates = sorted(_date(r.get("timestamp")) for r in recs if r.get("timestamp"))
        tools = sorted({t for r in recs for t in (r.get("tools_used") or [])})
        samples = sorted({str(r.get("failure_reason")).strip() for r in recs})[:5]

        lines = [
            f"## 繰り返している失敗パターン: {sig}",
            "記録の種類: エージェントの実行ログの集計 (同じ原因の失敗をまとめたもの)",
            f"同じ原因で {len(recs)} 回失敗している"
            + (f" ({dates[0]} 〜 {dates[-1]})" if dates else ""),
            # tools_used は「その実行で使われたツール全体」で、失敗したステップの
            # ツールだけではない。誤読されないよう書き方で限定する。
            "関係した実行で使われたツール: " + (", ".join(tools) or "不明"),
            "実際のエラーメッセージ:",
        ]
        lines.extend(f"- {s}" for s in samples)
        lines.append("該当した実行ID: " + ", ".join(
            str(r.get("id")) for r in recs[:10]))
        if excluded:
            lines.append(
                f"(注: 評価ハーネス scripts/eval_agent.py が意図的に失敗させた "
                f"{len(excluded)} 件は、実障害ではないため全ロールアップから"
                f"除外している)")

        docs.append({
            "doc_id": doc_id(SOURCE_AGENT_EXEC, f"rollup-failure-{index:02d}"),
            "source": SOURCE_AGENT_EXEC,
            "title": f"繰り返している失敗: {sig[:60]}",
            "text": "\n".join(lines),
        })
    return docs


# ---------------------------------------------------------
# agent_skill — ツール成否の観測 (skill_observations.jsonl)
# ---------------------------------------------------------

def render_skill_rollups(rows: list, descriptions: dict = None) -> list:
    """ツール別の実行実績。

    生の観測1行は「tool_web_search が成功した・3.2秒」しか言っておらず、
    132行がほぼ同じ文になる。ツール単位に畳んで初めて
    「どのツールが不安定か」という検索可能な事実になる。

    descriptions ({ツール名: 説明}) を渡す理由 (実測で分かったこと):
      `tool_gmail_check` という文字列には「メール」という語が一度も出てこない。
      ツール名は実装の識別子であってユーザーの語彙ではないので、
      「メールを確認する機能がうまく動かなかったことはあるか」と聞かれても
      bge-m3 が結び付けられず、評価 A02/A08 が圏外だった。
      lib/tool_registry.get_tool_descriptions() の日本語説明を本文に入れて橋渡しする。
      引数で受けるのはこのモジュールを純関数に保つため (レジストリを直接 import しない)。
    """
    descriptions = descriptions or {}
    by_tool = collections.defaultdict(list)
    for row in rows:
        by_tool[str(row.get("tool") or "unknown")].append(row)

    docs = []
    for tool, obs in sorted(by_tool.items()):
        ok = sum(1 for o in obs if o.get("success"))
        ng = len(obs) - ok
        durations = [float(o.get("duration_sec") or 0) for o in obs]
        avg = sum(durations) / len(durations) if durations else 0.0
        dates = sorted(_date(o.get("ts")) for o in obs if o.get("ts"))

        lines = [
            f"## {tool}",
            "記録の種類: ツールの実行実績 (成功率・所要時間・エラーの集計)",
        ]
        desc = str(descriptions.get(tool) or "").strip()
        if desc:
            lines.append(f"このツールの役割: {desc}")
        lines += [
            f"観測 {len(obs)} 件"
            + (f" ({dates[0]} 〜 {dates[-1]})" if dates else "")
            + f": 成功 {ok} 件 / 失敗 {ng} 件"
              f" (成功率 {ok / len(obs) * 100:.0f}%)",
            f"平均所要時間: {avg:.1f} 秒",
        ]

        errors = collections.Counter(
            str(o.get("error_type")) for o in obs
            if not o.get("success") and o.get("error_type"))
        if errors:
            lines.append("エラー種別: " + ", ".join(
                f"{k} {v}件" for k, v in errors.most_common()))
            messages = sorted({str(o.get("error_msg") or "").strip()
                               for o in obs if not o.get("success")})
            lines.append("実際のエラーメッセージ:")
            lines.extend(f"- {m}" for m in messages[:3] if m)
        else:
            lines.append("エラー種別: 失敗の記録なし")

        docs.append({
            "doc_id": doc_id(SOURCE_AGENT_SKILL, tool),
            "source": SOURCE_AGENT_SKILL,
            "title": tool,
            "text": "\n".join(lines),
        })
    return docs


# ---------------------------------------------------------
# まとめ
# ---------------------------------------------------------

def render_all(trades: list, executions: list, observations: list,
               descriptions: dict = None) -> list:
    """全ソースのドキュメントを組み立てる (純関数)。

    呼び出し側 (tool_activity_index) はファイルを読んでここに渡すだけ。
    分けてあるのは、この関数がテストから I/O なしで丸ごと検証できるようにするため。
    """
    docs = []
    docs.extend(render_trade(r) for r in trades)
    docs.extend(render_trade_rollups(trades))
    docs.extend(render_execution(r) for r in executions)
    docs.extend(render_execution_rollups(executions))
    docs.extend(render_skill_rollups(observations, descriptions))
    return docs
