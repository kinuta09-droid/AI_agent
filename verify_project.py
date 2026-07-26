# -*- coding: utf-8 -*-
"""
Antigravity Guardian (Stability Verifier)
プロジェクトの整合性を自動検証し、リファクタリング後の「破壊」を未然に防ぐ。
- 全ファイルの構文チェック (Compile Check)
- エンコーディングチェック (UTF-8)
- パッケージインポート整合性チェック (Dependency Check)
"""

import os
import sys
import subprocess
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# プロジェクトルートの設定
ROOT_DIR = Path(__file__).parent.absolute()
PYTHON_EXE = sys.executable

def check_encoding(file_path):
    """ファイルがUTF-8で保存されているか確認する"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            f.read()
        return True
    except UnicodeDecodeError:
        return False

def check_syntax(file_path):
    """Pythonの構文チェックを行う"""
    try:
        subprocess.run([PYTHON_EXE, "-m", "py_compile", str(file_path)], 
                       check=True, capture_output=True)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, e.stderr.decode('utf-8', errors='replace')

def check_undefined_names(file_path):
    """定義もインポートもされていない名前への参照を検出する。

    構文チェックでは捕まらない NameError 予備軍を静的に洗い出す。
    2026-02-28 の commands/ 分割リファクタで bot_discord.py の
    _dispatch_curate が移動済みシンボル (CurateView 等) を参照したまま残り、
    /curate が5ヶ月間壊れていたのを検出できなかった反省から追加 (2026-07-25)。

    返り値: [(スコープ名, 未定義名), ...]
    """
    import builtins
    import symtable
    import warnings

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            src = f.read()
        # symtable はコンパイルを伴うため SyntaxWarning が出ることがある。
        # 構文の問題は check_syntax の担当なのでここでは黙らせる。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            top = symtable.symtable(src, str(file_path), "exec")
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    module_names = {s.get_name() for s in top.get_symbols()}
    findings = []

    def walk(table, scope):
        for sym in table.get_symbols():
            name = sym.get_name()
            # ローカル/引数/クロージャ変数は対象外。モジュール定義済み・組込みも除外
            if not sym.is_referenced():
                continue
            if sym.is_local() or sym.is_parameter() or sym.is_free():
                continue
            if name in module_names or hasattr(builtins, name):
                continue
            if (scope, name) not in findings:
                findings.append((scope, name))
        for child in table.get_children():
            walk(child, f"{scope}.{child.get_name()}")

    walk(top, "<module>")
    return findings


def check_swallowed_exceptions(file_path):
    """例外を握りつぶしてユーザーに見せるだけの except 節を検出する。

    「捕まえた例外を f-string に埋め込んでいる (= ユーザーに見せている) のに、
    節内に print / logger / traceback / raise が一つも無い」ものを違反とする。
    この形はログに一行も残さないため、壊れていても誰も気付けない。
    実際 /curate は _dispatch_curate の except でこの形をしており、
    NameError のまま5ヶ月間発覚しなかった (2026-07-26 に全28箇所を lib.bot_errors
    経由へ置換)。discord.py は未捕捉例外を自前ロガーで出すので、
    この形の except は「無いほうがマシ」な状態になっている。

    例外変数を使っていない `except ImportError: pass` 等は対象外
    (意図的な握りつぶしであり、見せる情報も無い)。

    返り値: [(行番号, 節の先頭文の要約), ...]
    """
    import ast

    LOGGING_CALLS = {
        "print", "error", "warning", "exception", "critical",
        "format_exc", "print_exc", "log_command_error",
    }

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), str(file_path))
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    def logs_something(handler):
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in LOGGING_CALLS:
                    return True
            if isinstance(node, ast.Raise):
                return True
        return False

    def shows_exception(handler):
        """捕捉した例外変数を f-string に埋め込んでいるか"""
        if not handler.name:
            return False
        for node in ast.walk(handler):
            if isinstance(node, ast.FormattedValue):
                for inner in ast.walk(node.value):
                    if isinstance(inner, ast.Name) and inner.id == handler.name:
                        return True
        return False

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if shows_exception(node) and not logs_something(node):
            summary = ast.unparse(node.body[0]) if node.body else ""
            findings.append((node.lineno, summary[:80]))
    return findings


def check_import(module_name):
    """モジュールが正常にインポート可能か確認する"""
    try:
        # サブプロセスで実行することで、現在のプロセスを汚さず、かつパス解決を検証
        cmd = [PYTHON_EXE, "-c", f"import {module_name}; print('OK')"]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT_DIR))
        if result.returncode == 0:
            return True, ""
        return False, result.stderr
    except Exception as e:
        return False, str(e)

def run_verification():
    print("🛡️  Antigravity Guardian - System Verification Start")
    errors = []
    
    # 1. パッケージ構造の確認
    required_pkgs = ['lib', 'tools', 'commands', 'tests']
    print("[1] Checking Package Structure...")
    for pkg in required_pkgs:
        init_file = ROOT_DIR / pkg / "__init__.py"
        if not init_file.exists():
            errors.append(f"Missing __init__.py in {pkg}/")
            print(f"  ❌ {pkg}/ (Missing __init__.py)")

    # 2. 指定ディレクトリの構文 & エンコーディングチェック (env 等は除外)
    print("\n[2] Checking Syntax & Encoding (Source Only)...")
    target_dirs = ['.', 'lib', 'tools', 'commands', 'tests', 'scripts']
    py_files = []
    
    for d in target_dirs:
        d_path = ROOT_DIR / d
        if d_path.is_dir():
            if d == '.':
                py_files.extend(list(d_path.glob("*.py")))
            else:
                py_files.extend(list(d_path.glob("**/*.py")))
    
    # 重複排除と正規化
    py_files = sorted(list(set(f for f in py_files if f.is_file())))
    
    count = 0
    for py_file in py_files:
        rel_path = py_file.relative_to(ROOT_DIR)
        # 除外キーワード
        if any(ex in str(rel_path) for ex in ['antigravity_env', '.gemini', '__pycache__', '.git']):
            continue

        # Encoding
        if not check_encoding(py_file):
            errors.append(f"Encoding Error: {rel_path} is not UTF-8")
            print(f"  ❌ {rel_path} (Not UTF-8)")
            continue
            
        # Syntax
        success, err_msg = check_syntax(py_file)
        if not success:
            errors.append(f"Syntax Error: {rel_path}\n{err_msg}")
            print(f"  ❌ {rel_path} (Syntax Error)")
        
        # 未定義名 (NameError 予備軍) — ファイル分割リファクタの取り残し検出
        for scope, name in check_undefined_names(py_file):
            errors.append(f"Undefined Name: {rel_path} — {scope} が '{name}' を参照")
            print(f"  ❌ {rel_path} ({scope}: '{name}' が未定義)")

        count += 1

    print(f"  - Total {count} source files scanned.")

    # 3. 重要モジュールのインポート整合性
    print("\n[3] Checking Import Integrity...")
    critical_modules = [
        'lib.lib_shared',
        'lib.lib_obsidian_utils',
        'lib.pid_guard',
        'tools.tool_calendar',
        'tools.tool_web',
        'tools.tool_briefing_manager',
        'tools.tool_gmail_manager',
        'commands.bot_commands_tools',
        'commands.bot_commands_curator',
        'bot_discord',
        'agent_hello',
        'main',
        'tools.tool_future_search',
        'tools.tool_skill_observer',
        'tools.tool_xtrend',
        'tools.tool_vault_index',
        'tools.tool_user_memory',
        'tools.tool_crypto_stats',
        'tools.tool_crypto_watcher',
        'engine_plan_execute'
    ]
    
    for mod in critical_modules:
        success, err_msg = check_import(mod)
        if not success:
            errors.append(f"Import Error: {mod}\n{err_msg}")
            print(f"  ❌ {mod}")
        else:
            print(f"  ✅ {mod}")

    # 4. main.py の TOOLS レジストリ検証
    print("\n[4] Verifying main.py TOOLS registry...")
    try:
        # main.py をモジュールとして一時的に読み込み
        spec = importlib.util.spec_from_file_location("main_module", str(ROOT_DIR / "main.py"))
        main_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main_mod)
        
        if hasattr(main_mod, "TOOLS"):
            for tool in main_mod.TOOLS:
                name = tool.get("name")
                func_path = tool.get("function")
                if not func_path:
                    continue
                
                # インポートテスト
                success, err = check_import(func_path.rsplit(".", 1)[0])
                if not success:
                    errors.append(f"Main Registry Error [{name}]: Module {func_path} not found\n{err}")
                    print(f"  ❌ {name} ({func_path})")
                else:
                    # 関数存在チェック
                    try:
                        module_name, func_name = func_path.rsplit(".", 1)
                        mod = importlib.import_module(module_name)
                        if not hasattr(mod, func_name):
                            errors.append(f"Main Registry Error [{name}]: Function {func_name} not found in {module_name}")
                            print(f"  ❌ {name} ({func_name} missing)")
                        else:
                            print(f"  ✅ {name}")
                    except Exception as e:
                        errors.append(f"Main Registry Error [{name}]: {e}")
                        print(f"  ❌ {name} (Error: {e})")
        else:
            errors.append("main.py does not have a TOOLS list")
    except Exception as e:
        errors.append(f"Failed to load main.py for registry verification: {e}")

    # 5. Discord パターン禁止チェック
    print("\n[5] Checking Discord Anti-Patterns...")
    discord_files = [
        ROOT_DIR / 'bot_discord.py',
        ROOT_DIR / 'commands' / 'bot_commands_tools.py',
    ]
    for df in discord_files:
        if not df.exists():
            continue
        src = df.read_text(encoding='utf-8')
        rel = df.relative_to(ROOT_DIR)
        # io.StringIO を discord.File に渡すパターン（_make_discord_file の定義行は除外）
        for i, line in enumerate(src.splitlines(), 1):
            if 'discord.File' in line and 'io.StringIO' in line and 'def _make_discord_file' not in line:
                msg = f"Discord anti-pattern: {rel}:{i} — use _make_discord_file() instead of discord.File(io.StringIO())"
                errors.append(msg)
                print(f"  ❌ {rel}:{i}")
        print(f"  ✅ {rel} (no StringIO pattern)")

    # 例外の握りつぶし (ログを残さず Discord に出すだけ) — bot 層のみが対象。
    # tools/ の `return f"Error: {e}"` は呼び出し元への戻り値契約なので対象外。
    bot_layer = [ROOT_DIR / 'bot_discord.py'] + sorted((ROOT_DIR / 'commands').glob('*.py'))
    swallowed = 0
    for bf in bot_layer:
        if not bf.exists():
            continue
        rel = bf.relative_to(ROOT_DIR)
        for lineno, summary in check_swallowed_exceptions(bf):
            swallowed += 1
            errors.append(
                f"Swallowed Exception: {rel}:{lineno} — 例外をユーザーに見せるだけで"
                f"ログに残していない。lib.bot_errors.log_command_error() を使うこと\n"
                f"    {summary}"
            )
            print(f"  ❌ {rel}:{lineno} (例外がログに残らない)")
    if swallowed == 0:
        print(f"  ✅ bot層 {len(bot_layer)} ファイル (握りつぶした例外なし)")

    # 6. Briefing プロバイダー登録チェック
    print("\n[6] Checking Briefing Providers...")
    expected_providers = ['Crypto', 'Obsidian', 'Gmail']
    cmd = [PYTHON_EXE, "-c",
        "from tools.tool_briefing_manager import _providers; "
        f"missing=[p for p in {expected_providers} if p not in _providers]; "
        "print('MISSING:'+','.join(missing) if missing else 'OK:'+','.join(_providers.keys()))"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT_DIR))
    if result.returncode != 0 or result.stdout.startswith('MISSING:'):
        msg = f"Briefing providers missing: {result.stdout.strip() or result.stderr.strip()}"
        errors.append(msg)
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ Registered: {result.stdout.strip().replace('OK:','')}")

    # 判定
    print("\n" + "="*50)
    if not errors:
        print("✅ ALL SYSTEMS GO. Project is clean and stable.")
        return True
    else:
        print(f"❌ FOUND {len(errors)} ERROR(S):")
        for err in errors:
            print(f"\n--- ERROR ---\n{err}")
        return False

if __name__ == "__main__":
    success = run_verification()
    sys.exit(0 if success else 1)
