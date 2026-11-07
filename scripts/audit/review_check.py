"""复核「外部人工复核 4 条」用的一次性检查，落成可复跑脚本（对应 AUDIT.md §9）。

四段各自独立、只读：工具行底色 / 悬停按钮样式 / diff 抬头判定 / 气泡垫底色。
不写仓库里的 data/，渲染全在内存里，脏东西只落在系统临时目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NOESIA_HOME"] = str(Path(os.environ["TEMP"]) / "noesia-audit" / "review" / "home")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from app import chat_view, hover_actions, tool_scroll  # noqa: E402
from app.tool_view import diffview  # noqa: E402

GREEN = QtGui.QColor("#55a583")
RED = QtGui.QColor("#e75e78")


def block_backgrounds(scroll: tool_scroll.ToolScroll) -> list[str]:
    out: list[str] = []
    block = scroll.document().begin()
    while block.isValid():
        brush = block.blockFormat().background()
        out.append("—" if brush.style() == QtCore.Qt.BrushStyle.NoBrush else brush.color().name())
        block = block.next()
    return out


print("== 1. 工具行底色：上下文行不该继承上一行的绿/红 ==")
scroll = tool_scroll.ToolScroll()
scroll.set_rows([("context1", None, None), ("+added", None, GREEN),
                 ("context2", None, None), ("-deleted", None, RED),
                 ("context3", None, None)])
print("   set_rows:", " / ".join(block_backgrounds(scroll)))
add = tool_scroll.ToolScroll()
add.set_rows([("first", None, GREEN)])
add.add_rows([("after", None, None)])
print("   add_rows:", " / ".join(block_backgrounds(add)))

print("== 2. 悬停按钮：基础样式在 set_backdrop 之后是否还在 ==")
button = hover_actions.HoverActionButton(None, "copy", "复制", lambda: None)
base = button.styleSheet()
button.set_backdrop(True)
on_qss = button.styleSheet()
button.set_backdrop(False)
print(f"   border/padding 在 backdrop=True 里: {'border: none' in on_qss} / {'padding: 0' in on_qss}")
print(f"   backdrop=False 恢复成基础样式: {button.styleSheet() == base}")


def pixels(qss: str) -> tuple[int, tuple[int, int]]:
    b = hover_actions.HoverActionButton(None, "copy", "复制", lambda: None)
    if qss:
        b.setStyleSheet(qss)
    b.show()
    b.setAttribute(QtCore.Qt.WidgetAttribute.WA_UnderMouse, True)
    app.sendEvent(b, QtCore.QEvent(QtCore.QEvent.Type.Enter))
    app.processEvents()
    img = b.grab().toImage()
    pts = [(x, y) for y in range(img.height()) for x in range(img.width())
           if img.pixelColor(x, y).alpha() > 0]
    if not pts:
        return 0, (0, 0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return len(pts), (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


print("  悬停态像素（基础 QSS vs 空 QSS）:", pixels(base), pixels(""))

print("== 3. diff 抬头判定：带 a/ b/ 前缀与裸路径 ==")
for line in ("a/src/app/x.py → b/src/app/x.py",
             "a\\src\\x.py → b\\src\\x.py",
             "src/app/x.py → src/app/y.py",
             "D:\\proj\\app\\x.py → D:\\proj\\app\\y.py",
             "x.py → y.py",
             "+src/a.py → src/b.py"):
    print(f"   {diffview._is_arrow_header_line(line)!s:5} {line}")

print("== 4. 气泡垫底色：写死的值 vs 合成出来的值 ==")
fill = chat_view.BUBBLE_FILL
alpha = fill.alpha() / 255
print("   BUBBLE_FILL:", fill.getRgb(), " BACKDROP_QSS: (62, 47, 62, 245)")
for base_hex in ("#1b1724", "#241f2e", "#2a2434"):
    under = QtGui.QColor(base_hex)
    comp = tuple(round(getattr(fill, ch)() * alpha + getattr(under, ch)() * (1 - alpha))
                 for ch in ("red", "green", "blue"))
    print(f"   叠在 {base_hex}: {comp}  差={tuple(abs(c - d) for c, d in zip(comp, (62, 47, 62)))}")

print("REVIEW_DONE")
