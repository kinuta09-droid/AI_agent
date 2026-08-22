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


def build_contradiction_names_and_links(relpath: str, related_note: str) -> tuple[str, str, str, str]:
    """矛盾記録の own/related 双方について (name, wikilink) を組み立てる。

    Vault Insight Watcher (検知→Markdown記録) と矛盾裁定キュー (Discord裁定→
    実ファイル解決) が同じ組み立てロジックを共有するための切り出し
    (format_wikilink/split_heading_anchor の対使用と同じ理由: 片方だけ変えると
    ログの wikilink と裁定時のファイル解決が食い違う)。

    戻り値: (own_name, own_link, related_name, related_link)。
    own_name/related_name は見出しアンカーを除いた拡張子なしファイル名
    (find_file_in_vault にそのまま渡せる形)。related_note が空なら
    related_name="" / related_link="(不明)"。
    """
    own_name, own_heading = split_heading_anchor(os.path.splitext(relpath)[0])
    related_raw, related_heading = split_heading_anchor(str(related_note or ""))
    related_name = os.path.splitext(related_raw)[0] if related_raw else ""
    own_link = format_wikilink(own_name, own_heading)
    related_link = format_wikilink(related_name, related_heading) if related_name else "(不明)"
    return own_name, own_link, related_name, related_link


# ---------------------------------------------------------
# ノート本文からの検索クエリ抽出
# ---------------------------------------------------------
# commands/bot_commands_idea.py の /idea-relink 専用実装だったが、
# tools/tool_orphan_relink.py (スケジューラから直接呼ばれる) からも使うため、
# tools層がcommands層に依存しなくて済むようここに集約する。

# > [!SUMMARY] callout の次行にある要約本文を拾う（Tpl_Permanent.md の固定フォーマット）。
_SUMMARY_CALLOUT_RE = re.compile(r'^> \[!SUMMARY\].*\n((?:^>.*\n?)*)', re.MULTILINE)


def extract_note_query(content: str, max_chars: int = 200) -> str:
    """ノート本文から検索クエリを取り出す。

    SUMMARY calloutがあればその中身、無ければ本文先頭 max_chars 文字。
    """
    m = _SUMMARY_CALLOUT_RE.search(content or "")
    if m:
        return " ".join(line.lstrip('>').strip() for line in m.group(1).splitlines()).strip()
    return (content or "")[:max_chars]


# Vault のノート以外 (エージェントの行動ログ) を指す source 名。
# 検索インデックスが Vault と同居するようになったので (Phase B)、
# 出典を組み立てる側がここで分岐する必要がある。
VAULT_SOURCE = "vault"

# source を人間向けに読める名前にする。検索結果の出典ラベルに使う。
_SOURCE_LABEL = {
    "crypto": "crypto予測ログ",
    "agent_exec": "エージェント実行ログ",
    "agent_skill": "ツール実行実績",
}


def format_source_ref(source, filename: str, heading=None) -> str:
    """検索ヒットの出典表記を組み立てる。source によって形が変わる。

    - Vault のノート → `[[ファイル名#見出し]]` (従来どおり)
    - 行動ログ       → `` `crypto予測ログ: 見出し` `` (**wikilink にしない**)

    行動ログを [[ ]] で出してはいけない理由:
      `get_obsidian_uri_from_link` は [[ ]] の中身を Vault 内から再帰検索する。
      行動ログは実ファイルではないので必ず空振りし、Discord の
      「Obsidianで開く」ボタンが無言で消える (ログにも一行も残らない)。
      悪くすると名前の似た無関係なノートに当たって、間違ったノートを開く。

      これは Phase A2 で `[[file]]` → `[[file#heading]]` にしたときと同じ罠で、
      だから `format_wikilink` (出す) と `split_heading_anchor` (解決する) を
      同じモジュールに並べてある。この関数も同じ場所に置く — 出典の形を
      変える人が、解決側を見ずに済ませられないようにするため。

    出す側は表示だけではない: リランクのプロンプト
    (`tool_vault_hybrid._format_rerank_candidates`) にも出典ラベルが載る。
    そちらで偽の wikilink を出すと、判定するモデルに
    「これは Obsidian のノートである」という誤った手掛かりを与える。
    """
    if not source or str(source) == VAULT_SOURCE:
        return format_wikilink(filename, heading)
    label = _SOURCE_LABEL.get(str(source), str(source))
    # heading (=ドキュメントのタイトル) が本体。無ければ doc_id の末尾で代用する。
    name = " ".join(str(heading or filename or "").split())
    # バッククォートは Discord のコードスパンを閉じるので潰す
    return f"`{label}: {name.replace('`', '')}`"


# ---------------------------------------------------------
# Frontmatter の軽量読み取り (YAML全体はパースしない)
# ---------------------------------------------------------
# tool_list_open_ideas (status判定) と tool_vault_health (status欠落率メトリクス) が
# 同じ「frontmatter先頭を正規表現で読む」処理を別々に実装しないよう、ここに集約する。
# 全文パースしない理由: head_bytesで打ち切って良く、壊れたYAMLでも例外を出さず
# None を返せる方が、健全性メトリクスのような走査系の用途では安全 (1件の壊れたノートで
# 集計全体を止めてはいけない)。

def read_frontmatter_field(path: str, key: str, head_bytes: int = 500) -> str | None:
    """ノート先頭の frontmatter から `key: value` (単一行スカラー値) を1つ読む。

    ファイル未存在・読み取り失敗・キー未設定はすべて None (例外を投げない)。
    `tags:` のようなブロック値には非対応 (最初の空白区切りトークンのみを返す)。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            head = f.read(head_bytes)
    except OSError:
        return None
    m = re.search(rf'^{re.escape(key)}:\s*(\S+)', head, re.MULTILINE)
    return m.group(1) if m else None


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

