"""全模块导入冒烟：src/app、src/backends、src/*.py 每个模块真 import 一次。"""
from __future__ import annotations

import importlib
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("NOESIA_NO_DIALOG", "1")
sys.path.insert(0, str(REPO / "src"))

import PySide6.QtWidgets  # noqa: E402,F401

qt_app = PySide6.QtWidgets.QApplication.instance() or PySide6.QtWidgets.QApplication([])

names: list[str] = []
for path in sorted((REPO / "src" / "app").rglob("*.py")):
    rel = path.relative_to(REPO / "src").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names.append(".".join(parts))
for path in sorted((REPO / "src" / "backends").rglob("*.py")):
    rel = path.relative_to(REPO / "src").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names.append(".".join(parts))
for path in sorted((REPO / "src").glob("*.py")):
    names.append(path.stem)

skip = {"app._alirtc"}
fails = []
for name in sorted(set(names)):
    if name in skip:
        continue
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        fails.append((name, f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=2)))

print(f"MODULES {len(set(names)) - len(skip)} FAILS {len(fails)}")
for name, err, tb in fails:
    print(f"FAIL {name} -> {err}")
    print(tb)
