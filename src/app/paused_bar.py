"""这场会话被上一次重启打断时的横幅：说清为什么不动，并给一个「丢弃那一轮」的出口。

引擎对「owner 死在半路、那一轮到底跑没跑完不清楚」的处理是不重放：那行认领留在库里，
整条队列等人拍板（`prompt.resolve_unknown`），拍板之后后面的消息接着跑。外壳原来没有
这个动作，表现出来的就是「发了消息没反应」。这条横幅补上它。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtWidgets as QtWidgets


class PausedBar(QtWidgets.QFrame):
    """一条会自己藏起来的横幅：没有卡住的轮次时完全不占地方。"""

    discard_requested = QtCore.Signal()

    _FULL_TEXT = "上一轮在重启时被打断了，后面的消息都在排队"

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pausedbar")
        self.setVisible(False)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 3, 6, 3)
        row.setSpacing(8)

        self._title = QtWidgets.QLabel(self._FULL_TEXT, self)
        self._title.setObjectName("pausedtitle")
        self._title.setToolTip(self._FULL_TEXT)
        row.addWidget(self._title, 1)

        self._discard = QtWidgets.QToolButton(self)
        self._discard.setObjectName("icon")
        self._discard.setText("丢弃那一轮")
        self._discard.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._discard.setToolTip("那一轮的正文不会重发，排着的消息接着跑")
        self._discard.clicked.connect(self._on_discard)
        row.addWidget(self._discard)

    def set_paused(self, lost: object) -> None:
        """引擎的排队清单里有一行丢了：露出来；没有了：收回去。"""
        if not lost:
            self.hide()
            self._release()
            return
        self._release()
        self.show()

    def discard_failed(self) -> None:
        self._release()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        width = self._title.width()
        if width <= 0:
            return
        self._title.setText(self._title.fontMetrics().elidedText(
            self._FULL_TEXT, QtCore.Qt.TextElideMode.ElideRight, width))

    def _release(self) -> None:
        self._discard.setEnabled(True)
        self._discard.setText("丢弃那一轮")

    def _on_discard(self) -> None:
        self._discard.setEnabled(False)
        self._discard.setText("正在丢弃…")
        self.discard_requested.emit()
