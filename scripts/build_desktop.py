#!/usr/bin/env python3
"""Build standalone Wren desktop distribution using PyInstaller."""
import subprocess
import sys
from pathlib import Path


def main():
    repo_root = Path(__file__).resolve().parent.parent
    spec_file = repo_root / "wren.spec"
    
    print(f"Building standalone desktop distribution for Wren from {spec_file}...")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(spec_file)]
    res = subprocess.run(cmd, cwd=repo_root)
    if res.returncode == 0:
        print("\n=== Build successful! Standalone binary in dist/wren-desktop/ ===")
    else:
        print("\nBuild failed with code:", res.returncode)
        sys.exit(res.returncode)

if __name__ == "__main__":
    main()
