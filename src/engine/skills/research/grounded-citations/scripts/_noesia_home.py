"""Resolve NOESIA_HOME for standalone skill scripts.

Skill scripts may run outside the Noesia process (system Python, nix env,
CI) where ``noesia_constants`` is not importable.  This module provides the
same ``get_noesia_home()`` contract without requiring it on ``sys.path``.

When ``noesia_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from noesia_constants import get_noesia_home
except (ModuleNotFoundError, ImportError):

    def get_noesia_home() -> Path:
        """Return the Noesia home directory (default: ``~/.noesia``)."""
        val = os.environ.get("NOESIA_HOME", "").strip()
        return Path(val) if val else Path.home() / ".noesia"
