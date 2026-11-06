"""ChatPanel 行为探针：忙碌态、状态提示、机器行过滤、附件行、停止信号。"""
from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.gettempdir()) / "noesia-audit"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
home = SCRATCH / "noesia_panel_probe_home"
home.mkdir(parents=True, exist_ok=True)
os.environ["NOESIA_HOME"] = str(home)
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

from PySide6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])
from app.chat_panel import ChatPanel  # noqa: E402

panel = ChatPanel()
panel.resize(420, 640)
panel.show()
app.processEvents()

out = {}


def send_text() -> str:
    for btn in panel.findChildren(QtWidgets.QPushButton):
        if btn.property("role") == "send" or btn.objectName() in {"send", "send-button"}:
            return btn.text()
    texts = [b.text() for b in panel.findChildren(QtWidgets.QPushButton) if b.text()]
    return "|".join(texts)


out["send_idle"] = send_text()
panel.set_busy(True)
app.processEvents()
out["send_busy"] = send_text()
out["is_busy_true"] = panel.is_busy()
stops = []
panel.stop_requested.connect(lambda: stops.append(1)) if hasattr(panel, "stop_requested") else None
panel.set_busy(False)
app.processEvents()
out["send_after"] = send_text()
out["is_busy_false"] = panel.is_busy()

# 状态提示：显示/撤销
panel.show_notice("turn", "HTTP 403: 没有权限", level="error")
app.processEvents()
out["stack_visible"] = panel.stack.isVisible()
out["stack_labels"] = [l.text() for l in panel.stack.findChildren(QtWidgets.QLabel) if l.text()]
panel.clear_status_notice("turn")
app.processEvents()
out["stack_after_clear"] = panel.stack.isVisible()

# 机器行不许画成正文
panel.set_busy(True)
before = panel.view.toPlainText()
panel.append_chunk("[System note: memory write skipped]\n")
panel.append_chunk("Your request was not processed: busy\n")
app.processEvents()
after = panel.view.toPlainText()
out["machine_lines_visible"] = ("System note" in after) or ("not processed" in after)
out["machine_delta_chars"] = len(after) - len(before)

# 正常正文 + MEDIA 附件行
panel.append_chunk(f"这是正常回答。\nMEDIA:{REPO}/assets/preview.png\n")
app.processEvents()
text_now = panel.view.toPlainText()
out["normal_text_visible"] = "这是正常回答" in text_now
out["media_raw_visible"] = "MEDIA:" in text_now
panel.end_assistant()
panel.set_busy(False)
app.processEvents()

# 停止按钮真的发信号
sb = getattr(panel, "_send_btn", None)
out["send_btn_found"] = sb is not None
if sb is not None:
    panel.set_busy(True)
    app.processEvents()
    out["busy_btn_text"] = sb.text()
    sb.click()
    app.processEvents()
    out["stop_signals"] = len(stops)

panels = [f"{k}={v}" for k, v in out.items()]
print("PANEL " + " | ".join(panels))
panel.close()
