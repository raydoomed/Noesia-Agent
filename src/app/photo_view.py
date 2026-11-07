"""看原图的窗口：没有标题栏，窗口里就是这张图。

正文里的图是缩略图，点开这里看原图；原图字节还在手上就直接上原图，
不在（缓存已经松手）就先显示缩略图顶着，同时去把它取回来。
只看不存——想存用正文里缩略图右上角那个下载。
关闭按钮浮在右下角，不占图片的地方。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_BUTTON_STYLE = """
QPushButton {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    background: rgba(18, 20, 26, 190);
    color: #e6e8ef;
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 6px;
    padding: 5px 12px;
}
QPushButton:hover { background: rgba(40, 44, 56, 220); }
"""

_HINT_STYLE = """
font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
color: #dfe3ec;
background: rgba(18, 20, 26, 170);
border-radius: 5px;
padding: 3px 8px;
"""


class PhotoView(QtWidgets.QDialog):
    """一张图的大窗：没有标题栏，只看原图。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "图片")
        self.setWindowFlags(
            QtCore.Qt.WindowType.Dialog
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self._drag: QtCore.QPoint | None = None
        self._dragged = False

        self._photo = QtWidgets.QLabel()
        self._photo.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._photo.setMinimumSize(240, 160)
        self._photo.setStyleSheet("background:#191a20; color:#8f9bb0;")

        self._hint = QtWidgets.QLabel("")
        self._hint.setStyleSheet(_HINT_STYLE)
        self._hint.hide()

        close = QtWidgets.QPushButton("关闭")
        close.setStyleSheet(_BUTTON_STYLE)
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close)

        corner = QtWidgets.QHBoxLayout()
        corner.setContentsMargins(0, 0, 10, 10)
        corner.setSpacing(6)
        corner.addStretch(1)
        corner.addWidget(close)

        box = QtWidgets.QGridLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self._photo, 0, 0)
        box.addLayout(
            corner,
            0,
            0,
            QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignRight,
        )
        box.addWidget(
            self._hint,
            0,
            0,
            QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignLeft,
        )


    def set_photo(self, pixmap: QtGui.QPixmap, pending: bool = False) -> None:
        """把图放上去。`pending` 表示原图还在路上，先拿缩略图顶着。"""
        self._say("原图还在取，先看缩略图" if pending else "")

        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        shown = pixmap
        if screen is not None:
            area = screen.availableGeometry()
            room = QtCore.QSize(int(area.width() * 0.8), int(area.height() * 0.8))
            if pixmap.width() > room.width() or pixmap.height() > room.height():
                shown = pixmap.scaled(
                    room,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
        self._photo.setMinimumSize(1, 1)
        self._photo.setPixmap(shown)
        self.resize(shown.size())
        if not self._dragged:
            self._center()

    def set_failed(self, text: str) -> None:
        """原图没取回来：说清楚，缩略图当时什么样就还是什么样。"""
        pixmap = self._photo.pixmap()
        if pixmap is not None and not pixmap.isNull():
            self._say(text)
            return
        self._photo.setText(text)
        self._say("")

    def set_waiting(self, text: str) -> None:
        """连缩略图都没有：先在这儿说明在取，原图回来会直接换上。"""
        self._photo.setText(text)
        self._say("")

    def _say(self, text: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))


    def _center(self) -> None:
        """摆到屏幕正中间。

        别让 Qt 替我们摆：父窗口是聊天窗，它蹲在屏幕一角，跟着父窗口居中只会把大图顶到边上去。
        用户点的是哪块屏幕上的图，就摆在哪块屏幕中间。
        """
        screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.center().x() - self.width() // 2,
            area.center().y() - self.height() // 2,
        )

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._dragged:
            self._center()


    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() is QtCore.Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._dragged = True
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
