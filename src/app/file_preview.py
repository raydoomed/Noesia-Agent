"""整段 diff 的大视图——右栏预览里的那个 diff 模式。

预览区切三种模式（渲染 / 源码 / diff），diff 模式：VS Code 式**双列行号**——新增行与上下文行给新行号，
删除行给旧行号，行号列固定在最左、右对齐、等宽、颜色偏灰（`muted-foreground/55`）。

聊天窗里的工具行只放收缩版（不带行号），
要看全量就开这扇窗：行号、底色、语法高亮的格式全部由聊天窗按同一套规则算好传进来
（`ChatPanel._diff_rows(..., numbers=True)`），这里只负责摆出来。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 行号列一行 20px
PREVIEW_LINE_PX = 20


class DiffPreviewWindow(QtWidgets.QDialog):
    """一扇独立窗口：抬头写路径与 +/- 计数，正文是带双列行号的整段 diff。"""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        title: str,
        rows: list[list[tuple[str, QtGui.QTextCharFormat]]],
        summary: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "完整 diff")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(980, 700)

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)

        head = QtWidgets.QLabel(self)
        head.setTextFormat(QtCore.Qt.TextFormat.RichText)
        head.setText(
            f"<span style='font-family:Consolas,monospace;font-size:10pt'>{_escape(title)}</span>"
            + (
                f"<span style='color:#8f9bb0;font-size:9pt'>&nbsp;&nbsp;{_escape(summary)}</span>"
                if summary
                else ""
            )
        )
        box.addWidget(head)

        self.view = QtWidgets.QTextBrowser(self)
        self.view.setOpenExternalLinks(False)
        self.view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.view.setWordWrapMode(QtGui.QTextOption.WrapMode.NoWrap)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        box.addWidget(self.view, 1)

        document = self.view.document()
        document.setDocumentMargin(0)
        font = QtGui.QFont("Consolas")
        font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        font.setPointSizeF(9)
        document.setDefaultFont(font)

        cursor = QtGui.QTextCursor(document)
        block_format = QtGui.QTextBlockFormat()
        block_format.setLineHeight(PREVIEW_LINE_PX, QtGui.QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        for index, row in enumerate(rows):
            if index:
                cursor.insertBlock(block_format)
            else:
                cursor.setBlockFormat(block_format)
            for text, fmt in row:
                cursor.insertText(text, fmt)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.view.setTextCursor(cursor)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 - Qt 回调命名
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
