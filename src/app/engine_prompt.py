"""引擎起不来时贴在人物下方的一张卡片（不是系统消息框）。

为什么不用 `QMessageBox`：人物窗是 `WindowStaysOnTopHint` 的无边框窗，普通消息框没有这个标志，
就会被画在人物**后面**——截图里那段字被脸和身子挡住，正是这个原因。而且系统框的排版和我们在
别处用的卡片（审批 / 起名）不是一套，看着像外挂。

所以这里跟审批卡片用同一套样式和同一套定位（`window_geometry.card_position()`：贴在人物下方，
屏幕底边放不下就翻到上方，左右夹在可见区域内），并且**跟着人物走**——`FloatingAvatar` 在
`moveEvent/resizeEvent` 里调 `move_near()`。
"""

from __future__ import annotations

from collections.abc import Callable

import PySide6.QtCore as QtCore
import PySide6.QtWidgets as QtWidgets

from app.window_geometry import card_position, screen_area

_STYLE = """
#card {
    background: #23273a;
    border: 1px solid rgba(255, 190, 120, 120);
    border-radius: 14px;
}
#title { color: #ffd7a8; font-size: 15px; font-weight: 600; }
#desc  { color: #e6e9f2; font-size: 13px; }
#hint  { color: rgba(255, 255, 255, 120); font-size: 11px; }
QPushButton {
    color: #e9ecf4; background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,30);
    border-radius: 8px; padding: 7px 14px; font-size: 13px;
}
QPushButton:hover { background: rgba(255,255,255,34); }
QPushButton#primary { background: #6b4a2f; border-color: #a9743f; }
QPushButton#primary:hover { background: #7d5738; }
"""

_HINT = (
    "同意后这样做：先替命令行里的 noesia 会话按 Ctrl+C（让它自己收尾退出），再结束后台占用进程，然后启动。"
    "那台会话里没答完的话会中断。全程静默，不弹窗口。"
)


class EngineProblemDialog(QtWidgets.QDialog):
    """一张卡片、两个选择。结果通过 on_choice(是否结束并重试) 交回去。"""

    def __init__(
        self,
        message: str,
        action: str,
        on_choice: Callable[[bool], None],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_choice = on_choice
        self._answered = False
        self._drag_offset: QtCore.QPoint | None = None

        self.setWindowFlags(
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False)
        self.setStyleSheet(_STYLE)

        card = QtWidgets.QFrame(self)
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        title = QtWidgets.QLabel("引擎没起来", card)
        title.setObjectName("title")
        lay.addWidget(title)

        desc = QtWidgets.QLabel(message, card)
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        hint = QtWidgets.QLabel(_HINT, card)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        later = QtWidgets.QPushButton("稍后再说", card)
        later.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        later.clicked.connect(lambda: self._answer(False))
        row.addWidget(later)
        yes = QtWidgets.QPushButton(action, card)
        yes.setObjectName("primary")
        yes.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        yes.clicked.connect(lambda: self._answer(True))
        row.addWidget(yes)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(max(360, min(480, self.sizeHint().width())))

    def _answer(self, agree: bool) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_choice(agree)
        finally:
            self.close()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt
        if not self._answered:
            self._answered = True
            self._on_choice(False)
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._answer(False)
            return
        super().keyPressEvent(event)

    def show_near(self, anchor: QtCore.QRect | None) -> None:
        self.move_near(anchor)
        self.show()
        self.raise_()

    def move_near(self, anchor: QtCore.QRect | None) -> None:
        """只挪位置、不动显隐：人物窗被拖动时跟着走。

        尺寸在构造时就 `adjustSize()` 量死了，这里再量一遍纯属白算——人物窗每移动一像素
        都会调到这里。
        """
        self.move(card_position(anchor, self.size(), screen_area(anchor)))
