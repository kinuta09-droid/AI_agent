# -*- coding: utf-8 -*-
"""
diagnose_curate_note.py — /curate が「作成した」と表示したノートの実体を突き止める。

「Discord に `✅ Success: Created [[Knowledge_Base/...]]` と出たのに Obsidian に
ファイルが無い」という症状の切り分け用。読み取り専用（Vault を一切変更しない）。

やること:
  1. Curator が書き込む先のパスを解決して存在確認する
  2. Knowledge_Base の中身を新しい順に見る
  3. Vault 全体から「日付プレフィックス付きノート」を拾い、想定外フォルダを検出する
  4. Chatlog (50_Archive/Chatlog) の `Success: Created` 行と実ファイルを突合する
  5. Obsidian の [[wikilink]] が解決可能かを判定する

lib.lib_shared は import しない（GOOGLE_API_KEY 未設定だと sys.exit する上に
google-genai / tavily を要求するため）。Vault パスは依存の軽い lib.paths から取り、
それも読めなければ lib/paths.py のソースから正規表現で拾う。

実行:
  py scripts/diagnose_curate_note.py
  py scripts/diagnose_curate_note.py --vault "D:\\マイドライブ\\Obsidian" --days 7
"""
import argparse
import datetime
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# tool_curator.py と同じ構造（変更時はここも追従すること）
KB_SUBPATH = ("30_Main", "Knowledge_Base")
CHATLOG_SUBPATH = ("50_Archive", "Chatlog")
TEMPLATE_SUBPATH = ("99_Assets", "Templates", "Tpl_Permanent.md")

_DATE_PREFIX = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_')
_CREATED_LINE = re.compile(r'Success:\s*Created\s*\[\[(?P<link>[^\]]+)\]\](?:\s*\(Path:\s*(?P<path>[^)]+)\))?')

_findings = []


def _note(level: str, msg: str):
    """level: OK / WARN / NG"""
    _findings.append((level, msg))
    icon = {"OK": "✅", "WARN": "⚠️ ", "NG": "❌"}.get(level, "  ")
    print(f"  {icon} {msg}")


def resolve_vault_root(override: str = "") -> str:
    """本体と同じ VAULT_ROOT を、lib_shared を import せずに解決する。

    lib.paths は dotenv しか要求しないので import してよい（.env の
    OBSIDIAN_VAULT_ROOT による上書きもそのまま反映される）。dotenv すら
    入っていない環境向けに、ソースの DEFAULT_VAULT_ROOT へフォールバックする。
    """
    if override:
        return override
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    try:
        from lib.paths import VAULT_ROOT
        return VAULT_ROOT
    except Exception as e:
        print(f"⚠️  lib.paths を import できませんでした（{e}）。ソースから既定値を読みます。")
    paths_py = os.path.join(_PROJECT_ROOT, "lib", "paths.py")
    try:
        with open(paths_py, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        print(f"❌ lib/paths.py を読めません: {e}")
        return ""
    m = re.search(r"^DEFAULT_VAULT_ROOT\s*=\s*r?['\"](.+?)['\"]", src, re.MULTILINE)
    if not m:
        print("❌ lib/paths.py から DEFAULT_VAULT_ROOT を抽出できませんでした。--vault で指定してください。")
        return ""
    return m.group(1)


def section(title: str):
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


# ---------------------------------------------------------
# 1. パス解決
# ---------------------------------------------------------

def check_paths(vault: str) -> dict:
    section("1. パス解決 — Curator はどこへ書くのか")
    kb_dir = os.path.join(vault, *KB_SUBPATH)
    paths = {
        "VAULT_ROOT":    vault,
        "30_Main":       os.path.join(vault, "30_Main"),
        "Knowledge_Base": kb_dir,
        "Template":      os.path.join(vault, *TEMPLATE_SUBPATH),
        "Chatlog":       os.path.join(vault, *CHATLOG_SUBPATH),
    }
    for name, p in paths.items():
        if os.path.exists(p):
            _note("OK", f"{name:<15} {p}")
        else:
            lvl = "NG" if name in ("VAULT_ROOT", "30_Main") else "WARN"
            _note(lvl, f"{name:<15} {p}  ← 存在しない")

    if os.path.exists(vault) and not os.path.exists(kb_dir):
        _note("NG", "Knowledge_Base が無い。Curator は os.makedirs で自動生成するため、"
                    "書き込みは成功するが『ユーザーが見ている KB フォルダとは別物』になる。")

    # 紛らわしい同名フォルダが Vault 内の別階層に無いか
    if os.path.exists(vault):
        dupes = []
        for root, dirs, _ in os.walk(vault):
            if any(part in ('.git', '.obsidian', '.trash') for part in root.split(os.sep)):
                dirs[:] = []
                continue
            for d in dirs:
                if re.fullmatch(r'Knowledge[_ ]?Base', d, re.IGNORECASE):
                    full = os.path.join(root, d)
                    if os.path.normcase(full) != os.path.normcase(kb_dir):
                        dupes.append(os.path.relpath(full, vault))
        if dupes:
            _note("NG", "Knowledge_Base らしきフォルダが他にもある → 見ている場所と書かれる場所の"
                        f"食い違いが濃厚: {dupes}")
    return paths


# ---------------------------------------------------------
# 2. Knowledge_Base の中身
# ---------------------------------------------------------

def list_kb(kb_dir: str, days: int):
    section(f"2. Knowledge_Base の中身（新しい順・直近 {days} 日を強調）")
    if not os.path.isdir(kb_dir):
        _note("NG", "Knowledge_Base が存在しないため一覧できません。")
        return
    entries = []
    for f in os.listdir(kb_dir):
        p = os.path.join(kb_dir, f)
        if f.endswith(".md") and os.path.isfile(p):
            entries.append((os.path.getmtime(p), f))
    entries.sort(reverse=True)
    print(f"  .md 総数: {len(entries)}")
    if not entries:
        _note("NG", "Knowledge_Base が空。1件も書き込めていない可能性がある。")
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    recent = 0
    for mtime, name in entries[:20]:
        dt = datetime.datetime.fromtimestamp(mtime)
        mark = "🆕" if dt >= cutoff else "  "
        if dt >= cutoff:
            recent += 1
        print(f"  {mark} {dt:%Y-%m-%d %H:%M}  {name}")
    if recent == 0:
        _note("NG", f"直近 {days} 日に更新されたノートが 0 件。書き込み自体が届いていない。")
    else:
        _note("OK", f"直近 {days} 日のノート {recent} 件を Knowledge_Base 内で確認。")


# ---------------------------------------------------------
# 3. Vault 全体スキャン（想定外フォルダの検出）
# ---------------------------------------------------------

def scan_vault(vault: str, days: int, kb_dir: str):
    section(f"3. Vault 全体スキャン — 直近 {days} 日の日付プレフィックス付きノート")
    if not os.path.isdir(vault):
        _note("NG", "Vault が存在しないためスキャンできません。")
        return
    cutoff = (datetime.date.today() - datetime.timedelta(days=days))
    by_dir = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in ('.git', '.obsidian', '.trash', 'Trash')]
        for f in files:
            m = _DATE_PREFIX.match(f)
            if not (f.endswith(".md") and m):
                continue
            try:
                fdate = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if fdate < cutoff:
                continue
            rel = os.path.relpath(root, vault)
            by_dir.setdefault(rel, []).append(f)

    if not by_dir:
        _note("NG", f"直近 {days} 日の日付付きノートが Vault のどこにも無い。"
                    "書き込みが別ドライブ／別 Vault へ行っている可能性がある。")
        return
    kb_rel = os.path.relpath(kb_dir, vault)
    for rel in sorted(by_dir):
        files = sorted(by_dir[rel])
        tag = "  ← Curator の書き込み先" if os.path.normcase(rel) == os.path.normcase(kb_rel) else ""
        print(f"  [{rel}] {len(files)} 件{tag}")
        for f in files[:5]:
            print(f"      - {f}")
        if len(files) > 5:
            print(f"      ... 他 {len(files) - 5} 件")


# ---------------------------------------------------------
# 4. Chatlog と実ファイルの突合
# ---------------------------------------------------------

def cross_check_chatlog(vault: str, days: int):
    section(f"4. Chatlog の `Success: Created` と実ファイルの突合（直近 {days} 日）")
    chatlog_dir = os.path.join(vault, *CHATLOG_SUBPATH)
    if not os.path.isdir(chatlog_dir):
        _note("WARN", f"Chatlog が無いためスキップ: {chatlog_dir}")
        return

    today = datetime.date.today()
    claims = []
    for i in range(days + 1):
        day = today - datetime.timedelta(days=i)
        p = os.path.join(chatlog_dir, f"{day:%Y-%m-%d}.md")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _CREATED_LINE.search(line)
                    if m:
                        claims.append((day, m.group("link"), (m.group("path") or "").strip()))
        except OSError as e:
            _note("WARN", f"{p} を読めません: {e}")

    if not claims:
        _note("WARN", "Chatlog に `Success: Created` の記録が無い。"
                      "→ 表示された成功メッセージは Curator 由来ではないか、"
                      "_log_to_daily_note が動いていない。")
        return

    print(f"  『作成した』と主張している記録: {len(claims)} 件\n")
    for day, link, abs_path in claims:
        print(f"  ── {day} : [[{link}]]")
        basename = os.path.basename(link.split("|")[0].strip())

        # (a) メッセージ中の絶対パス（Chatlog は 200 文字で切られるため欠けることがある）
        if abs_path:
            if os.path.exists(abs_path):
                _note("OK", f"実ファイルあり: {abs_path}")
            else:
                _note("NG", f"実ファイル無し: {abs_path}  ← 書いた直後に消えた/移動された疑い")
        else:
            print("      (Path: ...) はログ 200 文字制限で欠落")

        # (b) wikilink を Vault ルート基準で解決できるか = Obsidian が辿れるか
        cand = os.path.join(vault, *link.split("/"))
        if not cand.endswith(".md"):
            cand += ".md"
        if os.path.exists(cand):
            _note("OK", f"wikilink は Vault ルート基準で解決可能: {link}")
        else:
            _note("NG", f"wikilink `[[{link}]]` は Vault ルートから辿れない "
                        "→ Obsidian 上では未解決リンク（クリックすると『新規作成』になる）")

        # (c) ファイル名だけで Vault 全体を捜索
        hits = []
        for root, dirs, files in os.walk(vault):
            dirs[:] = [d for d in dirs if d not in ('.git', '.obsidian', '.trash', 'Trash')]
            if basename in files:
                hits.append(os.path.relpath(os.path.join(root, basename), vault))
        if hits:
            _note("OK", f"実体はここにある: {hits}")
        else:
            _note("NG", f"`{basename}` は Vault のどこにも存在しない ← 本当に作られていない")
        print()


# ---------------------------------------------------------
# 5. 判定
# ---------------------------------------------------------

def verdict():
    section("5. 判定")
    ng = [m for lvl, m in _findings if lvl == "NG"]
    warn = [m for lvl, m in _findings if lvl == "WARN"]
    if not ng:
        print("  ✅ 致命的な不整合は検出されませんでした。")
        print("     ファイルは存在します。Obsidian 側の表示（Google ドライブ同期待ち／")
        print("     ファイルエクスプローラの再読込）を疑ってください。")
    else:
        print(f"  ❌ 要調査 {len(ng)} 件:")
        for m in ng:
            print(f"     - {m}")
    if warn:
        print(f"\n  ⚠️  参考 {len(warn)} 件:")
        for m in warn:
            print(f"     - {m}")
    return 1 if ng else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="/curate のノート作成を診断する（読み取り専用）")
    ap.add_argument("--vault", default="", help="Vault ルート（既定: lib_shared の VAULT_ROOT）")
    ap.add_argument("--days", type=int, default=3, help="遡る日数（既定 3）")
    args = ap.parse_args()

    vault = resolve_vault_root(args.vault)
    if not vault:
        return 2
    print(f"Vault: {vault}")
    print(f"対象期間: 直近 {args.days} 日")

    paths = check_paths(vault)
    list_kb(paths["Knowledge_Base"], args.days)
    scan_vault(vault, args.days, paths["Knowledge_Base"])
    cross_check_chatlog(vault, args.days)
    return verdict()


if __name__ == "__main__":
    sys.exit(main())
