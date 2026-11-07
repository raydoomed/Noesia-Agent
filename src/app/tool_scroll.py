"""工具输出的长内容：高度封顶，里面的滚动条翻剩下的。

一段 diff 动辄几百行，直接铺进正文会把整屏顶穿；文档又是流水排下来的，
块里没有"自己的滚动区"。所以这类内容不进文档流——正文里只留一块等高的空位，
真正的文字放进出这个控件，摆在那块空位上，超高处自己滚。

外形：5px 圆角 + 1px 描边，等宽 11.2px、行高 18、内边距 6/8，最高 224px。
"""

from PySide6 import QtCore, QtGui, QtWidgets

SHELL_RADIUS = 5
PAD_V = 6
PAD_H = 8
MAX_HEIGHT = 224
LINE_HEIGHT = 18
FONT_PX = 11.2

_SHELL_QSS = f"""
QTextBrowser {{
    background: rgba(236, 230, 239, 14);
    border: 1px solid rgba(236, 230, 239, 30);
    border-radius: {SHELL_RADIUS}px;
    padding: {PAD_V}px {PAD_H}px;
    color: rgba(236, 230, 239, 150);
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: {FONT_PX}px;
    selection-background-color: rgba(243, 172, 206, 70);
}}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px 1px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(236, 230, 239, 46);
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(236, 230, 239, 86);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


class ToolScroll(QtWidgets.QTextBrowser):
    """一段可以自己滚的工具输出。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._text_width = 0
        self._narrowed = False
        self.setReadOnly(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setStyleSheet(_SHELL_QSS)
        self.viewport().setAutoFillBackground(False)

    def set_rows(self, rows: list[tuple[str, QtGui.QColor | None, QtGui.QColor | None]]) -> None:
        """按行换内容；每行是（文字、前景色、底色），缺的给 None 用默认。"""
        self.clear()
        self._narrowed = False
        cursor = QtGui.QTextCursor(self.document())
        cursor.beginEditBlock()
        for index, (text, color, bg) in enumerate(rows):
            if index:
                cursor.insertBlock()
            self._insert_line(cursor, text, color, bg)
        cursor.endEditBlock()
        self._apply_line_height()
        self.verticalScrollBar().setValue(0)

    def add_rows(self, rows: list[tuple[str, QtGui.QColor | None, QtGui.QColor | None]]) -> None:
        """往后接几行，已经有的不动。

        整段重铺看着简单，但每来一行就把前面全清掉重排一遍，越到后面越慢：
        逐行推 200 行要两秒，界面跟着卡。
        """
        if not rows:
            return
        cursor = QtGui.QTextCursor(self.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        start = cursor.position() - 1
        has_content = self.document().characterCount() > 1
        for index, (text, color, bg) in enumerate(rows):
            if has_content or index:
                cursor.insertBlock()
            self._insert_line(cursor, text, color, bg)
        cursor.endEditBlock()
        self._apply_line_height(start)

    @staticmethod
    def _insert_line(
        cursor: QtGui.QTextCursor,
        text: str,
        color: QtGui.QColor | None,
        bg: QtGui.QColor | None = None,
    ) -> None:
        fmt = QtGui.QTextCharFormat()
        if color is not None and color.isValid():
            fmt.setForeground(color)
        cursor.insertText(text, fmt)
        block_fmt = cursor.blockFormat()
        if bg is not None and bg.isValid():
            block_fmt.setBackground(bg)
        else:
            block_fmt.clearBackground()
        cursor.setBlockFormat(block_fmt)

    def _apply_line_height(self, start: int = -1) -> None:
        """行距按 18px 固定——默认那点行距几行字就糊成一片。

        `start` 给位置就只摆从那块起往后的，新接的几行不必把前面全走一遍。
        """
        block = self.document().begin() if start < 0 else self.document().findBlock(start)
        while block.isValid():
            fmt = block.blockFormat()
            fmt.setLineHeight(
                LINE_HEIGHT, QtGui.QTextBlockFormat.LineHeightTypes.FixedHeight.value
            )
            QtGui.QTextCursor(block).setBlockFormat(fmt)
            block = block.next()

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        """滚到头了就把滚轮交回去：不然鼠标停在这块上，整页就滚不动了。"""
        bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        at_top = bar.value() <= bar.minimum() and delta > 0
        at_bottom = bar.value() >= bar.maximum() and delta < 0
        if bar.maximum() <= 0 or at_top or at_bottom:
            event.ignore()
            return
        super().wheelEvent(event)

    def fit(self, width: int) -> int:
        """定宽、按内容定高（封顶），返回最终高度。"""
        text_width = max(1, width - PAD_H * 2 - 2)
        if text_width != self._text_width:
            self._text_width = text_width
            self.setFixedWidth(max(1, width))
            self.document().setTextWidth(text_width)
        height = int(self.document().size().height()) + PAD_V * 2 + 2
        if height > MAX_HEIGHT and not self._narrowed:
            self._narrowed = True
            self._text_width = max(1, text_width - 6)
            self.document().setTextWidth(self._text_width)
            height = int(self.document().size().height()) + PAD_V * 2 + 2
        self.setFixedHeight(min(height, MAX_HEIGHT))
        if self.verticalScrollBar().value() <= 0:
            QtCore.QTimer.singleShot(0, self._rewind)
        return self.height()

    def _rewind(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
