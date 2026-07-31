# -*- coding: utf-8 -*-
"""
Phase 0 — docling 導入コストの実測 (実機 Windows で実行する)

plans/docling-integration.md の Phase 0-3。判断ゲートの材料を作るためのスクリプトで、
**このスクリプト自体は合否を決めない** (「日本語が化けていないか」「表が読めるか」は
人が変換結果を見て判断する。数値だけで決められる種類の問題ではない)。

やること:
  1. 環境の記録 (docling / torch のバージョン、CUDA が有効か)
  2. 検体ごとに変換して 秒数 / 文字数 / 表の数 / 警告 を測る
  3. 変換結果の Markdown を logs/docling_bench/ に保存する (**目視用**)
  4. plans/docling-integration.md の記入欄にそのまま貼れる Markdown 表を出力する

Usage:
  py scripts/bench_docling.py paper.pdf 請求書.pdf 資料.docx
  py scripts/bench_docling.py --ocr scanned.pdf          # OCR を有効にして比較
  py scripts/bench_docling.py --dir C:\\tmp\\bench_samples

検体の選び方 (計画書 0-3):
  - arXiv の英語論文 PDF   … 所要時間・表・数式
  - **日本語の PDF**       … 文字化け (cp932 環境で最も壊れやすい)
  - .docx か .pptx         … Office 系が同じ API で通るか
"""
import argparse
import datetime
import glob
import json
import os
import sys
import time
import tracemalloc

# プロジェクトルートを sys.path に追加（scripts/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import LOGS_DIR

OUTPUT_DIR = os.path.join(LOGS_DIR, "docling_bench")

# 対応拡張子 (docling がサポートする主なもの)
SUPPORTED_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm",
                 ".md", ".csv", ".png", ".jpg", ".jpeg", ".tiff"}


# ---------------------------------------------------------
# 環境の記録
# ---------------------------------------------------------

def _version(package: str) -> str:
    try:
        from importlib.metadata import version
        return version(package)
    except Exception as e:
        return f"(不明: {type(e).__name__})"


def collect_environment() -> dict:
    """docling / torch の状態を記録する。**ここで失敗しても続行する**。"""
    env = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "docling_version": _version("docling"),
        "torch_version": _version("torch"),
        "cuda_available": None,
        "artifacts_path": os.getenv("DOCLING_ARTIFACTS_PATH", "(未設定)"),
    }
    try:
        import torch
        env["cuda_available"] = bool(torch.cuda.is_available())
    except Exception as e:
        env["cuda_available"] = f"(判定不能: {e})"
    return env


# ---------------------------------------------------------
# 出力の計測 (表の数・日本語の健全性)
# ---------------------------------------------------------

def count_tables(markdown: str) -> int:
    """Markdown 表の数を数える。

    **インデクサと同じ判定器を使う** (`tool_vault_index._is_table_delimiter`)。
    ここで独自に数えると「ベンチでは表3個・インデクサからは0個」という
    食い違いが起きうる。import できない環境ではローカル判定に落とす。
    """
    try:
        from tools.tool_vault_index import _is_table_delimiter as is_delim
    except Exception:
        def is_delim(line):
            s = line.strip()
            return bool(s) and "|" in s and "-" in s and set(s) <= set("|-: \t")

    lines = markdown.split("\n")
    return sum(1 for i in range(len(lines) - 1)
               if "|" in lines[i] and is_delim(lines[i + 1]))


def japanese_health(markdown: str) -> dict:
    """文字化けの目安。**これで合否は決めない** (最終判断は目視)。

    - replacement: U+FFFD (デコード失敗の痕跡)。1文字でもあれば黒に近い
    - cjk_ratio  : 日本語文書なのに 0 に近ければ抽出に失敗している疑い
    """
    total = len(markdown) or 1
    cjk = sum(1 for ch in markdown
              if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff")
    return {
        "replacement_chars": markdown.count("\ufffd"),
        "cjk_ratio": round(cjk / total, 3),
    }


# ---------------------------------------------------------
# 変換
# ---------------------------------------------------------

def build_converter(ocr: bool):
    """DocumentConverter を作る。OCR 指定が通らない版なら既定構成に落とす。

    docling 2.x の想定 API (`PdfPipelineOptions` / `PdfFormatOption`) を使うが、
    **版によって名前が変わりうる**。ここで例外を投げると「まず最初に走らせる
    スクリプト」が環境確認の前に落ちてしまうので、既定構成へフォールバックし、
    OCR を有効にできなかったことを戻り値で申告する
    (黙って OCR 無しで測ると「OCR しても改善しなかった」という誤った結論になる)。

    Returns: (converter, note) — note は空文字なら要求どおりの構成
    """
    from docling.document_converter import DocumentConverter

    if not ocr:
        return DocumentConverter(), ""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import PdfFormatOption

        options = PdfPipelineOptions()
        options.do_ocr = True
        options.do_table_structure = True
        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        ), ""
    except Exception as e:
        print(f"  ⚠️ OCR 指定を適用できませんでした ({type(e).__name__}: {e}) — 既定構成で測ります")
        return DocumentConverter(), f"OCR 未適用 ({type(e).__name__}: {e})"


def convert_one(path: str, ocr: bool = False) -> dict:
    """1検体を変換して計測値を返す。失敗しても例外にせず結果へ記録する。"""
    result = {
        "file": os.path.basename(path),
        "size_kb": round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0,
        "ok": False,
        "seconds": 0.0,
        "chars": 0,
        "tables": 0,
        "peak_python_mb": 0.0,
        "error": "",
        "note": "",
        "output": "",
    }
    if not os.path.exists(path):
        result["error"] = "ファイルが存在しません"
        print(f"  ❌ {path} — ファイルが存在しません")
        return result

    try:
        # 可用性の確認だけ (実際の生成は build_converter が行う)
        import docling.document_converter  # noqa: F401
    except Exception as e:
        result["error"] = f"docling を import できません: {e}"
        print(f"  ❌ docling import 失敗: {e}")
        return result

    print(f"  ⏳ 変換中: {result['file']} ({result['size_kb']}KB){' [OCR]' if ocr else ''}")
    tracemalloc.start()
    started = time.perf_counter()
    try:
        converter, note = build_converter(ocr)
        result["note"] = note
        doc = converter.convert(path).document
        markdown = doc.export_to_markdown()
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        markdown = ""
        print(f"  ❌ 変換失敗: {result['error']}")
    finally:
        result["seconds"] = round(time.perf_counter() - started, 1)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # torch のネイティブ確保は含まない (Python 側の割当のみ) 点に注意
        result["peak_python_mb"] = round(peak / 1024 / 1024, 1)

    if not result["ok"]:
        return result

    result["chars"] = len(markdown)
    result["tables"] = count_tables(markdown)
    result.update(japanese_health(markdown))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = os.path.splitext(result["file"])[0]
    out_path = os.path.join(OUTPUT_DIR, f"{stem}{'_ocr' if ocr else ''}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    result["output"] = out_path

    print(f"  ✅ {result['seconds']}秒 / {result['chars']}字 / 表{result['tables']}個 "
          f"/ CJK率 {result.get('cjk_ratio')} / → {out_path}")
    return result


# ---------------------------------------------------------
# レポート
# ---------------------------------------------------------

def render_report(env: dict, results: list) -> str:
    """計画書の記入欄にそのまま貼れる Markdown を組み立てる。"""
    lines = [
        "### Phase 0-3 ベンチ結果",
        "",
        f"- 実施日: {datetime.datetime.now():%Y-%m-%d %H:%M}",
        f"- docling {env['docling_version']} / torch {env['torch_version']} "
        f"/ Python {env['python']}",
        f"- `torch.cuda.is_available()` = **{env['cuda_available']}** "
        f"(True なら Ollama/ComfyUI と VRAM を奪い合う — CPU版に入れ直す)",
        f"- `DOCLING_ARTIFACTS_PATH` = {env['artifacts_path']}",
        "",
        "| 検体 | 変換秒数 | 出力文字数 | 表の数 | CJK率 | 化け(U+FFFD) | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["ok"]:
            verdict = "✅" if r["seconds"] <= 120 else "⚠️ 120秒超"
            if r.get("note"):
                # 「要求した構成で測れなかった」を成功の陰に隠さない
                verdict += f" / ⚠️ {r['note'][:40]}"
        else:
            verdict = f"❌ {r['error'][:40]}"
        lines.append(
            f"| {r['file']} | {r['seconds']} | {r['chars']} | {r['tables']} | "
            f"{r.get('cjk_ratio', '-')} | {r.get('replacement_chars', '-')} | {verdict} |"
        )
    lines += [
        "",
        "**目視で確認すること** (数値では決められない):",
        f"- `{OUTPUT_DIR}` の .md を開き、日本語が化けていないか",
        "- 表が Markdown table (`| ... |`) として出ているか。"
        "ここが崩れていれば構造化の主目的が消えるので **導入見送り**",
        "- 表の数が原文と合っているか (0 なら table structure が効いていない)",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="docling 導入コストの実測 (Phase 0)")
    parser.add_argument("files", nargs="*", help="検体ファイル (PDF/DOCX/PPTX/画像)")
    parser.add_argument("--dir", default="", help="ディレクトリ内の対応ファイルを全部使う")
    parser.add_argument("--ocr", action="store_true", help="OCR を有効にして変換する")
    args = parser.parse_args()

    targets = list(args.files)
    if args.dir:
        for path in sorted(glob.glob(os.path.join(args.dir, "*"))):
            if os.path.splitext(path)[1].lower() in SUPPORTED_EXT:
                targets.append(path)

    if not targets:
        print("検体が指定されていません。\n"
              "  py scripts/bench_docling.py paper.pdf 日本語資料.pdf 資料.docx\n"
              "  py scripts/bench_docling.py --dir C:\\tmp\\bench_samples\n\n"
              "検体は3種類そろえること: arXiv英語PDF / 日本語PDF / Office ファイル")
        return 1

    env = collect_environment()
    print("=" * 60)
    print(f"docling {env['docling_version']} / torch {env['torch_version']} "
          f"/ CUDA={env['cuda_available']}")
    if env["cuda_available"] is True:
        print("⚠️ CUDA が有効です。Ollama(bge-m3)/ComfyUI と VRAM を奪い合う可能性があります。")
        print("   計画書 Phase 0-1 の通り CPU 版 torch で入れ直すことを検討してください。")
    print("=" * 60)

    if env["docling_version"].startswith("(不明"):
        print("\n❌ docling が入っていません。先に Phase 0-1 を実行してください:")
        print("   py -m pip install torch --index-url https://download.pytorch.org/whl/cpu")
        print("   py -m pip install -r requirements-docling.txt")
        print("   docling-tools models download")
        return 1

    results = [convert_one(p, ocr=args.ocr) for p in targets]

    os.makedirs(LOGS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(LOGS_DIR, f"docling_bench_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"environment": env, "results": results}, f, ensure_ascii=False, indent=2)

    report = render_report(env, results)
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)
    print(f"\n生データ: {json_path}")
    print("この表を plans/docling-integration.md の記入欄に貼り、"
          "目視確認の結果と併せて Go/No-Go を判定してください。")

    # 変換が1件も成功しなければ異常終了 (測定になっていない)
    return 0 if any(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
