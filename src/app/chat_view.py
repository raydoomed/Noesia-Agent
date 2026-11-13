"""聊天区的画法。

排版那一层（`QTextDocument`）负责全部位置，这里不算任何矩形，也不额外画东西：

* **气泡**——你发的那一条整条住在一个段框里（`QTextFrame`），底色、上下留白都是框自己的
  属性，铺多宽、铺多高都是排版算的。
* **长内容**——工具输出可能几百行，正文里只留一块等高的空位，真正的文字是独立控件摆在那块
  空位上，超高处自己滚（见 `tool_scroll`）。这里只把控件贴回排版给出的那块地方。
* **时间轴**——左边那条刻度不进文档：正文只按自己的列宽排，刻度是视口外的一列小控件
  （`RailStrip`），按每条消息开头块在排版里的位置画点和时间字。

文档再长，每帧代价也只跟一屏有多少块有关。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

BUBBLE_PAD_Y = 8

SCAFFOLD_IDLE = QtGui.QColor(236, 230, 239, 110)

RAIL_WIDTH = 49
RAIL_OVERHANG = 8
RAIL_LINE = QtGui.QColor(255, 255, 255, 17)
RAIL_LINE_NOW = QtGui.QColor(255, 215, 230, 102)
RAIL_DOT_EDGE = QtGui.QColor(255, 255, 255, 41)
RAIL_DOT = QtGui.QColor(42, 36, 54)
RAIL_TIME = QtGui.QColor(68, 61, 80)
RAIL_NOW = QtGui.QColor(255, 215, 230)
RAIL_NOW_GLOW = QtGui.QColor(255, 215, 230, 33)


class ChatView(QtWidgets.QTextBrowser):
    """排字的那一层：装饰交给文档，位置交给排版。"""

    scrolled = QtCore.Signal()

    BUBBLE_PAD_Y = BUBBLE_PAD_Y
    SCAFFOLD_IDLE = SCAFFOLD_IDLE
    RAIL_WIDTH = RAIL_WIDTH

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._folds = None
        self._rail = None
        self._strip = None
        self._sync_queued = False
        self._sync_folds = False
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().contentsChanged.connect(self._queue_sync)
        self.document().documentLayout().documentSizeChanged.connect(self._queue_sync)

    def attach_folds(self, folds) -> None:
        """把折叠段登记表接进来：长内容要按它们占的空位摆。"""
        self._folds = folds

    def attach_rail(self, provider) -> None:
        """接上时间轴：`provider()` 给 [(开头块, 刻度字, 'you'|'her'|'now'), …]。"""
        self._rail = provider
        if self._strip is None:
            self._strip = RailStrip(self, provider)
        self._strip.resync()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._strip is not None:
            self._strip.resync()
        self._sync_folds = True
        self._queue_sync()

    def _queue_sync(self, *_args) -> None:
        """内容变了：攒到本轮事件处理完再摆，一次插入只摆一遍。"""
        if self._sync_queued:
            return
        self._sync_queued = True
        QtCore.QTimer.singleShot(0, self._run_sync)

    def _run_sync(self) -> None:
        self._sync_queued = False
        with_folds = self._sync_folds
        self._sync_folds = False
        if with_folds and self._folds is not None:
            self._folds.relayout(self.visible_range())
        self._relayout_tools()
        if self._strip is not None:
            self._strip.resync()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        if self._folds is not None:
            self._folds.fit_window(self.visible_range())
        self._relayout_tools()
        # 透明窗口的滚动是搬上一帧的像素，不整屏重画就会留下上一帧的残影。
        self.viewport().update()
        if self._strip is not None:
            self._strip.resync()
        self.scrolled.emit()

    def visible_range(self) -> tuple[int, int]:
        """视口覆盖的字符位区间；拿它筛折叠块和动作条，不碰全文。"""
        blocks = self.visible_blocks()
        if not blocks:
            return (0, 0)
        return (blocks[0].position(), blocks[-1].position() + blocks[-1].length())

    def visible_blocks(self) -> list[QtGui.QTextBlock]:
        """视口里出现的块。文档再长也只碰这几块。"""
        height = max(1, self.viewport().height())
        first = self.cursorForPosition(QtCore.QPoint(1, 1)).block()
        last = self.cursorForPosition(QtCore.QPoint(1, height - 1)).block()
        blocks: list[QtGui.QTextBlock] = []
        block = first
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        return blocks

    def _relayout_tools(self) -> None:
        """长内容按它在正文里占的那块空位摆好；收起时整块不显示。

        只给落在视口里的那几块做几何换算，屏外的藏掉就好：这里挂在每次插入和
        滚动上，工具框一多，每帧全量摆一遍就把滚动拖卡了。
        """
        folds = self._folds
        if folds is None:
            return
        dy = -self.verticalScrollBar().value()
        layout = self.document().documentLayout()
        window = self.visible_range()
        for fold in folds.all():
            widget = getattr(fold, "scroll", None)
            if widget is None:
                continue
            slot = getattr(fold, "slot", None)
            if (
                not fold.open
                or slot is None
                or not slot.isValid()
                or not folds.in_window(fold, window)
            ):
                if widget.isVisible():
                    widget.hide()
                continue
            rect = layout.blockBoundingRect(slot).translated(0, dy)
            if rect.height() <= 0:
                if widget.isVisible():
                    widget.hide()
                continue
            reserve = float(slot.blockFormat().lineHeight())
            if reserve > rect.height():
                rect.setHeight(reserve)
            widget.setGeometry(rect.toRect())
            widget.show()


class RailStrip(QtWidgets.QWidget):
    """时间轴那条刻度：分隔线、时间字、圆点，画在正文左边那条空出来的边距里。

    不参与排版——正文的列宽由视口边距定，这里只在留出来的那一条上画，滚动时跟着换位置。
    """

    def __init__(self, view: "ChatView", provider) -> None:
        super().__init__(view)
        self._view = view
        self._provider = provider
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.resync()

    def resync(self) -> None:
        """滚动、排版、换窗大小之后重新摆一次：位置跟着视口走，内容自己重画。

        横着多留出一点：圆点骑在竖线上，右边那半个要落在留白里，不能叫自己的框裁掉。
        """
        self.setGeometry(0, 0, RAIL_WIDTH + RAIL_OVERHANG, self._view.height())
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        view = self._view
        rows = self._provider()
        painter = QtGui.QPainter(self)
        line_x = RAIL_WIDTH - 1
        painter.setPen(QtGui.QPen(RAIL_LINE, 1))
        painter.drawLine(line_x, 0, line_x, self.height())
        if not rows:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        layout = view.document().documentLayout()
        offset = view.viewport().geometry().top() - view.verticalScrollBar().value()
        marks: list[tuple[float, float, str, str]] = []
        for block, label, kind in rows:
            if block is None or not block.isValid():
                continue
            rect = layout.blockBoundingRect(block)
            top = rect.top() + offset
            if top < -30 or top > self.height() + 30:
                continue
            marks.append((top, rect.height(), label, kind))
        for index, (top, height, _label, kind) in enumerate(marks):
            if kind != "now":
                continue
            end = marks[index + 1][0] if index + 1 < len(marks) else min(top + max(height, 22.0), self.height())
            painter.setPen(QtGui.QPen(RAIL_LINE_NOW, 1))
            painter.drawLine(line_x, int(top), line_x, int(end))
        font = QtGui.QFont(view.font())
        font.setPixelSize(9)
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.4)
        painter.setFont(font)
        for top, _height, label, kind in marks:
            dot = QtCore.QPointF(line_x, top + 8)
            if kind == "now":
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(RAIL_NOW_GLOW)
                painter.drawEllipse(dot, 7.5, 7.5)
                painter.setBrush(RAIL_NOW)
                painter.drawEllipse(dot, 3.5, 3.5)
            else:
                painter.setPen(QtGui.QPen(RAIL_DOT_EDGE, 1))
                painter.setBrush(RAIL_DOT)
                painter.drawEllipse(dot, 3.5, 3.5)
            if not label:
                continue
            painter.setPen(RAIL_NOW if kind == "now" else RAIL_TIME)
            painter.drawText(
                QtCore.QRectF(0, top, line_x - 9, 16),
                int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter),
                label,
            )
