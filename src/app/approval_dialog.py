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
#card { background: rgba(25, 21, 32, 252); border: 1px solid rgba(255,255,255,26); border-radius: 14px; }
#title { color: #f4eaf1; font-size: 15px; font-weight: 600; }
#desc { color: rgba(246,223,234,110); font-size: 13px; }
#cmd {
    color: rgba(232,225,236,200); font-size: 12px; background: rgba(0,0,0,110);
    border: 1px solid rgba(255,255,255,20); border-radius: 8px; padding: 8px;
}
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

        # 不抢焦点、不进任务栏，但必须压在其它窗口上面——用户可能正看着别的窗口。
        # 不给 `WindowDoesNotAcceptFocus`：那样连点了之后键盘也进不来，Esc / 方向键全哑，
        # 与桌面端那张卡不一致（它是一张普通的可聚焦卡片）。不抢活动窗口由下面的属性保证。
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
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
            # 纯文本：说明里带 `<...>`（重定向、占位符、HTML 片段）时，AutoText 会把它们
            # 当标签吃掉，用户看到的不是真正的内容
            sub.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            lay.addWidget(sub)

        if command:
            # 整条命令原样给出，不截断：`python -c "…"`、heredoc、批量脚本、ffmpeg 长参数
            # 轻松超过 400 字，截掉一半等于让人凭前半句批准整条命令；手选也选不到被砍掉的部分。
            box = QtWidgets.QPlainTextEdit(command, card)
            box.setObjectName("cmd")
            box.setReadOnly(True)  # 只读但可选中、可滚动：长命令自己滚，卡片不撑破
            box.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            box.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            lines = command.count("\n") + 1
            box.setFixedHeight(min(240, 20 * lines + 12))
            # 别用控件级样式表（会盖掉 `#cmd` 那条边框/配色）：只把底色设成透明
            pal = box.palette()
            pal.setColor(box.backgroundRole(), QtCore.Qt.GlobalColor.transparent)
            box.setPalette(pal)
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
    def dismiss(self) -> None:
        """引擎自己撤了这张卡：关掉，但**不回任何选择**（不是用户拒绝）。

        走 `close()` 会触发 closeEvent 里的兜底拒绝，等于替用户回了一个已经失效的 reply_id。
        """
        self._answered = True
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """Esc＝拒绝（和关掉卡片同义）。Enter 故意不接：回一句手快的回车不该批准危险命令。"""
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._answer("deny")
            return
        super().keyPressEvent(event)

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
        head.setTextFormat(QtCore.Qt.TextFormat.PlainText)  # 同审批卡：不把 <...> 当标签吃掉
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
