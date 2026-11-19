"""定时任务投递进当前会话时，从聊天区顶部滑下的一张通知卡。

浮在正文之上：不进消息流、不进历史窗口、不占布局——滑下来的那一下不挤动正在进行的
这轮对话（把投递行并进历史那条路会把一轮拆成两个用户消息，这条卡就是替代它的）。
4 秒自己滑回去，右上角 × 直接关。
"""

from __future__ import annotations

import time

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

from app.hover_actions import svg_icon

MAX_W = 420
MARGIN = 14
TOP = 10
SHOW_MS = 220
HIDE_MS = 180
STAY_MS = 4000
ACCENT = "#f0a9c4"

STYLE = """
QFrame#crontoast {
    background: #241e1c;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 13px;
}
QLabel#toasticon {
    background: rgba(240, 169, 196, 0.16);
    border: 1px solid rgba(240, 169, 196, 0.30);
    border-radius: 9px;
}
QLabel#toasttitle { color: rgba(255, 255, 255, 0.88); font-size: 13px; font-weight: 600; }
QLabel#toastbody { color: rgba(255, 255, 255, 0.42); font-size: 11.5px; }
QLabel#toasttime { color: rgba(255, 255, 255, 0.34); font-size: 10.5px; }
QToolButton#toastclose {
    border: none; background: transparent; color: rgba(255, 255, 255, 0.34);
    font-size: 14px; padding: 0 2px;
}
QToolButton#toastclose:hover { color: rgba(255, 255, 255, 0.78); }
"""


class CronToast(QtWidgets.QFrame):
    """一条「定时任务已完成」的悬浮卡：滑下来 → 停 4 秒 → 自己滑回去。"""

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("crontoast")
        self.setStyleSheet(STYLE)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hide()

        self._hiding = False
        self._slide = QtCore.QPropertyAnimation(self, b"pos", self)
        self._slide.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._slide.finished.connect(self._slide_done)
        self._stay = QtCore.QTimer(self)
        self._stay.setSingleShot(True)
        self._stay.timeout.connect(self.dismiss)

        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 10)
        shadow.setColor(QtGui.QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(12, 10, 10, 10)
        row.setSpacing(10)

        self._icon = QtWidgets.QLabel(self)
        self._icon.setObjectName("toasticon")
        self._icon.setFixedSize(30, 30)
        self._icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._icon.setPixmap(svg_icon("clock", ACCENT, 16).pixmap(16, 16))

        body = QtWidgets.QVBoxLayout()
        body.setSpacing(2)
        self._title = QtWidgets.QLabel("定时任务已完成", self)
        self._title.setObjectName("toasttitle")
        self._body = QtWidgets.QLabel("", self)
        self._body.setObjectName("toastbody")
        # 任务名可能有长有短：给多少宽度画多少，超出用省略号，不许顶掉右边的时间。
        self._body.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self._body_full = ""
        body.addWidget(self._title)
        body.addWidget(self._body)

        self._time = QtWidgets.QLabel("", self)
        self._time.setObjectName("toasttime")
        self._time.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop)

        close = QtWidgets.QToolButton(self)
        close.setObjectName("toastclose")
        close.setText("×")
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.setAutoRaise(True)
        close.clicked.connect(self.dismiss)

        row.addWidget(self._icon)
        row.addLayout(body, 1)
        row.addWidget(self._time)
        row.addWidget(close)
        self.adjustSize()

    # ---- 对外 ----

    def show_notice(self, job_name: str) -> None:
        """新来一条投递：换字、从上面滑下来、重新计时（同时来两条只留后一条）。"""
        name = (job_name or "").strip()
        self._body_full = f"「{name}」" if name else "已投递到当前会话"
        self._body.setText(self._body_full)  # 先摆上整句，宽度排好后统一按宽度截断
        self._time.setText(time.strftime("%H:%M"))
        self._hiding = False
        self._slide.stop()
        rest = self._rest_pos()
        self.show()
        self.raise_()
        self.move(QtCore.QPoint(rest.x(), -self.height()))
        QtCore.QTimer.singleShot(0, self._elide_body)
        self._animate(rest, SHOW_MS)
        self._stay.start(STAY_MS)

    def dismiss(self) -> None:
        """滑回框外再藏起来（× 和到点都走这儿）。"""
        if self._hiding or not self.isVisible():
            return
        self._hiding = True
        self._stay.stop()
        self._animate(QtCore.QPoint(self.x(), -self.height()), HIDE_MS)

    def relayout(self) -> None:
        """聊天区尺寸变了：宽度和居中位置重算，副标题按新宽度重截（滑行中不动，等它自己落位）。"""
        rest = self._rest_pos()
        self._elide_body()
        if self.isVisible() and not self._hiding and self._slide.state() != QtCore.QAbstractAnimation.State.Running:
            self.move(rest)

    # ---- 内部 ----

    def _animate(self, end: QtCore.QPoint, duration: int) -> None:
        self._slide.stop()
        self._slide.setDuration(duration)
        self._slide.setStartValue(self.pos())
        self._slide.setEndValue(end)
        self._slide.start()

    def _slide_done(self) -> None:
        if self._hiding:
            self._hiding = False
            self.hide()

    def _elide_body(self) -> None:
        """副标题按实际宽度截断：任务名再长也不去挤右边的时间和 ×。"""
        width = self._body.width()
        if width < 40:  # 还没排版，等下一次重排再来
            return
        metrics = self._body.fontMetrics()
        self._body.setText(metrics.elidedText(self._body_full, QtCore.Qt.TextElideMode.ElideRight, width))

    def _rest_pos(self) -> QtCore.QPoint:
        """落位：宽度跟着聊天区收放，横向居中、贴着顶部。"""
        host = self.parentWidget()
        if host is None:
            return QtCore.QPoint(0, TOP)
        width = min(MAX_W, max(220, host.width() - 2 * MARGIN))
        self.setFixedWidth(width)
        self.adjustSize()
        return QtCore.QPoint((host.width() - width) // 2, TOP)
