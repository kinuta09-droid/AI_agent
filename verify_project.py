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
        
        count += 1
    
    print(f"  - Total {count} source files scanned.")

    # 3. 重要モジュールのインポート整合性
    print("\n[3] Checking Import Integrity...")
    critical_modules = [
        'lib.lib_shared',
        'lib.lib_obsidian_utils',
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
