# -*- coding: utf-8 -*-
r"""
lib/paths.py — 実行時に読み書きするパスの基準を一箇所に集めるモジュール。

なぜ独立モジュールにするか:
  2026-07-30 に sys.path のルート直書き (`sys.path.insert(0, r'C:\AI_agent')`) を
  __file__ 相対へ直したが、**同じ直書きが「実行時データパス」側に9箇所残っていた**
  (learning_db / コストガードのカウンタ / OHLCVキャッシュ / token.json 等)。
  sys.path の下駄は verify_project.check_machine_locked_syspath() が止めるが、
  データパスは検出対象外なので、ここに集約して「直書きする場所そのものを無くす」。

  各ファイルで `_PROJECT_ROOT = dirname(dirname(abspath(__file__)))` を繰り返す
  作法もリポジトリ内に既にあるが (tool_vault_index 等)、**学習DBやコストガードの
  カウンタは「解決先がズレる = 別ファイルに分裂して数字が嘘になる」**種類のパスで、
  分裂したことに気付く手段が無い。定義が1つなら分裂しようがない。

依存を持たない (lib_shared を import しない) こと:
  lib/bt_data.py は「APIキー不要のオフライン層」、lib/cost_guard.py は lib_shared に
  先んじて import されうる。ここが重い依存を持つとその前提が壊れる。

PROJECT_ROOT の意味:
  このファイル (lib/paths.py) から2段上 = リポジトリのルート。実運用の
  チェックアウトは C:\AI_agent にあるので解決先は従来の直書きと同一になる。
  git worktree や別マシンのクローンでは**そのチェックアウト配下**を見る
  (= .env / logs / learning_db がチェックアウト毎に独立する) 点に注意。
"""
import os

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# 単独 import されても .env を読めるようにする (load_dotenv は既存の
# os.environ を上書きしないので、何回呼んでも冪等)
load_dotenv(ENV_PATH)

LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# --- リポジトリの外にあるもの ------------------------------------------------
# Vault も BitNet もリポジトリ外の実体 (別ドライブ / 別インストール) なので
# __file__ 相対では表せない。**絶対パスの直書きを許すのはこのファイルだけ**にして、
# .env で差し替え可能にする。未設定なら従来の直書き値にフォールバックするので、
# 既存環境は .env を1行も足さずに挙動不変。
# (verify_project.check_hardcoded_abs_paths() が他ファイルへの再混入を止める)

# Obsidian Vault (Googleドライブ同期フォルダ)
DEFAULT_VAULT_ROOT = r"D:\マイドライブ\Obsidian"
VAULT_ROOT = os.getenv("OBSIDIAN_VAULT_ROOT", "").strip() or DEFAULT_VAULT_ROOT

# BitNet b1.58 のインストール先 (llama-cli.exe と gguf モデルの親)
DEFAULT_BITNET_DIR = r"C:\BitNet"
BITNET_DIR = os.getenv("BITNET_DIR", "").strip() or DEFAULT_BITNET_DIR

# docling のモデルアーティファクト置き場。他の2つと違い既定値を持たない —
# 未設定なら docling 本体の既定 (HuggingFace Hub から都度解決) に委ね、
# 「lib/paths.py を経由するだけで挙動が変わる」ことを避ける
# (plans/docling-integration.md Phase 1-3)。設定する場合は .env に1行:
#   DOCLING_ARTIFACTS_PATH=C:\AI_agent\models\docling
DOCLING_ARTIFACTS_PATH = os.getenv("DOCLING_ARTIFACTS_PATH", "").strip() or None

# AI生成日本語の文体チェック辞書 (グローバル ~/.claude/hooks/ 配下、A系統hookと共有)。
# ホームディレクトリ配下の軽い参照なので expanduser で解決する
# (VAULT_ROOT/BITNET_DIR と違いリポジトリ外の "実体" ではなく単なる設定ファイル)。
DEFAULT_JA_STYLE_DICT_PATH = os.path.join(os.path.expanduser("~"), ".claude", "hooks", "data", "ja_style_ng_words.json")
JA_STYLE_DICT_PATH = os.getenv("JA_STYLE_DICT_PATH", "").strip() or DEFAULT_JA_STYLE_DICT_PATH

# enforce_style がリトライしても解消できなかった実例のログ (JSONL、1行1件追記)。
# 辞書のpattern_hint/good_patternを実例で肉付けするための材料 (2026-08-17)。
JA_STYLE_UNRESOLVED_LOG = os.path.join(LOGS_DIR, "ja_style_unresolved.jsonl")
