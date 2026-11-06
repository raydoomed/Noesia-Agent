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
    # 平时几乎退进背景（40% 灰），鼠标停上去才提亮——设计上不要底色、不要边框，
    # 一排图标藏在正文最后一行下面，不跟内容抢注意力
    IDLE_COLOR = "#8b8492"
    HOVER_COLOR = "#f6dfea"
    DONE_COLOR = "#98c379"
    #: 摆进气泡里的那颗要盖住字尾：垫一层和气泡同色的底
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
        self._icon_name = ""  # 还没画过图标：retarget 第一次必须真的画
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
            # 同一颗按钮、同一个动作（鼠标每动一下都会重新 retarget 一次）：
            # 别把刚才点出来的 ✓ 抹回图标——之前复制成功的提示一闪就没，就是这样
            return
        QtWidgets.QToolTip.hideText()  # 换动作了：旧提示别留在原地
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
                 bottom_align: bool = False, anchor_right: bool = False) -> None:
        """贴在这块内容的右上角；`offset_x` 往左让位。

        `below` 挂到这一块下沿外（另起一行）；`anchor_right` 让这条另起的行贴右边
        （用户那条用），不给就贴左边起（回答用）。
        """
        parent = self.parentWidget()
        if parent is None:
            return
        right = rect.right() - self.BUTTON_PX - 6 + offset_x
        x = max(0, min(right, parent.width() - self.BUTTON_PX))
        if below:
            # 挂在块下沿：回答贴左起，像那句话自己的注脚；用户那条贴右边，和自己的话一列
            x = (max(0, rect.right() - self.BUTTON_PX - 6 + offset_x) if anchor_right
                 else max(0, rect.left() + 2 + offset_x))
            y = min(rect.bottom() + 6, parent.height() - self.BUTTON_PX - 4)
        elif bottom_align:
            y = max(rect.top(), rect.bottom() - self.BUTTON_PX - 2)
        else:
            y = max(0, rect.top() + 2)
        self.move(x, y)

    def enterEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        self._set_icon(self.HOVER_COLOR)  # 鼠标停上去：图标从 40% 灰提到暖白
        self._show_tip()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        self._set_icon(self.IDLE_COLOR)
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)

    def _set_icon(self, color: str) -> None:
        """按给定颜色重画图标。刚点过还在显示 ✓ 的那 1.5 秒里不动它。"""
        if self._flash and self._reset.isActive():
            return
        icon = icon_pixmap(self._icon_name, color, self.ICON_PX)
        if icon is not None:
            self.setIcon(QtGui.QIcon(icon))

    def _show_tip(self) -> None:
        """自己把提示弹出来：Qt 自带的悬停提示在无边框置顶窗里不弹（实测没有）。

        按钮只有 20px、只画图标，没有这行字就只剩猜。

        提示是独立的小窗，**不会跟着按钮走**：悬停条在我们手里来回摆、换条消息时按钮
        整批 hide()，这些都不是鼠标「离开」，`leaveEvent` 收不到——于是每挪一次就在
        原地多留一行字，几个按钮串下来屏幕上就是好几行「编辑重发」。所以这里三条一起：
        先收掉旧提示、给了超时自收、按钮自己 hide 的时候也收。
        """
        QtWidgets.QToolTip.hideText()  # 先收旧的：一次只留一行
        # 挂在按钮**正下方**：往上会压在它上面那行正文上
        QtWidgets.QToolTip.showText(
            self.mapToGlobal(QtCore.QPoint(0, self.height() + 4)),
            self.toolTip(),
            self,
            self.rect(),
            3000,  # 3 秒自收：定时器比「谁最后 hide 了」可靠
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
        QtWidgets.QToolTip.hideText()  # 点下去就把提示收掉：动作已经发生了
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
        else:
            # 图标名字不在表里（曾经 'edit' 就是这样）：至少别留着上一颗的图标，
            # 否则从 ✓ 退回来会一直挂着绿勾，看着像「已复制」永远不还原
            self.setIcon(QtGui.QIcon())
        self.setToolTip(self._idle_tooltip)
