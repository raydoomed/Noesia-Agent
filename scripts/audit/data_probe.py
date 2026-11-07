"""补充探针：附件卡（含空格路径）、偏好往返、窗口几何、状态条高度、模型分组。"""
from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.gettempdir()) / "noesia-audit"
TMP = SCRATCH / "noesia_probe2"
TMP.mkdir(parents=True, exist_ok=True)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
os.environ["NOESIA_HOME"] = str(TMP / "home")
(TMP / "home").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])

out = {}

from app import media_links  # noqa: E402
from app.chat_panel import ChatPanel  # noqa: E402

img = REPO / "assets" / "icon.png"
if not img.exists():
    img = next(iter(sorted((REPO / "assets").glob("*.png"))), None)
real = TMP / "有 空格 的文件.png"
QtGui.QPixmap(40, 40).save(str(real))

line_ok = f"MEDIA:{real}"
rest, path = media_links.split_media_line(line_ok)
out["split_space_path"] = path == str(real)
out["split_missing"] = media_links.split_media_line("MEDIA:D:/nope/zzz.png")[1]

panel = ChatPanel()
panel.resize(420, 640)
panel.show()
app.processEvents()
panel.append_chunk(line_ok + "\n")
panel.append_chunk("MEDIA:D:/nope/zzz.png\n")
panel.end_assistant()
app.processEvents()
text = panel.view.toPlainText()
out["card_text_absent"] = ("MEDIA:" not in text) and ("nope" not in text)
out["missing_notice"] = "找不到" in text
out["file_mentioned"] = ("有 空格 的文件.png" in text) or any(
    "有 空格 的文件" in w.text() for w in panel.findChildren(QtWidgets.QLabel)
)

from app import avatar_prefs, speech_prefs, text_prefs, voice_input_prefs  # noqa: E402

model = avatar_prefs.MODELS[0][0]
before = {p.name: p.read_text(encoding="utf-8") if p.exists() else None for p in (REPO / "data" / "settings").glob("*") if p.is_file()}
avatar_prefs.save(provider=(avatar_prefs.PROVIDERS[0][0] if avatar_prefs.PROVIDERS else "vidu"), model=model)
out["avatar_saved"] = avatar_prefs.current().get("model")
avatar_prefs.clear_image()
out["avatar_image_cleared"] = not avatar_prefs.image_setting()
speech_prefs.save(enabled=True, provider="edge", voice_id="zh-CN-XiaoxiaoNeural")
out["speech_saved"] = (speech_prefs.current().get("provider"), speech_prefs.current().get("voice_id"))
voice_input_prefs.remember("guid-1")
voice_input_prefs.remember_hotkey("Ctrl+Alt+V")
out["voice_roundtrip"] = (voice_input_prefs.device_guid(), voice_input_prefs.hotkey())
text_prefs.remember("deepseek", "deepseek-flash")
out["text_roundtrip"] = text_prefs.remembered("deepseek")

for path in (REPO / "data" / "settings").glob("*"):
    if path.is_file() and before.get(path.name) is None:
        path.unlink()
    elif path.is_file():
        path.write_text(before[path.name], encoding="utf-8")

from app import window_geometry as g  # noqa: E402

rect = QtCore.QRect(100, 100, 360, 480)
out["resize_right"] = g.rect_after_resize(rect, QtCore.Qt.RightEdge, QtCore.QSize(400, 480)).getRect()
out["resize_left"] = g.rect_after_resize(rect, QtCore.Qt.LeftEdge, QtCore.QSize(400, 480)).getRect()
out["fit"] = g.fit_width(360, 1.33).toTuple()
out["edges"] = int(g.edges_at(QtCore.QPoint(1, 1), QtCore.QSize(360, 480)).value)
out["in_card"] = g.point_in_rect(QtCore.QPoint(5, 5), QtCore.QRect(0, 0, 10, 10)) if hasattr(g, "point_in_rect") else "n/a"

from app.status_stack import StatusStack  # noqa: E402

stack = StatusStack()
stack.resize(420, 200)
stack.show()
for _ in range(6):
    app.processEvents()
h_empty = stack.height()
stack.show_notice("turn", "HTTP 403: 没有权限", level="error")
for _ in range(6):
    app.processEvents()
h_notice = stack.height()
img_shot = stack.grab().toImage()
painted = sum(1 for y in range(img_shot.height()) for x in range(img_shot.width()) if img_shot.pixelColor(x, y).alpha() > 0)
stack.set_processes([{"session_id": "s1", "status": "running", "command": "pytest", "pid": 1}])
for _ in range(6):
    app.processEvents()
h_bg = stack.height()
stack.clear_all()
for _ in range(6):
    app.processEvents()
h_clear = stack.height()
out["stack_heights"] = (h_empty, h_notice, h_bg, h_clear)
out["stack_painted_px"] = painted

from app import model_menu  # noqa: E402

items = [
    {"id": "deepseek:deepseek-flash", "label": "DeepSeek · deepseek-flash", "backend": "deepseek",
     "model": "deepseek-flash", "available": True, "current": True},
    {"id": "openrouter:z-model", "label": "OpenRouter · z-model", "backend": "openrouter",
     "model": "z-model", "available": True},
    {"id": "openrouter:a-model", "label": "OpenRouter · a-model", "backend": "openrouter",
     "model": "a-model", "available": True},
]
groups = model_menu.group_models(items)
out["groups"] = [(gr.get("title") or gr.get("provider") or gr.get("backend"), [i["model"] for i in gr["items"]]) for gr in groups]
out["current_label"] = model_menu.current_label(items, "deepseek:deepseek-flash")
out["missing_label"] = model_menu.current_label(items, "x:y")

for key, value in out.items():
    print(f"P2 {key} = {value!r}")
panel.close()
print("PROBE2_DONE")
