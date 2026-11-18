#!/usr/bin/env python3
"""Convenience root entrypoint for Playwright institutional authentication and proxy routing."""
import importlib.util
from pathlib import Path
import sys

# Ensure repository root and scripts directory are in sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

script_path = ROOT / "scripts" / "auth_setup.py"
spec = importlib.util.spec_from_file_location("auth_setup_script", script_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if __name__ == "__main__":
    sys.exit(mod.main())
