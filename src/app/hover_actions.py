"""悬停才出现的小动作按钮：代码/工具正文的「复制」、工具 diff 的「看完整 diff」。

画法：

- `inline` 外观 = 代码块右上角一条 20px 高的小按钮（`h-5 px-1 rounded-md`），只画图标；
- `tool-row` 外观 = 24px 见方（这里取 20px 与 12px 图标，跟聊天窗的正文比例一致）；
- 默认 `opacity-0`，悬停（`group-hover`）才显形；
- 点完图标换成 ✓，`COPIED_RESET_MS` 之后自动还原；文案走 `assistant.tool.copyCode` / `common.copied`。

「看完整 diff」在工具行里没有对应控件（收缩版不带行号），这个按钮是
我们按产品要求加的：它**只开窗**，不冒充复制成功（`flash=False` 就不换 ✓）。

Qt 没有 CSS 的 group-hover，所以按钮由聊天窗自己在鼠标移动时摆位与显隐（见
`ChatPanel._hover_at()`）；本模块只管样子与状态。
"""

from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from app.tool_view.icons import ui_icon_svg

# 复制完成后还原的间隔
COPIED_RESET_MS = 1500

# 「看完整 diff」是自加入口，没有这条文案键
DIFF_PREVIEW_LABEL = "看完整 diff"


def icon_pixmap(icon_name: str, color: str, px: int) -> QtGui.QPixmap | None:
    """把描边图标画成一张 pixmap；调用方不止悬停按钮，所以是公开的。"""
    svg = ui_icon_svg(icon_name, color, px)
    if not svg:
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
        pixmap = QtGui.QPixmap(px, px)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        renderer.render(painter)
        painter.end()
    except Exception:  # noqa: BLE001 - 画不出图标就不画，功能不受影响
        return None
    return pixmap


class HoverActionButton(QtWidgets.QToolButton):
    """一个悬停才显形的图标按钮，位置由调用方给。"""

    BUTTON_PX = 20
    ICON_PX = 12
    IDLE_COLOR = "#8f9bb0"
    DONE_COLOR = "#98c379"

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        icon_name: str,
        tooltip: str,
        slot: Callable[[], None],
        done_tooltip: str = "",
        flash: bool = True,
    ) -> None:
        super().__init__(parent)
        self._flash = flash
        self._icon_name = icon_name
        self._slot = slot
        self._reset = QtCore.QTimer(self)
        self._reset.setSingleShot(True)
        self._reset.setInterval(COPIED_RESET_MS)
        self._reset.timeout.connect(self._back_to_idle)

        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFixedSize(self.BUTTON_PX, self.BUTTON_PX)
        self.setIconSize(QtCore.QSize(self.ICON_PX, self.ICON_PX))
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            "QToolButton { border: none; border-radius: 6px; background: transparent; padding: 0; }"
            "QToolButton:hover { background: rgba(255, 255, 255, 0.10); }"
        )
        self.retarget(icon_name, tooltip, slot, done_tooltip)
        self.clicked.connect(self._clicked)
        self.hide()

    def retarget(
        self, icon_name: str, tooltip: str, slot: Callable[[], None], done_tooltip: str = ""
    ) -> None:
        """换图标与动作（同一个按钮在「复制代码」和「看完整 diff」之间复用）。"""
        self._icon_name = icon_name
        self._slot = slot
        self._idle_tooltip = tooltip
        self._done_tooltip = done_tooltip or tooltip
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self._reset.stop()
        self._back_to_idle()

    def place_at(self, rect: QtCore.QRect, offset_x: int = 0) -> None:
        """贴在这块内容的右上角；`offset_x` 往左让位。"""
        parent = self.parentWidget()
        if parent is None:
            return
        right = rect.right() - self.BUTTON_PX - 6 + offset_x
        x = max(0, min(right, parent.width() - self.BUTTON_PX))
        y = max(0, rect.top() + 2)
        self.move(x, y)

    def _clicked(self) -> None:
        self._slot()
        if not self._flash:
            return
        icon = icon_pixmap("check", self.DONE_COLOR, self.ICON_PX)
        if icon is not None:
            self.setIcon(QtGui.QIcon(icon))
        self.setToolTip(self._done_tooltip)
        self._reset.start()

    def _back_to_idle(self) -> None:
        icon = icon_pixmap(self._icon_name, self.IDLE_COLOR, self.ICON_PX)
        if icon is not None:
            self.setIcon(QtGui.QIcon(icon))
        self.setToolTip(self._idle_tooltip)
