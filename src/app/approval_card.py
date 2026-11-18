"""审批卡片：流里的一块，不是弹出窗。

弹出窗会被窗口级事故弄没——截图、最小化、抢焦点、退出程序，任何一样都能让它从眼前消失；
插在消息流里的卡片不会，它跟别的消息一起吃滚动、吃重画、吃重连恢复。样式照 `clarify_card`
那张卡走：粉底粉边自己画，答完只变灰不收起，卡片高度一点不动，聊天画面不跳。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from app.clarify_card import (
    CARD_BORDER,
    CARD_FILL_BOTTOM,
    CARD_FILL_TOP,
    CONFIRM_QSS,
    DONE_COLOR,
    GHOST_QSS,
    _flush_layout,
)
from app.redact import redact

TITLE_COLOR = "#e8e2f2"
DESC_COLOR = "#b5abc6"

_FRIENDLY = {
    "delete file or folder": "她要删掉一个文件或目录",
    "delete via script": "她要用脚本删东西",
    "delete in root path": "她要删除根目录下的东西",
    "pipe remote content to shell": "她要把网上的内容直接交给命令行执行",
    "format drive": "她要格式化磁盘",
    "git force push": "她要强制推送，会改写远端历史",
}

_CMD_QSS = (
    "QPlainTextEdit{border-radius:8px;font-family:Consolas,Menlo,monospace;font-size:11px;"
    f"color:{DESC_COLOR};background:rgba(255,255,255,6);"
    "border:1px solid rgba(255,255,255,28);padding:7px 9px;}"
)

_DONE_LABELS = {
    "deny": "✕ 已拒绝",
    "once": "✓ 允许一次",
    "session": "✓ 本次都允许",
    "always": "✓ 以后都允许",
}


class ApprovalCard(QtWidgets.QWidget):
    """一张审批卡：标题 / 说明 / 命令 / 选择。答完那行字留在原处，撤回标「已失效」。"""

    answered = QtCore.Signal(str)

    def __init__(self, payload, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ApprovalCard")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self._settled = False
        self._buttons: list[QtWidgets.QPushButton] = []
        self.wheel_target: QtWidgets.QWidget | None = None
        self._done = QtWidgets.QLabel("")
        self._done.setStyleSheet(f"color:{DONE_COLOR};font-size:10.5px;background:transparent;")
        self._done.setMinimumHeight(self._done.fontMetrics().height())
        self._build(payload)

    def _build(self, payload) -> None:
        desc = str(getattr(payload, "description", "") or "")
        command = redact(str(getattr(payload, "command", "") or ""))
        friendly = _FRIENDLY.get(desc.strip().lower(), "她要执行一个被标记为危险的操作")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(0)

        title = QtWidgets.QLabel(friendly)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color:{TITLE_COLOR};font-size:11.5px;font-weight:600;background:transparent;"
        )
        layout.addWidget(title)

        if desc and desc.strip().lower() not in _FRIENDLY:
            sub = QtWidgets.QLabel(desc)
            sub.setWordWrap(True)
            sub.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            sub.setStyleSheet(f"color:{DESC_COLOR};font-size:10.5px;background:transparent;")
            layout.addSpacing(4)
            layout.addWidget(sub)

        if command:
            box = QtWidgets.QPlainTextEdit(command)
            box.setReadOnly(True)
            box.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            box.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            box.setStyleSheet(_CMD_QSS)
            box.setFixedHeight(min(200, max(54, 18 * (command.count("\n") + 1) + 16)))
            layout.addSpacing(8)
            layout.addWidget(box)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch(1)
        layout.addSpacing(9)
        layout.addLayout(row)

        self._add(row, "拒绝", "deny", GHOST_QSS)
        self._add(row, "允许一次", "once", CONFIRM_QSS)
        if bool(getattr(payload, "allow_session", True)):
            self._add(row, "本次都允许", "session", GHOST_QSS)
        if bool(getattr(payload, "allow_permanent", True)):
            self._add(row, "以后都允许", "always", GHOST_QSS)

        layout.addSpacing(7)
        layout.addWidget(self._done)

    def _add(self, row, text: str, choice: str, qss: str) -> None:
        button = QtWidgets.QPushButton(text)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(qss)
        button.clicked.connect(lambda: self._answer(choice))
        row.addWidget(button)
        self._buttons.append(button)

    def _answer(self, choice: str) -> None:
        if self._settled:
            return
        self._settled = True
        for button in self._buttons:
            button.setEnabled(False)
        self._done.setText(_DONE_LABELS.get(choice, "已处理"))
        self.answered.emit(choice)

    def mark_withdrawn(self) -> None:
        """引擎撤回了这一轮：按钮按下去、卡上写清原因，别让它看着还像在等人答。"""
        if self._settled:
            return
        self._settled = True
        for button in self._buttons:
            button.setEnabled(False)
        self._done.setText("这张卡已失效（引擎撤回了这一轮）")

    @property
    def settled(self) -> bool:
        return self._settled

    def fit(self, width: int) -> int:
        """定宽、按内容定高：正文里那块空位要跟卡片一样高（和 `ClarifyCard.fit` 同一份约定）。"""
        width = max(1, int(width))
        self.setFixedWidth(width)
        layout = self.layout()
        if layout is None:
            return 1
        layout.invalidate()
        _flush_layout()
        layout.activate()
        _flush_layout()
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        return max(1, int(height))

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        """滚轮往上滚给聊天窗：卡片盖住了消息流，滚谁都该滚对话。"""
        target = self.wheel_target
        if target is not None:
            QtWidgets.QApplication.sendEvent(target, QtGui.QWheelEvent(event))
            return
        super().wheelEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        """卡片自己画：5px 圆角、5.5% 粉底、1px 粉边（样式表画出来的圆角会漏底，这里手画）。"""
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 5.0, 5.0)
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, CARD_FILL_TOP)
        gradient.setColorAt(1.0, CARD_FILL_BOTTOM)
        painter.fillPath(path, QtGui.QBrush(gradient))
        painter.setPen(QtGui.QPen(CARD_BORDER, 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()
