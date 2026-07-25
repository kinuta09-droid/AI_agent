# -*- coding: utf-8 -*-
"""
search_dev_logs.py — Dev_Logs から Vault 移行前後の記録を掘り起こす。

背景:
  Vault が `D:\\マイドライブ\\Obsidian` (Google ドライブ) にある経緯が git 履歴から
  辿れない。初回コミット (2026-03-06) の時点で既に D: だったため、移行はそれ以前。
  一方 `.agent/workflows/create_dev_log.md` と `.claude/commands/visualize.md` には
  移行元とみられる `C:\\obsidian\\Obsidian` が取り残されている。

なぜ専用スクリプトが要るのか:
  `Dev_Logs` は lib_shared.EXCLUDE_DIRS に入っており、tool_vault_search /
  tool_vault_index の対象外。既存のVault検索では原理的にヒットしない。
  さらに chat_history.json は現行コードから一切参照されない遺物 (Antigravity 時代の
  会話履歴) で、移行当時のやりとりが残っているならここ。

読み取り専用。Vault へは一切書き込まない (--out はプロジェクト側にのみ出力)。

日付の扱い:
  Google ドライブは再ダウンロード時に mtime を現在時刻へ書き換える
  (tools/tool_obsidian.py:_date_of_file と同じ既知の罠)。よって
  「ファイル名の日付 > mtime」の優先順で日付を決め、どちらを使ったかを必ず表示する。

実行例:
  py scripts/search_dev_logs.py                        # 既定キーワードで全期間
  py scripts/search_dev_logs.py --until 2026-03-31     # 移行時期に絞る
  py scripts/search_dev_logs.py --timeline             # 最古のファイルを日付順に列挙
  py scripts/search_dev_logs.py -k 移行 -k ドライブ     # キーワード指定
  py scripts/search_dev_logs.py --out found.md         # 結果をファイルへ
"""
import argparse
import datetime
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 移行の話題が出そうな語。日本語/英語の両方を張る。
DEFAULT_KEYWORDS = [
    "マイドライブ", "Google ドライブ", "Googleドライブ", "Google Drive", "gdrive",
    "ドライブに", "ドライブへ", "ドライブ移行",
    "C:\\obsidian", "C:/obsidian",
    "移行", "移動しま", "移しま", "引っ越",
    "VAULT_ROOT", "WORK_DIR",
    "同期", "sync",
]

# 日次生成物のファイル名に埋まった日付 (tool_obsidian._DATE_IN_NAME と同じ方針)
_DATE_IN_NAME = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
_DATE_IN_NAME_COMPACT = re.compile(r'(20\d{2})(\d{2})(\d{2})')

TEXT_EXT = {".md", ".txt", ".log", ".csv"}
JSON_EXT = {".json", ".jsonl"}
MAX_BYTES = 20 * 1024 * 1024      # これを超えるファイルはスキップ
MAX_HITS_PER_FILE = 12


# ---------------------------------------------------------
# パス解決
# ---------------------------------------------------------

def resolve_vault_root(override: str = "") -> str:
    """lib/lib_shared.py の VAULT_ROOT を import せずに取り出す。

    lib_shared は GOOGLE_API_KEY 未設定で sys.exit(1) する上に google-genai /
    tavily を要求するため、import ではなくソースの正規表現で拾う。
    """
    if override:
        return override
    shared = os.path.join(_PROJECT_ROOT, "lib", "lib_shared.py")
    try:
        with open(shared, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        print(f"❌ lib/lib_shared.py を読めません: {e}")
        return ""
    m = re.search(r"^VAULT_ROOT\s*=\s*r?['\"](.+?)['\"]", src, re.MULTILINE)
    if not m:
        print("❌ VAULT_ROOT を抽出できません。--vault で指定してください。")
        return ""
    return m.group(1)


def date_of_file(path: str) -> tuple:
    """(date, 判定根拠) を返す。ファイル名の日付を優先し、無ければ mtime。"""
    name = os.path.basename(path)
    for pat in (_DATE_IN_NAME, _DATE_IN_NAME_COMPACT):
        m = pat.search(name)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "名前"
            except ValueError:
                pass
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(path)), "mtime"
    except OSError:
        return datetime.date.min, "不明"


# ---------------------------------------------------------
# 検索本体
# ---------------------------------------------------------

def _iter_json_strings(obj, breadcrumb=""):
    """任意構造の JSON から (パンくず, 文字列) を再帰的に取り出す。

    chat_history.json のスキーマが不明 (現行コードは読み書きしていない) なので、
    構造を仮定せず全ての文字列値を舐める。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_json_strings(v, f"{breadcrumb}.{k}" if breadcrumb else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_json_strings(v, f"{breadcrumb}[{i}]")
    elif isinstance(obj, str):
        yield breadcrumb or "(root)", obj


def _excerpt(text: str, pos: int, width: int) -> str:
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    s = text[start:end].replace("\n", " ⏎ ").replace("\r", "")
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def search_file(path: str, keywords: list, width: int) -> list:
    """1ファイルを検索し [(位置ラベル, 抜粋, ヒット語), ...] を返す。"""
    ext = os.path.splitext(path)[1].lower()
    hits = []
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [("(skip)", f"{MAX_BYTES // 1024 // 1024}MB 超のためスキップ", "")]
    except OSError:
        return []

    try:
        if ext in JSON_EXT:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            try:
                data = json.loads(raw)
            except ValueError:
                # JSONL または壊れた JSON → 行検索へ縮退
                data = None
            if data is not None:
                for crumb, s in _iter_json_strings(data):
                    for kw in keywords:
                        p = s.find(kw)
                        if p >= 0:
                            hits.append((crumb, _excerpt(s, p, width), kw))
                            break
                    if len(hits) >= MAX_HITS_PER_FILE:
                        break
                return hits
            lines = raw.splitlines()
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
    except OSError as e:
        return [("(error)", f"読み込み失敗: {e}", "")]

    for i, line in enumerate(lines, 1):
        for kw in keywords:
            p = line.find(kw)
            if p >= 0:
                hits.append((f"L{i}", _excerpt(line, p, width), kw))
                break
        if len(hits) >= MAX_HITS_PER_FILE:
            break
    return hits


def collect_files(root: str) -> list:
    """検索対象ファイルを集める。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '.obsidian', '.trash')]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in (TEXT_EXT | JSON_EXT):
                out.append(os.path.join(dirpath, fn))
    return out


# ---------------------------------------------------------
# 出力
# ---------------------------------------------------------

class Out:
    """標準出力と (指定時は) ファイルへ同時に書く。"""

    def __init__(self, path: str = ""):
        self.buf = []
        self.path = path

    def __call__(self, line: str = ""):
        print(line)
        if self.path:
            self.buf.append(line)

    def flush(self):
        if not self.path:
            return
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.buf) + "\n")
        print(f"\n💾 保存しました: {self.path}")


def run_timeline(out: Out, roots: list, limit: int):
    out("")
    out("=" * 64)
    out(f"タイムライン — 各ルートの最古 {limit} 件")
    out("=" * 64)
    out("移行ログがキーワードに引っかからない可能性に備えた素の一覧です。")
    for root in roots:
        files = collect_files(root)
        out(f"\n[{root}]  対象 {len(files)} ファイル")
        dated = sorted(((date_of_file(p), p) for p in files), key=lambda x: x[0][0])
        for (d, basis), p in dated[:limit]:
            out(f"  {d}  ({basis:5}) {os.path.relpath(p, root)}")


def run_search(out: Out, roots: list, keywords: list, since, until, width: int) -> int:
    out("")
    out("=" * 64)
    out("キーワード検索")
    out("=" * 64)
    out(f"語: {', '.join(keywords)}")
    if since or until:
        out(f"期間: {since or '(制限なし)'} 〜 {until or '(制限なし)'}")
    total = 0

    for root in roots:
        files = collect_files(root)
        out(f"\n{'─' * 60}\n■ {root}  ({len(files)} ファイル)\n{'─' * 60}")
        dated = sorted(((date_of_file(p), p) for p in files), key=lambda x: x[0][0])
        shown = 0
        for (d, basis), path in dated:
            if since and d < since:
                continue
            if until and d > until:
                continue
            hits = search_file(path, keywords, width)
            if not hits:
                continue
            shown += 1
            total += len(hits)
            out(f"\n  📄 {os.path.relpath(path, root)}")
            out(f"     日付 {d} ({basis}判定)")
            for label, text, kw in hits:
                out(f"     [{label}] «{kw}»  {text}")
        if shown == 0:
            out("  (ヒットなし)")
    return total


def report_old_vault(out: Out, old_root: str):
    """移行元フォルダが残っていれば、その最終更新を移行日の下限として報告する。"""
    out("")
    out("=" * 64)
    out("移行元フォルダの痕跡")
    out("=" * 64)
    if not os.path.isdir(old_root):
        out(f"  {old_root} は存在しません（削除済み／別名に変更済み）。")
        return
    newest = (datetime.date.min, "")
    count = 0
    for dirpath, dirnames, filenames in os.walk(old_root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '.obsidian', '.trash')]
        for fn in filenames:
            count += 1
            p = os.path.join(dirpath, fn)
            try:
                d = datetime.date.fromtimestamp(os.path.getmtime(p))
            except OSError:
                continue
            if d > newest[0]:
                newest = (d, os.path.relpath(p, old_root))
    out(f"  ✅ 残っています: {old_root}  ({count} ファイル)")
    if newest[1]:
        out(f"  最終更新: {newest[0]}  ({newest[1]})")
        out(f"  → 移行はこの日以降に行われた可能性が高い（mtime のため参考値）。")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dev_Logs から Vault 移行前後の記録を探す（読み取り専用）")
    ap.add_argument("--vault", default="", help="Vault ルート（既定: lib_shared の VAULT_ROOT）")
    ap.add_argument("--old-vault", default=r"C:\obsidian\Obsidian",
                    help="移行元とみられる旧 Vault（既定: C:\\obsidian\\Obsidian）")
    ap.add_argument("--extra", action="append", default=[], help="追加の検索ディレクトリ（複数可）")
    ap.add_argument("-k", "--keyword", action="append", default=[],
                    help="検索語（複数可。未指定なら既定セット）")
    ap.add_argument("--since", default="", help="この日以降のみ（YYYY-MM-DD）")
    ap.add_argument("--until", default="", help="この日以前のみ（YYYY-MM-DD）")
    ap.add_argument("--timeline", action="store_true", help="キーワード検索の代わりに最古ファイル一覧")
    ap.add_argument("--limit", type=int, default=25, help="--timeline の表示件数（既定 25）")
    ap.add_argument("--width", type=int, default=160, help="抜粋の文字数（既定 160）")
    ap.add_argument("--out", default="", help="結果の保存先（プロジェクト相対。Vault へは書きません）")
    args = ap.parse_args()

    vault = resolve_vault_root(args.vault)
    if not vault:
        return 2

    def _parse(s):
        return datetime.date.fromisoformat(s) if s else None
    try:
        since, until = _parse(args.since), _parse(args.until)
    except ValueError as e:
        print(f"❌ 日付の指定が不正です: {e}")
        return 2

    # 検索ルート: 現Vaultの Dev_Logs → 旧Vaultの Dev_Logs → 追加指定
    candidates = [
        os.path.join(vault, "Dev_Logs"),
        os.path.join(args.old_vault, "Dev_Logs"),
    ] + list(args.extra)
    roots, missing = [], []
    for c in candidates:
        (roots if os.path.isdir(c) else missing).append(c)

    out_path = ""
    if args.out:
        out_path = args.out if os.path.isabs(args.out) else os.path.join(_PROJECT_ROOT, args.out)
        if os.path.normcase(out_path).startswith(os.path.normcase(vault)):
            print("❌ --out に Vault 内のパスは指定できません（読み取り専用のため）。")
            return 2
    out = Out(out_path)

    out(f"Vault      : {vault}")
    out(f"旧 Vault   : {args.old_vault}")
    for m in missing:
        out(f"  ⚠️  見つかりません（スキップ）: {m}")
    if not roots:
        out("❌ 検索できるディレクトリがありません。--vault / --extra を確認してください。")
        out.flush()
        return 1

    if args.timeline:
        run_timeline(out, roots, args.limit)
    else:
        keywords = args.keyword or DEFAULT_KEYWORDS
        total = run_search(out, roots, keywords, since, until, args.width)
        out("")
        out("=" * 64)
        out(f"ヒット合計: {total} 件")
        out("=" * 64)
        if total == 0:
            out("  キーワードでは見つかりませんでした。次を試してください:")
            out("    py scripts/search_dev_logs.py --timeline")
            out("    py scripts/search_dev_logs.py -k Obsidian -k パス")

    report_old_vault(out, args.old_vault)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
