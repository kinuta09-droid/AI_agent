# -*- coding: utf-8 -*-
"""
test_shorts_editor_practical.py — 実ffmpeg統合テスト

ffmpeg不在の環境(CI等)ではSKIPして正常終了する(tests/test_bitnet_practical.py と同方式)。
lavfi でその場生成したダミー動画を使い、Whisper/Gemini(課金APIの実呼び出し)には触れず、
_trim_video → _burn_subtitles_and_bgm (pad/crop両方) が実際にffmpegを通ることだけを確認する。

字幕焼き込み(subtitlesフィルタ)は libass 同梱ビルドでないと使えない。開発機のffmpeg
(Gyan.FFmpeg full build)は同梱だが、GitHub Actions windows-latest 等の環境が同じ構成とは
限らないため、この1項目だけ `-filters` で実在を確認し、無ければ SKIP して FAIL にしない
(known-bug-patterns.md B: 「動く」ことの確認を実行環境ではなく前提の有無で行う)。

実行:
  py tests/test_shorts_editor_practical.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    print("⏭️ SKIP: ffmpeg/ffprobe が見つかりません")
    sys.exit(0)

# libx264 はこのパイプライン全体の前提(トリミング/縦型変換の出力エンコード)。
# 無ければ何も検証できないため、個別チェックにせずファイル全体をSKIPする。
_encoders = subprocess.run(
    ["ffmpeg", "-hide_banner", "-encoders"],
    capture_output=True, encoding="utf-8", errors="replace", timeout=30,
).stdout
if "libx264" not in _encoders:
    print("⏭️ SKIP: このffmpegビルドはlibx264エンコーダを含みません")
    sys.exit(0)

from tools import tool_shorts_editor as se

_FAIL = False


def check(name, ok, detail=""):
    global _FAIL
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _FAIL = True


def _make_dummy_video(path, duration=3):
    """lavfi でカラーバー+サイン波のダミー動画をその場生成する(アセットをコミットしない)。"""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=640x360:duration={duration}:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", path,
        ],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _ffmpeg_has_filter(name):
    """ffmpeg -filters の一覧に name が実在するか(libass等、任意コンポーネント同梱の確認)。"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout
        return any(line.split()[1] == name for line in out.splitlines() if len(line.split()) > 1)
    except Exception:
        return False


def _probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    ).stdout.strip()
    return out


with tempfile.TemporaryDirectory() as work_dir:
    dummy_path = os.path.join(work_dir, "dummy.mp4")
    _make_dummy_video(dummy_path, duration=3)
    check("dummy video created", os.path.exists(dummy_path) and os.path.getsize(dummy_path) > 0)

    duration = se._ffprobe_duration(dummy_path)
    check("ffprobe duration detected", duration is not None and 2.0 < duration < 4.0, f"duration={duration}")

    trimmed_path = os.path.join(work_dir, "trimmed.mp4")
    try:
        se._trim_video(dummy_path, 0.0, 2.0, trimmed_path)
        check("trim succeeded", os.path.exists(trimmed_path))
    except Exception as e:
        check("trim succeeded", False, str(e))

    # SRT無し・BGM無しでの縦型変換(pad/crop両方)を確認
    for mode in ("pad", "crop"):
        out_path = os.path.join(work_dir, f"final_{mode}.mp4")
        try:
            se._burn_subtitles_and_bgm(trimmed_path, None, None, mode, out_path)
            size_str = _probe_size(out_path)
            check(f"vertical convert ({mode}) produced 1080x1920", size_str == "1080x1920", size_str)
        except Exception as e:
            check(f"vertical convert ({mode}) succeeded", False, str(e))

    # 字幕焼き込みあり(pad)も一応通す。libass同梱ビルドでない環境ではSKIP(FAILにしない)。
    if not _ffmpeg_has_filter("subtitles"):
        print("⏭️ SKIP: subtitle burn-in (このffmpegビルドはlibass/subtitlesフィルタを含まない)")
    else:
        srt_path = os.path.join(work_dir, "sub.srt")
        se._shift_srt([{"start": 0.0, "end": 2.0, "text": "テスト字幕"}], 0.0, 2.0, srt_path)
        out_sub_path = os.path.join(work_dir, "final_sub.mp4")
        try:
            se._burn_subtitles_and_bgm(trimmed_path, srt_path, None, "pad", out_sub_path)
            check("subtitle burn-in succeeded", os.path.exists(out_sub_path))
        except Exception as e:
            check("subtitle burn-in succeeded", False, str(e))

sys.exit(1 if _FAIL else 0)
