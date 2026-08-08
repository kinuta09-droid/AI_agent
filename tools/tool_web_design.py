# -*- coding: utf-8 -*-
"""
Web Design Tool — AI駆動型ウェブデザイン・量産ワークフローの自動化パーツ
(.claude/skills/ai-web-design-workflow/SKILL.md 参照)

Flow:
  Phase2: 背景アセット(Background Only) + UI構造イメージ を fal-ai/flux/schnell で生成
  Phase4 Option A: kling-video/v2.5-turbo/pro でループ動画を生成
  Phase4 Option B: tools/templates/webgl_scaffold.html からThree.js雛形を生成(外部API不要)

Usage:
  py tools/tool_web_design.py --phase2
  py tools/tool_web_design.py --phase4-video --image-url https://... --out-dir tmp_web_design
  py tools/tool_web_design.py --scaffold-webgl --pattern B --plugins trail,bloom --title "My Name" --out tmp_web_design/scaffold.html
"""

import os
import re
import sys
import argparse

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

import requests

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

TEMPLATE_DIR = os.path.join(_PROJECT_ROOT, "tools", "templates")
WEBGL_TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "webgl_scaffold.html")

_VALID_PATTERNS = {"A": 0, "B": 1, "C": 2}
_VALID_PLUGINS = {"trail", "bloom", "grain"}

_DEFAULT_BACKGROUND_PROMPT = (
    "Pure black background, abstract dark glass fluid 3D object floating center-left, "
    "subtle low-saturation teal-green gradient glow, fine grain texture, no text, no logo, "
    "no icons, no UI elements, minimalist portfolio website background, 4k"
)
_DEFAULT_UI_PROMPT = (
    "Pure black background (#000000) only, flat wireframe layout mockup for a personal "
    "portfolio website hero section, simple white rectangle placeholders and thin lines "
    "marking where a name headline, subtitle, and one button would go, no real text, "
    "no color, no photos, minimal UI structure diagram"
)
_DEFAULT_VIDEO_PROMPT = (
    "Extremely slow, subtle ambient drift of the subject, gentle internal light shimmer, "
    "no camera movement, no zoom in, no zoom out, seamless loopable motion"
)


# ---------------------------------------------------------
# Phase 2: 背景アセット + UI構造イメージ生成
# ---------------------------------------------------------

def run_phase2(background_prompt: str = "", ui_prompt: str = "",
               image_size: str = "landscape_16_9", out_dir: str = "") -> str:
    """Phase2: 背景アセット(Background Only)とUI構造イメージを fal-ai/flux/schnell で生成する。
    最小コストのモデル(flux/schnell)を使う。out_dir を指定するとダウンロードして保存し、
    未指定なら fal.ai 上のURLを返すだけ(SKILL.md Phase2参照)。
    """
    try:
        import fal_client
    except ImportError:
        return "Error: fal_client is not installed (py -m pip install fal_client)"

    prompts = {
        "background": background_prompt or _DEFAULT_BACKGROUND_PROMPT,
        "ui_layout": ui_prompt or _DEFAULT_UI_PROMPT,
    }

    lines = []
    try:
        for name, prompt in prompts.items():
            handler = fal_client.submit(
                "fal-ai/flux/schnell",
                arguments={"prompt": prompt, "image_size": image_size},
            )
            result = handler.get()
            url = result["images"][0]["url"]
            if out_dir:
                path = _download(url, out_dir, f"{name}.jpg")
                lines.append(f"{name}: {path} (source: {url})")
            else:
                lines.append(f"{name}: {url}")
    except Exception as e:
        # 2枚中1枚が成功した後にもう1枚が失敗した場合、成功分(=既に課金済み)を
        # Errorメッセージに埋め込んで残す。素の f"Error: {e}" だけ返すと、
        # 先に生成できたURLが消えて呼び出し元が再生成(=二重課金)しかねない。
        if lines:
            return f"Error: {e} (但し以下は生成済み・再生成不要:\n" + "\n".join(lines) + ")"
        return f"Error: {e}"

    return "run_phase2:\n" + "\n".join(lines)


# ---------------------------------------------------------
# Phase 4 Option A: 動画ループ生成
# ---------------------------------------------------------

def run_phase4_video(image_url: str, prompt: str = "", duration: str = "5",
                      tail_image_url: str = "", out_dir: str = "") -> str:
    """Phase4 Option A: fal-ai/kling-video/v2.5-turbo/pro で背景に動きを付ける。

    tail_image_url: SKILL.mdに基づく「終点フレームに始点と同じ画像を指定するとネイティブに
    シームレスループする」という改善案。このツールでは引数をそのままAPIへパススルーするのみで、
    実際にシームレスループになるかはこのツール側では検証していない(未検証)。
    """
    if not image_url:
        return "Error: image_url is required"

    try:
        import fal_client
    except ImportError:
        return "Error: fal_client is not installed (py -m pip install fal_client)"

    arguments = {
        "image_url": image_url,
        "prompt": prompt or _DEFAULT_VIDEO_PROMPT,
        "duration": duration,
    }
    if tail_image_url:
        arguments["tail_image_url"] = tail_image_url

    try:
        handler = fal_client.submit(
            "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            arguments=arguments,
        )
        result = handler.get()
        url = result["video"]["url"]
    except Exception as e:
        return f"Error: {e}"

    if out_dir:
        path = _download(url, out_dir, "loop.mp4")
        return f"run_phase4_video: {path} (source: {url})"
    return f"run_phase4_video: {url}"


def _download(url: str, out_dir: str, filename: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


# ---------------------------------------------------------
# Phase 4 Option B: Three.js/WebGL 雛形生成(外部API不要)
# ---------------------------------------------------------

def scaffold_webgl(pattern: str = "A", plugins: str = "trail", title: str = "Your Name",
                    eyebrow: str = "Option B — Live WebGL", tagline: str = "",
                    out: str = "") -> str:
    """Phase4 Option B: tools/templates/webgl_scaffold.html からThree.jsのライブ3Dシーン雛形を生成する。
    pattern: 初期アクティブにするシーン(A=ネオン管 / B=星雲パーティクル / C=ガラスの破片)。
    plugins: 初期チェック済みにするPostFXプラグイン名のカンマ区切り(trail/bloom/grainの部分集合)。
    切替UI自体は常に3パターン×3プラグイン全部を埋め込む(SKILL.mdの「3方向提案」用途に対応するため)。
    """
    if pattern not in _VALID_PATTERNS:
        return f"Error: unknown pattern '{pattern}' (expected A|B|C)"

    plugin_list = [p.strip() for p in plugins.split(",") if p.strip()]
    if not set(plugin_list) <= _VALID_PLUGINS:
        return f"Error: unknown plugin in '{plugins}' (expected subset of trail,bloom,grain)"

    try:
        with open(WEBGL_TEMPLATE_PATH, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        return f"Error: {e}"

    plugins_js = "{ " + ", ".join(
        f"{name}: {'true' if name in plugin_list else 'false'}" for name in sorted(_VALID_PLUGINS)
    ) + " }"

    # トークンは一括(1パス)で置換する。逐次 str.replace() を繋げると、
    # title/eyebrow/tagline (自由入力) がたまたま他のトークン文字列を含んでいた場合に
    # 後段の置換が前段の置換結果を上書きして黙って中身が壊れる (実測で再現済み)。
    tokens = {
        "__TITLE__": _escape_html(title),
        "__EYEBROW__": _escape_html(eyebrow),
        "__TAGLINE__": _escape_html(tagline),
        "__INITIAL_PATTERN_INDEX__": str(_VALID_PATTERNS[pattern]),
        "__INITIAL_PLUGINS_JS__": plugins_js,
    }
    token_pattern = re.compile("|".join(re.escape(k) for k in tokens))
    html = token_pattern.sub(lambda m: tokens[m.group(0)], html)

    out_path = out or os.path.join(_PROJECT_ROOT, "tmp_web_design", f"scaffold_{pattern}.html")
    try:
        out_dir = os.path.dirname(out_path)
        if out_dir:  # out_path がディレクトリ無しの相対ファイル名だと os.makedirs('') が例外を投げる
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as e:
        return f"Error: {e}"

    return f"scaffold_webgl: {out_path} (pattern={pattern}, plugins={','.join(plugin_list) or 'none'})"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AI駆動型ウェブデザインSOPの自動化パーツ")
    parser.add_argument("--phase2", action="store_true", help="背景アセット+UI構造イメージを生成")
    parser.add_argument("--background-prompt", default="", help="--phase2用の背景プロンプト")
    parser.add_argument("--ui-prompt", default="", help="--phase2用のUI構造イメージプロンプト")
    parser.add_argument("--image-size", default="landscape_16_9", help="--phase2の画像サイズ")

    parser.add_argument("--phase4-video", action="store_true", help="動画ループを生成")
    parser.add_argument("--image-url", default="", help="--phase4-video用の元画像URL")
    parser.add_argument("--prompt", default="", help="--phase4-video用のプロンプト")
    parser.add_argument("--duration", default="5", help="--phase4-videoの秒数")
    parser.add_argument("--tail-image-url", default="",
                        help="--phase4-video用の終点フレーム画像URL(シームレスループ狙い。未検証機能)")

    parser.add_argument("--scaffold-webgl", action="store_true", help="Option BのThree.js雛形を生成")
    parser.add_argument("--pattern", default="A", choices=sorted(_VALID_PATTERNS), help="初期アクティブパターン")
    parser.add_argument("--plugins", default="trail", help="初期チェック済みPostFXプラグイン(カンマ区切り)")
    parser.add_argument("--title", default="Your Name", help="ヒーローの見出し")
    parser.add_argument("--eyebrow", default="Option B — Live WebGL", help="ヒーローのアイキャッチ文言")
    parser.add_argument("--tagline", default="", help="ヒーローのタグライン")

    parser.add_argument("--out-dir", default="", help="--phase2/--phase4-videoの保存先ディレクトリ")
    parser.add_argument("--out", default="", help="--scaffold-webglの保存先パス")

    args = parser.parse_args()

    if args.phase2:
        print(run_phase2(args.background_prompt, args.ui_prompt, args.image_size, args.out_dir))
    elif args.phase4_video:
        print(run_phase4_video(args.image_url, args.prompt, args.duration, args.tail_image_url, args.out_dir))
    elif args.scaffold_webgl:
        print(scaffold_webgl(args.pattern, args.plugins, args.title, args.eyebrow, args.tagline, args.out))
    else:
        parser.print_help()
