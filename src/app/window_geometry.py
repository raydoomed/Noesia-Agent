"""悬浮窗的尺寸计算（纯函数，方便单测；不碰窗口状态）。

为什么把这些单独抽出来：缩放要处理「拖哪条边」「按比例锁高」「最小/最大钳制」
「屏幕上放不下」这些组合，写在事件处理里既难测也容易算错。这里只做数字计算，
`FloatingAvatar` 负责把结果套进 setGeometry。

比例锁定（aspect = 高 / 宽）是刻意的：形象图和待机视频都是固定构图，
任意拉伸会把人脸压扁。所以不管拖的是宽还是高，最终都换算成「宽度」再按比例取高。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

RESIZE_MARGIN = 12
MIN_WIDTH = 180
DEFAULT_WIDTH = 360
MAX_WIDTH = 1600


def edges_at(pos: QtCore.QPoint, size: QtCore.QSize, margin: int = RESIZE_MARGIN) -> QtCore.Qt.Edge:
    """按下位置命中的边/角；窗口正中返回空（表示应当拖动而不是缩放）。"""
    edges = QtCore.Qt.Edge(0)
    if pos.x() <= margin:
        edges |= QtCore.Qt.Edge.LeftEdge
    elif pos.x() >= size.width() - margin:
        edges |= QtCore.Qt.Edge.RightEdge
    if pos.y() <= margin:
        edges |= QtCore.Qt.Edge.TopEdge
    elif pos.y() >= size.height() - margin:
        edges |= QtCore.Qt.Edge.BottomEdge
    return edges


def cursor_for(edges: QtCore.Qt.Edge) -> QtCore.Qt.CursorShape:
    """不同边给不同鼠标指针，用户才知道这里能拉。"""
    left = bool(edges & QtCore.Qt.Edge.LeftEdge)
    right = bool(edges & QtCore.Qt.Edge.RightEdge)
    top = bool(edges & QtCore.Qt.Edge.TopEdge)
    bottom = bool(edges & QtCore.Qt.Edge.BottomEdge)
    if (left and top) or (right and bottom):
        return QtCore.Qt.CursorShape.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return QtCore.Qt.CursorShape.SizeBDiagCursor
    if left or right:
        return QtCore.Qt.CursorShape.SizeHorCursor
    if top or bottom:
        return QtCore.Qt.CursorShape.SizeVerCursor
    return QtCore.Qt.CursorShape.ArrowCursor


def screen_area(anchor: QtCore.QRect | None) -> QtCore.QRect:
    """锚点所在屏幕的可用区域（没有锚点就用主屏）。给卡片找位置用。"""
    screen = None
    if anchor is not None and anchor.isValid():
        screen = QtWidgets.QApplication.screenAt(anchor.center())
    screen = screen or QtWidgets.QApplication.primaryScreen()
    return screen.availableGeometry()


def card_position(
    anchor: QtCore.QRect | None,
    size: QtCore.QSize,
    area: QtCore.QRect,
    gap: int = 12,
    margin: int = 8,
) -> QtCore.QPoint:
    """一张卡片贴在锚点（人物窗）**下方**并夹在屏幕可见区域里。

    人物窗可能贴在屏幕底边或左右边，直接 `anchor.bottom() + gap` 会跑到屏幕外，
    所以放不下就翻到上方；左右夹在 `area` 里。没有锚点就居中——不是「随便找个地方」，
    而是「至少别跑到屏幕外」。
    """
    if anchor is None or not anchor.isValid():
        return QtCore.QPoint(
            area.center().x() - size.width() // 2,
            area.center().y() - size.height() // 2,
        )
    x = anchor.center().x() - size.width() // 2
    y = anchor.bottom() + gap
    if y + size.height() > area.bottom():
        y = max(area.top() + margin, anchor.top() - size.height() - gap)
    x = max(area.left() + margin, min(x, area.right() - size.width() - margin))
    return QtCore.QPoint(x, y)


def fit_width(
    width: float,
    aspect: float,
    *,
    min_width: int = MIN_WIDTH,
    max_width: int = MAX_WIDTH,
    max_height: int = 0,
) -> QtCore.QSize:
    """把宽度钳到合法区间，再按 aspect 算高；max_height>0 时以高度反推宽度。"""
    clamped = max(min_width, min(max_width, float(width)))
    height = clamped * aspect
    if max_height and height > max_height:
        height = float(max_height)
        clamped = max(float(min_width), height / aspect)
    return QtCore.QSize(max(1, round(clamped)), max(1, round(height)))


def fit_free_size(
    width: float,
    height: float,
    *,
    min_width: int,
    min_height: int,
    max_width: int,
    max_height: int = 0,
) -> QtCore.QSize:
    """宽高各自独立钳制的尺寸（对话面板用；形象窗要锁比例，用 fit_width）。"""
    w = max(min_width, min(max_width, float(width)))
    h = max(min_height, float(height))
    if max_height:
        h = min(h, float(max_height))
    return QtCore.QSize(max(1, round(w)), max(1, round(h)))


def size_from_drag_free(
    edge: QtCore.Qt.Edge,
    start: QtCore.QSize,
    delta: QtCore.QPoint,
    *,
    min_width: int,
    min_height: int,
    max_width: int,
    max_height: int = 0,
) -> QtCore.QSize:
    """拖边/角自由改宽高：左右改宽、上下改高，互不影响。"""
    width = float(start.width())
    height = float(start.height())
    if edge & QtCore.Qt.Edge.RightEdge:
        width += delta.x()
    if edge & QtCore.Qt.Edge.LeftEdge:
        width -= delta.x()
    if edge & QtCore.Qt.Edge.BottomEdge:
        height += delta.y()
    if edge & QtCore.Qt.Edge.TopEdge:
        height -= delta.y()
    return fit_free_size(
        width,
        height,
        min_width=min_width,
        min_height=min_height,
        max_width=max_width,
        max_height=max_height,
    )


def rect_after_resize(
    rect: QtCore.QRect, edges: QtCore.Qt.Edge, size: QtCore.QSize
) -> QtCore.QRect:
    """按新尺寸算窗口矩形；拖左边/上边时把右下角钉住，否则整窗会跑偏。"""
    geo = QtCore.QRect(rect.topLeft(), size)
    if edges & QtCore.Qt.Edge.LeftEdge:
        geo.moveRight(rect.right())
    if edges & QtCore.Qt.Edge.TopEdge:
        geo.moveBottom(rect.bottom())
    return geo


def size_from_drag(
    edge: QtCore.Qt.Edge,
    start: QtCore.QSize,
    delta: QtCore.QPoint,
    aspect: float,
    *,
    min_width: int = MIN_WIDTH,
    max_width: int = MAX_WIDTH,
    max_height: int = 0,
) -> QtCore.QSize:
    """按「拖了哪条边 + 位移」算出新尺寸。

    左右边直接用横向位移；只有上下边时用纵向位移换算宽度（比例锁定）。
    角同时含横纵，取横向（更符合直觉，且纵向会被比例修正）。
    """
    if edge & (QtCore.Qt.Edge.LeftEdge | QtCore.Qt.Edge.RightEdge):
        offset = delta.x() if edge & QtCore.Qt.Edge.RightEdge else -delta.x()
        width = start.width() + offset
    elif edge & (QtCore.Qt.Edge.TopEdge | QtCore.Qt.Edge.BottomEdge):
        offset = delta.y() if edge & QtCore.Qt.Edge.BottomEdge else -delta.y()
        height = start.height() + offset
        width = height / aspect if aspect else start.width()
    else:
        return start
    return fit_width(width, aspect, min_width=min_width, max_width=max_width, max_height=max_height)


def hit_region(image: QtGui.QImage, size: QtCore.QSize, step: int = 6,
               alpha_min: int = 32) -> QtGui.QRegion:
    """画面里不透明的部分 = 鼠标点得中的地方；透明处让鼠标穿过去。

    无边框透明窗的**整块矩形**都会吃鼠标事件，透明像素也吃——把它拖到桌面图标上面之后，
    那一片图标就点不动了（用户实际报的就是这个）。所以按 alpha 抠出她的轮廓当命中区，
    再往外放一格（`step` 像素），边缘仍然抓得到缩放。
    """
    if image is None or image.isNull() or size.width() <= 0 or size.height() <= 0:
        return QtGui.QRegion()
    scaled = image.scaled(size, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.FastTransformation)
    if scaled.format() != QtGui.QImage.Format.Format_Alpha8:
        scaled = scaled.convertToFormat(QtGui.QImage.Format.Format_Alpha8)
    region = QtGui.QRegion()
    for y in range(0, scaled.height(), step):
        for x in range(0, scaled.width(), step):
            if scaled.pixelColor(x, y).alpha() > alpha_min:
                region = region.united(QtGui.QRegion(x, y, step, step))
    for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
        region = region.united(region.translated(dx, dy))
    return region
