"""确认弹窗：删掉一段对话这种收不回来的事，停下来问一句。

审批卡不在这里——审批已经摆进消息流（`app/approval_card.py`），不再单开窗口：
窗口会被截图、最小化、抢焦点、退出程序弄没，流里的卡不会。

不用 `QMessageBox` / `QInputDialog`：系统原生模样跟面板对不上，也没有置顶标志，
会被置顶的人物窗盖住，看得见却点不着。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

from app.window_geometry import card_center, screen_area

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
QLineEdit {
    color: #f4eaf1; background: rgba(255,255,255,12); border: 1px solid rgba(255,255,255,30);
    border-radius: 8px; padding: 7px 10px; font-size: 13px;
    selection-background-color: rgba(255,215,230,80);
}
QLineEdit:focus { border-color: rgba(255,215,230,90); background: rgba(255,255,255,18); }
"""


class _Card(QtWidgets.QDialog):
    """卡片壳：置顶无边框、深色底、标题 + 一句说明 + 取消/确认两个按钮。

    两种用法共用这一副壳：不给 `field` 就是确认卡（确认键染红），给了就是输入卡
    （说明和按钮之间多一个输入框）。别在 `app/` 里再长第三种。
    """

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        confirm_label: str,
        *,
        danger: bool = False,
        field: str | None = None,
        password: bool = False,
    ) -> None:
        super().__init__(parent)
        self._accepted = False
        self._field: QtWidgets.QLineEdit | None = None
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

        if field is not None:
            entry = QtWidgets.QLineEdit(card)
            entry.setText(field)
            entry.setEchoMode(
                QtWidgets.QLineEdit.EchoMode.Password if password else QtWidgets.QLineEdit.EchoMode.Normal
            )
            entry.returnPressed.connect(self._accept)
            lay.addWidget(entry)
            self._field = entry

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", card)
        cancel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        confirm = QtWidgets.QPushButton(confirm_label, card)
        confirm.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        if danger:
            confirm.setObjectName("deny")
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

    def _run(self, parent: QtWidgets.QWidget | None) -> None:
        """压到锚点窗口正中再等回答（没有锚点就屏幕正中）。"""
        if parent is not None:
            self.move(card_center(parent.frameGeometry(), self.size(), screen_area(parent.frameGeometry())))
        self.exec()


class ConfirmDialog(_Card):
    """一句话说清代价，两个按钮：确认了再动手（删对话这种收不回来的事）。"""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        confirm_label: str,
    ) -> None:
        super().__init__(parent, title, description, confirm_label, danger=True)

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
        dialog._run(parent)
        return dialog._accepted


class TextDialog(_Card):
    """一句话的输入卡：改会话标题、填链接、要密钥都用它，和确认卡同一副壳。"""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        initial: str = "",
        confirm_label: str = "保存",
        password: bool = False,
    ) -> None:
        super().__init__(parent, title, description, confirm_label, field=initial, password=password)

    def value(self) -> str:
        return self._field.text().strip() if self._field is not None else ""

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt 命名
        """一出来就能直接打字：先抢到窗口激活，再把光标放进输入框并全选旧标题。"""
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        if self._field is not None:
            self._field.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            self._field.selectAll()

    @classmethod
    def ask(
        cls,
        parent: QtWidgets.QWidget | None,
        title: str,
        description: str,
        initial: str = "",
        confirm_label: str = "保存",
        password: bool = False,
    ) -> str | None:
        """弹出来等人打字：确认了返回去掉首尾空格的内容，取消/Esc/关窗返回 None。"""
        dialog = cls(parent, title, description, initial, confirm_label, password)
        dialog._run(parent)
        return dialog.value() if dialog._accepted else None
