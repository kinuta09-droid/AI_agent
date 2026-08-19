# -*- coding: utf-8 -*-
"""AI生成日本語の文体チェック (B系統: Gemini生成パイプライン向け後処理フィルタ)。

辞書は ~/.claude/hooks/guard_ja_style.py (グローバル・A系統hook) と共有する
(lib/paths.JA_STYLE_DICT_PATH)。検出ロジック自体はhook側と共有しない —
hookはグローバル ~/.claude 配下、こちらはプロジェクトローカルの lib/ 配下で
実行コンテキストが異なり、共有化するほどの分量でもないため。

辞書の entries/aggregate_checks のみ判定する。manual_notes (文書全体の意味判定が
必要なパターン) は判定対象外 — 詳細は辞書ファイル自体のコメント欄と
plans/ja-style-guard 系のセッション記録を参照。
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime

from lib.paths import JA_STYLE_DICT_PATH, JA_STYLE_UNRESOLVED_LOG
from lib.lib_shared import client

_ENTRY_PATTERNS = {
    "vague-effect-verb": re.compile(r"(効きます|効果があります|役立ちます)(?=[。\s]|$)"),
    "negation-lead": re.compile(r"ではなく、?[^。]{0,40}(だ|である|です)"),
    "ai-cliche-hype": re.compile(r"(革命的な|可能性を解き放|魔法のように|ゲームチェンジ)"),
    "format-clash": re.compile(r"[✅💡🚀✨🎉🔥]"),
    "literal-translation": re.compile(r"(掘り下げ|重要な役割を果た)"),
    "redundant-predicate": re.compile(r"(を行うことができ|ということが大切です)"),
    # 以下5件は japanese-tech-writing (gist:k16shikano/fd287c3133457c4fd8f5601d34aa817d) の
    # 「LLMっぽい表現の禁止」リストから、フレーズ直接マッチで安全に判定できるものを抽出 (2026-08-17)
    "preview-and-summary-cliche": re.compile(r"(重要なのは|本章では|ここでは.{0,10}(見ていき|扱います|扱う)|まとめると|要するに|に他ならない)"),
    "empty-adjective": re.compile(r"(不可欠|核心的|鍵となる|根本的|多角的|包括的|総合的)"),
    "empty-verb-llm": re.compile(r"(言語化する|言語化し)"),
    "weak-hedge-llm": re.compile(r"(と言えるだろう|非常に|極めて)"),
    "posture-without-content": re.compile(r"(正面から扱う|正面から回収する)"),
}
_MONOTONY_SUFFIXES = ("ます", "です", "した", "ました", "でした", "ません")
_KEIGO_RE = re.compile(r"(ます|です)。")
_JOTAI_RE = re.compile(r"(だ|である)。")
# 番号付き/箇条書きリストの項目は、語尾が揃うこと自体が構造上自然な並列表現なので
# 文末単調の判定対象から除外する (例: "1. 実行と評価: ...採点します。2. コードの書き換え:
# ...書き換えます。3. 適用判断: ...反映させます。" は"ます"の3連続だが問題ではない。
# 2026-08-17、logs/ja_style_unresolved.jsonl の実例より)
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.、)）]|[-*・])\s*")
# 「さらに/また/加えて」3語の合計出現回数の閾値。過去実データ(paper-digest下書き等)での
# 実測により決定 (japanese-tech-writing、2026-08-17)
_CONNECTOR_SPAM_THRESHOLD = 4


@dataclass
class Violation:
    id: str
    matched: str
    reason: str
    good_pattern: str


def load_dictionary(path: str = None) -> dict:
    """辞書JSONを読み込む。無い/壊れていれば空辞書 (fail-safe、検査は素通りさせる)。"""
    try:
        with open(path or JA_STYLE_DICT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"entries": [], "aggregate_checks": [], "manual_notes": []}


def _split_sentences(text: str) -> list:
    return [s for s in re.split(r"(?<=。)", text) if s.strip()]


def check_style(text: str, dictionary: dict = None) -> list:
    """entries/aggregate_checks のみ判定する (manual_notes は無視)。"""
    dictionary = dictionary if dictionary is not None else load_dictionary()
    violations = []

    by_id = {e["id"]: e for e in dictionary.get("entries", [])}
    for entry_id, pattern in _ENTRY_PATTERNS.items():
        entry = by_id.get(entry_id)
        if not entry:
            continue
        m = pattern.search(text)
        if m:
            violations.append(Violation(entry_id, m.group(0), entry["reason"], entry["good_pattern"]))

    agg_by_id = {c["id"]: c for c in dictionary.get("aggregate_checks", [])}
    sentences = _split_sentences(text)

    if "sentence-ending-monotony" in agg_by_id:
        run_suffix, run_sentences = None, []
        for s in sentences:
            if _LIST_ITEM_RE.match(s):
                run_suffix, run_sentences = None, []
                continue
            stripped = s.rstrip("。\n ")
            suffix = next((suf for suf in _MONOTONY_SUFFIXES if stripped.endswith(suf)), None)
            if suffix is not None and suffix == run_suffix:
                run_sentences.append(s.strip())
            else:
                run_suffix = suffix
                run_sentences = [s.strip()] if suffix is not None else []
            if len(run_sentences) >= 3:
                c = agg_by_id["sentence-ending-monotony"]
                excerpt = "".join(run_sentences[-3:])
                violations.append(Violation("sentence-ending-monotony", excerpt, c["desc"], c["good_pattern"]))
                break

    if "style-mixing" in agg_by_id and _KEIGO_RE.search(text) and _JOTAI_RE.search(text):
        c = agg_by_id["style-mixing"]
        violations.append(Violation("style-mixing", "敬体と常体の混在", c["desc"], c["good_pattern"]))

    if "ai-filler-myth-count" in agg_by_id:
        count = text.count("これにより")
        if count >= 3:
            c = agg_by_id["ai-filler-myth-count"]
            violations.append(Violation("ai-filler-myth-count", f"「これにより」が{count}回出現", c["desc"], c["good_pattern"]))

    if "connector-spam" in agg_by_id:
        count = sum(text.count(w) for w in ("さらに", "また", "加えて"))
        if count >= _CONNECTOR_SPAM_THRESHOLD:
            c = agg_by_id["connector-spam"]
            violations.append(Violation("connector-spam", f"「さらに/また/加えて」が合計{count}回出現", c["desc"], c["good_pattern"]))

    return violations


def build_feedback_prompt(original_prompt: str, text: str, violations: list) -> str:
    lines = "\n".join(
        f"- [{v.id}] 該当箇所: {v.matched!r} / 理由: {v.reason} / 直し方: {v.good_pattern}"
        for v in violations
    )
    return f"""以下は先ほどの指示に基づいて生成した文章だが、文体チェックで違反が見つかった。
該当箇所を含む文を丸ごと書き直し(語の置換のみは不可)、違反を解消した全文を出力すること。
それ以外の変更は最小限にとどめる。

【元の指示】
{original_prompt}

【生成した文章】
{text}

【文体チェックの違反】
{lines}
"""


def _log_unresolved(violations: list) -> None:
    """リトライしても解消しなかった実例をJSONLに追記する。

    辞書のpattern_hint/good_patternを実例で肉付けするための材料として蓄積する
    (2026-08-17)。ログ失敗で本処理を止めない (fail-safe)。
    """
    timestamp = datetime.now().isoformat()
    try:
        with open(JA_STYLE_UNRESOLVED_LOG, "a", encoding="utf-8") as f:
            for v in violations:
                f.write(json.dumps({
                    "timestamp": timestamp,
                    "id": v.id,
                    "matched": v.matched,
                    "reason": v.reason,
                    "good_pattern": v.good_pattern,
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def enforce_style(text: str, original_prompt: str, model: str, max_retries: int = 1) -> str:
    """違反ゼロなら即返す。違反があれば1回だけ書き直しを再呼び出しする。

    再チェックしてまだ違反が残れば、無言で通さず末尾に警告を付記する
    (Known Bug Pattern A: 無言の死を避ける)。あわせて未解消の実例をログする。
    """
    violations = check_style(text)
    if not violations:
        return text

    for _ in range(max_retries):
        prompt = build_feedback_prompt(original_prompt, text, violations)
        resp = client.models.generate_content(model=model, contents=prompt)
        text = resp.text
        violations = check_style(text)
        if not violations:
            return text

    _log_unresolved(violations)
    ids = ", ".join(v.id for v in violations)
    return f"{text}\n\n⚠️ 文体チェック未解消: {ids}"
