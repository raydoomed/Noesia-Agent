"""消息上的表情回应：六个常用 + 全库面板，语义照签收回执（Tapback）。

一条消息上每个作者只留一个：同一个人再点同一个就是取消，点别的就是换。
落库归引擎管（`message.react`），这里只管把列表算对、把挑选器画出来。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

QUICK_REACTIONS = ["❤️", "👍", "👎", "😂", "‼️", "❓"]
DOUBLE_CLICK_REACTION = QUICK_REACTIONS[0]

GROUP_NAMES = {
    "smileys-emotion": "表情",
    "people-body": "人物",
    "animals-nature": "动物与自然",
    "food-drink": "食物与饮料",
    "travel-places": "旅行与地点",
    "activities": "活动",
    "objects": "物品",
    "symbols": "符号",
    "flags": "旗帜",
}

_DATA_PATH = Path(__file__).parent / "data" / "emoji.json"
_data: dict | None = None


def emoji_data() -> dict:
    """整库：`groups` 每项是 (键, 名字)，`emojis` 每项是 (字符, 名称, 分类序号, 次序, 标签)。"""
    global _data
    if _data is None:
        try:
            loaded = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
            _data = loaded if isinstance(loaded, dict) else {"groups": [], "emojis": []}
        except (OSError, ValueError):
            _data = {"groups": [], "emojis": []}
    return _data if _data is not None else {"groups": [], "emojis": []}


def apply_reaction(
    entries: list[dict] | None, emoji: str | None, author: str = "user"
) -> list[dict]:
    """同一个人只占一格：再点同一个是取消，点别的就顶掉原来那个。"""
    current = list(entries or ())
    previous = next((e for e in current if e.get("author") == author), None)
    without = [e for e in current if e.get("author") != author]
    if not emoji or (previous is not None and previous.get("emoji") == emoji):
        return without
    return without + [{"emoji": emoji, "author": author}]


def merge_reactions(
    persisted: list[dict] | None,
    local: list[dict] | None = None,
    agent_live: list[dict] | None = None,
) -> list[dict]:
    """落库的那份打底，本窗口刚点的和 agent 实时来的盖在上面——各占自己那一格。"""
    stored = list(persisted or ())
    if local is None:
        user_side = [e for e in stored if e.get("author") == "user"]
    else:
        user_side = [e for e in local if e.get("author") == "user"]
    if agent_live is None:
        agent_side = [e for e in stored if e.get("author") == "agent"]
    else:
        agent_side = list(agent_live)
    return user_side + agent_side


class _EmojiGrid(QtWidgets.QListWidget):
    """一格一个表情；点一下就把字符抛出去。"""

    chose = QtCore.Signal(str)

    def __init__(self, rows: list[tuple[str, str, str]], cols: int = 9) -> None:
        super().__init__()
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setFlow(QtWidgets.QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setMovement(QtWidgets.QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QtCore.QSize(32, 32))
        self.setIconSize(QtCore.QSize(22, 22))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QListWidget { background: transparent; }"
            "QListWidget::item { border-radius: 6px; }"
            "QListWidget::item:hover { background: rgba(246,223,234,0.14); }"
        )
        font = QtGui.QFont("Segoe UI Emoji")
        font.setPointSize(12)
        self.setFont(font)
        for char, label, key in rows:
            item = QtWidgets.QListWidgetItem(char)
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, char)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, key)
            item.setToolTip(label)
            self.addItem(item)
        self.setFixedHeight(max(1, (len(rows) + cols - 1) // cols) * 32 + 8)
        self.itemClicked.connect(self._on_clicked)

    def _on_clicked(self, item: QtWidgets.QListWidgetItem) -> None:
        char = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if char:
            self.chose.emit(str(char))


class EmojiPanel(QtWidgets.QWidget):
    """全库：上面一行搜索，下面按分类滚。搜索时只留命中的那些。"""

    picked = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(360, 320)
        self.setObjectName("EmojiPanel")
        self.setAutoFillBackground(False)
        self.setStyleSheet("EmojiPanel { background: transparent; }")
        self._groups: list[tuple[QtWidgets.QLabel, _EmojiGrid]] = []
        self._grids: list[_EmojiGrid] = []
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索表情…")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(
            "QLineEdit { background: transparent; border: none;"
            " border-bottom: 1px solid #3a3448; padding: 4px 2px; color: #e8e4ee; }"
            "QLineEdit:focus { border-bottom-color: #6f6288; }"
        )
        self.search.textChanged.connect(self._filter)
        body = QtWidgets.QWidget()
        body.setAutoFillBackground(False)
        body.setStyleSheet("background: transparent;")
        self._body_layout = QtWidgets.QVBoxLayout(body)
        self._body_layout.setContentsMargins(4, 4, 4, 4)
        self._body_layout.setSpacing(2)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        scroll.viewport().setAutoFillBackground(False)
        scroll.verticalScrollBar().setStyleSheet(
            "QScrollBar:vertical { width: 6px; background: transparent; border: none; }"
            "QScrollBar::handle:vertical { background: #463d55; border-radius: 3px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self.search)
        layout.addWidget(scroll, 1)
        self._build(body)
        self._scroll = scroll

    def _build(self, body: QtWidgets.QWidget) -> None:
        data = emoji_data()
        buckets: dict[int, list[tuple[str, str, str]]] = {}
        catalog = data.get("emojis") or []
        for row in catalog:
            if len(row) < 5:
                continue
            char, label, group, _order, tags = row[0], row[1], row[2], row[3], row[4]
            key = f"{label}|{tags}".lower()
            buckets.setdefault(int(group), []).append((char, label, key))
        bins = data.get("groups") or []
        for index, (key, name) in enumerate(bins):
            rows = buckets.get(index) or []
            if not rows:
                continue
            header = QtWidgets.QLabel(GROUP_NAMES.get(key, name))
            header.setStyleSheet("QLabel { color: #8b8492; font-size: 8.5pt; padding: 6px 2px 2px 2px; }")
            grid = _EmojiGrid(rows)
            grid.chose.connect(self.picked)
            self._body_layout.addWidget(header)
            self._body_layout.addWidget(grid)
            self._groups.append((header, grid))
            self._grids.append(grid)
        self._body_layout.addStretch(1)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for header, grid in self._groups:
            shown = 0
            for i in range(grid.count()):
                item = grid.item(i)
                visible = not needle or needle in str(item.data(QtCore.Qt.ItemDataRole.UserRole + 1) or "")
                item.setHidden(not visible)
                shown += 0 if not visible else 1
            header.setVisible(shown > 0)
            grid.setVisible(shown > 0)
            if shown:
                grid.setFixedHeight(min(4, max(1, (shown + 8) // 9)) * 32 + 8)

    def focus_search(self) -> None:
        self.search.setFocus()


class ReactionPicker(QtWidgets.QFrame):
    """悬在消息旁边的小面板：一排常用，末尾一个「+」摊开成全库。"""

    picked = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None, selected: str = "") -> None:
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            "ReactionPicker { background: #241f2e; border: 1px solid #3b3448; border-radius: 12px; }"
        )
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(4)
        self._row = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self._row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for char in QUICK_REACTIONS:
            button = QtWidgets.QToolButton()
            button.setText(char)
            button.setFixedSize(30, 30)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.setToolTip(char)
            chosen = char == selected
            button.setStyleSheet(
                "QToolButton { border: none; border-radius: 8px; font-size: 14pt;"
                + (" background: rgba(246,223,234,0.18);" if chosen else " background: transparent;")
                + " } QToolButton:hover { background: rgba(246,223,234,0.14); }"
            )
            button.clicked.connect(lambda _c=False, e=char: self._pick(e))
            row.addWidget(button)
        more = QtWidgets.QToolButton()
        more.setText("+")
        more.setFixedSize(30, 30)
        more.setToolTip("更多表情")
        more.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        more.setStyleSheet(
            "QToolButton { border: none; border-radius: 8px; font-size: 12pt; color: #b9b2c4;"
            " background: transparent; } QToolButton:hover { background: rgba(246,223,234,0.14); }"
        )
        more.clicked.connect(self._expand)
        row.addWidget(more)
        self._layout.addWidget(self._row)
        self._panel: EmojiPanel | None = None
        self.adjustSize()

    def _pick(self, char: str) -> None:
        self.picked.emit(char)
        self.close()

    def _expand(self) -> None:
        if self._panel is None:
            self._panel = EmojiPanel(self)
            self._panel.picked.connect(self._pick)
            self._layout.addWidget(self._panel)
        self._row.hide()
        self._panel.show()
        self.adjustSize()
        self._keep_on_screen()
        self._panel.focus_search()

    def _keep_on_screen(self) -> None:
        screen = QtGui.QGuiApplication.screenAt(self.pos()) or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = min(max(self.x(), area.left() + 8), area.right() - self.width() - 8)
        y = min(max(self.y(), area.top() + 8), area.bottom() - self.height() - 8)
        if x != self.x() or y != self.y():
            self.move(x, y)
