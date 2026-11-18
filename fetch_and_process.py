#!/usr/bin/env python3
"""Convenience root entrypoint for the End-to-End Literature Pipeline."""
import importlib.util
from pathlib import Path
import sys

# Ensure repository root and scripts directory are in sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

script_path = ROOT / "scripts" / "fetch_and_process.py"
spec = importlib.util.spec_from_file_location("fetch_and_process_script", script_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if __name__ == "__main__":
    sys.exit(mod.main())
