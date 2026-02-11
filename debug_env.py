import sys
import os

print("--- DEBUG START ---")
print(f"Python Executable: {sys.executable}")
print(f"Python Version: {sys.version}")
print("Sys Path:")
for p in sys.path:
    print(f"  {p}")

try:
    import duckduckgo_search
    print(f"duckduckgo_search found at: {duckduckgo_search.__file__}")
except ImportError as e:
    print(f"ImportError: {e}")
print("--- DEBUG END ---")
