# -*- coding: utf-8 -*-
"""
手書き書類記入補助システム (tool_form_fill) の回帰テスト。

現在は段階[1][2][3][4][5][6] (検出・OCR・Gemini分類・プロフィール自動入力・確認画面・PDF出力) を実装済み。
実書類の画像は個人情報を含みうるためリポジトリにコミットせず、テスト用画像はその場で
OpenCVで合成する (白背景+黒い矩形枠+黒い下線)。classify_labels() はGeminiを実際に呼ぶため
ARMS_LIVE_TESTS=1 かつ実キーがあるときだけ実行する (test_client_review.py と同じ作法)。

実行: py tests/test_tool_form_fill.py
実行 (Gemini分類も含む): ARMS_LIVE_TESTS=1 py tests/test_tool_form_fill.py
"""
import io
import os
import sys
import tempfile
import shutil
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import cv2
import fitz
import numpy as np
import requests

import tools.tool_form_fill as form_fill
from tools.tool_form_fill import (
    detect_boxes, pdf_to_image, ocr_labels_near_boxes, classify_labels,
    parse_personal_info_section, autofill_values, run_review_server, render_pdf,
)


def _live_gemini_allowed() -> bool:
    """実 Gemini 呼び出しを行ってよいか (オプトイン + 実キーの両方が必要)。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def _make_synthetic_form(path: str) -> None:
    """白背景に「閉じた矩形の枠」と「下線のみ」を1つずつ描いた合成書類画像を作る。"""
    img = np.full((400, 600, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (50, 50), (250, 100), (0, 0, 0), 2)   # 矩形の記入枠
    cv2.line(img, (50, 250), (300, 250), (0, 0, 0), 2)       # 下線のみの記入欄
    cv2.imwrite(path, img)


def test_detect_boxes_on_synthetic_image():
    tmp = tempfile.mkdtemp(prefix="form_fill_test_")
    try:
        img_path = os.path.join(tmp, "synthetic_form.png")
        _make_synthetic_form(img_path)

        boxes = detect_boxes(img_path, min_area=500, min_line_length=100)

        kinds = {b["kind"] for b in boxes}
        assert "rect" in kinds, f"矩形枠が検出されなかった: {boxes}"
        assert "underline" in kinds, f"下線のみの記入欄が検出されなかった: {boxes}"
        print(f"✅ 合成画像から矩形枠・下線をともに検出できた ({len(boxes)}件)")

        # 実書類の確認画面(/)で発覚したバグの回帰テスト: cv2/numpy由来の整数型 (intc等) は
        # json.dumps できず、Stage 5 のレビュー画面が 500 エラーになっていた。
        import json
        json.dumps(boxes, ensure_ascii=False)
        for b in boxes:
            assert type(b["x"]) is int and type(b["y"]) is int, \
                f"x/yがnumpy型のままjson化できない可能性: {[(k, type(v)) for k, v in b.items()]}"
        print("✅ 検出結果がjson.dumpsできる (numpy整数型が残っていない)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_detect_boxes_missing_file_returns_empty():
    boxes = detect_boxes(os.path.join(tempfile.gettempdir(), "no_such_form_fill_image.png"))
    assert boxes == []
    print("✅ 存在しない画像パスは例外を投げず空リストを返す")


def test_pdf_to_image_rasterizes_first_page():
    tmp = tempfile.mkdtemp(prefix="form_fill_pdf_test_")
    try:
        pdf_path = os.path.join(tmp, "synthetic_form.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=400)
        page.draw_rect(fitz.Rect(50, 50, 250, 100))
        page.draw_line(fitz.Point(50, 250), fitz.Point(300, 250))
        doc.save(pdf_path)
        doc.close()

        out_path = os.path.join(tmp, "rasterized.png")
        result_path = pdf_to_image(pdf_path, out_path)

        assert result_path == out_path
        assert os.path.exists(out_path)
        boxes = detect_boxes(out_path, min_area=500, min_line_length=100)
        kinds = {b["kind"] for b in boxes}
        assert "rect" in kinds and "underline" in kinds, f"PDFラスター化後の検出結果: {boxes}"
        print("✅ PDFをラスター化した画像からも矩形枠・下線を検出できた (スキャナ到着前の代替入力経路)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ocr_labels_near_boxes_matches_inside_and_left():
    """実際の申込書 (個人番号カード交付申請書) での検証結果を踏まえたテスト:
    ラベルは枠の内側上部 (例: 氏名※2) にあることが多いが、枠の左 (例: 古い様式のラベル配置) の
    こともある。両方のケースで最も近いラベルを拾えることを確認する。RapidOCRはローカル完結
    (外部API不使用) のため ARMS_LIVE_TESTS ゲートは不要。
    """
    tmp = tempfile.mkdtemp(prefix="form_fill_ocr_test_")
    try:
        img = np.full((400, 600, 3), 255, dtype=np.uint8)
        # ケース1: ラベルが枠の内側上部にある (実書類で確認した主流パターン)
        cv2.rectangle(img, (50, 50), (300, 150), (0, 0, 0), 2)
        cv2.putText(img, "NAME", (60, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        # ケース2: ラベルが枠の左にある
        cv2.rectangle(img, (250, 250), (500, 300), (0, 0, 0), 2)
        cv2.putText(img, "ADDR", (150, 280), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        img_path = os.path.join(tmp, "synthetic_labeled_form.png")
        cv2.imwrite(img_path, img)

        boxes = detect_boxes(img_path, min_area=500)
        rects = [b for b in boxes if b["kind"] == "rect"]
        assert len(rects) == 2, f"矩形枠が2つ検出されるはず: {rects}"

        labeled = ocr_labels_near_boxes(img_path, rects)
        texts = {b["box_id"]: b["label_text"] for b in labeled}
        found = " ".join(texts.values())
        assert "NAME" in found, f"内側ラベルが見つからなかった: {texts}"
        assert "ADDR" in found, f"左側ラベルが見つからなかった: {texts}"
        print(f"✅ 内側/左側どちらのラベル配置でも紐付けできた: {texts}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_classify_labels_empty_input_skips_api_call():
    """ラベルが1件も無い、または全て長すぎる場合は Gemini を呼ばず即 {} を返す (課金ゼロ)。"""
    assert classify_labels([]) == {}
    long_text = "あ" * 30  # MAX_LABEL_LENGTH(20)を超える -> 注記文とみなして除外
    boxes = [{"box_id": 0, "label_text": long_text}]
    assert classify_labels(boxes) == {}
    print("✅ ラベル無し/長すぎるラベルのみの場合はAPIを呼ばず空の辞書を返す")


def test_classify_labels_live():
    """実際にGeminiへラベル文字列を送って分類する (画像・個人情報は一切送らない)。"""
    if not _live_gemini_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("   (classify_labels は Gemini を実呼び出しするため課金が発生します)")
        return

    long_text = "あ" * 25  # MAX_LABEL_LENGTH(20)を超える -> 注記文とみなして除外対象
    boxes = [
        {"box_id": 0, "label_text": "氏名"},
        {"box_id": 1, "label_text": "住所"},
        {"box_id": 2, "label_text": long_text},
    ]
    categories = classify_labels(boxes)
    assert categories.get(0) == "氏名", f"「氏名」が氏名カテゴリに分類されなかった: {categories}"
    assert categories.get(1) == "住所", f"「住所」が住所カテゴリに分類されなかった: {categories}"
    assert 2 not in categories, f"20文字超のラベルは分類対象から除外されるはず: {categories}"
    print(f"✅ 実際のGemini分類で正しいカテゴリが返ってきた: {categories}")


def test_parse_personal_info_section_parses_key_value_bullets():
    """架空のテストデータで User_Profile.md を一時的に作り、パーソナル情報欄をパースする。
    実Vaultには一切触れない (vault_root を明示的に差し替え)。
    """
    tmp = tempfile.mkdtemp(prefix="form_fill_profile_test_")
    try:
        system_dir = os.path.join(tmp, "00_System")
        os.makedirs(system_dir)
        profile_md = (
            "# User Profile\n\n"
            "## パーソナル情報\n"
            "> [!WARNING] 手動編集専用\n\n"
            "- 氏名:テスト太郎\n"
            "- 生年月日:2000年1月1日\n"
            "- 居住地:テスト県テスト市\n"
            "- その他:\n\n"
            "## 2026-02-08 21:48\n"
            "- **Q**: 無関係なセクション\n"
        )
        with open(os.path.join(system_dir, "User_Profile.md"), "w", encoding="utf-8") as f:
            f.write(profile_md)

        profile = parse_personal_info_section(vault_root=tmp)
        assert profile.get("氏名") == "テスト太郎"
        assert profile.get("生年月日") == "2000年1月1日"
        assert profile.get("居住地") == "テスト県テスト市"
        assert "その他" not in profile, "値が空の項目は辞書に含めないはず"
        assert "Q" not in profile, "他のセクションの内容を拾ってはいけない"
        print("✅ パーソナル情報セクションのkey:valueを正しくパースできた (テスト用の架空データ)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parse_personal_info_section_missing_file_returns_empty():
    tmp = tempfile.mkdtemp(prefix="form_fill_profile_missing_test_")
    try:
        assert parse_personal_info_section(vault_root=tmp) == {}
        print("✅ User_Profile.mdが無い場合は例外を投げず空の辞書を返す")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_autofill_values_matching_and_alias():
    """架空のテストデータでカテゴリ経由の辞書引きとエイリアス解決を確認する (Gemini不使用)。"""
    labeled_boxes = [
        {"box_id": 0, "label_text": "氏名2", "w": 800, "h": 100},
        {"box_id": 1, "label_text": "住所2", "w": 800, "h": 100},    # カテゴリ「住所」、プロフィール側キーは「居住地」
        {"box_id": 2, "label_text": "備考欄", "w": 800, "h": 100},    # 該当カテゴリなし
        {"box_id": 3, "label_text": "電話番号4", "w": 800, "h": 100},  # カテゴリ「電話番号」、プロフィールに未整備
        {"box_id": 4, "label_text": "メール", "w": 800, "h": 100},     # カテゴリ「メールアドレス」、旧キー「連絡先」に暫定フォールバック
    ]
    categories = {0: "氏名", 1: "住所", 2: "その他", 3: "電話番号", 4: "メールアドレス"}
    profile = {"氏名": "テスト太郎", "居住地": "テスト県テスト市", "連絡先": "test@example.com"}

    filled = autofill_values(labeled_boxes, categories, profile)
    by_id = {b["box_id"]: b["fill_text"] for b in filled}
    assert by_id[0] == "テスト太郎"
    assert by_id[1] == "テスト県テスト市", "「住所」カテゴリはプロフィールの「居住地」キーにエイリアス解決されるはず"
    assert by_id[2] == "", "「その他」カテゴリは自動入力しない"
    assert by_id[3] == "", "プロフィールに「電話番号」キーが無ければ空のまま (誤って連絡先を流用しない)"
    assert by_id[4] == "test@example.com", "「メールアドレス」カテゴリは旧キー「連絡先」にフォールバックするはず"
    print("✅ カテゴリ経由の辞書引き、電話番号/メールアドレス分割後のエイリアス解決が機能した")


def test_autofill_values_skips_narrow_duplicate_of_same_label():
    """実書類 (個人番号カード交付申請書) のPDF出力で発覚した回帰テスト:
    同じ label_text (例: 「住所2」) が「ラベル専用の狭いセル」と「実際に書き込む広いセル」の
    両方に紐付くとき、狭い方まで自動入力すると印字済みラベルと文字が重なって読めなくなる。
    同一ラベルの中で最も広い枠だけを自動入力対象にすることを確認する。
    """
    labeled_boxes = [
        {"box_id": 0, "label_text": "住所2", "w": 155, "h": 180},  # ラベル専用の狭いセル
        {"box_id": 1, "label_text": "住所2", "w": 866, "h": 180},  # 実際に書き込む広いセル
    ]
    categories = {0: "住所", 1: "住所"}
    profile = {"住所": "テスト県テスト市"}

    filled = autofill_values(labeled_boxes, categories, profile)
    by_id = {b["box_id"]: b["fill_text"] for b in filled}
    assert by_id[0] == "", f"狭い方のセルは自動入力しないはず: {by_id}"
    assert by_id[1] == "テスト県テスト市", f"広い方のセルだけ自動入力されるはず: {by_id}"
    print("✅ 同一ラベルの重複セルは、最も広いものだけが自動入力対象になる")


def test_run_review_server_returns_edited_boxes():
    """/submit へのPOSTでブロッキングが解除され、編集後のboxesが返ることを確認する。
    実ブラウザは使わずHTTPリクエストで「確定」操作を模擬する (webbrowser.openは無効化して
    テスト実行中に実ブラウザが開かないようにする)。画像・値はテスト専用のダミー。
    """
    tmp = tempfile.mkdtemp(prefix="form_fill_review_test_")
    original_open = form_fill.webbrowser.open
    form_fill.webbrowser.open = lambda url: None
    test_port = 15151
    try:
        img_path = os.path.join(tmp, "dummy.png")
        cv2.imwrite(img_path, np.full((100, 100, 3), 255, dtype=np.uint8))
        boxes = [{"box_id": 0, "x": 1, "y": 1, "w": 10, "h": 10, "kind": "rect",
                  "label_text": "TEST", "label_score": 1.0, "category": "その他", "fill_text": ""}]

        result_holder = {}

        def _run():
            result_holder["boxes"] = run_review_server(img_path, boxes, port=test_port)

        server_thread = threading.Thread(target=_run, daemon=True)
        server_thread.start()

        edited = [dict(boxes[0], fill_text="EDITED")]
        resp = None
        for _ in range(50):
            try:
                resp = requests.post(f"http://127.0.0.1:{test_port}/submit",
                                      json={"boxes": edited}, timeout=1)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.1)
        assert resp is not None and resp.status_code == 200, "レビューサーバが起動しなかった"

        server_thread.join(timeout=5)
        assert result_holder["boxes"][0]["fill_text"] == "EDITED", \
            f"確定後の編集内容が反映されていない: {result_holder}"
        print("✅ レビュー画面の確定 (/submit) でブロッキングが解除され、編集後の内容が返ってくる")
    finally:
        form_fill.webbrowser.open = original_open
        shutil.rmtree(tmp, ignore_errors=True)


def test_render_pdf_opacity_changes_output_and_skips_empty_fill_text():
    """ダミー画像・ダミー値でPDF出力を確認する (個人情報は含まない)。
    opacity違いで出力バイト列が変わること、fill_textが空の枠はスキップされることを検証する。
    """
    tmp = tempfile.mkdtemp(prefix="form_fill_render_test_")
    try:
        img_path = os.path.join(tmp, "dummy.png")
        cv2.imwrite(img_path, np.full((200, 300, 3), 255, dtype=np.uint8))
        boxes = [
            {"box_id": 0, "x": 10, "y": 10, "w": 100, "h": 20, "kind": "rect", "fill_text": "テスト太郎"},
            {"box_id": 1, "x": 10, "y": 50, "w": 100, "h": 20, "kind": "rect", "fill_text": ""},
        ]

        out_a = os.path.join(tmp, "pattern_a.pdf")
        out_c = os.path.join(tmp, "pattern_c.pdf")
        render_pdf(img_path, boxes, out_a, opacity=1.0)
        render_pdf(img_path, boxes, out_c, opacity=0.1)

        assert os.path.exists(out_a) and os.path.getsize(out_a) > 0
        assert os.path.exists(out_c) and os.path.getsize(out_c) > 0
        with open(out_a, "rb") as f:
            bytes_a = f.read()
        with open(out_c, "rb") as f:
            bytes_c = f.read()
        assert bytes_a != bytes_c, "opacityが違うのに出力PDFの内容が同一になっている"

        # 空文字の枠 (box_id=1) はスキップされ、テキスト挿入で例外にならないことも
        # 上のrender_pdf呼び出しが例外を投げずに完走した時点で確認済み
        print("✅ PDF出力ができ、opacity違いで内容が変わり、空文字の枠はスキップされる")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_render_pdf_missing_font_warns_instead_of_silent_failure():
    """grillで発見: フォントが見つからない場合、known-bug-patterns.md Category Aと同型の
    「無言の失敗」(日本語が描画されないPDFを何も警告せず'出力しました'と表示する)を防ぐため、
    警告を標準出力に出すことを確認する (例外は投げず、PDF自体は生成される)。
    """
    tmp = tempfile.mkdtemp(prefix="form_fill_font_test_")
    original_stdout = sys.stdout
    try:
        img_path = os.path.join(tmp, "dummy.png")
        cv2.imwrite(img_path, np.full((100, 100, 3), 255, dtype=np.uint8))
        boxes = [{"box_id": 0, "x": 10, "y": 10, "w": 50, "h": 20, "kind": "rect", "fill_text": "test"}]
        out_path = os.path.join(tmp, "out.pdf")
        bogus_font = os.path.join(tmp, "no_such_font.ttc")

        captured = io.StringIO()
        sys.stdout = captured
        render_pdf(img_path, boxes, out_path, font_path=bogus_font)
        sys.stdout = original_stdout

        assert os.path.exists(out_path), "フォントが無くてもPDF自体は例外を投げず生成されるはず"
        assert "⚠️" in captured.getvalue() and "フォント" in captured.getvalue(), \
            f"フォント欠落時に警告が出ていない (無言の失敗): {captured.getvalue()!r}"
        print("✅ フォントが見つからない場合、無言で失敗せず警告を出す")
    finally:
        sys.stdout = original_stdout
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_html_escapes_label_text_against_xss():
    """grillで発見したXSS修正の回帰テスト: label_textに<script>を壊す文字列が含まれても、
    レビュー画面のHTMLにraw な '</script>' が (意図したscriptタグの終端以外に) 出力されない
    ことを確認する。Flaskのtojsonフィルタ (HTMLセーフなJSONエスケープ) 経由で埋め込む。
    """
    from flask import Flask, render_template

    app = Flask("test_form_fill_review", template_folder=form_fill._TEMPLATE_DIR)
    malicious_boxes = [{"box_id": 0, "x": 0, "y": 0, "w": 10, "h": 10, "kind": "rect",
                         "label_text": '</script><script>alert(1)</script>', "fill_text": ""}]
    with app.app_context(), app.test_request_context():
        html = render_template("form_fill_review.html", image_data="data:image/png;base64,",
                                boxes=malicious_boxes)

    # 意図した唯一の </script> (scriptブロック自体の終端) 以外に出現しないこと
    assert html.count("</script>") == 1, \
        f"label_text由来の</script>がエスケープされずHTMLに出力されている (XSS): {html.count('</script>')}件"
    print("✅ label_textにHTML特殊文字が含まれてもtojson経由で安全にエスケープされる")


if __name__ == "__main__":
    test_detect_boxes_on_synthetic_image()
    test_detect_boxes_missing_file_returns_empty()
    test_pdf_to_image_rasterizes_first_page()
    test_ocr_labels_near_boxes_matches_inside_and_left()
    test_classify_labels_empty_input_skips_api_call()
    test_classify_labels_live()
    test_parse_personal_info_section_parses_key_value_bullets()
    test_parse_personal_info_section_missing_file_returns_empty()
    test_autofill_values_matching_and_alias()
    test_autofill_values_skips_narrow_duplicate_of_same_label()
    test_run_review_server_returns_edited_boxes()
    test_render_pdf_opacity_changes_output_and_skips_empty_fill_text()
    test_render_pdf_missing_font_warns_instead_of_silent_failure()
    test_review_html_escapes_label_text_against_xss()
    print("\n🎉 ALL TOOL_FORM_FILL TESTS PASSED")
