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

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

from app.tool_view.icons import ui_icon_svg

COPIED_RESET_MS = 1500

DIFF_PREVIEW_LABEL = "看完整 diff"


def icon_pixmap(icon_name: str, color: str, px: int,
                dpr: float | None = None) -> QtGui.QPixmap | None:
    """把描边图标画成一张 pixmap；调用方不止悬停按钮，所以是公开的。"""
    svg = ui_icon_svg(icon_name, color, px)
    if not svg:
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
        if dpr is None:
            screen = QtGui.QGuiApplication.primaryScreen()
            dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
        dpr = max(1.0, float(dpr))
        pixmap = QtGui.QPixmap(max(1, int(round(px * dpr))), max(1, int(round(px * dpr))))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        renderer.render(painter)
        painter.end()
    except Exception:  # noqa: BLE001
        return None
    return pixmap


class _SvgIconEngine(QtGui.QIconEngine):
    """矢量图标引擎：每次要图都按当前 DPI 重新栅格，不会留下启动时的旧比例。"""

    def __init__(self, svg: str) -> None:
        super().__init__()
        self._svg = svg

    def paint(self, painter, rect, mode, state) -> None:
        self._render(painter, QtCore.QRectF(rect))

    def pixmap(self, size, mode, state) -> QtGui.QPixmap:
        pix = QtGui.QPixmap(size)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pix)
        self._render(painter, QtCore.QRectF(0, 0, size.width(), size.height()))
        painter.end()
        return pix

    def clone(self) -> "_SvgIconEngine":
        return _SvgIconEngine(self._svg)

    def _render(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        from PySide6.QtSvg import QSvgRenderer

        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        QSvgRenderer(QtCore.QByteArray(self._svg.encode("utf-8"))).render(painter, rect)


def svg_icon(icon_name: str, color: str, px: int) -> QtGui.QIcon:
    """图标名 + 颜色 → 矢量 QIcon；不认识的名字给空图标。"""
    svg = ui_icon_svg(icon_name, color, px)
    if not svg:
        return QtGui.QIcon()
    return QtGui.QIcon(_SvgIconEngine(svg))


class HoverActionButton(QtWidgets.QToolButton):
    """一个悬停才显形的图标按钮，位置由调用方给。"""

    BUTTON_PX = 20
    ICON_PX = 14
    IDLE_COLOR = "#8b8492"
    HOVER_COLOR = "#f6dfea"
    DONE_COLOR = "#98c379"
    BACKDROP_QSS = """
QToolButton {
    background: rgba(62, 47, 62, 245);
    border-radius: 6px;
}
"""

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
        self._icon_name = ""
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
            "QToolButton { border: none; background: transparent; padding: 0; }"
        )
        self.retarget(icon_name, tooltip, slot, done_tooltip)
        self.clicked.connect(self._clicked)
        self.hide()

    def retarget(
        self, icon_name: str, tooltip: str, slot: Callable[[], None], done_tooltip: str = ""
    ) -> None:
        """换图标与动作（同一个按钮在「复制代码」和「看完整 diff」之间复用）。"""
        same = icon_name == getattr(self, "_icon_name", "") and tooltip == getattr(
            self, "_idle_tooltip", ""
        )
        self._icon_name = icon_name
        self._slot = slot
        self._idle_tooltip = tooltip
        self._done_tooltip = done_tooltip or tooltip
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        if same:
            self._reset.stop()
            self._back_to_idle()
            return
        QtWidgets.QToolTip.hideText()
        self._reset.stop()
        self._back_to_idle()

    def set_enabled(self, flag: bool) -> None:
        """能不能点：非末尾回答上的「刷新」照样摆着，只是灰掉点不动。"""
        self._enabled = bool(flag)
        self.setEnabled(self._enabled)
        self.setCursor(
            QtCore.Qt.CursorShape.PointingHandCursor if self._enabled
            else QtCore.Qt.CursorShape.ArrowCursor
        )

    def set_backdrop(self, on: bool) -> None:
        """要不要垫一层底。摆进气泡里的那颗要——它压在最后一行字上，得把字尾盖住。"""
        self.setStyleSheet(self.BACKDROP_QSS if on else "")

    def place_at(self, rect: QtCore.QRect, offset_x: int = 0, below: bool = False,
                 bottom_align: bool = False, anchor_right: bool = False,
                 margin: int = 4) -> None:
        """贴在这块内容的右上角；`offset_x` 往左让位。

        `below` 挂到这一块下沿外（另起一行）；`anchor_right` 让这条另起的行贴右边
        （用户那条用），不给就贴左边起（回答用）。
        """
        parent = self.parentWidget()
        if parent is None:
            return
        edge = self.BUTTON_PX + margin
        right = rect.right() - edge + offset_x
        x = max(0, min(right, parent.width() - self.BUTTON_PX))
        if below:
            x = (max(0, min(rect.right() - edge + offset_x, parent.width() - self.BUTTON_PX)) if anchor_right
                 else max(0, min(rect.left() + 2 + offset_x, parent.width() - self.BUTTON_PX)))
            y = min(rect.bottom() + 6, parent.height() - self.BUTTON_PX - 4)
        elif bottom_align:
            y = max(rect.top(), rect.bottom() - self.BUTTON_PX - margin)
        else:
            y = max(0, min(rect.top() + 6, rect.bottom() - self.BUTTON_PX))
        self.move(x, y)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:  # noqa: N802
        self._set_icon(self.HOVER_COLOR)
        self._show_tip()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        self._reset.stop()
        self._back_to_idle()
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)

    def _set_icon(self, color: str) -> None:
        """按给定颜色重画图标。刚点过还在显示 ✓ 的那 1.5 秒里不动它。"""
        if self._flash and self._reset.isActive():
            return
        self.setIcon(svg_icon(self._icon_name, color, self.ICON_PX))

    def _show_tip(self) -> None:
        """自己把提示弹出来：Qt 自带的悬停提示在无边框置顶窗里不弹（实测没有）。

        按钮只有 20px、只画图标，没有这行字就只剩猜。

        提示是独立的小窗，**不会跟着按钮走**：悬停条在我们手里来回摆、换条消息时按钮
        整批 hide()，这些都不是鼠标「离开」，`leaveEvent` 收不到——于是每挪一次就在
        原地多留一行字，几个按钮串下来屏幕上就是好几行「编辑重发」。所以这里三条一起：
        先收掉旧提示、给了超时自收、按钮自己 hide 的时候也收。
        """
        QtWidgets.QToolTip.hideText()
        QtWidgets.QToolTip.showText(
            self.mapToGlobal(QtCore.QPoint(0, self.height() + 4)),
            self.toolTip(),
            self,
            self.rect(),
            3000,
        )

    def event(self, event: QtCore.QEvent) -> bool:
        """吃掉 Qt 原生的 tooltip 事件：我们自己弹（上面那个），两条同时走就会在
        气泡里留下两行同样的字，而且原生那条不跟着按钮走。"""
        if event.type() == QtCore.QEvent.Type.ToolTip:
            return True
        return super().event(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802
        """按钮收起来，提示也得跟着收（hide 不走 leaveEvent）。"""
        QtWidgets.QToolTip.hideText()
        super().hideEvent(event)

    def _clicked(self) -> None:
        self._slot()
        QtWidgets.QToolTip.hideText()
        if not self._flash:
            return
        self.setIcon(svg_icon("check", self.DONE_COLOR, self.ICON_PX))
        self.setToolTip(self._done_tooltip)
        self._reset.start()

    def _back_to_idle(self) -> None:
        self.setIcon(svg_icon(self._icon_name, self.IDLE_COLOR, self.ICON_PX))
        self.setToolTip(self._idle_tooltip)
