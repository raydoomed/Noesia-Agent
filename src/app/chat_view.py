"""聊天区的画法。

排版那一层（`QTextDocument`）负责全部位置，这里不算任何矩形，也不额外画东西：

* **气泡**——你发的那一条整条住在一个段框里（`QTextFrame`），底色、上下留白都是框自己的
  属性，铺多宽、铺多高都是排版算的。
* **长内容**——工具输出可能几百行，正文里只留一块等高的空位，真正的文字是独立控件摆在那块
  空位上，超高处自己滚（见 `tool_scroll`）。这里只把控件贴回排版给出的那块地方。

文档再长，每帧代价也只跟一屏有多少块有关。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

BUBBLE_PAD_Y = 8

SCAFFOLD_IDLE = QtGui.QColor(236, 230, 239, 110)


class ChatView(QtWidgets.QTextBrowser):
    """排字的那一层：装饰交给文档，位置交给排版。"""

    scrolled = QtCore.Signal()

    BUBBLE_PAD_Y = BUBBLE_PAD_Y
    SCAFFOLD_IDLE = SCAFFOLD_IDLE

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._folds = None
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

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_folds = True
        self._queue_sync()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        if self._folds is not None:
            self._folds.fit_window(self.visible_range())
        self._relayout_tools()
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
            widget.setGeometry(rect.toRect())
            widget.show()
