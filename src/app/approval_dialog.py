"""弹窗：她要动危险东西、或者要删掉一段对话时，停下来问一句。

引擎负责「什么算危险」（smart / manual 策略、被标记的规则），我们只负责问和显示——
所以这里没有任何判定逻辑，只有一颗卡片、四个按钮和一段能看清的原文。

刻意**不抢焦点**：卡片是在用户正打字时弹出来的，抢了焦点下一次敲键盘就落到卡片上
（`WA_ShowWithoutActivating` 让 show() 不切活动窗口）。但键盘并不是废的——用户点了卡片
任何一处之后它就有了焦点，这时 Esc 等于「拒绝」（和关掉卡片一个意思），Enter 不设默认，
免得手快一句回车就批准了危险命令。
"""

from __future__ import annotations

from collections.abc import Callable

import PySide6.QtCore as QtCore
import PySide6.QtWidgets as QtWidgets

from app.redact import redact
from app.window_geometry import card_position, screen_area

_FRIENDLY = {
    "delete file or folder": "她要删掉一个文件或目录",
    "delete via script": "她要用脚本删东西",
    "delete in root path": "她要删除根目录下的东西",
    "pipe remote content to shell": "她要把网上的内容直接交给命令行执行",
    "format drive": "她要格式化磁盘",
    "git force push": "她要强制推送，会改写远端历史",
}

_CARD_STYLE = """
#card { background: rgba(25, 21, 32, 252); border: 1px solid rgba(255,255,255,26); border-radius: 14px; }
#title { color: #f4eaf1; font-size: 15px; font-weight: 600; }
#desc { color: rgba(246,223,234,110); font-size: 13px; }
#cmd {
    color: rgba(232,225,236,200); font-size: 12px; background: rgba(0,0,0,110);
    border: 1px solid rgba(255,255,255,20); border-radius: 8px; padding: 8px;
}
#cmd QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 1px 2px 0; }
#cmd QScrollBar::handle:vertical { background: rgba(255,255,255,48); border-radius: 3px; min-height: 20px; }
#cmd QScrollBar::handle:vertical:hover { background: rgba(255,255,255,86); }
#cmd QScrollBar::add-line:vertical, #cmd QScrollBar::sub-line:vertical { height: 0; }
#cmd QScrollBar::add-page:vertical, #cmd QScrollBar::sub-page:vertical { background: transparent; }
QPushButton {
    color: rgba(246,223,234,180); background: rgba(255,255,255,16); border: 1px solid rgba(255,255,255,26);
    border-radius: 8px; padding: 7px 14px; font-size: 13px;
}
QPushButton:hover { background: rgba(255,255,255,30); }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f7b6d2, stop:1 #c37ba6);
    border-color: transparent; color: #2b1520; font-weight: 600;
}
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffc9de, stop:1 #d18db5); }
QPushButton#deny { background: rgba(255,107,107,14); border-color: rgba(255,107,107,50); color: #ff9a9a; }
QPushButton#deny:hover { background: rgba(255,107,107,34); }
"""


class ApprovalDialog(QtWidgets.QDialog):
    """一张卡片，四个选择。选择结果通过 on_choice(choice) 交回去（once/session/always/deny）。"""

    request_id = ""

    def __init__(
        self,
        payload,
        on_choice: Callable[[str], None],
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
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
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
            sub.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            lay.addWidget(sub)

        command_box = None
        if command:
            box = QtWidgets.QPlainTextEdit(command, card)
            box.setObjectName("cmd")
            box.setReadOnly(True)
            box.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            box.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            lines = command.count("\n") + 1
            box.setFixedHeight(min(240, 20 * lines + 12))
            pal = box.palette()
            pal.setColor(box.backgroundRole(), QtCore.Qt.GlobalColor.transparent)
            box.setPalette(pal)
            lay.addWidget(box)
            command_box = box

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
        if command_box is not None:
            QtCore.QTimer.singleShot(0, lambda: self._fit_command(command_box))

    @staticmethod
    def _fit_command(box: QtWidgets.QPlainTextEdit) -> None:
        """按折行后的实际高度定高。

        命令里完全可以一个换行符都没有，但几百字塞在窄卡片里会折成五六行；
        照换行符个数只给一行高，第二行往后就全被裁掉了。
        """
        metrics = box.fontMetrics()
        inner = max(1, box.viewport().width() - 4)
        rect = metrics.boundingRect(
            QtCore.QRect(0, 0, inner, 100000),
            int(QtCore.Qt.TextFlag.TextWordWrap),
            box.toPlainText(),
        )
        box.setFixedHeight(max(34, min(240, rect.height() + 20)))
        while box.height() < 240 and box.verticalScrollBar().maximum() > 0:
            box.setFixedHeight(box.height() + 4)

    def _add_button(self, row, text: str, kind: str, choice: str, parent) -> None:
        btn = QtWidgets.QPushButton(text, parent)
        if kind:
            btn.setObjectName(kind)
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
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

    def dismiss(self) -> None:
        """引擎自己撤了这张卡：关掉，但**不回任何选择**（不是用户拒绝）。

        走 `close()` 会触发 closeEvent 里的兜底拒绝，等于替用户回了一个已经失效的 reply_id。
        """
        self._answered = True
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt
        """Esc＝拒绝（和关掉卡片同义）。Enter 故意不接：回一句手快的回车不该批准危险命令。"""
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._answer("deny")
            return
        super().keyPressEvent(event)

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
            self._on_choice("deny")
        super().closeEvent(event)

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
