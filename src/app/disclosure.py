"""可折叠的一段：一行抬头 + 藏在它下面的若干段（照桌面端 `DisclosureRow` / `ScaffoldRow`）。

桌面端一条脚手架行——「思考中」、「读取 config.py」——永远是同一副样子：一行暗色小字，
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

from PySide6 import QtGui

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


@dataclass
class Fold:
    key: int
    kind: str  # 谁在用：reasoning / tool / …
    label: str
    open: bool
    head_block: QtGui.QTextBlock = field(default=None, repr=False)
    has_body: bool = False
    pinned: bool = False  # 用户自己点过开合：别再替他自动收起/展开
    glyph: object | None = field(default=None, repr=False)  # 抬头左边的小图标（QTextImageFormat）


class Folds:
    """文档里的折叠段登记表。一个文档一个实例，`reset()` 跟着清空会话走。"""

    def __init__(
        self,
        doc: QtGui.QTextDocument,
        head_format: QtGui.QTextCharFormat,
        body_format: QtGui.QTextCharFormat,
        *,
        gap: int = 5,
    ) -> None:
        self._doc = doc
        self._head_format = head_format
        self._body_format = body_format
        self._gap = gap
        self._items: dict[int, Fold] = {}
        self._marks: list[_BodyMark] = []  # 保住 Python 侧引用：块只存指针
        self._seq = 0
        self._uid = 0

    # ---- 查询 ----

    def get(self, key: int) -> Fold | None:
        return self._items.get(key)

    def all(self) -> list[Fold]:
        return list(self._items.values())

    def reset(self) -> None:
        """会话清空：文档已经没了，标记跟着作废。"""
        self._items.clear()
        self._marks.clear()

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

    # ---- 建段 / 喂正文 ----

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
        fold.head_block = self._doc.findBlock(head_pos)  # 插入后再取：句柄跟着块走，不怕位置变
        self._items[fold.key] = fold
        return fold

    def append(self, fold: Fold, text: str, *, body_format: QtGui.QTextCharFormat | None = None) -> None:
        """往折叠段下面加一段（折叠着加进去的段落一样是藏着的）。"""
        if not text:
            return
        cursor = self._end_cursor()
        self._start_block(cursor, margin=0)
        start = cursor.position()
        cursor.insertText(text, body_format or self._body_format)
        end = cursor.position()
        fold.has_body = True
        # 文本里的换行会额外切出段来：每一块都得打上标记，不然折叠着还能看见半截
        block = self._doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            mark = self._mark(fold)
            block.setUserData(mark)
            block.setVisible(fold.open)
            block = block.next()
        # 段落可见性变了，布局得重排：只改块标志不会让高度自己失效（会露出一大块空白）
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
        # 选中「块里的字」而不是整块：`BlockUnderCursor` 连着段尾分隔符一起选，
        # 换字会把这一块并到上一块里去（两行子代理挤成一行）
        cursor = QtGui.QTextCursor(block)
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + len(block.text()), QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text, body_format or self._body_format)
        self._doc.markContentsDirty(0, self._doc.characterCount())

    def _mark(self, fold: Fold) -> _BodyMark:
        self._uid += 1
        mark = _BodyMark(fold.key, self._uid)
        self._marks.append(mark)  # 保住 Python 侧引用：块只存指针
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
            cursor.setPosition(after.position() + len(after.text()))
            cursor.insertBlock()
            block_format = cursor.blockFormat()
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
        cursor = self._end_cursor()
        # `start` 必须取**另起一段之后**的位置：取在那之前，`findBlock(start)` 落在抬头块
        # 的末尾，抬头会被一起标成正文块——折叠着的时候整行（连标题）跟着消失。
        self._start_block(cursor, margin=0)
        start = cursor.position()
        for index, row in enumerate(rows):
            if index:
                self._start_block(cursor, margin=0)
            for text, fmt in row:
                if isinstance(fmt, QtGui.QTextImageFormat):
                    cursor.insertImage(fmt)  # 图片没有文字，只能整段插图
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

    # ---- 开合 / 改名 ----

    def toggle(self, key: int) -> bool | None:
        fold = self._items.get(key)
        if fold is None:
            return None
        self.set_open(fold, not fold.open)
        self.retitle(fold, fold.label)  # 箭头得跟着翻
        return fold.open

    def set_open(self, fold: Fold, open_: bool) -> None:
        fold.open = bool(open_)
        if not fold.has_body:
            return  # 还没正文：只影响箭头
        for block in self.body_blocks(fold):
            block.setVisible(fold.open)
        self._doc.markContentsDirty(0, self._doc.characterCount())

    def retitle(self, fold: Fold, label: str) -> None:
        """换抬头文字（「思考中」→「思考了 3s」）。

        只改抬头那一块：正文由块标记自己带着走，这里不用补偿任何位置。
        """
        block = fold.head_block
        if block is None or not block.isValid():
            block = self._find_head(fold)
        if block is None or not block.isValid():
            return
        pos = block.position()
        cursor = QtGui.QTextCursor(self._doc)
        cursor.setPosition(pos)
        cursor.setPosition(pos + len(block.text()), QtGui.QTextCursor.KeepAnchor)
        if fold.glyph is not None:
            cursor.insertImage(fold.glyph)
            cursor.insertText(" ")
        cursor.insertText(head_text(label, fold.open), self._head_link(fold))
        fold.label = label
        fold.head_block = self._doc.findBlock(pos)

    # ---- 内部 ----

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
        cursor.movePosition(QtGui.QTextCursor.End)
        return cursor

    def _start_block(self, cursor: QtGui.QTextCursor, *, margin: int) -> None:
        if not self._doc.isEmpty():
            cursor.insertBlock()
        block_format = cursor.blockFormat()
        block_format.setTopMargin(margin)
        cursor.setBlockFormat(block_format)
