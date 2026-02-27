# -*- coding: utf-8 -*-
import os
import urllib.parse
from lib.lib_shared import VAULT_ROOT

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
    """
    link_text = link_text.strip()
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

