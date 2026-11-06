"""聊天区的画法：谁的话，长什么样。

两层东西需要自己画或者自己摆：

* **气泡**——你发的那一条是唯一带填充的块。`QTextBrowser` 只排字，这里在它把字
  画上去之前先垫一层圆角矩形，所以选中、链接、双击选词这些还是它原生的行为。
* **长内容**——工具输出可能几百行，正文里只留一块等高的空位，真正的文字是独立
  控件摆在那块空位上，超高处自己滚（见 `tool_scroll`）。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

#: 气泡：圆角、内边距。文字左右各让 12，气泡边正好落在视口边上。
BUBBLE_RADIUS = 12
BUBBLE_PAD_X = 12
BUBBLE_PAD_Y = 8
BUBBLE_FILL = QtGui.QColor(243, 172, 206, 44)
BUBBLE_EDGE = QtGui.QColor(243, 172, 206, 72)

#: 过程行（「思考了片刻」、工具抬头）的静止/悬停灰。
SCAFFOLD_IDLE = QtGui.QColor(236, 230, 239, 110)
SCAFFOLD_HOVER = QtGui.QColor(236, 230, 239, 163)


class ChatView(QtWidgets.QTextBrowser):
    """排字的那一层。气泡自己画，长内容自己摆。"""

    scrolled = QtCore.Signal()  # 正文滚了：贴在字上的那些小按钮位置就作废了

    BUBBLE_RADIUS = BUBBLE_RADIUS
    BUBBLE_PAD_X = BUBBLE_PAD_X
    BUBBLE_PAD_Y = BUBBLE_PAD_Y
    SCAFFOLD_IDLE = SCAFFOLD_IDLE
    SCAFFOLD_HOVER = SCAFFOLD_HOVER

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._bubbles: list[QtCore.QRectF] = []
        self._bubbles_stale = True
        self._folds = None
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(self._mark_stale)
        self.document().contentsChanged.connect(self._mark_stale)

    # ---- 外部接线 ----

    def attach_folds(self, folds) -> None:
        """把折叠段登记表接进来：长内容要按它们占的空位摆。"""
        self._folds = folds

    # ---- 气泡 ----

    def _mark_stale(self, *_args) -> None:
        self._bubbles_stale = True

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._bubbles_stale = True
        if self._folds is not None:
            # 宽度变了：里面滚着的那些得按新宽度重新量，不然还按老宽度占着地方
            self._folds.relayout()
        self._relayout_tools()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        # 字挪了，挂在字上的控件要跟着挪；挂在字上的小按钮索性收掉，等鼠标再过来
        self._relayout_tools()
        self.scrolled.emit()

    def _text_rect(self, block: QtGui.QTextBlock) -> QtCore.QRectF:
        """这一块里文字占的框（文档坐标）。"""
        layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            return QtCore.QRectF()
        left = right = None
        for index in range(layout.lineCount()):
            line = layout.lineAt(index)
            if line.textLength() <= 0:
                continue
            start = line.x()
            end = start + line.width()
            left = start if left is None else min(left, start)
            right = end if right is None else max(right, end)
        if left is None or right is None:
            return QtCore.QRectF()
        rect = QtCore.QRectF(left, block.layout().boundingRect().top(), right - left,
                             block.layout().boundingRect().height())
        rect.translate(0, self.document().documentLayout().blockBoundingRect(block).top())
        return rect

    def _collect_bubbles(self) -> None:
        """把每条「你的话」的块合成一个气泡框。一块消息就是一块。"""
        rects: list[QtCore.QRectF] = []
        block = self.document().begin()
        while block.isValid():
            data = block.userData()
            msg = getattr(data, "msg", None)
            if msg is not None and msg.get("role") == "user":
                rect = self._text_rect(block)
                if not rect.isNull():
                    rects.append(rect.adjusted(-BUBBLE_PAD_X, -BUBBLE_PAD_Y,
                                               BUBBLE_PAD_X, BUBBLE_PAD_Y))
            block = block.next()
        self._bubbles = rects
        self._bubbles_stale = False

    def bubble_rect(self, block: QtGui.QTextBlock) -> QtCore.QRectF | None:
        """这条消息的气泡框（视口坐标）。悬停按钮要摆进气泡里，得知道它画在哪儿。"""
        if self._bubbles_stale:
            self._collect_bubbles()
        index = -1
        probe = self.document().begin()
        while probe.isValid():
            msg = getattr(probe.userData(), "msg", None)
            if msg is not None and msg.get("role") == "user":
                index += 1
                if probe == block:
                    break
            probe = probe.next()
        if not 0 <= index < len(self._bubbles):
            return None
        return self._bubbles[index].translated(0, -self.verticalScrollBar().value())

    # ---- 长内容 ----

    def _relayout_tools(self) -> None:
        """长内容按它在正文里占的那块空位摆好；收起时整块不显示。"""
        folds = self._folds
        if folds is None:
            return
        dy = -self.verticalScrollBar().value()
        layout = self.document().documentLayout()
        for fold in folds.all():
            widget = getattr(fold, "scroll", None)
            if widget is None:
                continue
            slot = getattr(fold, "slot", None)
            if not fold.open or slot is None or not slot.isValid():
                widget.hide()
                continue
            rect = layout.blockBoundingRect(slot).translated(0, dy)
            if rect.height() <= 0:
                widget.hide()
                continue
            widget.setGeometry(rect.toRect())
            widget.show()

    # ---- 画 ----

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        if self._bubbles_stale:
            self._collect_bubbles()
        dy = -self.verticalScrollBar().value()
        painter = QtGui.QPainter(self.viewport())
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        for rect in self._bubbles:
            box = rect.translated(0, dy)
            if not box.intersects(QtCore.QRectF(event.rect())):
                continue
            painter.setPen(QtGui.QPen(BUBBLE_EDGE, 1))
            painter.setBrush(BUBBLE_FILL)
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5),
                                    BUBBLE_RADIUS, BUBBLE_RADIUS)
        painter.end()
        super().paintEvent(event)
        self._relayout_tools()
