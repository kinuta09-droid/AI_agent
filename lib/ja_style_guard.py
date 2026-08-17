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

from lib.paths import JA_STYLE_DICT_PATH
from lib.lib_shared import client

_ENTRY_PATTERNS = {
    "vague-effect-verb": re.compile(r"(効きます|効果があります|役立ちます)(?=[。\s]|$)"),
    "negation-lead": re.compile(r"ではなく、?[^。]{0,40}(だ|である|です)"),
    "ai-cliche-hype": re.compile(r"(革命的な|可能性を解き放|魔法のように|ゲームチェンジ)"),
    "format-clash": re.compile(r"[✅💡🚀✨🎉🔥]"),
    "literal-translation": re.compile(r"(掘り下げ|重要な役割を果た)"),
    "redundant-predicate": re.compile(r"(を行うことができ|ということが大切です)"),
}
_MONOTONY_SUFFIXES = ("ます", "です", "した", "ました", "でした", "ません")
_KEIGO_RE = re.compile(r"(ます|です)。")
_JOTAI_RE = re.compile(r"(だ|である)。")


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
        run_suffix, run_len = None, 0
        for s in sentences:
            stripped = s.rstrip("。\n ")
            suffix = next((suf for suf in _MONOTONY_SUFFIXES if stripped.endswith(suf)), None)
            if suffix is not None and suffix == run_suffix:
                run_len += 1
            else:
                run_suffix, run_len = suffix, (1 if suffix is not None else 0)
            if run_len >= 3:
                c = agg_by_id["sentence-ending-monotony"]
                violations.append(Violation("sentence-ending-monotony", f"「{run_suffix}」が3文以上連続", c["desc"], c["good_pattern"]))
                break

    if "style-mixing" in agg_by_id and _KEIGO_RE.search(text) and _JOTAI_RE.search(text):
        c = agg_by_id["style-mixing"]
        violations.append(Violation("style-mixing", "敬体と常体の混在", c["desc"], c["good_pattern"]))

    if "ai-filler-myth-count" in agg_by_id:
        count = text.count("これにより")
        if count >= 3:
            c = agg_by_id["ai-filler-myth-count"]
            violations.append(Violation("ai-filler-myth-count", f"「これにより」が{count}回出現", c["desc"], c["good_pattern"]))

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


def enforce_style(text: str, original_prompt: str, model: str, max_retries: int = 1) -> str:
    """違反ゼロなら即返す。違反があれば1回だけ書き直しを再呼び出しする。

    再チェックしてまだ違反が残れば、無言で通さず末尾に警告を付記する
    (Known Bug Pattern A: 無言の死を避ける)。
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

    ids = ", ".join(v.id for v in violations)
    return f"{text}\n\n⚠️ 文体チェック未解消: {ids}"
