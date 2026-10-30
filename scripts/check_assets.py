#!/usr/bin/env python3
"""薄封装：等价于 `noesia check-assets`。

保留独立脚本是为了让不懂 `-m` 的用户也能直接 `python scripts/check_assets.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from noesia_entry import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["check-assets", *sys.argv[1:]]))
