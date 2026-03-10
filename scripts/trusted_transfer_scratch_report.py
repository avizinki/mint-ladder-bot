#!/usr/bin/env python3
"""
Step 3: Scratch reconstruction for one trusted source wallet (read-only).

Usage:
  python scripts/trusted_transfer_scratch_report.py [options]
  trusted-transfer-scratch-report   # if installed as console script
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

from mint_ladder_bot.trusted_transfer_scratch_cli import entry

if __name__ == "__main__":
    entry()
