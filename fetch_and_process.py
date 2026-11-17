#!/usr/bin/env python3
"""Convenience root entrypoint for the End-to-End Literature Pipeline."""
from pathlib import Path
import sys

# Ensure repository root and scripts directory are in sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_and_process import main

if __name__ == "__main__":
    sys.exit(main())
