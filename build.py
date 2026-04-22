#!/usr/bin/env python3
"""
FDM Build Script - Builds the desktop app using PyInstaller.
Usage:  python build.py
Output: dist/FDM.app (macOS) or dist/FDM.exe (Windows)
"""

import os
import sys
import subprocess
import platform

# Force UTF-8 output so Windows cmd does not throw UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def main():
    system = platform.system()
    print(f"\n{'='*50}")
    print(f"  FDM Desktop App Builder")
    print(f"  Platform: {system} ({platform.machine()})")
    print(f"{'='*50}\n")

    # Base PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",                    # No console window
        "--name", "FDM",
        "--add-data", f"static{os.pathsep}static",  # Bundle the frontend
        "--add-data", f"server.py{os.pathsep}.",     # Bundle server module
    ]

    # Platform-specific options
    if system == "Darwin":
        # macOS: use .icns icon if available
        if os.path.exists("icon.icns"):
            cmd.extend(["--icon", "icon.icns"])
        cmd.extend([
            "--osx-bundle-identifier", "com.fdm.downloadmanager",
        ])
    elif system == "Windows":
        # Windows: use .ico icon if available
        if os.path.exists("icon.ico"):
            cmd.extend(["--icon", "icon.ico"])

    # Hidden imports that PyInstaller may miss
    cmd.extend([
        "--hidden-import", "flask",
        "--hidden-import", "requests",
        "--hidden-import", "webview",
        "--hidden-import", "engineio.async_drivers.threading",
    ])

    # Exclude conflicting / unused heavy packages from the conda env
    excludes = [
        "PySide6", "PySide2", "PyQt4", "PyQt6",
        "matplotlib", "scipy", "numpy", "pandas",
        "sphinx", "jedi", "IPython", "ipykernel",
        "zmq", "nbformat", "notebook",
    ]
    for pkg in excludes:
        cmd.extend(["--exclude-module", pkg])

    # Entry point
    cmd.append("app.py")

    print("Running PyInstaller...")
    print(f"  Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))

    if result.returncode == 0:
        print(f"\n{'='*50}")
        print("  [OK] Build successful!")
        if system == "Darwin":
            print(f"  Output: dist/FDM.app")
        elif system == "Windows":
            print(f"  Output: dist/FDM.exe")
        else:
            print(f"  Output: dist/FDM")
        print(f"{'='*50}\n")
    else:
        print(f"\n  [FAILED] Build failed with exit code {result.returncode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
