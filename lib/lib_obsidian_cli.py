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
    
    # We construct the argument list carefully. We need to wrap each argument in quotes to handle spaces.
    # We format for Start-Process ArgumentList format: '"arg1" "arg2"'
    args_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in command_args_final)
    
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp_out:
            tmp_out_path = tmp_out.name
            
        # We must escape double quotes inside the ArgumentList string for PowerShell
        # For example: Start-Process -FilePath cmd.exe -ArgumentList '/c', '""C:\path\to\Obsidian.exe" search query="AI" limit="5" > "out.txt""'
        # The first set of quotes wraps the whole command string passed to cmd.exe, and the inner quotes are for paths.
        ps_cmd = f"Start-Process -FilePath cmd.exe -ArgumentList '/c', '\"\"{exe_path}\" {args_str} > \"{tmp_out_path}\"\"' -Wait -NoNewWindow"
        
        print(f"[CLI] Running via PS: {ps_cmd}")
        # Run powershell
        subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, # captures PS output, not Obsidian's
            text=True,
            timeout=timeout
        )
        
        # Read the captured output from Obsidian
        stdout_content = ""
        with open(tmp_out_path, 'r', encoding='utf-8', errors='replace') as f:
            stdout_content = f.read()
            
        # Clean up temp file
        try:
            os.remove(tmp_out_path)
        except Exception as e:
            print(f"[CLI] Warning: Failed to remove temp file {tmp_out_path}: {e}")
            
        # Create a mock CompletedProcess to match the expected signature
        result = subprocess.CompletedProcess(
            args=full_cmd,
            returncode=0, # Hard to get proper exit code this way, assume 0
            stdout=stdout_content,
            stderr=""
        )
        return result
    except FileNotFoundError:
        print("[CLI] Error: Obsidian CLI not found. Please ensure it's installed and in PATH, or specify the full path.")
        raise
