"""起名卡片：第一次见面时问一句「我该怎么称呼你」。

引擎的引导流程一共七张卡（名字/主题/布局/插件/导览/首个任务…），我们只做名字这一张——
其余的直接关掉，引擎按「卡片被关」记一笔继续往下走（`flow.TimedOut`），不影响流程。

回包契约在引擎 `tools/setup_beat_tool.py::_reply_event`：
- 正常回答 → `{"answer": {"name": "阿岚"}}`（`flow.Answered`）
- 关掉/不答 → `{}`（`flow.TimedOut`，跟「跳过」一样让它往下走）
帧 id 用 server→client 请求的 `srq-...`，跟审批同一套。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtWidgets

from app.window_geometry import card_position, screen_area

_STYLE = """
#card { background: #23273a; border: 1px solid rgba(255,255,255,40); border-radius: 14px; }
#title { color: #f2f4fb; font-size: 15px; font-weight: 600; }
#hint { color: #9fb3c8; font-size: 12px; }
QLineEdit {
    color: #f2f4fb; background: rgba(0,0,0,70); border: 1px solid rgba(255,255,255,35);
    border-radius: 9px; padding: 8px 10px; font-size: 14px;
}
QLineEdit:focus { border-color: #5b8fb9; }
QPushButton {
    color: #e9ecf4; background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,30);
    border-radius: 8px; padding: 7px 14px; font-size: 13px;
}
QPushButton:hover { background: rgba(255,255,255,34); }
QPushButton#primary { background: #3d6f5b; border-color: #4f8a71; }
QPushButton#primary:hover { background: #4a8369; }
"""


class NameDialog(QtWidgets.QDialog):
    """问她该怎么称呼他。结果通过 on_name(name) 交回去；name 为空 = 这次先跳过。"""

    def __init__(self, payload, on_name: Callable[[str], None],
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_name = on_name
        self._answered = False
        self._drag_offset: QtCore.QPoint | None = None

        self.setWindowFlags(
            QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setModal(False)
        self.setStyleSheet(_STYLE)

        card = QtWidgets.QFrame(self)
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        title = QtWidgets.QLabel("第一次见面，我该怎么称呼你？", card)
        title.setObjectName("title")
        lay.addWidget(title)

        card_data = payload.get("card") if isinstance(payload, dict) else None
        suggested = ""
        if isinstance(card_data, dict):
            suggested = str(card_data.get("suggested") or "").strip()

        self._edit = QtWidgets.QLineEdit(card)
        self._edit.setPlaceholderText("输入你的名字")
        if suggested:
            self._edit.setText(suggested)
        self._edit.returnPressed.connect(self._submit)
        lay.addWidget(self._edit)

        hint_text = ("这是你的系统账户名，不对就改掉。" if suggested
                     else "名字会写进我的人设文件；以后随时能改。")
        hint = QtWidgets.QLabel(hint_text, card)
        hint.setObjectName("hint")
        lay.addWidget(hint)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        skip = QtWidgets.QPushButton("以后再说", card)
        skip.clicked.connect(lambda: self._answer(""))
        row.addWidget(skip)
        ok = QtWidgets.QPushButton("就叫这个", card)
        ok.setObjectName("primary")
        ok.clicked.connect(self._submit)
        row.addWidget(ok)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(max(320, min(420, self.sizeHint().width())))

    def _submit(self) -> None:
        self._answer(self._edit.text().strip())

    def _answer(self, name: str) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_name(name)
        finally:
            self.close()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt
        if not self._answered:
            self._answered = True
            self._on_name("")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt
        if event.key() == QtCore.Qt.Key_Escape:
            self._answer("")
            return
        super().keyPressEvent(event)

    def show_near(self, anchor: QtCore.QRect | None) -> None:
        self.move_near(anchor)
        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus()

    def move_near(self, anchor: QtCore.QRect | None) -> None:
        self.adjustSize()
        self.move(card_position(anchor, self.size(), screen_area(anchor)))
