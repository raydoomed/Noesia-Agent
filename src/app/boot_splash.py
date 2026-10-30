"""启动时的加载动画。

引擎是独立进程，冷启动实测 5–7 秒。以前的做法是先把人物窗口亮出来，等用户点开聊天才
发现「还在加载」——看着像卡住。现在改成：先只显示这层小动画，引擎连上、模型清单到了
才露出人物窗口；那之后再打开聊天窗就是即时的。
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

TITLE = "Noesia Agent"
HINT = "准备就绪后窗口才会出现，稍等一下"


class BootSplash(QtWidgets.QWidget):
    """无边框小卡片：转圈 + 当前正在做什么。

    只属于启动阶段：`finish()` 之后就销毁，不参与后面的任何逻辑。
    """

    MIN_VISIBLE_MS = 500  # 引擎早就在跑时也别一闪而过，看着像闪屏

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setFixedSize(360, 200)
        self._step = "正在准备…"
        self._failed = False
        self._angle = 0
        self._started = time.monotonic()
        self._last_emit = 0.0  # 给探针/日志用：最近一次「步骤变了」
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)  # 30fps 够用，别跟待机视频抢
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._center_on_screen()

    # ---- 对外 ----
    def set_step(self, text: str) -> None:
        """换一句「现在在做什么」。界面上只留最新一句，不堆历史。"""
        self._step = text
        self.update()

    @property
    def step(self) -> str:
        return self._step

    def mark_failed(self, text: str) -> None:
        """引擎没起来：动画停住，说明原因，让用户知道窗口马上还是会出来。"""
        self._failed = True
        self._step = text
        self._timer.stop()
        self.update()

    def finish(self, then) -> None:
        """收起动画，然后执行 `then`（露出人物窗口）。太早完成就等到最短时长。"""
        waited = (time.monotonic() - self._started) * 1000
        delay = max(0, int(self.MIN_VISIBLE_MS - waited))
        if delay <= 0:
            then()
            return
        QtCore.QTimer.singleShot(delay, then)

    # ---- 内部 ----
    def _center_on_screen(self) -> None:
        screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.center() - self.rect().center())

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = QtCore.QRectF(4, 4, self.width() - 8, self.height() - 8)

        card = QtGui.QPainterPath()
        card.addRoundedRect(rect, 18, 18)
        painter.fillPath(card, QtGui.QColor(16, 18, 24, 235))
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 34), 1))
        painter.drawPath(card)

        # 转圈：留出口子的一段弧，比三颗跳动的点安静
        ring = QtCore.QRectF(self.width() / 2 - 17, 44, 34, 34)
        painter.setPen(QtGui.QPen(QtGui.QColor(154, 208, 255, 80), 3))
        painter.drawEllipse(ring)
        colour = QtGui.QColor(255, 120, 120, 200) if self._failed else QtGui.QColor(154, 208, 255, 230)
        painter.setPen(QtGui.QPen(colour, 3, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        painter.drawArc(ring, int((90 - self._angle) * 16), int(110 * 16))

        painter.setPen(QtGui.QColor(255, 255, 255, 240))
        title = painter.font()
        title.setPointSize(15)
        title.setBold(True)
        painter.setFont(title)
        painter.drawText(
            QtCore.QRectF(0, 92, self.width(), 26), QtCore.Qt.AlignHCenter, TITLE
        )

        body = painter.font()
        body.setPointSize(10)
        body.setBold(False)
        painter.setFont(body)
        painter.setPen(QtGui.QColor(255, 255, 255, 190))
        painter.drawText(
            QtCore.QRectF(20, 122, self.width() - 40, 22),
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter,
            self._step,
        )
        painter.setPen(QtGui.QColor(255, 255, 255, 110))
        painter.drawText(
            QtCore.QRectF(20, 148, self.width() - 40, 22),
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter,
            HINT,
        )
        painter.end()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)
