# -*- coding: utf-8 -*-
import os
import re
import urllib.parse
from lib.lib_shared import VAULT_ROOT

# ---------------------------------------------------------
# Wikilink の見出しアンカー ([[file#heading]])
# ---------------------------------------------------------
# 検索ツールが出典として見出し付きリンクを返すようになったため (Phase A2)、
# 「リンクを組み立てる側」と「リンクを解決する側」の規則をここに一元化する。
# 片方だけを直すと、リンクは出るのにボタンが無反応という無言の劣化になる。

# Obsidian の [[file#heading]] は見出しテキストの完全一致を要求する。
# これらの文字が見出しに含まれるとリンク構文そのものが壊れるため除去する。
_HEADING_UNSAFE_RE = re.compile(r"[#|\[\]^]")


def sanitize_heading(heading) -> str:
    """見出しを wikilink のアンカーとして安全な形に整える。

    リンク構文を壊す文字 (#, |, [, ], ^) と改行を除去し、空白を畳む。
    None / 空 / 除去後に空になった場合は "" を返す (＝アンカーなし扱い)。
    """
    if not heading:
        return ""
    cleaned = _HEADING_UNSAFE_RE.sub("", str(heading))
    return " ".join(cleaned.split())


def format_wikilink(filename: str, heading=None) -> str:
    """[[ファイル名]] または [[ファイル名#見出し]] を組み立てる。

    heading が None / 空 / サニタイズ後に空なら見出しなしのリンクを返す
    (## を持たないノートでも壊れたリンクを出さない)。

    既知の限界: filename 側はサニタイズしない。Vault には
    "... [辛口診断].md" のように角括弧を含む実ファイルがあり、名前を変えると
    リンクがどのノートも指さなくなるため。[[ ]] は最初の ]] で閉じるので
    単独の ] は問題にならないが、ファイル名に ]] を含むノートは作らないこと。
    """
    anchor = sanitize_heading(heading)
    return f"[[{filename}#{anchor}]]" if anchor else f"[[{filename}]]"


def split_heading_anchor(link_text: str) -> tuple[str, str]:
    """"file#heading" を (file, heading) に分解する。アンカーが無ければ heading は ""。

    ファイル名自体に # は使えない (sanitize_heading と同じ前提) ので、
    最初の # 以降をアンカーとみなす。
    """
    text = (link_text or "").strip()
    if "#" not in text:
        return text, ""
    file_part, _, heading = text.partition("#")
    return file_part.strip(), heading.strip()


def get_obsidian_uri(file_path: str) -> str:
    """
    絶対パスを受け取り、ObsidianのURIスキーム（obsidian://open...）を返す。
    ボルト名は 'Obsidian' 固定。
    日本語ファイル名やスペースも適切にエンコードする。
    """
    if not file_path:
        return ""

    # パスの正規化
    abs_path = os.path.abspath(file_path)
    vault_root = os.path.abspath(VAULT_ROOT)

    # ボルト外のファイルは変換不可
    # Windowsは大文字小文字を区別しないのでlowerで比較
    if not abs_path.lower().startswith(vault_root.lower()):
        return ""

    # ボルトルートからの相対パスを取得
    rel_path = os.path.relpath(abs_path, vault_root)

    # Windowsのバックスラッシュをスラッシュに置換
    rel_path = rel_path.replace('\\', '/')

    # URLエンコード
    # Note: ObsidianのURIスキームでは、パス区切り文字'/'はエンコードせずそのまま残すのが一般的
    encoded_path = urllib.parse.quote(rel_path, safe='/')

    return f"obsidian://open?vault=Obsidian&file={encoded_path}"


def get_obsidian_uri_from_link(link_text: str) -> str:
    """
    [[Link]] 形式のテキストから Obsidian URI を生成する。
    1. 絶対パスならそのまま get_obsidian_uri
    2. 相対パスなら VAULT_ROOT 起点で存在確認
    3. ファイル名のみなら再帰検索して最初に見つかったものを採用

    見出しアンカー ([[file#heading]]) は**ファイル解決の前に切り落とす**。
    obsidian://open の file パラメータは見出しを解釈しないため、付けたままだと
    "foo.md#見出し.md" を探して必ず空文字を返す = Discord の
    「Obsidianで開く」ボタンが無言で消える (Phase A2 で検索結果が見出し付き
    リンクを返すようになったため実際に起きる)。ノートを開くところまでは
    保証し、見出しへのジャンプは諦める。
    """
    link_text, _heading = split_heading_anchor(link_text)
    if not link_text:
        return ""

    # 拡張子がない場合は .md を付与してみる（Obsidianの挙動に合わせる）
    # ただし、ディレクトリリンクの場合は除く
    base_candidate = link_text
    if not os.path.splitext(base_candidate)[1]:
        base_candidate += ".md"

    # 1. 絶対パス or VAULT_ROOT 起点の相対パス
    candidates = [
        link_text,
        base_candidate,
        os.path.join(VAULT_ROOT, link_text),
        os.path.join(VAULT_ROOT, base_candidate),
    ]

    for c in candidates:
        if os.path.exists(c):
             return get_obsidian_uri(c)

    # 2. ファイル名のみ検索 (再帰的)
    # 浅い階層から探すために os.walk を使う
    filename = os.path.basename(base_candidate)
    for root, dirs, files in os.walk(VAULT_ROOT):
        if filename in files:
            return get_obsidian_uri(os.path.join(root, filename))
            
    return ""

