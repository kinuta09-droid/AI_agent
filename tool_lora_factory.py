# -*- coding: utf-8 -*-
"""
LoRA Dataset Factory
ComfyUI API 経由で WD14 Tagger を実行し、kohya_ss 互換のデータセットを自動構築する。
"""

import os
import sys
import shutil

sys.stdout.reconfigure(encoding='utf-8')

from lib_comfy_client import ComfyUIClient

# Gemini フィルタ用（lazy — import失敗してもタグ付け自体は動く）
try:
    from agent_hello import client as gemini_client, MODEL_NAME, safe_json_loads
    _HAS_GEMINI = True
except ImportError:
    _HAS_GEMINI = False

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

RAW_IMAGES_DIR = os.path.join(
    "C:\\obsidian", "Obsidian", "30_Main", "Assets", "Raw_Images"
)
DATASETS_DIR = os.path.join(
    "C:\\obsidian", "Obsidian", "30_Main", "Assets", "LoRA_Datasets"
)
SUPPORTED_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
WD14_NODE_CLASS = "WD14Tagger|pysssss"
WD14_MODEL_NAME = "wd-vit-tagger-v3"
DEFAULT_THRESHOLD = 0.35
DEFAULT_CHAR_THRESHOLD = 0.85
DEFAULT_REPEATS = 10


# ---------------------------------------------------------
# WD14 Workflow Builder
# ---------------------------------------------------------

def _build_wd14_workflow(image_name: str,
                         threshold: float = DEFAULT_THRESHOLD,
                         char_threshold: float = DEFAULT_CHAR_THRESHOLD) -> dict:
    """ComfyUI API 形式の WD14 Tagger ワークフローを生成する。"""
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {
                "image": image_name,
            },
        },
        "2": {
            "class_type": WD14_NODE_CLASS,
            "inputs": {
                "image": ["1", 0],
                "model": WD14_MODEL_NAME,
                "threshold": threshold,
                "character_threshold": char_threshold,
                "replace_underscore": False,
            },
        },
    }


# ---------------------------------------------------------
# Tag Extraction
# ---------------------------------------------------------

def _extract_tags_from_history(history: dict, node_id: str = "2") -> str:
    """ComfyUI history レスポンスから WD14 タグ文字列を抽出する。"""
    outputs = history.get("outputs", {})
    node_output = outputs.get(node_id, {})

    # WD14Tagger|pysssss は "text" キーにタグを返す
    tags = node_output.get("text")
    if tags:
        if isinstance(tags, list):
            return tags[0] if tags else ""
        return str(tags)

    # フォールバック: 他のキー名を探す
    for key, val in node_output.items():
        if isinstance(val, list) and val and isinstance(val[0], str):
            return val[0]
        if isinstance(val, str):
            return val

    return ""


# ---------------------------------------------------------
# Gemini Tag Filter (optional)
# ---------------------------------------------------------

def _filter_tags_with_gemini(tags: str, trigger_word: str,
                             style_hint: str = "") -> str:
    """Gemini でタグをフィルタリングする（オプション）。"""
    if not _HAS_GEMINI or not style_hint:
        return tags

    prompt = f"""以下は画像のWD14タグです。LoRA学習用にタグを最適化してください。

【タグ】
{tags}

【トリガーワード】（先頭に必ず残す）
{trigger_word}

【スタイルの方向性】
{style_hint}

ルール:
- トリガーワードは必ず先頭に置く
- 学習対象のキャラクター特徴タグは残す（髪色、目の色、服装など）
- 背景描写・品質タグ（best quality, masterpiece 等）は除外
- カンマ区切りのタグ文字列のみを出力（説明文は不要）"""

    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME, contents=prompt
        )
        filtered = response.text.strip()
        # トリガーワードが先頭にあるか確認
        if not filtered.startswith(trigger_word):
            filtered = f"{trigger_word}, {filtered}"
        return filtered
    except Exception as e:
        print(f"  [Gemini] フィルタリング失敗（元タグを使用）: {e}")
        return f"{trigger_word}, {tags}"


# ---------------------------------------------------------
# Main Entry: run_lora_factory
# ---------------------------------------------------------

def run_lora_factory() -> str:
    """LoRA データセットを対話的に構築する。

    1. プロジェクト名・トリガーワード・入力フォルダを対話で取得
    2. ComfyUI 経由で WD14 タグ付け
    3. kohya_ss 互換のフォルダ構造に出力
    """
    print()
    print("=" * 50)
    print("  🎨 LoRA Dataset Factory")
    print("=" * 50)
    print()

    # --- 1. パラメータ入力 ---
    project_name = input("  プロジェクト名（例: my_character）: ").strip()
    if not project_name:
        return "エラー: プロジェクト名を入力してください。"

    trigger_word = input(f"  トリガーワード（デフォルト: {project_name}）: ").strip()
    if not trigger_word:
        trigger_word = project_name

    default_input = RAW_IMAGES_DIR
    input_dir = input(f"  画像フォルダ（デフォルト: {default_input}）: ").strip()
    if not input_dir:
        input_dir = default_input

    repeats_str = input(f"  リピート数（デフォルト: {DEFAULT_REPEATS}）: ").strip()
    repeats = int(repeats_str) if repeats_str.isdigit() else DEFAULT_REPEATS

    style_hint = input("  スタイルヒント（Geminiフィルタ用、スキップは空Enter）: ").strip()

    # --- 2. バリデーション ---
    if not os.path.isdir(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        return (
            f"エラー: 画像フォルダが空です。\n"
            f"フォルダを作成しました: {input_dir}\n"
            f"ここに学習用画像を配置してから再実行してください。"
        )

    # 画像ファイルを収集
    images = []
    for fname in sorted(os.listdir(input_dir)):
        ext = os.path.splitext(fname)[1].lower()
        if ext in SUPPORTED_EXT:
            images.append(os.path.join(input_dir, fname))

    if not images:
        return (
            f"エラー: {input_dir} に画像ファイルが見つかりません。\n"
            f"対応形式: {', '.join(SUPPORTED_EXT)}"
        )

    print(f"\n  📁 {len(images)} 枚の画像を検出")

    # --- 3. ComfyUI 疎通確認 ---
    comfy = ComfyUIClient()
    if not comfy.is_alive():
        return (
            "エラー: ComfyUI に接続できません。\n"
            "以下を確認してください:\n"
            f"  1. ComfyUI が起動していること (http://{comfy.base_url})\n"
            "  2. WD14 Tagger カスタムノードがインストールされていること\n"
            "  3. .env に COMFYUI_HOST / COMFYUI_PORT が正しく設定されていること"
        )

    print("  ✅ ComfyUI 接続OK")

    # --- 4. 出力フォルダ作成 ---
    output_base = os.path.join(DATASETS_DIR, project_name, "img",
                               f"{repeats}_{trigger_word}")

    if os.path.exists(output_base) and os.listdir(output_base):
        confirm = input(
            f"\n  ⚠️ 出力フォルダが既に存在します: {output_base}\n"
            f"  上書きしますか？ (y/N): "
        ).strip().lower()
        if confirm != 'y':
            return "中断しました。"
        # 既存ファイルをクリア
        for f in os.listdir(output_base):
            os.remove(os.path.join(output_base, f))

    os.makedirs(output_base, exist_ok=True)
    print(f"  📂 出力先: {output_base}")

    # --- 5. WD14 タグ付けループ ---
    print(f"\n  🔄 WD14 タグ付け開始...\n")
    results = []
    errors = []

    for i, img_path in enumerate(images, 1):
        fname = os.path.basename(img_path)
        base_name = os.path.splitext(fname)[0]
        ext = os.path.splitext(fname)[1]

        # 連番ファイル名
        new_name = f"{i:03d}{ext}"
        new_txt = f"{i:03d}.txt"

        print(f"  [{i}/{len(images)}] {fname}", end="")

        try:
            # a. ComfyUI にアップロード
            uploaded_name = comfy.upload_image(img_path)

            # b. WD14 ワークフロー生成 → キュー投入
            workflow = _build_wd14_workflow(uploaded_name)
            prompt_id = comfy.queue_prompt(workflow)

            # c. 完了待機
            history = comfy.wait_for_completion(prompt_id, timeout=120)

            # d. タグ抽出
            raw_tags = _extract_tags_from_history(history)
            if not raw_tags:
                print(" → ⚠️ タグ取得失敗（スキップ）")
                errors.append(f"{fname}: タグ取得失敗")
                continue

            # e. トリガーワード付与 + オプションフィルタ
            if style_hint:
                final_tags = _filter_tags_with_gemini(
                    raw_tags, trigger_word, style_hint
                )
            else:
                final_tags = f"{trigger_word}, {raw_tags}"

            # f. 画像コピー + .txt 保存
            dst_img = os.path.join(output_base, new_name)
            dst_txt = os.path.join(output_base, new_txt)

            shutil.copy2(img_path, dst_img)
            with open(dst_txt, 'w', encoding='utf-8') as f:
                f.write(final_tags)

            tag_count = len(final_tags.split(','))
            print(f" → ✅ {tag_count} tags")
            results.append(f"{new_name}: {tag_count} tags")

        except TimeoutError:
            print(" → ⏱️ タイムアウト（スキップ）")
            errors.append(f"{fname}: タイムアウト")
        except Exception as e:
            print(f" → ❌ {e}")
            errors.append(f"{fname}: {e}")

    # --- 6. サマリー ---
    print()
    summary_parts = [
        f"✅ LoRA Dataset Factory 完了",
        f"  プロジェクト: {project_name}",
        f"  トリガーワード: {trigger_word}",
        f"  処理: {len(results)}/{len(images)} 枚成功",
        f"  出力: {output_base}",
    ]
    if errors:
        summary_parts.append(f"\n  ⚠️ エラー ({len(errors)} 件):")
        for err in errors:
            summary_parts.append(f"    - {err}")

    return "\n".join(summary_parts)
