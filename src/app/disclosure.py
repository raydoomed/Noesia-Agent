"""可折叠的一段：一行抬头 + 藏在它下面的若干段。

一条过程行——「思考中」、「读取 config.py」——永远是同一副样子：一行暗色小字，
点一下才展开底下的内容。Qt 的文档流里没有「折叠」这种东西，但段落能单独隐藏
（`QTextBlock.setVisible(False)`，实测 106px 的正文藏起来后只剩 8px 的抬头），
于是抬头占一段、正文是紧接着的若干段，点抬头就切换那些段的可见性。

**正文块自己带标记**（`QTextBlockUserData`），不靠字符位置记账。位置账在插入文本、改抬头、
别的折叠段插进来之后一定会漂：抬头从「思考中」换成「思考了 3s」、工具跑完补一段结果，
后面所有位置整体平移，按旧位置去藏就会藏到别人头上（表现为「折叠着却露着一段正文」）。
块标记跟着块走，插入多少内容都不会错位，所以本模块只管标记、不管位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

from app.tool_scroll import ToolScroll

CARET_OPEN = "▾"
CARET_CLOSED = "▸"


def head_text(label: str, open_: bool) -> str:
    """抬头那一行的原文：箭头 + 标签（箭头跟着开合翻）。"""
    return f"{CARET_OPEN if open_ else CARET_CLOSED} {label}"


class _BodyMark(QtGui.QTextBlockUserData):
    """「这一块是某个折叠段的正文」——块自己记着属于谁，不依赖字符位置。

    `uid` 是这一块自己的编号：卡片要**原地改某一行**（子代理的活动行）时得先找回那一块，
    按 uid 找比按 Python 对象身份找稳（Qt 把 userData 交回来时可能换了层壳）。
    """

    def __init__(self, key: int, uid: int = 0) -> None:
        super().__init__()
        self.key = key
        self.uid = uid


def _inherit_clean(fmt: QtGui.QTextBlockFormat) -> QtGui.QTextBlockFormat:
    """新块只继承字号行距，不继承对齐、左右缩进和行高。

    用户那条是右对齐带左缩进的，工具行、思考段跟着借来的格式就会一起跑到右边去；
    长内容那块空位是固定行高，后面的块继承过去就变成一块凭空高出来的空白。
    """
    fmt.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
    fmt.setLeftMargin(0)
    fmt.setRightMargin(0)
    fmt.setTopMargin(0)
    fmt.setBottomMargin(0)
    fmt.setLineHeight(100, QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    return fmt


@dataclass
class Fold:
    key: int
    kind: str
    label: str
    open: bool
    head_block: QtGui.QTextBlock | None = field(default=None, repr=False)
    has_body: bool = False
    pinned: bool = False
    glyph: QtGui.QTextImageFormat | None = field(default=None, repr=False)
    scroll: ToolScroll | None = field(default=None, repr=False)
    slot: QtGui.QTextBlock | None = field(default=None, repr=False)
    chunks: list[tuple[str, QtGui.QColor | None, QtGui.QColor | None]] = field(default_factory=list, repr=False)
    drawn: int = 0
    needs_fit: bool = False


class Folds:
    """文档里的折叠段登记表。一个文档一个实例，`reset()` 跟着清空会话走。"""

    def __init__(
        self,
        doc: QtGui.QTextDocument,
        head_format: QtGui.QTextCharFormat,
        body_format: QtGui.QTextCharFormat,
        *,
        gap: int = 5,
        host: QtWidgets.QWidget | None = None,
    ) -> None:
        self._doc = doc
        self._head_format = head_format
        self._body_format = body_format
        self._gap = gap
        self._host = host
        self._items: dict[int, Fold] = {}
        self._marks: list[_BodyMark] = []
        self._seq = 0
        self._uid = 0


    def get(self, key: int) -> Fold | None:
        return self._items.get(key)

    def all(self) -> list[Fold]:
        return list(self._items.values())

    def reset(self) -> None:
        """会话清空：文档已经没了，标记跟着作废，长内容控件一并收掉。"""
        for fold in self._items.values():
            if fold.scroll is not None:
                fold.scroll.setParent(None)
                fold.scroll.deleteLater()
                fold.scroll = None
        self._items.clear()
        self._marks.clear()

    def drop_invalid(self) -> None:
        """文档被截断后，把已经不在文档里的段丢出登记表。

        删掉的那些块会变成 `isValid() == False`；不丢的话 `all()` 还把它们算作「屏幕上有东西」，
        `has_transcript()` 就永远为真（换会话时判断要不要重铺历史全靠它）。
        """
        for key in [
            key
            for key, fold in self._items.items()
            if fold.head_block is None or not fold.head_block.isValid()
        ]:
            self._items.pop(key, None)

    def owns(self, pos: int) -> bool:
        """这个位置是不是落在某个折叠段的正文里（抬头不算）。"""
        return isinstance(self._doc.findBlock(pos).userData(), _BodyMark)

    def body_blocks(self, fold: Fold) -> list[QtGui.QTextBlock]:
        """这个折叠段的所有正文块（按文档顺序）。"""
        out: list[QtGui.QTextBlock] = []
        block = self._doc.begin()
        while block.isValid():
            mark = block.userData()
            if isinstance(mark, _BodyMark) and mark.key == fold.key:
                out.append(block)
            block = block.next()
        return out


    def set_glyph(self, fold: Fold, glyph) -> None:
        """换掉抬头前面那颗图标，抬头其余内容原样重贴。"""
        fold.glyph = glyph
        self.retitle(fold, fold.label)

    def new(self, kind: str, label: str, *, open_: bool = False, glyph=None) -> Fold:
        cursor = self._end_cursor()
        self._start_block(cursor, margin=self._gap)
        head_pos = cursor.position()
        self._seq += 1
        fold = Fold(key=self._seq, kind=kind, label=label, open=bool(open_))
        if glyph is not None:
            fold.glyph = glyph
            cursor.insertImage(glyph)
            cursor.insertText(" ")
        cursor.insertText(head_text(label, fold.open), self._head_link(fold))
        fold.head_block = self._doc.findBlock(head_pos)
        head_format = fold.head_block.blockFormat()
        head_format.setLineHeight(18, QtGui.QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        QtGui.QTextCursor(fold.head_block).setBlockFormat(head_format)
        self._items[fold.key] = fold
        return fold

    def _body_cursor(self, fold: Fold) -> QtGui.QTextCursor:
        """给这个折叠段新开一段正文，光标落在新段里。

        必须接在**这张卡自己的**正文尾部，不能写死成「文档末尾」：引擎并发调两把工具时，
        先回来的那张的输出会被排到后一张下面——收起 A 时消失的却是 B 下面那一行。
        """
        after = self._tail_block(fold)
        if after is None or not after.isValid():
            cursor = self._end_cursor()
            self._start_block(cursor, margin=0)
            return cursor
        cursor = QtGui.QTextCursor(self._doc)
        cursor.setPosition(after.position() + after.length() - 1)
        cursor.insertBlock()
        block_format = _inherit_clean(cursor.blockFormat())
        block_format.setTopMargin(0)
        block_format.setBottomMargin(6)
        cursor.setBlockFormat(block_format)
        return cursor

    def append(self, fold: Fold, text: str, *, body_format: QtGui.QTextCharFormat | None = None) -> None:
        """往折叠段下面加一段（折叠着加进去的段落一样是藏着的）。

        工具输出走另一条路：它可能几百行，铺进正文会把整屏顶穿，所以只留一块
        等高的空位，字放进独立控件里自己滚（`_append_scroll`）。
        """
        if not text:
            return
        if fold.kind == "tool" and self._host is not None:
            self._append_scroll(fold, text)
            return
        cursor = self._body_cursor(fold)
        start = cursor.position()
        cursor.insertText(text, body_format or self._body_format)
        end = cursor.position()
        fold.has_body = True
        block = self._doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            mark = self._mark(fold)
            block.setUserData(mark)
            block.setVisible(fold.open)
            block = block.next()
        self._doc.markContentsDirty(0, self._doc.characterCount())

    def append_marked(
        self,
        fold: Fold,
        text: str,
        *,
        body_format: QtGui.QTextCharFormat | None = None,
        runs: list[tuple[str, QtGui.QTextCharFormat]] | None = None,
    ) -> _BodyMark | None:
        """加一段正文，并把它那一块的标记交回去（卡片要原地改这一行）。

        `runs` 是「同一行里好几种颜色」：给了它就按这对 (文字, 格式) 依次拼，`text` 只用来量长度。
        """
        return self._insert_block(
            fold, text, after=self._tail_block(fold), body_format=body_format, runs=runs
        )

    def insert_after(
        self, fold: Fold, mark: _BodyMark, text: str, *, body_format: QtGui.QTextCharFormat | None = None
    ) -> _BodyMark | None:
        """插在某一正文块**后面**：子代理的活动行必须紧跟它自己的目标行，不能排到别的行后面去。"""
        return self._insert_block(fold, text, after=self._block_of(mark), body_format=body_format)

    def set_mark_text(
        self, mark: _BodyMark, text: str, *, body_format: QtGui.QTextCharFormat | None = None
    ) -> None:
        """把某一正文块的字**原地换掉**（块还在原处，卡片不长高、顺序不变）。"""
        block = self._block_of(mark)
        if block is None:
            return
        cursor = QtGui.QTextCursor(block)
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + block.length() - 1, QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text, body_format or self._body_format)
        self._doc.markContentsDirty(0, self._doc.characterCount())

    def _append_scroll(self, fold: Fold, text: str) -> None:
        """长内容不进文档流：正文里留一块等高的空位，字交给独立控件自己滚。"""
        for line in text.split("\n"):
            fold.chunks.append((line, None, None))
        self._refresh_scroll(fold)

    def _append_scroll_runs(
        self, fold: Fold, rows: list[list[tuple[str, QtGui.QTextCharFormat]]]
    ) -> None:
        """同上，但每行自带颜色（diff、ANSI 输出）。"""
        for row in rows:
            text = "".join(piece for piece, _fmt in row)
            color = None
            bg = None
            for piece, fmt in row:
                if not piece.strip():
                    continue
                if color is None and fmt.foreground().style() != QtCore.Qt.BrushStyle.NoBrush:
                    color = fmt.foreground().color()
                if bg is None and fmt.background().style() != QtCore.Qt.BrushStyle.NoBrush:
                    bg = fmt.background().color()
            fold.chunks.append((text, color, bg))
        self._refresh_scroll(fold)

    def _refresh_scroll(self, fold: Fold) -> None:
        from .tool_scroll import ToolScroll

        scroll = fold.scroll
        if scroll is None:
            scroll = fold.scroll = ToolScroll(self._host)
            scroll.set_rows(fold.chunks)
            fold.drawn = len(fold.chunks)
        elif len(fold.chunks) > fold.drawn:
            scroll.add_rows(fold.chunks[fold.drawn :])
            fold.drawn = len(fold.chunks)
        scroll.setVisible(fold.open)
        fold.has_body = True
        self._sync_slot(fold)

    def relayout(self, window: tuple[int, int] | None = None) -> None:
        """窗口宽度变了：滚动的那些按新宽度重新量高度，正文里留的空位跟着改。

        给了 `window`（字符位区间）就只量落在里面的：几百个工具框每个都要重排一遍，
        窗口每动一格全来一次，拉伸就卡在这上面。没量的记下 `needs_fit`，滚到时补。
        """
        for fold in self._items.values():
            if fold.scroll is None:
                continue
            if window is not None and not self._in_window(fold, window):
                fold.needs_fit = True
                continue
            fold.needs_fit = False
            self._sync_slot(fold)

    def _in_window(self, fold: Fold, window: tuple[int, int]) -> bool:
        slot = fold.slot
        if slot is None or not slot.isValid():
            return False
        return window[0] <= slot.position() <= window[1]

    def fit_window(self, window: tuple[int, int]) -> None:
        """滚进来才发现宽度变过的那些，就地量一次。"""
        for fold in self._items.values():
            if fold.scroll is None or not fold.needs_fit:
                continue
            if not self._in_window(fold, window):
                continue
            fold.needs_fit = False
            self._sync_slot(fold)

    def _sync_slot(self, fold: Fold) -> None:
        """让正文里那块空位和控件一样高——版面按空位算，滚到它时才对得上。"""
        width = max(1, int(self._doc.pageSize().width()) - 2 * int(self._doc.documentMargin()))
        scroll = fold.scroll
        if scroll is None:
            return
        height = scroll.fit(width)
        block = fold.slot
        if block is None or not block.isValid():
            cursor = self._body_cursor(fold)
            start = cursor.position()
            cursor.insertText("\u200b", self._body_format)
            block = self._doc.findBlock(start)
        cursor = QtGui.QTextCursor(block)
        fmt = _inherit_clean(block.blockFormat())
        fmt.setTopMargin(6)
        fmt.setBottomMargin(0)
        fmt.setLineHeight(height, QtGui.QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.setBlockFormat(fmt)
        block = self._doc.findBlock(cursor.block().position())
        if not isinstance(block.userData(), _BodyMark):
            block.setUserData(self._mark(fold))
        block.setVisible(fold.open)
        fold.slot = block
        self._doc.markContentsDirty(block.position(), block.length())

    def _mark(self, fold: Fold) -> _BodyMark:
        self._uid += 1
        mark = _BodyMark(fold.key, self._uid)
        self._marks.append(mark)
        return mark

    def _block_of(self, mark: _BodyMark) -> QtGui.QTextBlock | None:
        block = self._doc.begin()
        while block.isValid():
            data = block.userData()
            if isinstance(data, _BodyMark) and data.uid == mark.uid:
                return block
            block = block.next()
        return None

    def _tail_block(self, fold: Fold) -> QtGui.QTextBlock | None:
        """这个折叠段现有正文的最后一块（还没有正文就是抬头）。"""
        blocks = self.body_blocks(fold)
        if blocks:
            return blocks[-1]
        head = fold.head_block
        return head if head is not None and head.isValid() else self._find_head(fold)

    def _insert_block(
        self,
        fold: Fold,
        text: str,
        *,
        after: QtGui.QTextBlock | None,
        body_format: QtGui.QTextCharFormat | None = None,
        runs: list[tuple[str, QtGui.QTextCharFormat]] | None = None,
    ) -> _BodyMark | None:
        if not text and not runs:
            return None
        cursor = QtGui.QTextCursor(self._doc)
        if after is None:
            cursor = self._end_cursor()
            self._start_block(cursor, margin=0)
        else:
            cursor.setPosition(after.position() + after.length() - 1)
            cursor.insertBlock()
            block_format = _inherit_clean(cursor.blockFormat())
            block_format.setTopMargin(0)
            cursor.setBlockFormat(block_format)
        start = cursor.position()
        if runs:
            for piece, fmt in runs:
                if piece:
                    cursor.insertText(piece, fmt or body_format or self._body_format)
        else:
            cursor.insertText(text, body_format or self._body_format)
        end = cursor.position()
        fold.has_body = True
        first: _BodyMark | None = None
        block = self._doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            mark = self._mark(fold)
            block.setUserData(mark)
            block.setVisible(fold.open)
            first = first or mark
            block = block.next()
        self._doc.markContentsDirty(0, self._doc.characterCount())
        return first

    def append_runs(self, fold: Fold, rows: list[list[tuple[str, QtGui.QTextCharFormat]]]) -> None:
        """按行插一段「多彩正文」：每行是若干 (文字, 字符格式)。

        ANSI 输出和 diff 都是「一行里好几种颜色」，`append()` 只能整段一种格式，所以另开一条。
        折叠标记的规则与 `append()` 完全一样：文本里每一段都要打标记。
        """
        if not rows:
            return
        if fold.kind == "tool" and self._host is not None:
            has_image = any(
                isinstance(fmt, QtGui.QTextImageFormat)
                and not fmt.name().startswith("noesia-icon://")
                for row in rows
                for _text, fmt in row
            )
            if not has_image:
                self._append_scroll_runs(fold, rows)
                return
        cursor = self._body_cursor(fold)
        start = cursor.position()
        for index, row in enumerate(rows):
            if index:
                self._start_block(cursor, margin=0)
            for text, fmt in row:
                if isinstance(fmt, QtGui.QTextImageFormat):
                    cursor.insertImage(fmt)
                elif text:
                    cursor.insertText(text, fmt)
        end = cursor.position()
        fold.has_body = True
        block = self._doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            mark = self._mark(fold)
            block.setUserData(mark)
            block.setVisible(fold.open)
            block = block.next()
        self._doc.markContentsDirty(0, self._doc.characterCount())


    def toggle(self, key: int) -> bool | None:
        fold = self._items.get(key)
        if fold is None:
            return None
        self.set_open(fold, not fold.open)
        self.retitle(fold, fold.label)
        return fold.open

    def set_open(self, fold: Fold, open_: bool) -> None:
        fold.open = bool(open_)
        if not fold.has_body:
            return
        for block in self.body_blocks(fold):
            block.setVisible(fold.open)
        if fold.scroll is not None:
            fold.scroll.setVisible(fold.open)
        self._doc.markContentsDirty(0, self._doc.characterCount())

    def retitle(self, fold: Fold, label: str) -> None:
        """换抬头文字（「思考中」→「思考了 3s」）。

        只改抬头那一块：正文由块标记自己带着走，这里不用补偿任何位置。

        删旧插新包进同一个编辑块：不包的话「删光」那一瞬文档只剩一行，滚动上限
        跟着塌一次，贴底的视口值被夹到瞬时最大值上，等内容插回、上限涨回去时
        Qt 只发 rangeChanged 不发 valueChanged，就再也回不到真底。
        """
        block = fold.head_block
        if block is None or not block.isValid():
            block = self._find_head(fold)
        if block is None or not block.isValid():
            return
        pos = block.position()
        cursor = QtGui.QTextCursor(self._doc)
        cursor.beginEditBlock()
        cursor.setPosition(pos)
        cursor.setPosition(pos + block.length() - 1, QtGui.QTextCursor.MoveMode.KeepAnchor)
        if fold.glyph is not None:
            cursor.insertImage(fold.glyph)
            cursor.insertText(" ")
        cursor.insertText(head_text(label, fold.open), self._head_link(fold))
        cursor.endEditBlock()
        fold.label = label
        fold.head_block = self._doc.findBlock(pos)


    def _find_head(self, fold: Fold) -> QtGui.QTextBlock | None:
        """抬头块丢了（例如整份文档重建过）就按锚点找回来。"""
        needle = f"#fold:{fold.key}"
        block = self._doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.isAnchor() and fmt.anchorHref() == needle:
                    return block
                it += 1
            block = block.next()
        return None

    def _head_link(self, fold: Fold) -> QtGui.QTextCharFormat:
        fmt = QtGui.QTextCharFormat(self._head_format)
        fmt.setAnchor(True)
        fmt.setAnchorHref(f"#fold:{fold.key}")
        return fmt

    def _end_cursor(self) -> QtGui.QTextCursor:
        cursor = QtGui.QTextCursor(self._doc)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        return cursor

    def _start_block(self, cursor: QtGui.QTextCursor, *, margin: int) -> None:
        if not self._doc.isEmpty():
            cursor.insertBlock()
        block_format = _inherit_clean(cursor.blockFormat())
        block_format.setTopMargin(margin)
        cursor.setBlockFormat(block_format)
