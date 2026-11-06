"""Headless UI probe — one widget per process (a modal click can hang the event loop).

Usage: python noesia_ui_probe_one.py <case-id>
"""
from __future__ import annotations

import json
import os
import tempfile
import shutil
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.gettempdir()) / "noesia-audit"
TMP = SCRATCH / "noesia_ui_probe"
TMP.mkdir(parents=True, exist_ok=True)
(TMP / "home").mkdir(parents=True, exist_ok=True)
if not (TMP / "home" / "config.yaml").exists():
    shutil.copy2(REPO / "data" / "NoesiaHome" / "config.yaml", TMP / "home" / "config.yaml")

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
os.environ["NOESIA_HOME"] = str(TMP / "home")

sys.path.insert(0, str(REPO / "src"))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# 模态框/文件框一律不真开：探针只验证「点了不炸、画面有像素」
QtWidgets.QDialog.exec = lambda self, *a, **k: 0
if hasattr(QtWidgets.QDialog, "exec_"):
    QtWidgets.QDialog.exec_ = lambda self, *a, **k: 0
QtWidgets.QMenu.exec = lambda self, *a, **k: None
QtWidgets.QMenu.exec_ = lambda self, *a, **k: None
QtWidgets.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
QtWidgets.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
QtWidgets.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "")
for _n in ("information", "warning", "critical", "question", "about"):
    setattr(QtWidgets.QMessageBox, _n, staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok))
QtWidgets.QMessageBox.exec = lambda self, *a, **k: 0


# PySide 的 C++ 方法（QMenu.exec / QDialog.exec）改不了类属性，只能靠定时器把弹出的模态窗关掉，
# 否则点「＋」这种按钮会卡死在菜单事件循环里
_popup_killer = QtCore.QTimer()
_popup_killer.setInterval(60)


def _close_popups():
    for w in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(w, (QtWidgets.QMenu, QtWidgets.QDialog)) and w.isVisible() and w is not globals().get("_current"):
            with __import__("contextlib").suppress(Exception):
                w.reject() if hasattr(w, "reject") else w.close()
                w.close()


_popup_killer.timeout.connect(_close_popups)
_popup_killer.start()


def count_pixels(img: QtGui.QImage):
    if img.isNull():
        return 0, 0
    img = img.convertToFormat(QtGui.QImage.Format_ARGB32)
    seen = set()
    painted = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            px = img.pixel(x, y)
            if ((px >> 24) & 0xFF) > 8:
                painted += 1
                seen.add(px)
    return painted, len(seen)


class Cfg:
    always_on_top = False
    transparent = True


def _idle():
    from app.idle_video import IdleVideoPlayer
    return IdleVideoPlayer(REPO / "assets" / "idle_video.mp4"), True


CASES = {
    "idle_video": _idle,
    "boot_splash": lambda: (__import__("app.boot_splash", fromlist=["x"]).BootSplash(), True),
    "chat_view": lambda: (__import__("app.chat_view", fromlist=["x"]).ChatView(), True),
    "status_stack": lambda: (__import__("app.status_stack", fromlist=["x"]).StatusStack(), True),
    "tool_scroll": lambda: (__import__("app.tool_scroll", fromlist=["x"]).ToolScroll(), True),
    "speech_bar": lambda: (__import__("app.speech_bar", fromlist=["x"]).SpeechBar(), True),
    "voice_orb": lambda: (__import__("app.voice_orb", fromlist=["x"]).VoiceOrb(), True),
    "chat_panel": lambda: (__import__("app.chat_panel", fromlist=["x"]).ChatPanel(), True),
    "chat_input": lambda: (__import__("app.chat_panel", fromlist=["x"])._Input(), True),
    "confirm": lambda: (__import__("app.approval_dialog", fromlist=["x"]).ConfirmDialog(None, "确认操作", "说明文字", "确定"), True),
    "approval": lambda: (__import__("app.approval_dialog", fromlist=["x"]).ApprovalDialog(
        {"title": "批准", "description": "运行命令", "command": "ls -la", "request_id": "r1"}, lambda *a: None), True),
    "engine_prompt": lambda: (__import__("app.engine_prompt", fromlist=["x"]).EngineProblemDialog("引擎被占用", "takeover", lambda *a: None), True),
    "name_dialog": lambda: (__import__("app.name_dialog", fromlist=["x"]).NameDialog({"prompt": "叫你什么？"}, lambda *a: None), True),
    "photo_view": lambda: (__import__("app.photo_view", fromlist=["x"]).PhotoView("图片"), True),
    "diff_preview": lambda: (__import__("app.file_preview", fromlist=["x"]).DiffPreviewWindow(
        None, "diff", [[("1", QtGui.QTextCharFormat()), (" def f():", QtGui.QTextCharFormat())],
                       [("2", QtGui.QTextCharFormat()), ("     return 1", QtGui.QTextCharFormat())]],
        "+1 -0"), True),
    "avatar_status": lambda: (__import__("app.model_dialog", fromlist=["x"])._AvatarStatus(), True),
    "model_dialog": lambda: (__import__("app.model_dialog", fromlist=["x"]).ModelDialog(
        [{"provider": "deepseek", "label": "DeepSeek", "models": [{"id": "deepseek-flash", "name": "flash"}], "available": True}]), True),
    "floating_avatar": lambda: (__import__("app.main", fromlist=["x"]).FloatingAvatar(
        REPO / "assets" / "transparent" / "noesia_transparent.png", Cfg()), True),
}

CALL_ONLY = {
    "speaker": ("app.speech_speaker", "Speaker"),
    "stream_speaker": ("app.speech_interactive", "StreamSpeaker"),
    "recorder": ("app.voice_input", "VoiceRecorder"),
    "hotkey": ("app.global_hotkey", "GlobalHotkey"),
    "avatar_rtc": ("app.avatar_rtc", "AvatarRtc"),
    "engine_process": ("app.engine_wire", "_EngineProcess"),
    "token_checker": ("app.model_dialog", "_TokenChecker"),

}

cid = sys.argv[1]
rec = {"case": cid, "ok": False, "error": "", "painted": 0, "colors": 0, "buttons": 0, "click_errors": []}
try:
    if cid in CASES:
        obj, do_click = CASES[cid]()
        globals()["_current"] = obj
        obj.resize(obj.sizeHint() if obj.sizeHint().isValid() else QtCore.QSize(640, 420))
        obj.show()
        for _ in range(3):
            qt_app.processEvents()
        img = obj.grab().toImage()
        rec["painted"], rec["colors"] = count_pixels(img)
        buttons = obj.findChildren(QtWidgets.QAbstractButton)
        rec["buttons"] = len(buttons)
        if do_click:
            for b in buttons:
                if not b.isEnabled() or not b.isVisible():
                    continue
                try:
                    with __import__("contextlib").suppress(Exception):
                        b.click()
                    qt_app.processEvents()
                except Exception as exc:  # noqa: BLE001
                    rec["click_errors"].append(f"{type(b).__name__}: {type(exc).__name__}: {exc}")
        with __import__("contextlib").suppress(Exception):
            obj.hide()
            obj.deleteLater()
        qt_app.processEvents()
        rec["ok"] = True
    elif cid in CALL_ONLY:
        mod_name, cls_name = CALL_ONLY[cid]
        mod = __import__(mod_name, fromlist=[cls_name])
        obj = getattr(mod, cls_name)()
        rec["ok"] = True
        rec["type"] = type(obj).__name__
    else:
        rec["error"] = "unknown case"
except Exception as exc:  # noqa: BLE001
    rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["trace"] = traceback.format_exc(limit=5)
print("PROBE_JSON " + json.dumps(rec, ensure_ascii=False))
