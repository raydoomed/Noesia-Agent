"""Left session column: one row per conversation, grouped by recency, click to switch."""
from __future__ import annotations

import datetime
import time

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

ROW_HEIGHT = 26
DIVIDER_HEIGHT = 26
MIN_WIDTH = 132
MAX_WIDTH = 240
WIDTH_RATIO = 0.42
MORE_WIDTH = 16


def sidebar_width(panel_width: int) -> int:
    """The list takes about 42% of the panel; both sides stay readable at any size."""
    return max(MIN_WIDTH, min(MAX_WIDTH, int(panel_width * WIDTH_RATIO)))


def age_label(now: float, ts: float) -> str:
    delta = max(0.0, float(now) - float(ts or 0))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分"
    if delta < 86400:
        return f"{int(delta // 3600)} 时"
    return f"{int(delta // 86400)} 天"


def day_bucket(now: float, ts: float) -> tuple[str, str]:
    """(key, label) of the calendar divider a session belongs under."""
    day = datetime.date.fromtimestamp(ts)
    today = datetime.date.fromtimestamp(now)
    diff = (today - day).days
    if diff <= 0:
        return "today", "今天早些时候"
    if diff == 1:
        return "yesterday", "昨天"
    week_start = today - datetime.timedelta(days=today.weekday())
    if day >= week_start:
        return "this-week", "本周"
    if day >= week_start - datetime.timedelta(days=7):
        return "last-week", "上周"
    if day.year == today.year and day.month == today.month:
        return "this-month", "本月"
    if day.year == today.year:
        return f"m-{day.year}-{day.month}", f"{day.month} 月"
    return f"my-{day.year}-{day.month}", f"{day.year} 年 {day.month} 月"


def _recency(session: dict) -> float:
    return float(session.get("last_active") or session.get("started_at") or 0)


def session_title(session: dict) -> str:
    """列表里那一行的字：引擎还没起标题就写「无标题会话」，不拿 id 前缀凑数。"""
    return str(session.get("title") or "").strip() or "无标题会话"


class _Row(QtWidgets.QFrame):
    """One session: title on the left, age on the right, the selected row tinted.

    Hovering shows a ⋯ that opens 重命名 / 置顶 / 删除; right-click opens the same menu, so the
    actions stay reachable without hovering first.
    """

    clicked = QtCore.Signal(str)
    action = QtCore.Signal(str, str)

    def __init__(self, session: dict, now: float, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ROW_HEIGHT)
        self._id = str(session.get("id") or session.get("session_id") or "")
        self._full = session_title(session)
        self._pinned = bool(session.get("pinned"))
        # 压缩过的会话行里 id 是谱系根、session_id 才是能挂上去的那一支。
        self._sid = str(session.get("session_id") or self._id)
        self._keys = {value for value in (self._id, self._sid, str(session.get("resolved_id") or "")) if value}
        box = QtWidgets.QHBoxLayout(self)
        box.setContentsMargins(8, 0, 8, 0)
        box.setSpacing(6)
        self._pin = QtWidgets.QLabel("●", self)
        self._pin.setObjectName("rowpin")
        self._pin.setVisible(self._pinned)
        self._title = QtWidgets.QLabel(self)
        self._title.setObjectName("rowtitle")
        self._age = QtWidgets.QLabel(age_label(now, _recency(session)), self)
        self._age.setObjectName("rowage")
        self._more = QtWidgets.QPushButton("⋯", self)
        self._more.setObjectName("rowmore")
        self._more.setFixedSize(MORE_WIDTH, 18)
        self._more.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._more.setToolTip("重命名 / 置顶 / 删除")
        self._more.clicked.connect(lambda: self._open_menu(
            self._more.mapToGlobal(QtCore.QPoint(0, self._more.height()))))
        box.addWidget(self._pin, 0)
        box.addWidget(self._title, 1)
        box.addWidget(self._age, 0)
        box.addWidget(self._more, 0)
        self.setToolTip(self._full)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self._open_menu(self.mapToGlobal(pos)))
        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_for(self, session_id: str) -> bool:
        return bool(session_id) and session_id in self._keys

    def _open_menu(self, at: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self)
        menu.addAction("重命名", lambda: self.action.emit(self._sid, "rename"))
        menu.addAction("取消置顶" if self._pinned else "置顶",
                       lambda: self.action.emit(self._sid, "unpin" if self._pinned else "pin"))
        menu.addSeparator()
        menu.addAction("删除", lambda: self.action.emit(self._sid, "delete"))
        menu.exec(at)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        used = 22 + self._age.sizeHint().width() + MORE_WIDTH + 6
        if not self._pin.isHidden():
            used += self._pin.sizeHint().width() + 6
        room = max(24, self.width() - used)
        self._title.setText(self._title.fontMetrics().elidedText(
            self._full, QtCore.Qt.TextElideMode.ElideRight, room))

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self._sid)
        super().mouseReleaseEvent(event)


class SessionList(QtWidgets.QWidget):
    """The column itself: header, group dividers, one clickable row per session."""

    switch_requested = QtCore.Signal(str)
    session_action = QtCore.Signal(str, str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        # A plain QWidget ignores the stylesheet background unless it is told to paint it;
        # without this the column falls back to Qt's light window colour.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self._current = ""
        self._rows: list[dict] = []
        self._by_id: dict[str, _Row] = {}
        head = QtWidgets.QLabel("会话", self)
        head.setObjectName("shead")
        self._head = head
        self._body = QtWidgets.QWidget(self)
        self._body.setObjectName("sidebarBody")
        self._body.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self._box = QtWidgets.QVBoxLayout(self._body)
        self._box.setContentsMargins(4, 0, 4, 4)
        self._box.setSpacing(1)
        area = QtWidgets.QScrollArea(self)
        area.setObjectName("sidebarscroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._body)
        area.viewport().setAutoFillBackground(False)
        self._area = area
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 6, 6)
        outer.setSpacing(4)
        outer.addWidget(head)
        outer.addWidget(area, 1)
        self._rebuild()

    def set_sessions(self, rows, current_id: str = "") -> None:
        self._rows = [row for row in (rows or [])
                      if isinstance(row, dict) and (row.get("id") or row.get("session_id"))]
        if current_id:
            self._current = str(current_id)
        self._rebuild()

    def current(self) -> str:
        return self._current

    def set_current(self, session_id: str) -> None:
        """标出正在聊的那场；空串 = 一场都没挂（删掉当前那场之后就是这样）。"""
        sid = str(session_id or "")
        if sid == self._current:
            return
        self._current = sid
        for row in self._by_id.values():
            row.set_selected(row.is_for(sid))

    def title_for(self, session_id: str) -> str:
        row = next((r for r in self._by_id.values() if r.is_for(str(session_id or ""))), None)
        return row._full if row is not None else ""

    def groups(self) -> list[tuple[str, str, list[dict]]]:
        """Pinned first, then one block per calendar bucket (engine order kept inside)."""
        blocks: list[tuple[str, str, list[dict]]] = []
        pinned = sorted((s for s in self._rows if s.get("pinned")), key=_recency, reverse=True)
        if pinned:
            blocks.append(("pinned", "置顶", pinned))
        now = time.time()
        for session in self._rows:
            if session.get("pinned"):
                continue
            key, label = day_bucket(now, _recency(session))
            if blocks and blocks[-1][0] == key:
                blocks[-1][2].append(session)
            else:
                blocks.append((key, label, [session]))
        return blocks

    def _rebuild(self) -> None:
        keep = self._area.verticalScrollBar().value()
        self._by_id = {}
        while self._box.count():
            item = self._box.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        if not self._rows:
            self._head.setText("会话")
            empty = QtWidgets.QLabel("还没有会话", self._body)
            empty.setObjectName("divider")
            empty.setFixedHeight(DIVIDER_HEIGHT)
            self._box.addWidget(empty)
            self._box.addStretch(1)
            return
        now = time.time()
        self._head.setText(f"会话 · {len(self._rows)}")
        for _key, label, items in self.groups():
            divider = QtWidgets.QLabel(label, self._body)
            divider.setObjectName("divider")
            divider.setFixedHeight(DIVIDER_HEIGHT)
            self._box.addWidget(divider)
            for session in items:
                row = _Row(session, now, self._body)
                self._by_id[row._id] = row
                row.set_selected(row.is_for(self._current))
                row.clicked.connect(self._on_click)
                row.action.connect(self.session_action.emit)
                self._box.addWidget(row)
        self._box.addStretch(1)
        if keep:
            QtCore.QTimer.singleShot(0, lambda: self._area.verticalScrollBar().setValue(keep))

    def _on_click(self, session_id: str) -> None:
        row = next((r for r in self._by_id.values() if r.is_for(session_id)), None)
        if row is not None and row.is_for(self._current):
            return  # 点的就是当前这场，不用再挂一遍
        if session_id:
            self.switch_requested.emit(session_id)
