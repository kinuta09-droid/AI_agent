# -*- coding: utf-8 -*-
"""
tool_shorts_editor.py — SNS用ショート動画自動編集 (Path A: 既存動画からハイライト抽出)

長尺動画 → 文字起こし(OpenAI Whisper API) → ハイライト区間選定(Gemini) → トリミング →
縦型9:16変換+字幕焼き込み+BGM合成(ffmpeg) → Vault保存、を一括で行う。

前提: システムPATHに ffmpeg/ffprobe が導入済み (新規pip依存なし)。
テキスト/画像から新規動画を生成するPath Bは tools/tool_shorts_script.py (未実装) を参照。
"""
import os
import re
import shutil
import subprocess
import logging
import datetime
import requests

from lib.paths import VAULT_ROOT
from lib.lib_shared import client, MODEL_NAME_HEAVY, safe_json_loads

logger = logging.getLogger(__name__)

TMP_DIR = "tmp_shorts"
SHORTS_VAULT_DIR = os.path.join(VAULT_ROOT, "30_Main", "Assets", "Shorts")

# OpenAI Whisper API のファイルサイズ上限
WHISPER_MAX_BYTES = 25 * 1024 * 1024


def _run_ffmpeg(args: list[str], cwd: str = None, timeout: int = 600) -> str:
    """ffmpeg/ffprobe を subprocess で実行する。tool_bitnet._run_bitnet と同じ encoding/cwd 規約。"""
    exe = args[0]
    if shutil.which(exe) is None:
        raise FileNotFoundError(f"{exe} not found in PATH")
    result = subprocess.run(
        args,
        capture_output=True,
        encoding='utf-8',
        errors='replace',
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.warning(f"{exe} stderr: {result.stderr[-300:]}")
        raise RuntimeError(f"{exe} exited with code {result.returncode}: {result.stderr[-300:]}")
    return result.stdout


def _ffprobe_duration(path: str) -> float | None:
    """動画の長さ(秒)を返す。取得できなければ None(ベストエフォート、無くても処理は続行できる)。"""
    try:
        out = _run_ffmpeg([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
        ])
        return float(out.strip())
    except Exception as e:
        logger.warning(f"ffprobe duration failed: {e}")
        return None


def _extract_audio_for_whisper(video_path: str, out_dir: str) -> str:
    """Whisper API の25MB上限に収まるよう、モノラル圧縮音声を抽出する。"""
    out_path = os.path.join(out_dir, "audio.m4a")
    for bitrate in ("64k", "32k"):
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", bitrate,
            out_path,
        ])
        if os.path.getsize(out_path) <= WHISPER_MAX_BYTES:
            return out_path
    raise RuntimeError(
        f"音声圧縮後も{WHISPER_MAX_BYTES // 1_048_576}MBを超えています"
        "(長尺動画の分割文字起こしは現状未対応)"
    )


def _transcribe_with_timestamps(audio_path: str) -> dict:
    """OpenAI Whisper API (verbose_json) でセグメント別タイムスタンプ付き文字起こしを取得する。

    Returns: {"text": str, "segments": [{"start": float, "end": float, "text": str}, ...]}
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": "whisper-1", "language": "ja", "response_format": "verbose_json"}
    with open(audio_path, "rb") as f:
        files = {"file": f}
        res = requests.post(url, headers=headers, files=files, data=data, timeout=300)
    res.raise_for_status()
    body = res.json()
    segments = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": (s.get("text") or "").strip()}
        for s in body.get("segments", [])
    ]
    return {"text": body.get("text", ""), "segments": segments}


def _select_highlight_gemini(
    segments: list[dict], target_seconds: int, topic_hint: str | None, total_duration: float | None = None
) -> dict:
    """target_seconds に収まる最良区間を Gemini に選ばせる。

    パース失敗・異常値(start>=end、動画長超過)は例外にせず先頭 target_seconds 秒へ
    フォールバックし、reason に "fallback" と明記する(捏造しない設計)。
    Returns: {"start": float, "end": float, "reason": str}
    """
    fallback_end = float(target_seconds)
    if total_duration:
        fallback_end = min(fallback_end, total_duration)
    fallback = {"start": 0.0, "end": fallback_end, "reason": "fallback: 選定できなかったため先頭を使用"}

    if not segments:
        return fallback

    transcript_lines = "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments
    )
    hint_line = f"\n注目してほしい観点: {topic_hint}" if topic_hint else ""
    prompt = f"""以下は動画の文字起こし(タイムスタンプ付き)です。SNSショート動画として
最も引きが強い連続区間を1つ選んでください。区間の長さは{target_seconds}秒程度に収めること。{hint_line}

【文字起こし】
{transcript_lines}

【出力形式】厳密に以下のキーだけを持つJSON。
{{"start": 開始秒(数値), "end": 終了秒(数値), "reason": "選んだ理由を1文で"}}"""

    try:
        resp = client.models.generate_content(model=MODEL_NAME_HEAVY, contents=prompt)
        picked = safe_json_loads(resp.text or "")
    except Exception as e:
        logger.warning(f"highlight selection Gemini call failed: {e}")
        return fallback

    try:
        start, end = float(picked["start"]), float(picked["end"])
    except (KeyError, TypeError, ValueError):
        return fallback

    if start < 0 or end <= start:
        return fallback
    if total_duration and end > total_duration:
        end = total_duration
    if end - start < 1.0:
        return fallback

    return {"start": start, "end": end, "reason": picked.get("reason", "")}


def _trim_video(video_path: str, start: float, end: float, out_path: str) -> None:
    """正確シーク(-i の後に -ss/-to)でハイライト区間を切り出す(再エンコード前提のため精度優先)。"""
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-c:v", "libx264", "-c:a", "aac",
        out_path,
    ])


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _shift_srt(segments: list[dict], highlight_start: float, highlight_end: float, out_srt_path: str) -> int:
    """ハイライト区間に重なるセグメントだけ、区間開始基準に時刻シフトしてSRT書き出しする。

    Returns: 書き出した字幕行数(0なら字幕焼き込みステップをスキップする合図)。
    """
    lines = []
    idx = 1
    for seg in segments:
        s, e = seg["start"], seg["end"]
        if e <= highlight_start or s >= highlight_end:
            continue
        rel_start = max(s, highlight_start) - highlight_start
        rel_end = min(e, highlight_end) - highlight_start
        if rel_end - rel_start < 0.05:
            continue
        lines.append(str(idx))
        lines.append(f"{_srt_timestamp(rel_start)} --> {_srt_timestamp(rel_end)}")
        lines.append(seg["text"])
        lines.append("")
        idx += 1

    with open(out_srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return idx - 1


def _build_vertical_filtergraph(vertical_mode: str, width: int = 1080, height: int = 1920) -> str:
    """pad(ぼかし背景、コンテンツ非破壊)/crop(中央クロップ)の映像チェーンを返す(純粋関数)。

    入力ラベル [0:v] を受け取り、出力ラベル [vbase] で終わる filter_complex 断片。
    """
    if vertical_mode == "crop":
        return f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}[vbase]"
    # pad: ぼかし背景 + 中央に等比縮小した本編を重ねる
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=20[bgblur];"
        f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fgscaled];"
        f"[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2[vbase]"
    )


def _burn_subtitles_and_bgm(
    trimmed_video_path: str,
    srt_path: str | None,
    bgm_path: str | None,
    vertical_mode: str,
    out_path: str,
    width: int = 1080,
    height: int = 1920,
) -> None:
    """縦型変換+字幕焼き込み+BGMミックスを1回のffmpeg呼び出しで実行する。

    Windowsパスのコロン問題対策: cwd をトリム済み動画のあるディレクトリに設定し、
    subtitles= フィルタにはファイル名のみ(自前採番の英数字名)を渡す。
    """
    work_dir = os.path.dirname(trimmed_video_path)
    video_name = os.path.basename(trimmed_video_path)

    filter_complex = _build_vertical_filtergraph(vertical_mode, width, height)
    video_label = "[vbase]"
    has_subtitles = bool(srt_path and os.path.exists(srt_path) and os.path.getsize(srt_path) > 0)
    if has_subtitles:
        srt_name = os.path.basename(srt_path)
        filter_complex += f";{video_label}subtitles=filename={srt_name}[vout]"
        video_label = "[vout]"

    args = ["ffmpeg", "-y", "-i", video_name]
    map_args = ["-map", video_label if has_subtitles else "[vbase]", "-map", "0:a?"]

    if bgm_path:
        bgm_name = os.path.basename(bgm_path)
        args += ["-i", bgm_name]
        filter_complex += (
            ";[1:a]volume=0.15,aloop=loop=-1:size=2e9[bgmloop]"
            ";[0:a][bgmloop]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        map_args = ["-map", video_label, "-map", "[aout]"]

    args += ["-filter_complex", filter_complex] + map_args
    args += [
        "-c:v", "libx264", "-crf", "28", "-maxrate", "2M", "-bufsize", "4M",
        "-c:a", "aac", "-shortest", os.path.basename(out_path),
    ]

    _run_ffmpeg(args, cwd=work_dir)


def _resolve_unique_path(save_dir: str, filename: str) -> tuple[str, str]:
    """tool_obsidian._resolve_unique_path 相当(同名衝突時はタイムスタンプ接尾辞で退避)。

    ponytail: あちらは拡張子を .md 固定で書き換えるため mp4 に使えず、ここに複製する。
    """
    save_path = os.path.join(save_dir, filename)
    if os.path.exists(save_path):
        timestamp = datetime.datetime.now().strftime('%H%M%S')
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{timestamp}{ext}"
        save_path = os.path.join(save_dir, filename)
    return filename, save_path


def _save_to_vault(final_video_path: str, meta: dict) -> tuple[str, str]:
    """VAULT_ROOT/30_Main/Assets/Shorts/ へ動画+メタデータノートを保存する。"""
    os.makedirs(SHORTS_VAULT_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    slug = re.sub(r'[\\/:*?"<>|]', '_', (meta.get("topic_hint") or "short").strip())[:30] or "short"
    base_name = f"{date_str}_{slug}"

    video_filename, video_path = _resolve_unique_path(SHORTS_VAULT_DIR, f"{base_name}.mp4")
    shutil.copy2(final_video_path, video_path)

    note_filename, note_path = _resolve_unique_path(SHORTS_VAULT_DIR, f"{base_name}.md")
    note_content = (
        f"# {base_name}\n\n"
        f"- 元動画: {meta.get('source_video')}\n"
        f"- ハイライト区間: {meta['start']:.1f}s - {meta['end']:.1f}s ({meta.get('reason', '')})\n"
        f"- 尺: {meta['end'] - meta['start']:.1f}秒\n"
        f"- 縦型変換: {meta.get('vertical_mode')}\n"
        f"- BGM: {'適用 (' + meta['bgm_path'] + ')' if meta.get('bgm_path') else '未適用'}\n"
        f"- 字幕行数: {meta.get('subtitle_lines', 0)}\n"
        f"- 動画: [[{video_filename}]]\n"
    )
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note_content)

    return video_path, note_path


def tool_shorts_from_video(
    video_path: str,
    target_seconds: int = 30,
    topic_hint: str | None = None,
    bgm_path: str | None = None,
    vertical_mode: str = "pad",
) -> str:
    """既存の長尺動画からSNSショート動画(縦型9:16+字幕+BGM)を自動生成する。

    失敗時は "Error: ..." を返す(例外を外に投げない、lib.bot_errors.is_failure_result 契約)。
    BGMは自動生成/自動取得しない(著作権上不可能)。bgm_path を渡した場合のみミックスする。
    """
    if not os.path.exists(video_path):
        return f"Error: 動画ファイルが見つかりません: {video_path}"
    if vertical_mode not in ("pad", "crop"):
        vertical_mode = "pad"

    run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    work_dir = os.path.join(TMP_DIR, run_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        total_duration = _ffprobe_duration(video_path)

        audio_path = _extract_audio_for_whisper(video_path, work_dir)
        transcript = _transcribe_with_timestamps(audio_path)

        highlight = _select_highlight_gemini(
            transcript["segments"], target_seconds, topic_hint, total_duration
        )

        trimmed_path = os.path.join(work_dir, "highlight.mp4")
        _trim_video(video_path, highlight["start"], highlight["end"], trimmed_path)

        srt_path = os.path.join(work_dir, "highlight.srt")
        subtitle_lines = _shift_srt(
            transcript["segments"], highlight["start"], highlight["end"], srt_path
        )

        work_bgm_path = None
        if bgm_path and os.path.exists(bgm_path):
            bgm_ext = os.path.splitext(bgm_path)[1] or ".mp3"
            work_bgm_path = os.path.join(work_dir, f"bgm{bgm_ext}")
            shutil.copy2(bgm_path, work_bgm_path)

        final_path = os.path.join(work_dir, "final.mp4")
        _burn_subtitles_and_bgm(
            trimmed_path,
            srt_path if subtitle_lines > 0 else None,
            work_bgm_path,
            vertical_mode,
            final_path,
        )

        meta = {
            "source_video": video_path,
            "start": highlight["start"],
            "end": highlight["end"],
            "reason": highlight.get("reason", ""),
            "vertical_mode": vertical_mode,
            "bgm_path": bgm_path if work_bgm_path else None,
            "subtitle_lines": subtitle_lines,
            "topic_hint": topic_hint,
        }
        video_out, note_out = _save_to_vault(final_path, meta)

        applied = [f"縦型変換({vertical_mode})"]
        applied.append(f"字幕焼き込み({subtitle_lines}行)" if subtitle_lines > 0 else "字幕なし(文字起こしセグメント0件)")
        applied.append("BGM適用" if work_bgm_path else "BGMなし(bgm_path未指定)")

        return (
            f"ショート動画を生成しました: {video_out}\n"
            f"ハイライト区間: {highlight['start']:.1f}s - {highlight['end']:.1f}s ({highlight.get('reason', '')})\n"
            f"適用内容: {' / '.join(applied)}\n"
            f"メタデータ: {note_out}"
        )
    except Exception as e:
        logger.error(f"[tool_shorts_from_video] {e}")
        return f"Error: {e}"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
