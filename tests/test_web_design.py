# -*- coding: utf-8 -*-
"""
tool_web_design のテスト。

scaffold_webgl は外部API不要・決定的出力なので常時実行する。
run_phase2 / run_phase4_video は fal.ai への実課金呼び出しを伴うため、
既定では **SKIP して正常終了する**。実行するには:
    set ARMS_LIVE_TESTS=1 && py tests/test_web_design.py
"""
import os
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_web_design import scaffold_webgl
from lib.bot_errors import is_failure_result

_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _FAIL
    if ok:
        print(f"✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


def test_scaffold_static() -> None:
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "out.html")
        result = scaffold_webgl(pattern="B", plugins="trail,grain", title="Test Client", out=out_path)
        check("scaffold_webgl が失敗接頭辞を返さない", not is_failure_result(result), result)
        check("出力ファイルが存在する", os.path.exists(out_path))

        html = open(out_path, encoding="utf-8").read()
        for token in ("buildPatternA", "buildPatternB", "buildPatternC", "POSTFX_REGISTRY",
                      "AfterimagePass", "UnrealBloomPass", "FilmPass", "OutputPass",
                      "prefers-reduced-motion", "no-webgl", "nowebgl", "URLSearchParams", "Test Client"):
            check(f"生成HTMLに '{token}' を含む", token in html)
        check("未置換トークンが残っていない", re.search(r"__[A-Z_]+__", html) is None, "")

    check("不正パターン名はError:を返す", scaffold_webgl(pattern="Z").startswith("Error:"))
    check("不正プラグイン名はError:を返す", scaffold_webgl(plugins="glow").startswith("Error:"))


def test_phase2_and_video_live() -> None:
    from tools.tool_web_design import run_phase2, run_phase4_video

    result = run_phase2()
    check("run_phase2 が失敗接頭辞を返さない", not is_failure_result(result), result)

    image_url = ""
    m = re.search(r"background: (\S+)", result)
    if m:
        image_url = m.group(1)
    if image_url:
        video_result = run_phase4_video(image_url)
        check("run_phase4_video が失敗接頭辞を返さない", not is_failure_result(video_result), video_result)
    else:
        check("run_phase4_video 用の画像URLを取得できた", False, result)


def main() -> int:
    test_scaffold_static()

    if os.getenv("ARMS_LIVE_TESTS") == "1":
        test_phase2_and_video_live()
    else:
        print("⏭️ SKIP: run_phase2/run_phase4_video は fal.ai 課金があるため既定では実行しません "
              "(ARMS_LIVE_TESTS=1 で有効化)")

    print("\n" + ("❌ FAILURES あり" if _FAIL else "✅ すべて成功"))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
