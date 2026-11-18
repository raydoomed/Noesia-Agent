"""确认弹窗：删掉一段对话这种收不回来的事，停下来问一句。

审批卡不在这里——审批已经摆进消息流（`app/approval_card.py`），不再单开窗口：
窗口会被截图、最小化、抢焦点、退出程序弄没，流里的卡不会。

不用 `QMessageBox`：它是系统原生模样，没有置顶标志，会被置顶的人物窗盖住。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtWidgets as QtWidgets

from app.window_geometry import card_position, screen_area

_CARD_STYLE = """
#card { background: rgba(25, 21, 32, 252); border: 1px solid rgba(255,255,255,26); border-radius: 14px; }
#title { color: #f4eaf1; font-size: 15px; font-weight: 600; }
#desc { color: rgba(246,223,234,110); font-size: 13px; }
QPushButton {
    color: rgba(246,223,234,180); background: rgba(255,255,255,16); border: 1px solid rgba(255,255,255,26);
    border-radius: 8px; padding: 7px 14px; font-size: 13px;
}
QPushButton:hover { background: rgba(255,255,255,30); }
QPushButton#deny { background: rgba(255,107,107,14); border-color: rgba(255,107,107,50); color: #ff9a9a; }
QPushButton#deny:hover { background: rgba(255,107,107,34); }
"""


class ConfirmDialog(QtWidgets.QDialog):
    """一句话说清代价，两个按钮：确认了再动手（删对话这种收不回来的事）。"""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        confirm_label: str,
    ) -> None:
        super().__init__(parent)
        self._accepted = False
        self.setWindowFlags(
            QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet(_CARD_STYLE)

        card = QtWidgets.QFrame(self)
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        head = QtWidgets.QLabel(title, card)
        head.setObjectName("title")
        head.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        lay.addWidget(head)

        body = QtWidgets.QLabel(description, card)
        body.setObjectName("desc")
        body.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        lay.addWidget(body)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", card)
        cancel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        confirm = QtWidgets.QPushButton(confirm_label, card)
        confirm.setObjectName("deny")
        confirm.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        confirm.clicked.connect(self._accept)
        row.addWidget(confirm)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(max(320, min(460, self.sizeHint().width())))

    def _accept(self) -> None:
        self._accepted = True
        self.accept()

    @classmethod
    def ask(
        cls,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        confirm_label: str,
    ) -> bool:
        """弹出来等人回答：确认 True，取消/关窗/Esc 都是 False（沉默不算同意）。"""
        dialog = cls(parent, title, description, confirm_label)
        if parent is not None:
            dialog.move(
                card_position(parent.frameGeometry(), dialog.size(), screen_area(parent.frameGeometry()))
            )
        dialog.exec()
        return dialog._accepted
