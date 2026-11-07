"""念出来那条进度：正在合成 / 正在朗读 + 播放进度 + 单独的停止键。

摆的位置和桌面端一样——输入框正上方单独一条。它只管「声音」这一件事：
按这里的停止只掐语音，不动这一轮对话（要叫住整轮用发送键的「停止」）。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


def _clock(ms: int) -> str:
    seconds = max(int(ms), 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class SpeechBar(QtWidgets.QFrame):
    """一条会自己藏起来的横幅：没人念的时候完全不占地方。"""

    stop_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("speechbar")
        self.setVisible(False)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 3, 6, 3)
        row.setSpacing(8)

        self._title = QtWidgets.QLabel("正在准备音频", self)
        self._title.setObjectName("speechtitle")
        row.addWidget(self._title, 1)

        self._progress = QtWidgets.QProgressBar(self)
        self._progress.setObjectName("speechprogress")
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setFixedWidth(120)
        row.addWidget(self._progress)

        self._clock = QtWidgets.QLabel("", self)
        self._clock.setObjectName("speechclock")
        row.addWidget(self._clock)

        self._stop = QtWidgets.QToolButton(self)
        self._stop.setObjectName("icon")
        self._stop.setText("停止")
        self._stop.setCursor(QtCore.Qt.PointingHandCursor)
        self._stop.setToolTip("只停这句朗读，不动这一轮对话")
        self._stop.clicked.connect(self.stop_requested.emit)
        row.addWidget(self._stop)


    def show_preparing(self) -> None:
        self._title.setText("正在准备音频")
        self._progress.setRange(0, 0)
        self._clock.setText("")
        self.setVisible(True)

    def show_playing(self) -> None:
        self._title.setText("正在朗读")
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._clock.setText("")
        self.setVisible(True)

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        if not self.isVisible():
            return
        if duration_ms <= 0:
            self._progress.setRange(0, 0)
            self._clock.setText(_clock(position_ms))
            return
        self._progress.setRange(0, 1000)
        self._progress.setValue(min(1000, max(0, int(position_ms * 1000 / duration_ms))))
        self._clock.setText(f"{_clock(position_ms)} / {_clock(duration_ms)}")

    def dismiss(self) -> None:
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self.hide()
