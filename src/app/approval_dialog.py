"""弹窗：她要动危险东西、或者要删掉一段对话时，停下来问一句。

引擎负责「什么算危险」（smart / manual 策略、被标记的规则），我们只负责问和显示——
所以这里没有任何判定逻辑，只有一颗卡片、四个按钮和一段能看清的原文。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtWidgets

from app.redact import redact
from app.window_geometry import card_position, screen_area

# 引擎给的 description 是英文规则名（delete in root path / format drive …），
# 界面上先给一句中文人话，再把原文放小字里：用户看的是「要干什么」，不是内部规则名。
_FRIENDLY = {
    "delete file or folder": "她要删掉一个文件或目录",
    "delete via script": "她要用脚本删东西",
    "delete in root path": "她要删除根目录下的东西",
    "pipe remote content to shell": "她要把网上的内容直接交给命令行执行",
    "format drive": "她要格式化磁盘",
    "git force push": "她要强制推送，会改写远端历史",
}

_CARD_STYLE = """
#card { background: #23273a; border: 1px solid rgba(255,255,255,40); border-radius: 14px; }
#title { color: #f2f4fb; font-size: 15px; font-weight: 600; }
#desc { color: #cfd6e6; font-size: 13px; }
#cmd {
    color: #9fb3c8; font-size: 12px; background: rgba(0,0,0,60);
    border: 1px solid rgba(255,255,255,25); border-radius: 8px; padding: 8px;
}
QPushButton {
    color: #e9ecf4; background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,30);
    border-radius: 8px; padding: 7px 14px; font-size: 13px;
}
QPushButton:hover { background: rgba(255,255,255,34); }
QPushButton#primary { background: #3d6f5b; border-color: #4f8a71; }
QPushButton#primary:hover { background: #4a8369; }
QPushButton#deny { background: #5a3340; border-color: #7d465a; }
QPushButton#deny:hover { background: #6d3d4d; }
"""


class ApprovalDialog(QtWidgets.QDialog):
    """一张卡片，四个选择。选择结果通过 on_choice(choice) 交回去（once/session/always/deny）。"""

    def __init__(
        self,
        payload,
        on_choice: Callable[[str], None],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_choice = on_choice
        self._answered = False
        self._drag_offset: QtCore.QPoint | None = None  # 按住卡片拖动时的抓点

        # 不抢焦点、不进任务栏，但必须压在其它窗口上面——用户可能正看着别的窗口
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setModal(False)
        self.setStyleSheet(_CARD_STYLE)

        desc = str(getattr(payload, "description", "") or "")
        command = redact(str(getattr(payload, "command", "") or ""))
        friendly = _FRIENDLY.get(desc.strip().lower(), "她要执行一个被标记为危险的操作")

        card = QtWidgets.QFrame(self)
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        title = QtWidgets.QLabel(friendly, card)
        title.setObjectName("title")
        lay.addWidget(title)

        if desc and desc.strip().lower() not in _FRIENDLY:
            sub = QtWidgets.QLabel(desc, card)
            sub.setObjectName("desc")
            lay.addWidget(sub)

        if command:
            box = QtWidgets.QLabel(command[:400] + ("…" if len(command) > 400 else ""), card)
            box.setObjectName("cmd")
            box.setWordWrap(True)
            box.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            lay.addWidget(box)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self._add_button(row, "拒绝", "deny", "deny", card)
        self._add_button(row, "允许一次", "primary", "once", card)
        if bool(getattr(payload, "allow_session", True)):
            self._add_button(row, "本次都允许", "", "session", card)
        if bool(getattr(payload, "allow_permanent", True)):
            self._add_button(row, "以后都允许", "", "always", card)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(max(320, min(460, self.sizeHint().width())))

    def _add_button(self, row, text: str, kind: str, choice: str, parent) -> None:
        btn = QtWidgets.QPushButton(text, parent)
        if kind:
            btn.setObjectName(kind)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self._answer(choice))
        row.addWidget(btn)

    def _answer(self, choice: str) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            self._on_choice(choice)
        finally:
            self.close()

    # 关窗＝拒绝：沉默不是同意，含糊更不是
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 无边框卡片必须自己处理拖动，否则用户没法把它从挡住的内容上挪开。
        # 输入/按钮自己吃掉按下事件，所以点它们不会连带拖窗。
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if not self._answered:
            self._answered = True
            self._on_choice("deny")
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == QtCore.Qt.Key_Escape:
            self._answer("deny")
            return
        super().keyPressEvent(event)

    def show_near(self, anchor: QtCore.QRect | None) -> None:
        """放在锚点（头像窗）旁边；没有锚点就放屏幕中间。"""
        self.move_near(anchor)
        self.show()
        self.raise_()

    def move_near(self, anchor: QtCore.QRect | None) -> None:
        """只挪位置、不动显隐：头像窗被拖动时跟着走。

        尺寸在构造时就 `adjustSize()` 量死了（宽度还固定住），这里再量一遍纯属白算——
        头像窗拖动/缩放时每移动一像素都会调到这里。
        """
        self.move(card_position(anchor, self.size(), screen_area(anchor)))


class ConfirmDialog(QtWidgets.QDialog):
    """一句话说清代价，两个按钮：确认了再动手（删对话这种收不回来的事）。

    外观和窗口标志跟审批卡片同一套（无边框、压在最上面、能拖走）。不用 `QMessageBox`：
    它是系统原生模样，而且没有置顶标志，会被置顶的人物窗盖住。
    """

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
            QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setModal(True)
        self.setStyleSheet(_CARD_STYLE)

        card = QtWidgets.QFrame(self)
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        head = QtWidgets.QLabel(title, card)
        head.setObjectName("title")
        lay.addWidget(head)

        body = QtWidgets.QLabel(description, card)
        body.setObjectName("desc")
        body.setWordWrap(True)
        lay.addWidget(body)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", card)
        cancel.setCursor(QtCore.Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        confirm = QtWidgets.QPushButton(confirm_label, card)
        confirm.setObjectName("deny")  # 破坏性动作：跟审批卡片里的「拒绝」同一个红
        confirm.setCursor(QtCore.Qt.PointingHandCursor)
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
