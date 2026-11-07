"""截图探针：真实窗口尺寸下渲染聊天窗（含工具行/提示条/附件卡）与人物窗，落 PNG 供人眼复核。"""
from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(tempfile.gettempdir()) / "noesia-audit" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["NOESIA_NO_DIALOG"] = "1"
os.environ["NOESIA_HOME"] = str(OUT / "home")
(OUT / "home").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)

from PySide6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])

from app.chat_panel import ChatPanel  # noqa: E402
from app.tool_view.adapter import view_for  # noqa: E402

panel = ChatPanel()
panel.resize(460, 720)
panel.show()
for _ in range(8):
    app.processEvents()

panel.append_user("帮我看看这段代码为什么报错，顺便把改了的地方标出来。")
panel.set_busy(True)
panel.append_chunk("先说结论：`_show_avatar(False)` 无条件 `resume()`，藏起来时解码又开了。\n\n")
panel.append_chunk("| 位置 | 触发 | 后果 |\n|---|---|---|\n| main.py:812 | 会话结束 | 托盘里还在解码 |\n\n")
panel.append_chunk("```python\ndef f(x):\n    return x + 1\n```\n")
view, part = view_for(
    {
        "kind": "tool.complete",
        "name": "terminal",
        "args": {"command": "pytest -q"},
        "result": {"output": "20 passed in 42s", "exit_code": 0, "duration_seconds": 42.1},
        "is_error": False,
        "tool_call_id": "t1",
    }
)
panel.show_tool_view(view, tool_id=part.tool_call_id, tool_name=part.tool_name, pending=False)
panel.append_chunk(f"MEDIA:{REPO}/assets/noesia_transparent_x.png\n")
panel.end_assistant()
panel.show_notice("turn", "语音这次没合成成功：网络超时", level="warn")
panel.set_busy(False)
for _ in range(10):
    app.processEvents()

img = panel.grab().toImage()
img.save(str(OUT / "chat_panel.png"))

from app.main import FloatingAvatar  # noqa: E402


class _Cfg:
    always_on_top = False
    transparent = True


window = FloatingAvatar(REPO / "assets" / "transparent" / "noesia_transparent.png", _Cfg())
window.resize(360, 480)
window.show()
for _ in range(8):
    app.processEvents()
window.grab().toImage().save(str(OUT / "floating_avatar.png"))

print("SHOTS", (OUT / "chat_panel.png").exists(), (OUT / "floating_avatar.png").exists())
print("SIZE", img.width(), img.height())
