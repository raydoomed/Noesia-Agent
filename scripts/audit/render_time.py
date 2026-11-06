"""渲染耗时：每个控件建一次 + render 一次，记毫秒（性能维度的实测证据）。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
os.environ["NOESIA_HOME"] = str(Path(tempfile.gettempdir()) / "noesia-audit" / "shots" / "home")
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

from PySide6 import QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])

from app import (  # noqa: E402
    boot_splash, chat_panel, chat_view, engine_prompt, model_dialog,
    name_dialog, photo_view, speech_bar, status_stack, tool_scroll, voice_orb,
)

cases = {
    "ChatPanel": chat_panel.ChatPanel,
    "ChatView": chat_view.ChatView,
    "StatusStack": status_stack.StatusStack,
    "SpeechBar": speech_bar.SpeechBar,
    "VoiceOrb": voice_orb.VoiceOrb,
    "BootSplash": boot_splash.BootSplash,
    "ToolScroll": tool_scroll.ToolScroll,
    "ModelDialog": lambda: model_dialog.ModelDialog([]),

    "NameDialog": name_dialog.NameDialog,
    "EngineProblemDialog": lambda: engine_prompt.EngineProblemDialog("boom", "retry", lambda ok: None),
    "PhotoView": lambda: photo_view.PhotoView(QtGui.QPixmap(200, 300)),
}

for name, factory in cases.items():
    start = time.perf_counter()
    try:
        widget = factory()
    except Exception as exc:  # noqa: BLE001
        print(f"TIME {name} SKIP {type(exc).__name__}")
        continue
    built = time.perf_counter()
    widget.resize(420, 640)
    if hasattr(widget, "show"):
        widget.show()
    app.processEvents()
    image = widget.grab().toImage()
    done = time.perf_counter()
    print(f"TIME {name} build={int((built - start) * 1000)}ms render={int((done - built) * 1000)}ms px={image.width()}x{image.height()}")
    widget.close()
