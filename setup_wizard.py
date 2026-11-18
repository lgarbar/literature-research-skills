#!/usr/bin/env python3
"""Convenience root entrypoint for the Interactive Setup Wizard."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

script_path = ROOT / "scripts" / "setup_wizard.py"
spec = importlib.util.spec_from_file_location("setup_wizard_script", script_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if __name__ == "__main__":
    sys.exit(mod.main())
