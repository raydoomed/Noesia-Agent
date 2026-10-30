"""Resolve NOESIA_HOME for standalone skill scripts.

Skill scripts may run outside the Noesia process (e.g. system Python,
nix env, CI) where ``noesia_constants`` is not importable.  This module
provides the same ``get_noesia_home()`` and ``display_noesia_home()``
contracts as ``noesia_constants`` without requiring it on ``sys.path``.

When ``noesia_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``noesia_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``NOESIA_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from noesia_constants import display_noesia_home
    from noesia_constants import get_noesia_home
except (ModuleNotFoundError, ImportError):

    def get_noesia_home() -> Path:
        """Return the Noesia home directory (default: ~/.noesia).

        Mirrors ``noesia_constants.get_noesia_home()``."""
        val = os.environ.get("NOESIA_HOME", "").strip()
        return Path(val) if val else Path.home() / ".noesia"

    def display_noesia_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``noesia_constants.display_noesia_home()``."""
        home = get_noesia_home()
        try:
            return "~/" + home.relative_to(Path.home()).as_posix()
        except ValueError:
            return str(home)
