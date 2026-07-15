# -*- coding: utf-8 -*-
import os
import subprocess
from typing import List

# To avoid hardcoding, we use a constant. In standard environments Obsidian might be added to PATH.
# If not, it might reside in %LOCALAPPDATA%/Obsidian/Obsidian.exe
OBSIDIAN_EXE = "obsidian"

def get_obsidian_exe() -> str:
    """Gets the path to the Obsidian executable."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        default_path = os.path.join(local_app_data, "Programs", "Obsidian", "Obsidian.exe")
        if os.path.exists(default_path):
            return default_path
            
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        pf_path = os.path.join(program_files, "Obsidian", "Obsidian.exe")
        if os.path.exists(pf_path):
            return pf_path
            
    return OBSIDIAN_EXE


def run_obsidian_cli(command_args: List[str], vault_name: str = "Obsidian", timeout: int = 30) -> subprocess.CompletedProcess:
    """
    Run an Obsidian CLI command.
    
    Args:
        command_args: A list of arguments to pass to the obsidian executable (e.g. ["search", "keywords"]).
        vault_name: The name of the vault to operate on.
        timeout: Timeout in seconds for the command.
        
    Raises:
        subprocess.TimeoutExpired: If the command takes too long.
        subprocess.CalledProcessError: If the command returns a non-zero exit code.
    """
    exe_path = get_obsidian_exe()
    
    # Base command
    full_cmd = [exe_path]
    
    # If the first argument is a known command, we can inject the vault parameter
    command_args_final = []
    if vault_name and "--vault" not in command_args and len(command_args) > 0:
        command_args_final.append(command_args[0])
        command_args_final.append(f"vault={vault_name}")
        command_args_final.extend(command_args[1:])
    else:
        command_args_final.extend(command_args)
        
    import tempfile

    # Obsidian.exe is a GUI app whose stdout is only visible via OS-level handle
    # redirection (equivalent to `cmd /c ... > file`). We redirect stdout to a
    # temp file directly — no shell layer, so arguments are never interpreted.
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp_out:
            tmp_out_path = tmp_out.name

        print(f"[CLI] Running: {exe_path} {command_args_final}")
        with open(tmp_out_path, 'w', encoding='utf-8', errors='replace') as out_handle:
            proc = subprocess.run(
                [exe_path] + command_args_final,
                stdout=out_handle,
                stderr=subprocess.STDOUT,
                timeout=timeout
            )

        with open(tmp_out_path, 'r', encoding='utf-8', errors='replace') as f:
            stdout_content = f.read()

        # Clean up temp file
        try:
            os.remove(tmp_out_path)
        except Exception as e:
            print(f"[CLI] Warning: Failed to remove temp file {tmp_out_path}: {e}")

        result = subprocess.CompletedProcess(
            args=full_cmd,
            returncode=proc.returncode,
            stdout=stdout_content,
            stderr=""
        )
        return result
    except FileNotFoundError:
        print("[CLI] Error: Obsidian CLI not found. Please ensure it's installed and in PATH, or specify the full path.")
        raise
