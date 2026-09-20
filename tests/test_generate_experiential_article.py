# -*- coding: utf-8 -*-
"""
scripts/generate_experiential_article.py のプロンプト構築ロジックの回帰テスト
(indexed-snacking-crab plan レビュー対応)。

対象:
  - refine_article: evaluation_result に "scores" キーが無くても TypeError を起こさない
    (f-string 内の {{}} が「空dictを1つ持つset」として評価され unhashable エラーになる
    既存バグ。再発防止として、以前から存在する不具合だが新機能もこの経路を通るため)
  - refine_article: reference_material が渡されたとき、参考資料の本文自体と
    出典維持・創作禁止・個人情報禁止のルールがプロンプトに実際に含まれること
    (以前は reference_material を受け取るだけで本文をプロンプトに埋め込んでいなかった)
  - generate_final_article / extract_missing_info_questions / refine_article /
    run_article_reflection_loop: audience(想定読者)が実際のプロンプト文字列に反映されること

client.models.generate_content をモックし、実Gemini APIは一切叩かない。

実行: py tests/test_generate_experiential_article.py
"""
import os
import sys
import types

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import scripts.generate_experiential_article as gea  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


class _RecordingClient:
    """generate_content に渡された prompt を記録するだけの偽クライアント。"""

    def __init__(self, response_text: str = "# 出力"):
        self.response_text = response_text
        self.last_prompt = None
        self.models = types.SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, model=None, contents=None, config=None):
        self.last_prompt = contents
        return types.SimpleNamespace(text=self.response_text)


def _with_recording_client(response_text: str = "# 出力"):
    class _Ctx:
        def __enter__(self):
            self._orig = gea.client
            self.fake = _RecordingClient(response_text)
            gea.client = self.fake
            return self.fake

        def __exit__(self, *exc):
            gea.client = self._orig

    return _Ctx()


def test_refine_article_does_not_raise_when_scores_missing():
    """evaluate_deliverable が例外時に返す {"error": ..., "status": "fail"} のように
    scores キーが無い evaluation_result でも TypeError を起こさないこと。"""
    with _with_recording_client("# 推敲後"):
        try:
            result = gea.refine_article(
                "# 初稿", "目標G", [{"point": "p", "question": "q", "answer": "a"}],
                {"error": "boom", "status": "fail"},
            )
            check("refine_article does not raise on missing scores", True)
            check("refine_article still returns text", result == "# 推敲後", result)
        except TypeError as e:
            check("refine_article does not raise on missing scores", False, str(e))


def test_refine_article_includes_reference_material_body():
    with _with_recording_client("# 推敲後") as fake:
        gea.refine_article(
            "# 初稿", "目標G", [{"point": "p", "question": "q", "answer": "a"}],
            {"scores": {"average": 6.0}, "feedback": "fb", "priority_improvement": "pi"},
            reference_material="採用資料の本文はこれです",
        )
    prompt = fake.last_prompt or ""
    check("reference material body present in refine prompt", "採用資料の本文はこれです" in prompt, prompt[:300])
    check("citation rule present in refine prompt", "出典" in prompt, prompt[:300])
    check("no-fabrication rule present in refine prompt", "創作" in prompt, prompt[:300])


def test_refine_article_includes_audience():
    with _with_recording_client("# 推敲後") as fake:
        gea.refine_article(
            "# 初稿", "目標G", [{"point": "p", "question": "q", "answer": "a"}],
            {"scores": {"average": 6.0}, "feedback": "fb", "priority_improvement": "pi"},
            audience="初心者向け",
        )
    prompt = fake.last_prompt or ""
    check("audience present in refine prompt", "初心者向け" in prompt, prompt[:300])


def test_generate_final_article_includes_audience():
    with _with_recording_client("# 記事") as fake:
        gea.generate_final_article(
            "目標G", [{"point": "p", "question": "q", "answer": "a"}], audience="初心者向け",
        )
    prompt = fake.last_prompt or ""
    check("audience present in generate_final_article prompt", "初心者向け" in prompt, prompt[:300])


def test_extract_missing_info_questions_includes_audience_and_reference():
    with _with_recording_client('[{"point": "p", "question": "q"}]') as fake:
        gea.extract_missing_info_questions("目標G", reference_material="採用資料X", audience="初心者向け")
    prompt = fake.last_prompt or ""
    check("audience present in extract_missing_info_questions prompt", "初心者向け" in prompt, prompt[:300])
    check("reference material present in extract_missing_info_questions prompt", "採用資料X" in prompt, prompt[:300])


def test_run_article_reflection_loop_forwards_audience_to_refine():
    """job_description に audience が乗るだけでなく、revision 時の refine_article 呼び出しにも
    audience が渡ること (2ループ目以降の推敲プロンプトにも反映される)。"""
    calls = []
    orig_refine = gea.refine_article

    def _spy_refine(*args, **kwargs):
        calls.append(kwargs.get("audience"))
        return "# 推敲後"

    orig_evaluate = gea.evaluate_deliverable
    try:
        gea.refine_article = _spy_refine
        # 1回目は revision_needed、2回目のループには入らない (max_loops=2 で1回だけ revise)
        gea.evaluate_deliverable = lambda *a, **k: {"status": "revision_needed", "scores": {"average": 5.0}, "feedback": ""}
        gea.run_article_reflection_loop(
            "# 初稿", "目標G", [{"point": "p", "question": "q", "answer": "a"}],
            max_loops=2, audience="初心者向け",
        )
    finally:
        gea.refine_article = orig_refine
        gea.evaluate_deliverable = orig_evaluate

    check("refine_article called during reflection loop", len(calls) == 1, calls)
    check("audience forwarded to refine_article call", calls == ["初心者向け"], calls)


def test_apply_user_revision_includes_article_and_request():
    with _with_recording_client("# 修正後") as fake:
        result = gea.apply_user_revision("# 元の記事\n本文", "もっと具体的にして")
    prompt = fake.last_prompt or ""
    check("original article present in revision prompt", "# 元の記事" in prompt, prompt[:300])
    check("user's revision request present in prompt", "もっと具体的にして" in prompt, prompt[:300])
    check("apply_user_revision returns generated text", result == "# 修正後", result)


def test_apply_user_revision_includes_reference_and_audience():
    with _with_recording_client("# 修正後") as fake:
        gea.apply_user_revision(
            "# 元の記事", "出典を維持して", reference_material="採用資料の本文", audience="初心者向け",
        )
    prompt = fake.last_prompt or ""
    check("reference material present in revision prompt", "採用資料の本文" in prompt, prompt[:300])
    check("citation-preservation rule present", "出典" in prompt, prompt[:300])
    check("audience present in revision prompt", "初心者向け" in prompt, prompt[:300])


def test_apply_user_revision_does_not_call_evaluation_loop():
    """単発修正であり、評価・推敲ループ(evaluate_deliverable)は呼ばないこと
    (ユーザーの直接指示を汎用品質フィードバックで上書きしないため)。"""
    eval_calls = []
    orig_evaluate = gea.evaluate_deliverable
    try:
        gea.evaluate_deliverable = lambda *a, **k: eval_calls.append(1)
        with _with_recording_client("# 修正後"):
            gea.apply_user_revision("# 元の記事", "直して")
    finally:
        gea.evaluate_deliverable = orig_evaluate
    check("evaluate_deliverable not called by apply_user_revision", len(eval_calls) == 0, eval_calls)


def test_apply_user_revision_returns_original_on_failure():
    class _FailingClient:
        def __init__(self):
            self.models = types.SimpleNamespace(generate_content=self._raise)

        def _raise(self, model=None, contents=None, config=None):
            raise RuntimeError("boom")

    orig_client = gea.client
    try:
        gea.client = _FailingClient()
        result = gea.apply_user_revision("# 元の記事", "直して")
    finally:
        gea.client = orig_client
    check("original article returned when generation fails", result == "# 元の記事", result)


if __name__ == "__main__":
    test_refine_article_does_not_raise_when_scores_missing()
    test_refine_article_includes_reference_material_body()
    test_refine_article_includes_audience()
    test_generate_final_article_includes_audience()
    test_extract_missing_info_questions_includes_audience_and_reference()
    test_run_article_reflection_loop_forwards_audience_to_refine()
    test_apply_user_revision_includes_article_and_request()
    test_apply_user_revision_includes_reference_and_audience()
    test_apply_user_revision_does_not_call_evaluation_loop()
    test_apply_user_revision_returns_original_on_failure()

    print("\n" + "=" * 60)
    print(f"結果: {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("失敗:")
        for f in _FAIL:
            print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
