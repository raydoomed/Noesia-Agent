"""聊天输入面板：悬浮窗旁的一个小窗，负责「打字 → 发送 → 显示流式回复」。

无边框 + 置顶 + 半透明圆角，和头像窗同一套视觉；不进任务栏，Esc 收起。
只做 UI，不知道 IPC 细节：发出去走 `submitted` 信号，收回来由 `append_chunk` 之类的方法喂。
"""

from __future__ import annotations

import json

import html
import re
import ctypes
import hashlib
import logging
import time
from pathlib import Path
from typing import Callable, cast
from urllib.parse import unquote, urlparse

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtNetwork as QtNetwork
import PySide6.QtWidgets as QtWidgets

from app import (
    audio_devices,
    disclosure,
    file_preview,
    global_hotkey,
    markdown_render,
    media_links,
    voice_input_prefs,
)
from app.chat_view import ChatView
from app.hover_actions import DIFF_PREVIEW_LABEL, HoverActionButton, icon_pixmap, svg_icon
from app.approval_dialog import ConfirmDialog
from app.photo_view import PhotoView
from app.speech_bar import SpeechBar
from app.status_stack import StatusStack
from app.voice_input import SILENT_PEAK, VoiceRecorder, peak_of, write_wav
from app.voice_orb import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_THINKING, VoiceOrb
from app.tool_view.i18n import tr as tool_tr

logger = logging.getLogger(__name__)

_ERROR_NOISE = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*|/(?:[\w.\-]+/)+[\w.\-]+|\b[0-9a-f]{8,}\b|\d+|\s+")


MAX_REVIEW_SUMMARIES = 100

_SCROLL_KEYS = frozenset({
    QtCore.Qt.Key.Key_Up,
    QtCore.Qt.Key.Key_Down,
    QtCore.Qt.Key.Key_PageUp,
    QtCore.Qt.Key.Key_PageDown,
    QtCore.Qt.Key.Key_Home,
    QtCore.Qt.Key.Key_End,
})


def _error_key(text: str) -> str:
    """同一条错误 → 同一个指纹：去掉路径、数字、空白的差别再比。"""
    return _ERROR_NOISE.sub("#", (text or "").strip().lower())


class _Input(QtWidgets.QPlainTextEdit):
    """输入框：Qt 自带的 placeholder 只认 document 有没有内容，而输入法组着字时那几个字
    还在预编辑里、没进 document，于是它照样画，跟正在打的拼音叠在一起。这里自己画，
    位置直接取光标矩形——跟正文第一行同一条基线，不用手算 padding。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._ph_text = ""
        self._ph_on = False

    def set_placeholder(self, text: str) -> None:
        self._ph_text = text
        self.viewport().update()

    def set_placeholder_visible(self, on: bool) -> None:
        if self._ph_on != on:
            self._ph_on = on
            self.viewport().update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._ph_on or not self._ph_text:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.setPen(QtGui.QColor(255, 255, 255, 88))
        painter.setFont(self.font())
        rect = self.cursorRect()
        rect.setWidth(max(0, self.viewport().width() - rect.x() - 2))
        painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, self._ph_text)
        painter.end()


class _CodeMark(QtGui.QTextBlockUserData):
    """「这一块是代码块」——复制按钮靠它命中，不再靠底色猜（底色的 HTML 行为不稳）。"""


class _MsgMark(QtGui.QTextBlockUserData):
    """「这一块是一条消息的开头」——悬停时靠它从光标所在块往上找回整条消息。

    存的是消息字典本身（不是副本）：流式回答还在往里加字，复制拿到的就是最后的样子。
    """

    def __init__(self, msg: dict) -> None:
        super().__init__()
        self.msg = msg


class _BarMark(QtGui.QTextBlockUserData):
    """动作条占的那一段：给按钮留的位置，不是一条消息的开头。"""

    def __init__(self, msg: dict) -> None:
        super().__init__()
        self.msg = msg


class _PhotoMark(QtGui.QTextBlockUserData):
    """用户消息里的图片那一段：属于那条消息，但不是它的开头（气泡不从这里另起一个）。

    属性叫 `owner` 而不叫 `msg`：气泡收集按「带 msg 的块」数气泡，叫 msg 会多画一个。
    """

    def __init__(self, msg: dict) -> None:
        super().__init__()
        self.owner = msg


MSG_GAP = 12
MSG_GAP_TIGHT = 4


class _JumpPill(QtWidgets.QWidget):
    """没看完的时候浮在正文底下那颗胶囊：图标 + 「滚动到底部」。

    桌面端那颗就是长这样：圆角胶囊、图标在左、文字在右，锚在输入框上方居中。
    这里不能交给 QSS——正文视口里的子控件画不出 `background`（只画得出边框和字），
    所以整颗自己画。
    """

    HEIGHT = 32
    RADIUS = 16
    PAD_X = 12
    GAP = 6
    ICON_PX = 14
    LABEL = "滚动到底部"

    FILL = QtGui.QColor(34, 36, 44, 246)
    FILL_HOVER = QtGui.QColor(48, 51, 62, 250)
    BORDER = QtGui.QColor(255, 255, 255, 46)
    TEXT = QtGui.QColor(214, 218, 229)
    TEXT_HOVER = QtGui.QColor(255, 255, 255)

    clicked = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._hover = False
        self._font = QtGui.QFont(self.font())
        self._font.setPixelSize(12)
        self.setFont(self._font)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setToolTip(self.LABEL)
        self.setFixedHeight(self.HEIGHT)
        metrics = QtGui.QFontMetrics(self._font)
        self.setFixedWidth(self.PAD_X * 2 + self.ICON_PX + self.GAP + metrics.horizontalAdvance(self.LABEL))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        box = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QtGui.QPen(self.BORDER, 1))
        painter.setBrush(self.FILL_HOVER if self._hover else self.FILL)
        painter.drawRoundedRect(box, self.RADIUS, self.RADIUS)

        color = self.TEXT_HOVER if self._hover else self.TEXT
        icon = icon_pixmap("arrow-down", color.name(), self.ICON_PX, self.devicePixelRatioF())
        if icon is not None:
            painter.drawPixmap(self.PAD_X, (self.height() - self.ICON_PX) // 2, icon)
        painter.setPen(color)
        text = QtCore.QRect(self.PAD_X + self.ICON_PX + self.GAP, 0, self.width(), self.height())
        painter.drawText(text, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, self.LABEL)
        painter.end()

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _StickyPrompt(QtWidgets.QWidget):
    """这一轮的回答还在屏幕上时，把你这轮的提示词钉在正文顶上。

    桌面上它就是同一条气泡加了 `position: sticky`：气泡还占着原位，被它盖住的
    内容由 `use-sticky-prompt-clip` 裁掉，不是往上糊一块实心矩形。照这个做，就不该
    自己画底色——这条带占的是正文视口让出来的那一块，本来露着面板背景，等于裁掉了。
    只作展示：不挂按钮、不接鼠标，滚轮落上去直接交给正文。
    """

    PAD = 8
    EDGE_PX = 1
    NAME = QtGui.QColor("#c9a6e0")
    TEXT = QtGui.QColor(214, 198, 222, 215)

    def __init__(self, parent: QtWidgets.QWidget, edge: QtGui.QColor,
                 name_font: QtGui.QFont, text_font: QtGui.QFont) -> None:
        super().__init__(parent)
        self._edge = edge
        self._name_font = name_font
        self._text_font = text_font
        self._name = ""
        self._shown = ""
        self._inset = 0.0
        self._gap = 0
        self._top = 0.0
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.hide()

    def set_line(self, name: str, shown: str) -> None:
        if name == self._name and shown == self._shown:
            return
        self._name = name
        self._shown = shown
        self.update()

    def set_layout(self, margin: float, gap: int, top: float) -> None:
        """气泡画在哪：margin 是左边距（跟文档里那条同一个数），gap 是下面那段
        间距（对应桌面端粘顶容器的 `pb-(--conversation-turn-gap)`），top 是气泡
        外沿相对带子顶的位置——被自己这一轮的底边顶上去时会变成负数。
        """
        key = (float(margin), int(gap), float(top))
        if key == (self._inset, self._gap, self._top):
            return
        self._inset = float(margin)
        self._gap = int(gap)
        self._top = float(top)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        margin = self._inset
        top = self._top
        painter = QtGui.QPainter(self)
        box = QtCore.QRectF(
            margin + 0.5,
            top + 0.5,
            self.width() - 2 * margin - 1,
            self.height() - top - self._gap - 1,
        )
        painter.setPen(QtGui.QPen(self._edge, self.EDGE_PX))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(box)

        metrics = QtGui.QFontMetricsF(self._text_font)
        baseline = top + self.EDGE_PX + self.PAD + metrics.ascent()
        x = margin + self.EDGE_PX + self.PAD
        painter.setFont(self._name_font)
        painter.setPen(self.NAME)
        head = f"{self._name} "
        painter.drawText(QtCore.QPointF(x, baseline), head)
        painter.setFont(self._text_font)
        painter.setPen(self.TEXT)
        painter.drawText(
            QtCore.QPointF(x + QtGui.QFontMetricsF(self._name_font).horizontalAdvance(head), baseline),
            self._shown,
        )
        painter.end()


_SELF_BLOCK_TAGS = ("<ul", "<ol", "<table", "<hr")
_HEADING_RE = re.compile(r"^\s*<h([1-6])[^>]*>(.*)</h\1>\s*$", re.S | re.I)
_HEADING_PX = {1: 20, 2: 18, 3: 16, 4: 15, 5: 14, 6: 14}
_QUOTE_TAG = "<blockquote"
_QUOTE_COLOR = "#b9c0cf"


def _starts_own_block(markup: str) -> bool:
    """这段 HTML 是不是「Qt 会自己开段」的块级元素。"""
    return markup.lstrip().lower().startswith(_SELF_BLOCK_TAGS)


def _paint_range(cursor: QtGui.QTextCursor, start: int, end: int, char_format: QtGui.QTextCharFormat) -> None:
    """把字符格式刷到 [start, end) 这段已插入的文字上，刷完把光标放回末尾。"""
    if end <= start:
        return
    cursor.setPosition(start)
    cursor.setPosition(end, QtGui.QTextCursor.MoveMode.KeepAnchor)
    cursor.mergeCharFormat(char_format)
    cursor.clearSelection()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)


def _plain_block_format() -> QtGui.QTextBlockFormat:
    """普通段落：只留 6px 上间距，不带上一段的底色/缩进（代码块的底色否则会渗下来）。"""
    block_format = QtGui.QTextBlockFormat()
    block_format.setTopMargin(6)
    block_format.setLineHeight(130, QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    return block_format


def _keep_edge_spaces(markup: str) -> str:
    """把片段首尾的空格换成 `&nbsp;`：否则流式时字会粘在一起。

    `QTextCursor.insertHtml()` 会在片段边界把空白吃掉。流式是按 token 一段段插的，
    `` `docx` Word · `xlsx` `` 被切成好几片之后，屏幕上就成了 `docxWord·xlsxExcel`——
    这正是用户看到的「清单糊成一坨」。片段内部的空格不受影响，只保护首尾。
    """
    if not markup:
        return markup
    lead = markup[: len(markup) - len(markup.lstrip(" "))]
    trail = markup[len(markup.rstrip(" ")) :]
    core = markup.strip(" ")
    if not core:
        return "&nbsp;" * len(markup)
    return lead.replace(" ", "&nbsp;") + core + trail.replace(" ", "&nbsp;")


_PHOTO_CACHE = 24
_ORIGINAL_CACHE = 6
_THUMB_WIDTH = 200
_HINT_TEXT = "Enter 发送 · Esc 收起"

_IMAGE_SUFFIX = {
    "png": ".png",
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
    "svg": ".svg",
}


def _image_extension(data: bytes) -> str:
    """看字节本身认格式。认不出来按 .png 兜底（跟 desktop 一样）。"""
    buffer = QtCore.QBuffer()
    buffer.setData(QtCore.QByteArray(data))
    buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
    fmt = bytes(QtGui.QImageReader(buffer).format().data()).decode("ascii", "ignore").lower()
    return _IMAGE_SUFFIX.get(fmt, ".png")


def _photo_name(url: str, data: bytes | None = None) -> str:
    """保存时的默认文件名：取网址最后一段，没后缀就按图片真实格式补一个。

    图片地址经常没有后缀（`.../pexels-photo-33999682.jpeg?auto=compress` 这类还行，
    生成图的地址干脆就是一串 hash），硬补 `.jpg` 会把 webp/gif 存成双击打不开的 .jpg。
    """
    parsed = urlparse(url)
    base = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) or "图片"
    if Path(base).suffix:
        return base
    return base + (_image_extension(data) if data else ".png")


def save_photo_bytes(parent: QtWidgets.QWidget, data: bytes, name: str) -> str | None:
    """问存哪儿、把原图字节写下去。存成了返回落地的路径，取消或者写失败返回空。

    保存框默认落在系统「下载」夹（没有就退回保存框上次的位置），跟 desktop 一致。
    """
    folder = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.DownloadLocation
    )
    default = str(Path(folder) / name) if folder else name
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        parent,
        "保存图片",
        default,
        "图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;所有文件 (*)",
    )
    if not path:
        return None
    try:
        Path(path).write_bytes(data)
    except OSError as err:
        QtWidgets.QMessageBox.warning(parent, "存不下来", str(err))
        return None
    return path


def _open_link(text: str) -> bool:
    """这一段里有个图片或链接还没写完（`![说明](地址` 还差最后那个括号）。

    回复是一小块一小块推来的：`![冰岛极光](https://thumb.wiki` 这时候照行内画出去，
    屏幕上就留下那行原始 markdown，后面几块再补也接不回成图片——只能等括号到齐再整块画。
    """
    start = text.rfind("[")
    if start < 0:
        return False
    rest = text[start:]
    closing = rest.find("]")
    if closing < 0:
        return True
    opened = rest.find("(", closing)
    if opened < 0:
        return False
    return ")" not in rest[opened:]


from app.user_state import window_settings
from app.env_keys import env_file
from app.tool_view.ansi import has_ansi_codes as ansi_codes, parse_ansi
from app.tool_scroll import ToolScroll
from app.tool_view.diffview import diff_rows, strip_diff_file_headers
from app.tool_view.code import highlight_code as code_highlight
from app.tool_view import delegate as delegate_view
from app.tool_view.fmt import format_duration_seconds
from app.tool_view.helpers import is_file_edit_tool, strip_ansi
from app.tool_view.icons import icon_svg
from app.tool_view.codicon_icons import codicon_for_filename
from app.tool_view.model import count_diff_line_stats, looks_redundant, tool_meta
from app.model_menu import current_label, fill_menu, group_models
from app.window_geometry import (
    cursor_for,
    edges_at,
    fit_free_size,
    rect_after_resize,
    size_from_drag_free,
)

PANEL_WIDTH = 420
PANEL_HEIGHT = 460
MIN_PANEL_WIDTH = 280
MIN_PANEL_HEIGHT = 300
TITLE_FULL = "Noesia Agent"
TITLE_SHORT = "Noesia"
TITLE_FULL_MIN_WIDTH = 430

_USER_NAME_CACHE: dict[str, object] = {"stamp": None, "name": ""}


def _name_from_profile(text: str) -> str:
    """从 USER.md 正文里挑出名字：只认 `Name: X` / `名字：X` 这种键值行，最多 12 个字。"""
    found = re.search(
        r"^[ \t]*(?:name|名字)[ \t]*[:：][ \t]*(.+?)[ \t]*$", text, re.MULTILINE | re.IGNORECASE
    )
    return " ".join(found.group(1).split())[:12] if found else ""


def _user_display_name() -> str:
    """用户那条冒什么名：USER.md 里记着名字就用名字，没有就叫「你」。

    引擎起名那一轮会把名字写成一条 `Name: Ray`，所以这里只认这种键值行（中英文冒号都认）。
    档案还没写、读不到、或者行不成样子，一律退回「你」——宁可叫「你」，不能叫错。
    """
    path = env_file().with_name("memories") / "USER.md"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return "你"
    if _USER_NAME_CACHE["stamp"] == stamp:
        return str(_USER_NAME_CACHE["name"] or "你")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        text = ""
    name = _name_from_profile(text)
    _USER_NAME_CACHE["stamp"] = stamp
    _USER_NAME_CACHE["name"] = name
    return name or "你"


RESIZE_MARGIN = 12
SNAP_DISTANCE = 24

STYLE = """
#frame {
    background-color: rgba(25, 21, 32, 210);
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: 16px;
}
#title { color: #f6dfea; font-size: 13px; font-weight: 600; letter-spacing: 0.6px; }
#hint  { color: rgba(255, 255, 255, 90); font-size: 10.5px; }
#state { color: rgba(240, 178, 210, 200); font-size: 11px; }
#dot { color: #7fe0ac; font-size: 9px; }
/* 输入框上方那一叠状态（引擎提示 / 后台进程 / 正在调用）：一行一个，暗底小字 */
#statusrow {
    background-color: rgba(255, 255, 255, 12);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 8px;
}
#statusnote { color: #cfd6e4; font-size: 11px; }
#statuswarn { color: #ffd166; font-size: 11px; }
#statusbusy { color: rgba(240, 178, 210, 210); font-size: 11px; }
#statusdone { color: #8fd8a8; font-size: 11px; }
#statusfailed { color: #ff6b6b; font-size: 11px; }  /* 真报错用他定的红；设置页 FAIL_STYLE 也是这个值 */
/* 后台任务那张卡的标题：默认字色太暗（在深色底上几乎看不见），提亮 + 拉开一点间距 */
#statusgroup {
    background-color: rgba(255, 255, 255, 8);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 8px;
    padding: 3px 2px;
}
#statusgroup QToolButton {
    color: #e2e9f6;
    font-size: 11.5px;
    font-weight: 600;
    background: transparent;
    border: none;
    padding: 2px 4px;
}
#statusgroup QToolButton:hover { color: #ffd7e6; }
#statusrow QToolButton {
    background: transparent;
    border: none;
    color: rgba(255, 255, 255, 150);
    font-size: 11px;
    padding: 0 4px;
}
#statusrow QToolButton:hover { color: #ffd7e6; }
#modelrow { color: rgba(255, 255, 255, 120); font-size: 11px; }
#model, #model::menu-indicator { background: transparent; }
#model {
    background-color: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 36);
    border-radius: 6px;
    color: rgba(255, 215, 230, 235);
    font-size: 11px;
    padding: 2px 6px;
    text-align: left;
}
#model:hover { border: 1px solid rgba(240, 178, 210, 120); }
#model:disabled { color: rgba(255, 255, 255, 90); }
QMenu {
    background-color: #241f2e;
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 10px;
    padding: 6px;
    color: #ece6ef;
    font-size: 12px;
}
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 6px; }
QMenu::item:selected { background-color: rgba(240, 178, 210, 52); color: #ffffff; }
QMenu::item:disabled { color: #6b6b76; }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 20); margin: 5px 10px; }
QMenu::indicator { width: 14px; height: 14px; }
#view {
    background: transparent;
    border: none;
    color: #ece6ef;
    font-size: 13px;
}
#view QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0px;
}
#view QScrollBar::handle:vertical {
    background: rgba(236, 230, 239, 38);
    border-radius: 4px;
    min-height: 28px;
}
#view QScrollBar::handle:vertical:hover {
    background: rgba(236, 230, 239, 72);
}
#view QScrollBar::add-line:vertical, #view QScrollBar::sub-line:vertical {
    height: 0px;
}
#view QScrollBar::add-page:vertical, #view QScrollBar::sub-page:vertical {
    background: transparent;
}
/* 输入框外壳：审批/思考两个图标在框里面，窗口拉窄也不会把输入框挤没 */
#inputbox {
    background-color: rgba(255, 255, 255, 22);
    border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 17px;
}
#inputbox[focused="true"] { border: 1px solid rgba(240, 178, 210, 150); }
/* 语音模式：整条输入区就是这一颗球，不要框、不要底——有框就像「球被装在一个盒子里」 */
#inputbox[voice="true"] {
    background-color: transparent;
    border: none;
}
#attachbar {
    background-color: rgba(255, 255, 255, 12);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 8px;
}
#attachchip {
    background-color: rgba(255, 255, 255, 20);
    border-radius: 6px;
}
#attachchip QLabel { color: #d8e2ee; font-size: 11px; }
#input {
    background: transparent;
    border: none;
    color: #ffffff;
    padding: 6px 2px;
    font-size: 13px;
}
#input:focus { border: none; }
/* 输入框长到 5 行以上才冒出来的细滚动条：低调些，别把字挤窄 */
#input QScrollBar:vertical {
    background: transparent;
    width: 5px;
    margin: 2px 0;
}
#input QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 55);
    border-radius: 2px;
    min-height: 18px;
}
#input QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 95); }
#input QScrollBar::add-line:vertical,
#input QScrollBar::sub-line:vertical { height: 0; }
#input QScrollBar::add-page:vertical,
#input QScrollBar::sub-page:vertical { background: transparent; }
#send {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f7b6d2, stop:1 #c37ba6);
    border: none;
    border-radius: 16px;
    padding: 0px;
    color: #2b1520;
    font-size: 11px;
    font-weight: 600;
    min-width: 32px;
    max-height: 32px;
    min-height: 32px;
}
#send:hover { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffc9de, stop:1 #d18db5); }
/* 念出来那条进度：浅蓝细条，和输入框一个调子；没人念的时候整条藏起来不占地方 */
#speechbar {
    background: rgba(240, 178, 210, 16);
    border: 1px solid rgba(240, 178, 210, 44);
    border-radius: 9px;
}
#speechtitle { color: #f0dde8; font-size: 12px; }
#speechclock { color: rgba(240, 221, 232, 140); font-size: 11px; }
#speechprogress { background: rgba(255, 255, 255, 22); border: none; border-radius: 2px; }
#speechprogress::chunk { background: rgba(247, 182, 210, 210); border-radius: 2px; }
#speechbar QToolButton { color: rgba(240, 221, 232, 190); font-size: 11px; }
/* 输入框里的图标键：审批盾牌 / 思考火花，低调，别跟「发送」抢眼 */
#icon {
    background: transparent;
    border: none;
    border-radius: 5px;
    padding: 0px;
}
#icon:hover { background-color: rgba(240, 178, 210, 40); border-radius: 7px; }
#icon:checked { background-color: rgba(240, 178, 210, 66); border-radius: 7px; }
#icon::menu-indicator { image: none; }
#mini {
    background-color: rgba(255, 255, 255, 14);
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 6px;
    color: rgba(255, 255, 255, 190);
    font-size: 11px;
    padding: 2px 8px;
}
#mini:hover { border: 1px solid rgba(240, 178, 210, 120); color: #ffffff; }
"""


APPROVAL_TINT = {"manual": "#ffcf8a", "smart": "#e8e8ef", "off": "#ff9a9a"}

_THINKING_LABEL = "思考中"

_APPROVAL_MODES = (
    ("manual", "手动 · 执行需要审批的操作前询问"),
    ("smart", "智能 · 自动评估操作，并在需要时询问"),
    ("off", "关闭 · 不显示审批提示，直接运行"),
)

INPUT_MAX_LINES = 5

_REASONING_LEVELS = (
    ("minimal", "最小"),
    ("low", "低"),
    ("medium", "中"),
    ("high", "高"),
    ("xhigh", "极高"),
    ("max", "最高"),
    ("ultra", "超高"),
)
_REASONING_OFF = ("none", "关闭思考")


_THOUGHT_DONE = "已思考"

_BREATHE_STAGES = (
    (),
    ((1, 0),),
    ((0, 1), (2, 0)),
    ((0, 0), (1, 1), (3, 0)),
    ((0, 0), (1, 1), (2, 0), (3, 1)),
    ((0, 0), (0, 1), (1, 1), (2, 0), (3, 1)),
    ((0, 0), (0, 1), (1, 0), (2, 1), (3, 0), (3, 1)),
    ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (3, 0), (3, 1)),
    ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)),
)


def _breathe_frames() -> tuple[tuple[tuple[int, int], ...], ...]:
    """运行中抬头那颗图标：照桌面端 `GlyphSpinner spinner="breathe"` 的 17 帧，每帧 100ms。

    每一帧是点阵里亮着的 (行, 列)，4 行 2 列；正放再倒放，看起来就是一次呼吸。
    """
    stages = list(_BREATHE_STAGES)
    return tuple(stages + stages[-2::-1])


_BREATHE_FRAMES = _breathe_frames()
_SPIN_INTERVAL_MS = 100


def _glyph_pixmap(kind: str, color: str, size: int = 14) -> QtGui.QPixmap:
    """画 18px 网格里的线条图标（2x 采样，缩放后边缘不发虚）。

    自己画而不是塞 emoji：emoji 在各家 Windows 上字形/色板都不一样，线条图标
    跟这套深色界面更配，也不用带图片资源。
    """
    scale = 2
    pix = QtGui.QPixmap(size * scale, size * scale)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale * size / 18.0, scale * size / 18.0)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    if kind == "shield":
        path = QtGui.QPainterPath()
        path.moveTo(9.0, 2.4)
        path.lineTo(15.0, 4.9)
        path.lineTo(15.0, 9.0)
        path.cubicTo(15.0, 12.9, 12.4, 15.2, 9.0, 16.6)
        path.cubicTo(5.6, 15.2, 3.0, 12.9, 3.0, 9.0)
        path.lineTo(3.0, 4.9)
        path.closeSubpath()
        painter.drawPath(path)
        tick = QtGui.QPainterPath()
        tick.moveTo(6.3, 9.1)
        tick.lineTo(8.3, 11.2)
        tick.lineTo(11.9, 7.3)
        painter.drawPath(tick)
    elif kind == "plus":
        painter.drawLine(QtCore.QPointF(9.0, 4.6), QtCore.QPointF(9.0, 13.4))
        painter.drawLine(QtCore.QPointF(4.6, 9.0), QtCore.QPointF(13.4, 9.0))
    elif kind == "gear":
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.drawEllipse(QtCore.QPointF(9.0, 9.0), 4.2, 4.2)
        pen.setWidthF(2.4)
        painter.setPen(pen)
        for dx, dy in (
            (1.0, 0.0), (0.7071, 0.7071), (0.0, 1.0), (-0.7071, 0.7071),
            (-1.0, 0.0), (-0.7071, -0.7071), (0.0, -1.0), (0.7071, -0.7071),
        ):
            painter.drawLine(
                QtCore.QPointF(9.0 + dx * 5.0, 9.0 + dy * 5.0),
                QtCore.QPointF(9.0 + dx * 6.4, 9.0 + dy * 6.4),
            )
    elif kind == "file":
        page = QtGui.QPainterPath()
        page.moveTo(5.2, 2.6)
        page.lineTo(10.4, 2.6)
        page.lineTo(13.2, 5.6)
        page.lineTo(13.2, 15.4)
        page.lineTo(5.2, 15.4)
        page.closeSubpath()
        painter.drawPath(page)
        fold = QtGui.QPainterPath()
        fold.moveTo(10.4, 2.6)
        fold.lineTo(10.4, 5.6)
        fold.lineTo(13.2, 5.6)
        painter.drawPath(fold)
    elif kind == "folder":
        folder = QtGui.QPainterPath()
        folder.moveTo(2.8, 5.4)
        folder.lineTo(7.0, 5.4)
        folder.lineTo(8.4, 7.0)
        folder.lineTo(15.2, 7.0)
        folder.lineTo(15.2, 14.6)
        folder.lineTo(2.8, 14.6)
        folder.closeSubpath()
        painter.drawPath(folder)
    elif kind == "image":
        painter.drawRoundedRect(QtCore.QRectF(2.8, 4.4, 12.4, 9.6), 1.4, 1.4)
        ridge = QtGui.QPainterPath()
        ridge.moveTo(4.2, 12.8)
        ridge.lineTo(7.4, 9.2)
        ridge.lineTo(10.0, 11.8)
        ridge.lineTo(11.8, 10.0)
        ridge.lineTo(13.8, 12.8)
        painter.drawPath(ridge)
        painter.drawEllipse(QtCore.QPointF(11.6, 7.0), 1.0, 1.0)
    elif kind == "clipboard":
        painter.drawRoundedRect(QtCore.QRectF(5.0, 4.6, 8.0, 10.8), 1.4, 1.4)
        painter.drawRoundedRect(QtCore.QRectF(7.2, 2.8, 3.6, 2.6), 0.9, 0.9)
        painter.drawLine(QtCore.QPointF(7.0, 9.0), QtCore.QPointF(11.0, 9.0))
        painter.drawLine(QtCore.QPointF(7.0, 11.6), QtCore.QPointF(11.0, 11.6))
    elif kind == "globe":
        painter.drawEllipse(QtCore.QPointF(9.0, 9.0), 6.2, 6.2)
        painter.drawEllipse(QtCore.QPointF(9.0, 9.0), 2.8, 6.2)
        painter.drawLine(QtCore.QPointF(2.9, 9.0), QtCore.QPointF(15.1, 9.0))
    elif kind == "snippet":
        painter.drawLine(QtCore.QPointF(3.4, 6.0), QtCore.QPointF(14.6, 6.0))
        painter.drawLine(QtCore.QPointF(3.4, 9.0), QtCore.QPointF(14.6, 9.0))
        painter.drawLine(QtCore.QPointF(3.4, 12.0), QtCore.QPointF(10.4, 12.0))
    elif kind == "voice":
        body = QtGui.QPainterPath()
        body.moveTo(4.0, 7.3)
        body.lineTo(6.2, 7.3)
        body.lineTo(9.2, 4.6)
        body.lineTo(9.2, 13.4)
        body.lineTo(6.2, 10.7)
        body.lineTo(4.0, 10.7)
        body.closeSubpath()
        painter.drawPath(body)
        for radius in (2.4, 4.4):
            painter.drawArc(
                QtCore.QRectF(9.2 - radius, 9.0 - radius, radius * 2, radius * 2),
                -50 * 16,
                100 * 16,
            )
    elif kind == "mic":
        painter.drawRoundedRect(QtCore.QRectF(6.2, 2.4, 5.6, 8.2), 2.8, 2.8)
        painter.drawArc(QtCore.QRectF(4.8, 5.4, 8.4, 7.4), 180 * 16, 180 * 16)
        painter.drawLine(QtCore.QPointF(9.0, 12.8), QtCore.QPointF(9.0, 15.4))
    elif kind == "keyboard":
        painter.drawRoundedRect(QtCore.QRectF(2.6, 4.6, 12.8, 8.8), 1.6, 1.6)
        for x in (5.2, 7.8, 10.4, 13.0):
            painter.drawPoint(QtCore.QPointF(x, 8.2))
        painter.drawLine(QtCore.QPointF(7.0, 10.6), QtCore.QPointF(11.0, 10.6))
    elif kind == "keycap":
        painter.drawRoundedRect(QtCore.QRectF(3.2, 5.0, 11.6, 8.0), 2.2, 2.2)
        painter.drawLine(QtCore.QPointF(7.2, 9.0), QtCore.QPointF(10.8, 9.0))
    else:
        star = QtGui.QPainterPath()
        points = [(9.0, 2.4), (11.0, 7.0), (15.6, 9.0), (11.0, 11.0),
                  (9.0, 15.6), (7.0, 11.0), (2.4, 9.0), (7.0, 7.0)]
        star.moveTo(*points[0])
        for point in points[1:]:
            star.lineTo(*point)
        star.closeSubpath()
        painter.drawPath(star)
    painter.end()
    return pix


_VOICE_MODES = (("muted", "静音"), ("plain", "纯语音"), ("interactive", "语音互动"))
_VOICE_TINT = {"muted": "#c9c3d4", "plain": "#9ad0ff", "interactive": "#b39dff"}


def _voice_mode() -> str:
    """当前档位存在 speech_prefs 里（和音色、Key 同一个家）。

    读不出来（文件缺了/坏了）就当「静音」：显示成「纯语音」会让人以为语音是好的，
    一点却不出声，还不如老实显示关着。
    """
    try:
        from app.speech_prefs import current as voice_prefs

        prefs = voice_prefs()
        if not prefs["enabled"]:
            return "muted"
        return "interactive" if prefs["mode"] == "interactive" else "plain"
    except Exception:
        return "muted"


def _glyph_icon(kind: str, color: str = "#e8e8ef") -> QtGui.QIcon:
    """同一套线条图标的常态 / 禁用态（禁用态自己画灰，QIcon 不会自动调色）。"""
    icon = QtGui.QIcon(_glyph_pixmap(kind, color))
    icon.addPixmap(_glyph_pixmap(kind, color), QtGui.QIcon.Mode.Active)
    icon.addPixmap(_glyph_pixmap(kind, color), QtGui.QIcon.Mode.Selected)
    icon.addPixmap(_glyph_pixmap(kind, "#6b6b76"), QtGui.QIcon.Mode.Disabled)
    return icon


IMAGE_SUFFIXES = media_links.IMAGE_SUFFIXES

_REF_LINE = re.compile(r"@(image|file|folder|url):(`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)\Z")
_SCREENSHOT_PLACEHOLDER = "[screenshot]"

_NOTICE_LABELS = {
    "auto_continue": "上次那一轮被中断了，引擎重启后自己接着跑完",
    "failed_turn": "这一轮没跑成：引擎没处理，要的话把话重发一次",
}
_DELEGATION_DISPLAY_KIND = "async_delegation_complete"
_AUTO_CONTINUE_REQUEST = "The interrupted request was:]"

_ATTACHED_CONTEXT = re.compile(r"(?:^|\n)--- Attached Context ---[ \t]*\n")
_CONTEXT_WARNINGS = re.compile(r"(?:^|\n)--- Context Warnings ---[\s\S]*$")
_CONTEXT_REF = re.compile(
    r"@(?:file|folder|url|image|tool|terminal):(?:\"[^\"\n]+\"|'[^'\n]+'|`[^`\n]+`|\S+)"
)


def strip_expanded_context(text: str) -> str:
    """去掉引擎展开的「--- Attached Context ---」大段，正文里缺的引用补回最前面。"""
    source = str(text or "")
    marker = _ATTACHED_CONTEXT.search(source)
    if marker is None:
        return _CONTEXT_WARNINGS.sub("", source).strip()
    visible = _CONTEXT_WARNINGS.sub("", source[: marker.start()]).strip()
    expanded = source[marker.end():]
    missing = [
        ref
        for ref in dict.fromkeys(match.group(0) for match in _CONTEXT_REF.finditer(expanded))
        if ref not in visible
    ]
    return "\n\n".join(part for part in ("\n".join(missing), visible) if part)


def split_attachment_refs(text: str) -> tuple[str, list[dict]]:
    """把正文里的 @image:/@file:/@folder:/@url: 标记摘出来，返回（正文, 附件条目）；条目按出现顺序。"""
    lines: list[str] = []
    items: list[dict] = []
    for raw in strip_expanded_context(text).split("\n"):
        line = raw.strip()
        if line == _SCREENSHOT_PLACEHOLDER:
            continue
        match = _REF_LINE.fullmatch(line)
        if match is None:
            lines.append(raw)
            continue
        kind, value = match.group(1), match.group(2)
        ref = value[1:-1] if value[0] in ("`", '"', "'") else value
        if kind == "image":
            if Path(ref).exists():
                items.append({"kind": "image", "name": Path(ref).name, "path": ref})
        elif kind == "url":
            items.append({"kind": "url", "name": ref})
        else:
            items.append({"kind": kind, "name": Path(ref).name or ref})
    return "\n".join(lines).strip(), items



PROMPT_SNIPPETS = (
    ("代码审查", "审查当前更改是否存在回归、遗漏的边界情况和缺失的测试。",
     "请审查这部分是否存在缺陷、回归和缺失的测试。"),
    ("实现计划", "在动代码之前先勾勒方案，让 diff 保持聚焦。",
     "请在修改代码前制定一个简洁的实现计划。"),
    ("解释这段", "讲解所选代码的工作方式，并链接到关键文件。",
     "请解释这是如何工作的，并指给我关键文件。"),
)


_PTT_DIALOG_QSS = """
#pttcard {
    background: #1a1d24;
    border: 1px solid rgba(255, 255, 255, 20);
    border-top: 1px solid rgba(255, 255, 255, 34);
    border-radius: 14px;
}
#pttlabel {
    color: #ffb877;
    font-size: 12px;
    font-weight: 600;
}
#pttclose {
    background: transparent;
    border: none;
    color: rgba(255, 255, 255, 95);
    font-size: 16px;
}
#pttclose:hover { color: rgba(255, 255, 255, 225); }
#pttcap {
    color: #f4f7fc;
    font-family: "Consolas", "Microsoft YaHei UI", "Microsoft YaHei";
    font-weight: 600;
}
#pttcap[len="s"] { font-size: 28px; }
#pttcap[len="m"] { font-size: 22px; }
#pttcap[len="l"] { font-size: 17px; }
/* 两条键槽并排看，字号必须一样大；按长度缩是给单行大字用的 */
#pttrow #pttcap { font-size: 19px; }
#ptthr {
    background: rgba(255, 255, 255, 18);
    border: none;
}
#pttdesc {
    color: rgba(255, 169, 77, 165);
    font-size: 11px;
}
#pttrow {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
}
#pttrow[act="1"] {
    background: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 26);
}
#pttrowname {
    color: rgba(255, 255, 255, 105);
    font-size: 12px;
}
#pttrow[act="1"] #pttrowname {
    color: rgba(255, 255, 255, 165);
}
#pttwarn {
    color: #ff7b72;
    font-size: 11px;
}
QPushButton {
    color: rgba(255, 255, 255, 190);
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 32);
    border-radius: 8px;
    padding: 8px 18px;
    font-size: 12px;
}
QPushButton:hover {
    background: rgba(255, 255, 255, 16);
    color: #ffffff;
}
QPushButton#primary {
    background: #e9eef7;
    border: 1px solid #e9eef7;
    color: #14171d;
    font-weight: 600;
}
QPushButton#primary:hover { background: #ffffff; }
QPushButton#primary:disabled {
    background: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 18);
    color: rgba(255, 255, 255, 60);
    font-weight: 400;
}
"""


def _cap_size(text: str) -> str:
    """键名档位：字越长字号越小，长的组合键才放得下不折行。"""
    if len(text) <= 3:
        return "s"
    if len(text) <= 7:
        return "m"
    return "l"


def _key_name(sequence: QtGui.QKeySequence | None) -> str:
    """键名给人看：空格 / F2 / Ctrl+空格 / Win+空格。"""
    if sequence is None or sequence.count() != 1:
        return "空格"
    text = sequence.toString(QtGui.QKeySequence.SequenceFormat.NativeText)
    if text in ("Space", ""):
        return "空格"
    return text.replace("Meta", "Win").replace("Space", "空格")


def _to_native(sequence: QtGui.QKeySequence) -> tuple[int, int] | None:
    """Qt 的键序列 → (修饰位, 虚拟键码)。钩子只认这两个。"""
    if sequence.count() != 1:
        return None
    combo = sequence[0]  # pyright: ignore[reportIndexIssue]
    mods = 0
    flags = combo.keyboardModifiers()
    if flags & QtCore.Qt.KeyboardModifier.ShiftModifier:
        mods |= global_hotkey.MOD_SHIFT
    if flags & QtCore.Qt.KeyboardModifier.ControlModifier:
        mods |= global_hotkey.MOD_CONTROL
    if flags & QtCore.Qt.KeyboardModifier.AltModifier:
        mods |= global_hotkey.MOD_ALT
    if flags & QtCore.Qt.KeyboardModifier.MetaModifier:
        mods |= global_hotkey.MOD_WIN
    name = QtCore.Qt.Key(combo.key()).name[4:]     # "Key_Slash" -> "Slash"
    if flags & QtCore.Qt.KeyboardModifier.KeypadModifier:
        return (mods, 0x60 + ord(name) - 0x30) if name in "0123456789" else None
    vk = _qt_key_to_vk(name)
    return (mods, vk) if vk is not None else None


_OEM_KEYS = {
    "Semicolon": 0xBA, "Equal": 0xBB, "Comma": 0xBC, "Minus": 0xBD,
    "Period": 0xBE, "Slash": 0xBF, "QuoteLeft": 0xC0,
    "BracketLeft": 0xDB, "Backslash": 0xDC, "BracketRight": 0xDD,
    "Apostrophe": 0xDE,
}
_NAMED_KEYS = {
    "Escape": 0x1B, "Tab": 0x09, "Backspace": 0x08, "Return": 0x0D,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "CapsLock": 0x14, "NumLock": 0x90, "ScrollLock": 0x91,
    "Print": 0x2C, "Pause": 0x13,
}
_VK_TO_QT_NAME = {vk: name for name, vk in {**_OEM_KEYS, **_NAMED_KEYS}.items()}
_VK_TO_QT_NAME[0x0D] = "Return"


_HOLD_MAX_SECONDS = 120.0

_MOD_KEY_CODES = (
    (global_hotkey.MOD_SHIFT, (0x10, 0xA0, 0xA1)),
    (global_hotkey.MOD_CONTROL, (0x11, 0xA2, 0xA3)),
    (global_hotkey.MOD_ALT, (0x12, 0xA4, 0xA5)),
    (global_hotkey.MOD_WIN, (0x5B, 0x5C)),
)


def _sequence_down(sequence: QtGui.QKeySequence) -> bool:
    """这个组合现在是不是完整按着——直接问系统键态。

    按住说话不能靠事件推：keyup 会被系统截走（Ctrl+空格 是输入法切换）、
    切窗口时收不到、安全桌面期间整组丢失。键态不会骗人。
    """
    if sequence.count() != 1:
        return False
    native = _to_native(sequence)
    if native is None:
        return False
    mods, vk = native
    user32 = ctypes.windll.user32
    if not user32.GetAsyncKeyState(vk) & 0x8000:
        return False
    for bit, codes in _MOD_KEY_CODES:
        if mods & bit and not any(user32.GetAsyncKeyState(c) & 0x8000 for c in codes):
            return False
    return True


def _qt_key_to_vk(name: str) -> int | None:
    """Qt 键名 → 虚拟键码。翻不出来就是 None，不猜。"""
    if name in _OEM_KEYS:
        return _OEM_KEYS[name]
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1 and ("A" <= name <= "Z" or "0" <= name <= "9"):
        return ord(name)
    if name == "Space":
        return 0x20
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    return None


def _vk_to_qt_key(vk: int) -> QtCore.Qt.Key | None:
    """虚拟键码 → Qt 键。翻不出来就是 None。"""
    name = _VK_TO_QT_NAME.get(vk)
    if name is not None:
        return QtCore.Qt.Key["Key_" + name]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return QtCore.Qt.Key(vk)
    if vk == 0x20:
        return QtCore.Qt.Key.Key_Space
    if 0x70 <= vk <= 0x87:
        return QtCore.Qt.Key(int(QtCore.Qt.Key.Key_F1) + (vk - 0x70))
    return None


def _from_native(mods: int, vk: int) -> QtGui.QKeySequence | None:
    """钩子给的 (修饰位, 虚拟键码) → Qt 键序列。改键靠钩子收键，收到的得转回来。"""
    if not vk:
        return None
    modifier = QtCore.Qt.KeyboardModifier.NoModifier
    if mods & global_hotkey.MOD_SHIFT:
        modifier |= QtCore.Qt.KeyboardModifier.ShiftModifier
    if mods & global_hotkey.MOD_CONTROL:
        modifier |= QtCore.Qt.KeyboardModifier.ControlModifier
    if mods & global_hotkey.MOD_ALT:
        modifier |= QtCore.Qt.KeyboardModifier.AltModifier
    if mods & global_hotkey.MOD_WIN:
        modifier |= QtCore.Qt.KeyboardModifier.MetaModifier
    if 0x60 <= vk <= 0x69:
        modifier |= QtCore.Qt.KeyboardModifier.KeypadModifier
        key = QtCore.Qt.Key(int(QtCore.Qt.Key.Key_0) + (vk - 0x60))
    else:
        key = _vk_to_qt_key(vk)
        if key is None:
            return None
    return QtGui.QKeySequence(QtCore.QKeyCombination(modifier, key))


class _PttKeyDialog(QtWidgets.QDialog):
    """改「按住说话的键」：窗口内和系统级各一条，点哪条改哪条，Esc 不改。"""

    def __init__(
        self,
        window_current: QtGui.QKeySequence | None,
        global_current: QtGui.QKeySequence | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._before: dict[str, QtGui.QKeySequence | None] = {
            "window": window_current,
            "global": global_current,
        }
        self._pending: dict[str, QtGui.QKeySequence | None] = dict(self._before)
        self._target = "window"
        self._drag_offset: QtCore.QPoint | None = None
        self._caps: dict[str, QtWidgets.QLabel] = {}
        self._rows: dict[str, QtWidgets.QFrame] = {}

        self._capture = global_hotkey.GlobalHotkey(self)
        self._capture.captured.connect(self._on_captured)

        self.setWindowFlags(
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setStyleSheet(_PTT_DIALOG_QSS)

        card = QtWidgets.QFrame(self)
        card.setObjectName("pttcard")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(0)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        label = QtWidgets.QLabel("按住说话的键", card)
        label.setObjectName("pttlabel")
        head.addWidget(label)
        head.addStretch(1)
        close = QtWidgets.QToolButton(card)
        close.setObjectName("pttclose")
        close.setText("×")
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(20, 20)
        close.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.reject)
        head.addWidget(close)
        lay.addLayout(head)
        lay.addSpacing(10)

        lay.addWidget(self._make_row(card, "window", "窗口内"))
        lay.addSpacing(4)
        lay.addWidget(self._make_row(card, "global", "系统级"))
        lay.addSpacing(10)

        self._desc = QtWidgets.QLabel("按一下想用的键，回车确定", card)
        self._desc.setObjectName("pttdesc")
        lay.addWidget(self._desc)
        lay.addSpacing(14)

        hr = QtWidgets.QWidget(card)
        hr.setObjectName("ptthr")
        hr.setFixedHeight(1)
        lay.addWidget(hr)
        lay.addSpacing(12)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", card)
        cancel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._ok = QtWidgets.QPushButton("确定", card)
        self._ok.setObjectName("primary")
        self._ok.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._ok.clicked.connect(self.accept)
        row.addWidget(self._ok)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(340)

        cancel.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._ok.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._set_target("window")

    def _make_row(self, card: QtWidgets.QWidget, key: str, name: str) -> QtWidgets.QFrame:
        """一条键槽：左边是哪里的键，右边是键名。整条可点。"""
        frame = QtWidgets.QFrame(card)
        frame.setObjectName("pttrow")
        frame.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        line = QtWidgets.QHBoxLayout(frame)
        line.setContentsMargins(12, 4, 12, 4)
        line.setSpacing(12)

        tag = QtWidgets.QLabel(name, frame)
        tag.setObjectName("pttrowname")
        tag.setMinimumWidth(52)
        line.addWidget(tag)

        cap = QtWidgets.QLabel(card)
        cap.setObjectName("pttcap")
        cap.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        cap.setFixedHeight(26)
        line.addWidget(cap, 1)

        self._caps[key] = cap
        self._rows[key] = frame
        self._paint_cap(key)
        frame.mousePressEvent = lambda _e, k=key: self._set_target(k)  # noqa: ARG005
        return frame

    def _paint_cap(self, key: str) -> None:
        """换键名并按长短选字号档位，长的组合键不折行。"""
        cap = self._caps[key]
        text = _key_name(self._pending[key])
        cap.setText(text)
        cap.setProperty("len", _cap_size(text))
        cap.style().unpolish(cap)
        cap.style().polish(cap)

    def _set_target(self, key: str) -> None:
        """切到哪一条：名片上高亮它，按键记给它。"""
        self._target = key
        for name, frame in self._rows.items():
            frame.setProperty("act", "1" if name == key else "0")
            frame.style().unpolish(frame)
            frame.style().polish(frame)
        self._refresh()

    def _conflict(self) -> tuple[str, str]:
        """系统级这条的真实占用：向系统注册一次，系统怎么回就怎么显示。

        窗口内不查占用——它只在窗口有焦点时生效，不跟任何程序抢同一个键。
        """
        window = self._pending["window"]
        globe = self._pending["global"]

        if window is not None and globe is not None and window == globe:
            return "block", "两条键不能一样，按下去分不清是哪一个"

        native = _to_native(globe) if globe is not None else None
        if native is None:
            return "block", "这个键当系统级用不了，换成字母、数字、F 键或标点"
        mods, vk = native
        if mods == 0:
            return "block", "系统级的键必须带修饰键（Ctrl / Alt / Shift / Win），不然分不清是在打字还是要说话"
        ok, why = global_hotkey.probe(mods, vk)
        if ok:
            return "", ""
        return "block", why

    def _refresh(self) -> None:
        """确定键能不能按、下面那行提示说什么。"""
        level, warn = self._conflict()
        changed = self._pending != self._before
        self._ok.setEnabled(changed and level != "block")
        if warn:
            self._desc.setText(warn)
            self._desc.setStyleSheet("color: #ff7b72;")
        elif not changed:
            self._desc.setText("按一下想用的键，回车确定")
            self._desc.setStyleSheet("")
        else:
            self._desc.setText("回车确定，现在按的是「%s」" % _key_name(self._pending[self._target]))
            self._desc.setStyleSheet("")

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.setFocus()
        panel = self.parent()
        running = getattr(panel, "_global_ptt", None)
        if running is not None:
            running.stop()
        self._capture.start_capture()

    def done(self, result: int) -> None:
        """改键结束（确定、取消、Esc、点外面关掉都走这里）先卸钩子。"""
        self._capture.stop_capture()
        super().done(result)
        panel = self.parent()
        if getattr(panel, "_voice_mode_on", False):
            apply = getattr(panel, "_apply_global_ptt", None)
            if apply is not None:
                apply()

    def _on_captured(self, mods: int, vk: int) -> None:
        """钩子收到一个键：跟窗口事件走同一套——填进当前那行，再看冲不冲突。"""
        if ctypes.windll.user32.GetForegroundWindow() != int(self.winId()):
            return
        sequence = _from_native(mods, vk)
        if sequence is None:
            return
        if vk in (0x0D, 0x0A):
            return
        self._pending[self._target] = sequence
        self._paint_cap(self._target)
        self._refresh()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == QtCore.Qt.Key.Key_Escape:
            self.reject()
            return
        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            if self._pending != self._before and self._conflict()[0] != "block":
                self.accept()
            return
        if key in (
            QtCore.Qt.Key.Key_Shift,
            QtCore.Qt.Key.Key_Control,
            QtCore.Qt.Key.Key_Alt,
            QtCore.Qt.Key.Key_Meta,
        ):
            return
        if event.isAutoRepeat():
            return
        sequence = QtGui.QKeySequence(event.keyCombination())
        if not sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText):
            return
        self._pending[self._target] = sequence
        self._paint_cap(self._target)
        self._refresh()

    def window_sequence(self) -> QtGui.QKeySequence | None:
        """窗口内确认下来的键。"""
        return self._pending["window"]

    def global_sequence(self) -> QtGui.QKeySequence | None:
        """系统级确认下来的键。"""
        return self._pending["global"]




BG_PORTRAIT_CROP = 0.66
BG_PORTRAIT_SCALE = 0.30


def _dimmed(pixmap: QtGui.QPixmap, factor: float) -> QtGui.QPixmap:
    """按比例压暗一张带透明的图：只动亮度，轮廓（alpha）原样保留。"""
    if pixmap.isNull() or factor >= 1.0:
        return pixmap
    out = QtGui.QPixmap(pixmap.size())
    out.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(out)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceAtop)
    painter.fillRect(out.rect(), QtGui.QColor(0, 0, 0, int(round(255 * (1.0 - factor)))))
    painter.end()
    return out


class ChatPanel(QtWidgets.QWidget):
    """打字对话窗。"""

    submitted = QtCore.Signal(str, str)
    model_chosen = QtCore.Signal(str)
    models_refresh_requested = QtCore.Signal()
    model_settings_requested = QtCore.Signal()
    new_session_requested = QtCore.Signal()
    approval_requested = QtCore.Signal(str)
    stop_requested = QtCore.Signal()
    speech_stop_requested = QtCore.Signal()
    voice_mode_changed = QtCore.Signal(str)
    voice_transcribe_requested = QtCore.Signal(str)
    voice_record_started = QtCore.Signal()
    voice_input_opened = QtCore.Signal()
    reasoning_requested = QtCore.Signal(str)
    attachment_picked = QtCore.Signal(list)
    attachment_failed = QtCore.Signal(str)
    clarify_answered = QtCore.Signal(str, str, str)
    clarify_skipped = QtCore.Signal(str)
    process_kill_requested = QtCore.Signal(str)
    resubmit_requested = QtCore.Signal(object, str, bool, bool, bool)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        settings: QtCore.QSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(MIN_PANEL_WIDTH, MIN_PANEL_HEIGHT)
        self.setMouseTracking(True)
        self._settings = settings or window_settings()
        self._streaming = False
        self._drag_offset: QtCore.QPoint | None = None
        self._anchor: QtCore.QRect | None = None
        self._snapped = False
        self._resize_edge = None
        self._resize_start_size: QtCore.QSize | None = None
        self._resize_start_global: QtCore.QPoint | None = None

        frame = QtWidgets.QFrame(self)
        frame.setObjectName("frame")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(TITLE_FULL, frame)
        title.setObjectName("title")
        state = QtWidgets.QLabel("", frame)
        state.setObjectName("state")
        dot = QtWidgets.QLabel("●", frame)
        dot.setObjectName("dot")
        dot.setToolTip("在线")
        hint_label = QtWidgets.QLabel(_HINT_TEXT, frame)
        hint_label.setObjectName("hint")
        self._hint_label = hint_label
        header.addWidget(title)
        header.addWidget(dot)
        header.addWidget(state)
        header.addStretch(1)
        new_session = QtWidgets.QPushButton("新会话", frame)
        new_session.setObjectName("mini")
        new_session.setToolTip("开一段新对话：换回历史里不带的干净上下文（改了人设、SOUL.md 之后用它生效）")
        new_session.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        new_session.clicked.connect(self.new_session_requested.emit)
        layout.addLayout(header)
        self._state_label = state
        self._title_label = title

        combo = QtWidgets.QToolButton(frame)
        combo.setObjectName("model")
        combo.setToolTip("点开按供应商分组选模型（右键头像 → 模型 也能选）；「设置」里填密钥")
        combo.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        combo.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        combo.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        combo.setText("模型")
        combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
        menu = QtWidgets.QMenu(combo)
        menu.setObjectName("modelmenu")
        menu.setToolTipsVisible(True)
        menu.setStyleSheet(STYLE)
        combo.setMenu(menu)
        header.addWidget(combo)
        header.addWidget(new_session)
        set_model = QtWidgets.QPushButton(frame)
        set_model.setObjectName("mini")
        set_model.setIcon(_glyph_icon("gear"))
        set_model.setIconSize(QtCore.QSize(14, 14))
        set_model.setFixedSize(26, 20)
        set_model.setToolTip("设置：选模型；没有可用的就告诉你密钥该放在哪个文件")
        set_model.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        set_model.clicked.connect(self.model_settings_requested.emit)
        header.addWidget(set_model)
        self._model_button = combo
        self._model_menu = menu
        self._models: list[dict] = []
        self._models_refreshing = False
        self._current_model_id = ""
        self._refresh_button: QtWidgets.QPushButton | None = None

        self._bubble_has_text = False
        self._busy = False
        self._interrupted = False
        self._assistant_pending = ""
        self._history_older: list = []
        self._history_rendered: list = []
        self._history_loading = False
        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(self.FLUSH_SLICE_MS)
        self._flush_timer.timeout.connect(lambda: self._pump_assistant(final=False))
        self._last_error_key = ""
        self._review_count = 0
        self._review_folded = False
        self._review_pending: list[str] = []
        self._reasoning_pending = ""
        self._md_block: list[str] = []
        self._para_break = False
        self._md_fence = False
        self._md_math = False
        self._media_roots: tuple = ()
        self._photos: dict[str, QtGui.QPixmap] = {}
        self._photo_keys: dict[str, str] = {}
        self._photo_sources: dict[str, str] = {}
        self._originals: dict[str, bytes] = {}
        self._downloading: set[str] = set()
        self._local_photos: dict[str, tuple[str, "Path | bytes"]] = {}
        self._hint_label: QtWidgets.QLabel | None
        self._hint_timer = QtCore.QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._restore_hint)
        self._photo_pending: set[str] = set()
        self._press_pos: QtCore.QPoint | None = None
        self._net = QtNetwork.QNetworkAccessManager(self)
        self._reason_fold: disclosure.Fold | None = None
        self._spin_frame = 0
        self._spin_timer = QtCore.QTimer(self)
        self._spin_timer.setInterval(_SPIN_INTERVAL_MS)
        self._spin_timer.timeout.connect(self._tick_spin)
        
        self._tool_folds: dict[str, disclosure.Fold] = {}
        self._tool_images: dict[int, str] = {}
        self._tool_term_drawn: dict[int, bool] = {}
        self._tool_detail_seen: dict[str, tuple] = {}
        self._delegate_cards: dict[str, dict] = {}
        self._delegate_latest: str = ""
        self._clarify: dict | None = None
        self._tool_started: dict[str, tuple[disclosure.Fold, str, float, str | None]] = {}
        self._tool_clock = QtCore.QTimer(self)
        self._tool_clock.setInterval(1000)
        self._tool_clock.timeout.connect(self._tick_running_tools)
        self._icon_cache: dict[tuple, QtGui.QPixmap] = {}
        self._icon_registered: set[str] = set()
        self._fence_cache: dict[tuple[int, int], str] = {}
        self._image_seq = 0
        self._clarify_gen = 0
        self._show_reasoning = True
        self._tool_progress = "all"
        self._provider_wait = ""

        self.view = ChatView(frame)
        self.view.setObjectName("view")
        self.view.document().setDocumentMargin(ChatView.BUBBLE_PAD_Y)
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._on_anchor)
        self.view.scrolled.connect(self._hide_hover)
        self.view.scrolled.connect(self._fit_visible_user_lines)
        self.view.scrolled.connect(self._maybe_load_older_history)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.view, 1)

        head_format = QtGui.QTextCharFormat()
        head_format.setForeground(ChatView.SCAFFOLD_IDLE)
        head_format.setFontPointSize(9)
        body_format = QtGui.QTextCharFormat()
        body_format.setForeground(QtGui.QColor("#6f7a8c"))
        body_format.setFontPointSize(9)
        body_format.setFontItalic(True)
        self._tool_body_format = QtGui.QTextCharFormat()
        self._tool_body_format.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        self._tool_body_format.setFontPointSize(8.5)
        self._tool_body_format.setForeground(QtGui.QColor("#7c8798"))
        self._folds = disclosure.Folds(
            self.view.document(), head_format, body_format, host=self.view.viewport()
        )
        self.view.attach_folds(self._folds)

        self._el_buttons: dict[tuple, HoverActionButton] = {}
        self._shown_buttons: list[HoverActionButton] = []
        self._el_scan_pos = 0
        self._user_line_width = 0
        self._zone: tuple | None = None
        self._hooked_scrolls: set[int] = set()
        self._msgs: list[dict] = []
        self._stream_msg: dict | None = None
        self._bar_buttons: list[HoverActionButton] = []
        self._bar_msg: dict | None = None
        self._edge_cursor_shape = None
        self._rewind_waiting = False
        self._rewind_after_stop: tuple[dict, str, bool] | None = None
        self._row_stamp_pending: int | None = None
        self._jump: _JumpPill | None = None
        self._follow = True
        self._self_scroll = False
        self._user_scroll = False
        self._resizing = False
        self._sticky: _StickyPrompt | None = None
        self._sticky_busy = False
        self._sticky_dirty = False
        self._band_freeze = False
        self._restoring = False
        self._diff_texts: dict[int, tuple[str | None, str]] = {}
        self._tool_payloads: dict[int, tuple[str, str]] = {}
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.installEventFilter(self)
        bar = self.view.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.sliderMoved.connect(self._user_scroll_now)
        bar.actionTriggered.connect(self._user_scroll_now)
        bar.rangeChanged.connect(self._range_changed)
        self.view.document().contentsChanged.connect(self._sync_elements)
        bar.valueChanged.connect(lambda _value: self._place_msg_bars())
        bar.rangeChanged.connect(lambda *_args: QtCore.QTimer.singleShot(0, self._place_msg_bars))

        self.stack = StatusStack(frame)
        self.stack.kill_requested.connect(self.process_kill_requested.emit)
        layout.addWidget(self.stack)

        self.speech_bar = SpeechBar(frame)
        self.speech_bar.stop_requested.connect(self.speech_stop_requested.emit)
        layout.addWidget(self.speech_bar)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self._inputbox = box = QtWidgets.QFrame(frame)
        box.setObjectName("inputbox")
        box_row = QtWidgets.QHBoxLayout(box)
        box_row.setContentsMargins(8, 0, 4, 0)
        box_row.setSpacing(2)

        self._attach_button = QtWidgets.QToolButton(box)
        self._attach_button.setObjectName("icon")
        self._attach_button.setIcon(_glyph_icon("plus"))
        self._attach_button.setIconSize(QtCore.QSize(14, 14))
        self._attach_button.setFixedSize(22, 22)
        self._attach_button.setToolTip("加附件（图片、文档；也可以直接粘贴或拖进来）")
        self._attach_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._attach_menu = QtWidgets.QMenu(self._attach_button)
        self._attach_menu.setObjectName("modelmenu")
        self._attach_menu.addSection("附加")
        for label, slot, glyph in (
            ("文件…", self._pick_files, "file"),
            ("文件夹…", self._pick_folders, "folder"),
            ("图片…", self._pick_images, "image"),
            ("粘贴图片", self._paste_image, "clipboard"),
            ("URL…", self._add_url, "globe"),
        ):
            action = self._attach_menu.addAction(_glyph_icon(glyph), label)
            action.triggered.connect(lambda _checked=False, run=slot: run())
        self._attach_menu.addSeparator()
        snippets = self._attach_menu.addAction(_glyph_icon("snippet"), "提示词片段…")
        snippets.triggered.connect(lambda _checked=False: self._show_snippets())
        self._attach_menu.addSeparator()
        hint = self._attach_menu.addAction("提示：输入 @ 以内联引用文件。")
        hint.setEnabled(False)
        self._attach_button.clicked.connect(self._show_attach_menu)
        box_row.addWidget(self._attach_button)

        self.input = _Input(box)
        self.input.setObjectName("input")
        self.input.set_placeholder("和 Noesia 说点什么…")
        self.input.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.input.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.input.setTabChangesFocus(True)
        self.input.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.document().setDocumentMargin(0)
        self.input.installEventFilter(self)
        self.input.viewport().installEventFilter(self)
        self.input.setAcceptDrops(True)
        self.input.textChanged.connect(self._fit_input_height)
        box_row.addWidget(self.input, 1)
        self._ime_composing = False
        self.input.textChanged.connect(self._sync_input_ph)
        self.input.textChanged.connect(self._sync_send_button)
        self._sync_input_ph()
        self._fit_input_height()

        self._box_row = box_row
        self._voice_orb = VoiceOrb(box)
        self._voice_orb.hide()
        self._voice_orb.pressed.connect(self._voice_hold_start)
        self._voice_orb.released.connect(self._voice_hold_end)
        box_row.addWidget(self._voice_orb, 1, QtCore.Qt.AlignmentFlag.AlignCenter)

        self._approval_button = QtWidgets.QToolButton(box)
        self._approval_button.setObjectName("icon")
        self._approval_button.setIcon(_glyph_icon("shield"))
        self._approval_button.setIconSize(QtCore.QSize(14, 14))
        self._approval_button.setFixedSize(22, 22)
        self._approval_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._approval_button.setToolTip("审批档位：还没问到引擎")
        self._approval_menu = QtWidgets.QMenu(self._approval_button)
        self._approval_menu.setObjectName("modelmenu")
        self._approval_menu.setStyleSheet(STYLE)
        self._approval_group = QtGui.QActionGroup(self._approval_menu)
        self._approval_group.setExclusive(True)
        for value, label in _APPROVAL_MODES:
            action = self._approval_menu.addAction(label)
            action.setCheckable(True)
            action.setActionGroup(self._approval_group)
            action.setData(value)
            action.triggered.connect(
                lambda _checked=False, chosen=value: self.approval_requested.emit(chosen)
            )
        self._approval_button.setMenu(self._approval_menu)
        self._approval_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        box_row.addWidget(self._approval_button)

        self._reasoning_button = QtWidgets.QToolButton(box)
        self._reasoning_button.setObjectName("icon")
        self._reasoning_button.setIcon(_glyph_icon("spark"))
        self._reasoning_button.setIconSize(QtCore.QSize(14, 14))
        self._reasoning_button.setFixedSize(22, 22)
        self._reasoning_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._reasoning_button.setEnabled(False)
        self._reasoning_button.setToolTip("推理强度：等引擎回读当前档位")
        self._reasoning_menu = QtWidgets.QMenu(self._reasoning_button)
        self._reasoning_group = QtGui.QActionGroup(self._reasoning_menu)
        self._reasoning_group.setExclusive(True)
        self._reasoning_menu.setObjectName("modelmenu")
        self._reasoning_menu.setStyleSheet(STYLE)
        self._reasoning_button.setMenu(self._reasoning_menu)
        self._reasoning_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        box_row.addWidget(self._reasoning_button)

        self._voice_button = QtWidgets.QToolButton(box)
        self._voice_button.setObjectName("icon")
        self._voice_button.setIconSize(QtCore.QSize(14, 14))
        self._voice_button.setFixedSize(22, 22)
        self._voice_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._voice_menu = QtWidgets.QMenu(self._voice_button)
        self._voice_group = QtGui.QActionGroup(self._voice_menu)
        self._voice_group.setExclusive(True)
        for value, label in _VOICE_MODES:
            action = self._voice_menu.addAction(label)
            action.setCheckable(True)
            action.setActionGroup(self._voice_group)
            action.setData(value)
            action.triggered.connect(lambda _checked=False, mode=value: self.set_voice_mode(mode))
        self._voice_button.setMenu(self._voice_menu)
        self._voice_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        box_row.addWidget(self._voice_button)
        self.set_voice_mode(_voice_mode())

        self._mic_button = QtWidgets.QToolButton(box)
        self._mic_button.setObjectName("icon")
        self._mic_button.setIcon(_glyph_icon("mic"))
        self._mic_button.setIconSize(QtCore.QSize(14, 14))
        self._mic_button.setFixedSize(22, 22)
        self._mic_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._mic_button.setToolTip("语音输入（右键选麦克风）")
        self._mic_button.clicked.connect(self._toggle_voice_input)
        self._mic_button.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._mic_button.customContextMenuRequested.connect(self._show_mic_menu)
        box_row.addWidget(self._mic_button)
        self._init_voice_input()

        self._send_button = send = QtWidgets.QPushButton(frame)
        send.setFixedSize(32, 32)
        send.setIconSize(QtCore.QSize(15, 15))
        send.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        send.setFlat(True)
        glow = QtWidgets.QGraphicsDropShadowEffect(send)
        glow.setBlurRadius(11)
        glow.setColor(QtGui.QColor(196, 110, 158, 97))
        glow.setOffset(0, 3)
        send.setGraphicsEffect(glow)
        send.setObjectName("send")
        send.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._attachments: list[dict] = []
        self._clipboard_seq = 0
        self._attach_chips: list[QtWidgets.QFrame] = []
        self._attach_bar = QtWidgets.QFrame(frame)
        self._attach_bar.setObjectName("attachbar")
        self._attach_row = QtWidgets.QHBoxLayout(self._attach_bar)
        self._attach_row.setContentsMargins(6, 4, 6, 4)
        self._attach_row.setSpacing(6)
        self._attach_row.addStretch(1)
        self._attach_bar.hide()
        self._queued: list[dict] = []
        self._queue_chips: list[QtWidgets.QFrame] = []
        self._queue_bar = QtWidgets.QFrame(frame)
        self._queue_bar.setObjectName("attachbar")
        self._queue_col = QtWidgets.QVBoxLayout(self._queue_bar)
        self._queue_col.setContentsMargins(6, 4, 6, 4)
        self._queue_col.setSpacing(3)
        self._queue_bar.hide()
        layout.addWidget(self._queue_bar)
        layout.addWidget(self._attach_bar)

        row.addWidget(box, 1)
        row.addWidget(send)
        layout.addLayout(row)

        meta_row = QtWidgets.QHBoxLayout()
        meta_row.setContentsMargins(4, 4, 4, 0)
        meta_row.setSpacing(6)
        meta_row.addWidget(hint_label)
        meta_row.addStretch(1)
        layout.addLayout(meta_row)

        send.clicked.connect(self._emit)

        self._frame = frame
        frame.setMouseTracking(True)
        frame.installEventFilter(self)
        self._init_background(frame)
        self._restore_geometry()
        self._sync_send_button()

    def _init_background(self, frame) -> None:
        """右下角一张她的立绘当底纹：压在所有内容下面，淡到只当作画面。

        路径必须走 `repo_root()`：`project_root()` 在 `NOESIA_HOME` 指到
        `data/NoesiaHome` 时会跟着指过去，那儿没有 assets。
        """
        from paths import repo_root

        source = QtGui.QPixmap(
            str(repo_root() / "assets" / "transparent" / "noesia_background.png")
        )
        if not source.isNull():
            source = source.copy(
                0, 0, source.width(), int(round(source.height() * BG_PORTRAIT_CROP))
            )
        self._bg_pixmap = _dimmed(source, 0.72)
        if self._bg_pixmap.isNull():
            logger.warning("背景图读不到：%s", repo_root() / "assets" / "transparent" / "noesia_background.png")
            return
        self._bg = QtWidgets.QLabel(frame)
        self._bg.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._bg.setStyleSheet("background: transparent;")
        fade = QtWidgets.QGraphicsOpacityEffect(self._bg)
        fade.setOpacity(0.34)
        self._bg.setGraphicsEffect(fade)
        self._bg.lower()
        self._place_background()

    def _place_background(self) -> None:
        """按原图比例缩放贴右下：先定高，宽度跟着比例走；宽过面板就反过来以宽为准。"""
        if getattr(self, "_bg", None) is None or self._bg_pixmap.isNull():
            return
        frame = self._bg.parentWidget()
        if frame is None or frame.width() <= 0 or frame.height() <= 0:
            return
        height = int(frame.height() * BG_PORTRAIT_SCALE)
        width = int(height * self._bg_pixmap.width() / self._bg_pixmap.height())
        limit = int(frame.width() * 0.72)
        if width > limit:
            width = limit
            height = int(width * self._bg_pixmap.height() / self._bg_pixmap.width())
        scaled = self._bg_pixmap.scaled(
            width, height,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self._bg.setPixmap(scaled)
        self._bg.resize(scaled.size())
        self._bg.move(frame.width() - scaled.width(), frame.height() - scaled.height())


    def set_models(self, items: list[dict], current: str, *, switching: bool = False) -> None:
        """填模型菜单：一级按供应商分组，二级是那家的模型；按钮上显示当前模型名。

        清单一到就说明刷新结束了，所以顺手把「刷新中…」收回去。
        """
        self._models = list(items)
        self._current_model_id = current
        self._models_refreshing = False
        self._repaint_model_menu()
        text = current_label(self._models, current, fallback="选模型")
        self._model_button.setText(("切换中…" if switching else text) + "  ▾")
        self._model_button.setEnabled(bool(self._models or current) and not switching)
        if not self._models:
            self._model_button.setToolTip("模型清单还没到；点「设置」可以先配一家")
        elif not any(group["available"] for group in group_models(self._models) if group["provider"]):
            self._model_button.setToolTip("这些供应商都还没配密钥：点「设置」填一个")
        else:
            self._model_button.setToolTip("点开按供应商分组选模型（右键头像 → 模型 也能选）")

    def set_models_refreshing(self, refreshing: bool) -> None:
        """「刷新模型」点下去到清单回来之间：那一条写「刷新中…」、变灰、点了不再发请求。

        只改那一颗按钮：整张菜单清空重建会在弹出的菜单上闪一下（先空一帧再画回来），
        而这里根本没必要动别的行。
        """
        self._models_refreshing = bool(refreshing)
        button = self._refresh_button
        if button is None:
            self._repaint_model_menu()
            return
        button.setText("刷新中…" if self._models_refreshing else "刷新模型")
        button.setProperty("busy", self._models_refreshing)
        button.style().unpolish(button)
        button.style().polish(button)

    def _request_models_refresh(self) -> None:
        """菜单里那颗「刷新模型」：刷新途中再点不动（不重复发请求）。"""
        if self._models_refreshing:
            return
        self.models_refresh_requested.emit()

    def _repaint_model_menu(self) -> None:
        """重画模型菜单。菜单正开着就把重绘压到一次：清空到重建之间不许露出空白帧。

        重建会把「看着/展开着的是哪一家」一起清掉，Qt 于是把高亮丢回第一行（用户看到的是
        「刷新完自己跳到第一个」）。所以重建前记下当前那一家、哪一个子菜单是开着的，
        重建后按供应商名（挂在 action 的 `data()` 上，不含会变的「N 个模型」）找回来重新
        点亮、把子菜单原样展开。
        """
        menu = self._model_menu
        was_open = menu.isVisible()
        keep = ""
        keep_open_sub = ""
        keep_sub_item = ""
        if was_open:
            active = menu.activeAction()
            if active is not None and not active.isSeparator():
                keep = self._menu_row_key(active)
            for action in menu.actions():
                sub = action.menu()
                if isinstance(sub, QtWidgets.QMenu) and sub.isVisible():
                    keep_open_sub = self._menu_row_key(action)
                    inner = sub.activeAction()
                    if inner is not None and not inner.isSeparator():
                        keep_sub_item = inner.text()
                    break
            menu.setUpdatesEnabled(False)
        self._refresh_button = fill_menu(
            menu,
            self._models,
            str(self._current_model_id),
            self.model_chosen.emit,
            on_refresh=self._request_models_refresh,
            refreshing=self._models_refreshing,
        )
        if was_open:
            menu.adjustSize()
            menu.setUpdatesEnabled(True)
            self._keep_menu_focus(menu, keep_open_sub or keep, keep_sub_item)
            menu.update()

    @staticmethod
    def _menu_row_key(action) -> str:
        """这一行是哪一家：优先用挂在 action 上的供应商名（标题里的数量会变），没有就用文字。"""
        key = action.data()
        return key if isinstance(key, str) and key else action.text()

    def _keep_menu_focus(self, menu: QtWidgets.QMenu, wanted: str, wanted_item: str = "") -> None:
        """重建后把高亮/展开还回原来那一家（子菜单里停在哪个模型也一起还回去）。"""
        if not wanted:
            return
        for action in menu.actions():
            if action.isSeparator() or self._menu_row_key(action) != wanted:
                continue
            menu.setActiveAction(action)
            sub = action.menu()
            if isinstance(sub, QtWidgets.QMenu):
                sub.popup(menu.mapToGlobal(menu.actionGeometry(action).topRight()))
                if wanted_item:
                    for row in sub.actions():
                        if not row.isSeparator() and row.text() == wanted_item:
                            sub.setActiveAction(row)
                            break
            return

    def models(self) -> list[dict]:
        """当前菜单里的模型项（含可用性与原因），给「设置」按钮判断用。"""
        return list(self._models)

    def clear_conversation(self, reason: str = "") -> None:
        """新会话：清掉窗口里的旧记录（历史本身在引擎会话库里，那边已经换成了新会话）。

        `reason` 只进日志：窗口被清空时屏幕上东西都没了，事后只能靠这行日志判断是谁清的。
        """
        logger.info("[界面] 清空窗口（%s）", reason or "未说明")
        self._destroy_msg_bars()
        self._destroy_elements()
        self._msgs = []
        self.view.clear()
        self._streaming = False
        self._row_stamp_pending = None
        self._stream_msg = None
        self._rewind_waiting = False
        self._rewind_after_stop = None
        self._row_stamp_pending = None
        self._flush_timer.stop()
        self._assistant_pending = ""
        self._history_older = []
        self._history_rendered = []
        self._reasoning_pending = ""
        self._md_block = []
        self._md_fence = False
        self._md_math = False
        self._para_break = False
        self._reason_fold = None

        self._tool_folds = {}
        self._clarify = None
        self._clear_queue()
        self.stack.clear_all()
        self._voice_orb.set_state(STATE_IDLE)
        self._tool_started.clear()
        self._tool_clock.stop()
        self._spin_timer.stop()
        self._provider_wait = ""
        self._folds.reset()
        self._diff_texts = {}
        self._tool_payloads = {}
        self._tool_images = {}
        self._tool_term_drawn = {}
        self._tool_detail_seen = {}
        self._delegate_cards = {}
        self._delegate_latest = ""
        self._interrupted = False
        self._icon_registered.clear()
        self._photos.clear()
        self._photo_keys.clear()
        self._photo_sources.clear()
        self._originals.clear()
        self._local_photos.clear()
        self._photo_pending.clear()
        self._fence_cache.clear()
        self._review_count = 0
        self._review_folded = False
        self._review_pending = []
        self._bubble_has_text = False
        self._last_error_key = ""
        self._follow = True
        self._sync_send_button()
        self.speech_bar.dismiss()
        self._hide_hover()
        self._hide_jump()

    def has_transcript(self) -> bool:
        """屏幕上有没有真东西（正文或工具行）。铺历史前用它判断要不要动这个窗口。"""
        if self._folds.all():
            return True
        if self._streaming:
            return True
        return bool(self.view.toPlainText().strip())

    HISTORY_PAGE = 15

    def set_history(self, messages) -> None:
        """界面打开：把上一段对话铺出来（不是让用户去输入什么）。

        内容来自引擎的会话库，界面只负责显示——所以这里是整窗重画，不留第二份历史
        状态。为开窗快，只铺最近 `HISTORY_PAGE` 条；剩下的暂存成「还没铺的那截」，
        翻到顶再往前补（`load_older_history`），那截也只是显示源。

        但这个窗口里已经铺了正文/工具行时**绝不能**重画：引擎的 `session.resume` 会在
        开窗、重连、切模型之后陆续回来，一旦重画，刚显示出来的工具行（历史里没有它们，
        历史只有 role/text）就整片消失——屏幕上只剩用户和回答，工具痕迹全没了。
        """
        if self.has_transcript():
            return
        items = [item for item in (messages or ()) if str(item.get("text") or "").strip()]
        tail = items[-self.HISTORY_PAGE:]
        self._history_loading = True
        try:
            self.clear_conversation("历史重铺")
            self._history_older = items[: -self.HISTORY_PAGE]
            self._history_rendered = list(tail)
            self._paint_history(tail)
        finally:
            self._history_loading = False
        self._follow = True
        self._scroll_to_end()

    def load_older_history(self) -> None:
        """翻到顶：往前补一页更早的消息，铺完把滚动位置按新增高度让回来。"""
        if not self._history_older or self._restoring or self._history_loading:
            return
        self._history_loading = True
        try:
            self._follow = False
            take = self._history_older[-self.HISTORY_PAGE:]
            older = self._history_older[: -self.HISTORY_PAGE]
            rendered = take + self._history_rendered
            layout = self.view.document().documentLayout()
            old_height = layout.documentSize().height()
            old_value = self.view.verticalScrollBar().value()
            self.clear_conversation("历史补页")
            self._follow = False
            self._history_older = older
            self._history_rendered = rendered
            self._paint_history(rendered)
            QtCore.QTimer.singleShot(0, lambda: self._restore_scroll(old_height, old_value))
        finally:
            self._history_loading = False

    def _restore_scroll(self, old_height: float, old_value: int) -> None:
        """按新增高度把视野让回来：正在看的那行停在原位。

        滚动值就是「内容区顶上是哪一行」：补页铺了多少高度就把值让多少，刚补出来的那页
        正好顶到内容区顶、贴着带子下沿，跟补页前贴着带子下沿一致。这拍里带子若正好让位，
        `_set_band` 的值补偿会把顶上的行裁进带子底下——恢复是有意重定位，不是无感换带，
        这里的补偿冻住不认。
        """
        new_height = self.view.document().documentLayout().documentSize().height()
        bar = self.view.verticalScrollBar()
        frozen = self._band_freeze
        self._band_freeze = True
        try:
            bar.setValue(int(old_value + (new_height - old_height)))
        finally:
            self._band_freeze = frozen

    def _maybe_load_older_history(self) -> None:
        """滚到顶就往前补一页。"""
        if self.view.verticalScrollBar().value() <= 0:
            self.load_older_history()

    def _paint_history(self, items) -> None:
        """把一批历史消息按顺序铺出来；只管画，清窗与分页状态由调用方处理。"""
        self._restoring = True
        try:
            for item in items:
                text = str(item.get("text") or "")
                if not text.strip():
                    continue
                kind = str(item.get("display_kind") or "")
                if kind == _DELEGATION_DISPLAY_KIND:
                    self.append_notice_lines(delegate_view.completion_lines(text))
                    continue
                if kind in _NOTICE_LABELS:
                    label = _NOTICE_LABELS[kind]
                    tail = ""
                    if kind == "auto_continue" and _AUTO_CONTINUE_REQUEST in text:
                        tail = " ".join(text.split(_AUTO_CONTINUE_REQUEST, 1)[1].split())
                    self.append_notice_lines([f"{label}：{tail}" if tail else label])
                    continue
                if str(item.get("role") or "") == "user":
                    row_id = item.get("row_id")
                    self.append_user(text, row_id=row_id if isinstance(row_id, int) else None)
                else:
                    self.append_chunk(text)
                    self.end_assistant()
        finally:
            self._restoring = False

    def append_notice_lines(self, lines) -> None:
        """灰色说明行：引擎回灌给模型的机器消息（子代理完成之类），不把英文原文铺出来。"""
        for line in lines or ():
            self._insert_html(f"<span style='color:#8f9bb0;'>{html.escape(str(line))}</span>")

    def _user_name_font(self) -> QtGui.QFont:
        font = QtGui.QFont(self.view.font())
        font.setPixelSize(13)
        font.setBold(True)
        return font

    def _user_text_font(self) -> QtGui.QFont:
        font = QtGui.QFont(self.view.font())
        font.setPixelSize(13)
        return font

    USER_BAR_PX = 52
    FRAME_PAD = 8
    FRAME_EDGE = QtGui.QColor(243, 172, 206, 130)
    BODY_BUTTON_MARGIN = 16

    def _user_line_room(self, name_width: float) -> float:
        document = self.view.document()
        margins = 2 * (document.documentMargin() + self.FRAME_PAD) + self.USER_BAR_PX + 4
        return max(60.0, self.view.viewport().width() - margins - name_width)

    @staticmethod
    def _close_user_frame(frame: QtGui.QTextFrame) -> None:
        """框后面留一个空段：往后的内容落在气泡外面，不会写进这条消息里。"""
        cursor = frame.lastCursorPosition()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.NextBlock)
        if cursor.currentFrame() is frame:
            cursor.insertBlock()

    @staticmethod
    def _apply_frame_gap(frame: QtGui.QTextFrame, gap: int) -> None:
        """两条消息之间的空白挂在框外面，气泡里就只剩自己的留白。"""
        fmt = frame.frameFormat()
        fmt.setTopMargin(gap)
        fmt.setBottomMargin(0)
        frame.setFrameFormat(fmt)

    def _open_user_frame(self) -> QtGui.QTextFrame:
        """给这条消息开一个框：只描一条边，里面不填色。"""
        fmt = QtGui.QTextFrameFormat()
        fmt.setBorder(1)
        fmt.setBorderBrush(QtGui.QBrush(self.FRAME_EDGE))
        fmt.setBorderStyle(QtGui.QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        fmt.setPadding(self.FRAME_PAD)
        cursor = QtGui.QTextCursor(self.view.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        frame = cursor.insertFrame(fmt)
        self._collapse_doc_head()
        return frame

    def _collapse_doc_head(self) -> None:
        """文档起手那个空块还空着就塌成 0 高，首条消息顶到第一行。"""
        block = self.view.document().firstBlock()
        if block.text() or block.userData() is not None:
            return
        fmt = block.blockFormat()
        fmt.setLineHeight(
            0.0, QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        )
        QtGui.QTextCursor(block).setBlockFormat(fmt)

    def _fit_user_line(self, msg: dict) -> None:
        """一行装不下就把显示截断：原文不动，仍留在 msg["text"] 里。"""
        block = msg.get("head_block")
        text = str(msg.get("text") or "")
        if block is None or not block.isValid():
            return
        name_width = QtGui.QFontMetricsF(self._user_name_font()).horizontalAdvance(
            f"{_user_display_name()} "
        )
        shown = QtGui.QFontMetricsF(self._user_text_font()).elidedText(
            text, QtCore.Qt.TextElideMode.ElideRight, self._user_line_room(name_width)
        )
        msg["ellipsis"] = shown != text
        msg["fit_width"] = self.view.viewport().width()
        self._write_user_line(block, shown)
        self._apply_head_format(block)

    def _apply_head_format(self, block: QtGui.QTextBlock) -> None:
        """重写那一行会把块格式一并换掉：段前距和气泡里给按钮留的宽度重算一遍。

        气泡的底色和留白不在这里——它们挂在段框上（见 `_open_user_frame`）。
        """
        fmt = block.blockFormat()
        gap = self._msg_gap(block)
        mark = block.userData()
        bubble = isinstance(mark, _MsgMark) and mark.msg.get("role") == "user"
        changed = False
        if bubble:
            if int(fmt.leftMargin()) != 0:
                fmt.setLeftMargin(0)
                changed = True
            if int(fmt.rightMargin()) != self.USER_BAR_PX:
                fmt.setRightMargin(self.USER_BAR_PX)
                changed = True
            if int(fmt.bottomMargin()) != 0:
                fmt.setBottomMargin(0)
                changed = True
            if fmt.background().style() != QtCore.Qt.BrushStyle.NoBrush:
                fmt.setBackground(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
                changed = True
            if not fmt.nonBreakableLines():
                # 这一行按算好的宽度截断，不让它自己折行：折了就不是「一行加省略号」了。
                fmt.setNonBreakableLines(True)
                changed = True
        elif int(fmt.topMargin()) != gap:
            fmt.setTopMargin(gap)
            changed = True
        if changed:
            QtGui.QTextCursor(block).setBlockFormat(fmt)

    def _write_user_line(self, block: QtGui.QTextBlock, shown: str) -> None:
        """这一行只有「署名 + 正文」两段字，直接按字符格式写，不走 HTML 解析。"""
        cursor = QtGui.QTextCursor(block)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QtGui.QTextCursor.MoveOperation.EndOfBlock, QtGui.QTextCursor.MoveMode.KeepAnchor
        )
        cursor.removeSelectedText()
        name = QtGui.QTextCharFormat()
        name.setFont(self._user_name_font())
        name.setForeground(QtGui.QColor("#c9a6e0"))
        body = QtGui.QTextCharFormat()
        body.setFont(self._user_text_font())
        body.setForeground(QtGui.QColor(214, 198, 222, 215))
        cursor.insertText(f"{_user_display_name()} ", name)
        cursor.insertText(shown, body)

    def append_user(self, message: str, attachments=(), row_id: int | None = None) -> None:
        self._last_error_key = ""
        message, refs = split_attachment_refs(message)
        items = [*refs, *attachments]
        start = self.view.document().characterCount() - 1
        frame = self._open_user_frame()
        self._begin_message("user", start, message, row_id, frame)
        self._fit_user_line(self._msgs[-1])
        if attachments:
            self._msgs[-1]["attachments"] = list(attachments)
        if row_id is None and self._row_stamp_pending is not None:
            self._msgs[-1]["row_id"] = self._row_stamp_pending
            self._row_stamp_pending = None
        for item in items:
            self._append_user_attachment(item)
        frame = self._msgs[-1].get("frame")
        if frame is not None:
            self._close_user_frame(frame)
        self._mount_msg_bar(self._msgs[-1])
        if not self._restoring:
            self._follow = True
            self._scroll_to_end_later()
            self._hide_jump()

    def _append_user_attachment(self, item: dict) -> None:
        """你发的附件留在气泡里：图片给缩略图，其它给一行文件名。"""
        pixmap = self._attachment_pixmap(item)
        if pixmap is None:
            self._insert_html(f"<br><span style='color:#9aa4b2;'>📎 {html.escape(item['name'])}</span>")
            return
        self._image_seq += 1
        key = f"noesia-image://user-{self._image_seq}"
        self.view.document().addResource(
            QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key), pixmap
        )
        self._local_photos[key] = (
            item.get("name") or Path(item.get("path") or "").name or "图片",
            Path(item["path"]) if item.get("path") else item.get("data") or b"",
        )
        width = min(200, pixmap.width() or 200)
        self._insert_photo_block(key, width)

    def _insert_photo_block(self, key: str, width: int) -> None:
        """图自己占一段：段格式沿用这条消息的缩进与留白，行高按单倍，不另起空行。

        `<br><img>` 塞进同一条消息时，Qt 会先给一行空行再把图放上去——图上方平白多
        17px，且这一块没有消息标记，下一段就按「同一轮继续写」算间距。
        """
        document = self.view.document()
        head: QtGui.QTextBlock | None = None
        if self._msgs:
            head = self._msgs[-1].get("head_block")
        cursor = QtGui.QTextCursor(document)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if head is not None and head.isValid() and not head.text().strip():
            block = head
            cursor.setPosition(block.position())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock)
        else:
            fmt = QtGui.QTextBlockFormat()
            if head is not None:
                fmt = QtGui.QTextBlockFormat(head.blockFormat())
            fmt.setTopMargin(ChatView.BUBBLE_PAD_Y + MSG_GAP_TIGHT)
            fmt.setBottomMargin(0)
            fmt.setLineHeight(0, QtGui.QTextBlockFormat.LineHeightTypes.SingleHeight.value)
            cursor.insertBlock(fmt)
            block = cursor.block()
        if self._msgs:
            block.setUserData(_PhotoMark(self._msgs[-1]))
        image = QtGui.QTextImageFormat()
        image.setName(key)
        image.setWidth(width)
        cursor.insertImage(image)

    def begin_assistant(self) -> None:
        self._end_reasoning_group()
        self._flush_md()
        start = self.view.document().characterCount() - 1
        self._insert_html("<span style='color:#d9b8cd;font-size:11px;'><b>Noesia</b></span><br>")
        self._begin_message("assistant", start, "", None)
        self._streaming = True
        self._bubble_has_text = False

    FLUSH_SLICE_MS = 40

    def append_chunk(self, chunk: str) -> None:
        if self._interrupted:
            return
        if not self._streaming:
            self.begin_assistant()
            self._bubble_has_text = False
        self._assistant_pending += chunk
        if self._stream_msg is not None:
            self._stream_msg["text"] += chunk
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def end_assistant(self) -> None:
        self._flush_timer.stop()
        self._end_reasoning_group()
        self._pump_assistant(final=True)
        self._flush_md()
        self._drop_trailing_blank_line()
        if self._msgs:
            self._mount_msg_bar(self._msgs[-1])
        self._assistant_pending = ""
        self._streaming = False
        self._stream_msg = None
        pending, self._review_pending = self._review_pending, []
        for text in pending:
            self.append_review_summary(text)

    def set_media_roots(self, roots) -> None:
        """相对路径（模型常写 `MEDIA:data/workspace/x.png`）从哪些目录开始找。"""
        self._media_roots = tuple(path for path in (roots or ()) if path)

    def _pump_assistant(self, *, final: bool) -> None:
        """把缓冲里的完整行落进窗口。

        只认完整的行：`MEDIA:D:/…` 在流里常被切成两半，抢先显示就会露出半截路径。
        最后那半行如果是普通文字就立刻显示（不拖节奏），只有可能是 `MEDIA:` 开头时才压着。
        """
        data = self._assistant_pending
        while True:
            cut = data.find("\n")
            if cut < 0:
                break
            self._emit_line(data[:cut])
            data = data[cut + 1:]
        if final:
            if data:
                self._emit_line(data)
            self._assistant_pending = ""
            return
        stripped = data.strip()
        hold_partial = (
            self._md_fence
            or self._md_math
            or (not self._md_block and "```".startswith(stripped))
            or data.count("**") % 2
            or data.rstrip().endswith("*")
            or data.count("`") % 2
            or _open_link(data)
            or markdown_render.math_incomplete(data)
            or markdown_render.is_structural_prefix(data)
        )
        if (
            data
            and not hold_partial
            and not media_links.might_be_media(data)
            and not markdown_render.is_structural(data)
        ):
            self._insert_inline(markdown_render.inline(data))
            data = ""
        self._assistant_pending = data

    def _emit_line(self, line: str) -> None:
        rest, path = media_links.split_media_line(line)
        if path is None:
            self._md_feed(rest)
            return
        if rest:
            self._md_feed(rest)
        self._flush_md()
        self._insert_media(path)


    def _md_feed(self, line: str) -> None:
        """一行行喂进来：结构行攒着整段画，普通行立刻行内画。

        这样列表、表格、代码块才是一个整体（一行一行画会把一张表拆成几十段），
        而普通句子仍然是「来一行显示一行」，不损失流式手感。
        """
        if markdown_render.is_fence(line):
            self._md_block.append(line)
            self._md_fence = not self._md_fence
            if not self._md_fence:
                self._flush_md()
            return
        if self._md_fence:
            self._md_block.append(line)
            return
        if self._md_math:
            self._md_block.append(line)
            if markdown_render.math_fence_closes(line):
                self._md_math = False
                self._flush_md()
            return
        if markdown_render.is_math_fence(line):
            self._flush_md()
            self._md_block.append(line)
            self._md_math = True
            return
        if markdown_render.is_math_line(line):
            self._flush_md()
            self._md_block.append(line)
            self._flush_md()
            return
        if not line.strip():
            self._flush_md()
            self._break_paragraph()
            return
        if markdown_render.is_structural(line):
            self._md_block.append(line)
            if len(self._md_block) >= 80:
                self._flush_md()
            return
        self._flush_md()
        self._insert_inline(markdown_render.inline(line) + "<br/>")

    def _break_paragraph(self) -> None:
        """空行 = 另起一段（markdown 里的段落边界）。

        这里只记一笔、不当场开段：空行后面若是列表/表格，Qt 自己会开段（当场开就是白留一整行）；
        若是普通句子，等它真来的时候再开（`_insert_inline`）。

        紧随列表/表格之后的那段普通文字也要断开，否则会被并进最后一个列表项。
        但这一轮还什么都没写时不算（回复开头的空行不该在名字底下顶出一行空白）。
        """
        if self._bubble_has_text:
            self._para_break = True

    def _flush_md(self) -> None:
        """把攒着的结构段一次画出来（列表 / 表格 / 代码块 / 标题）。"""
        text = "\n".join(self._md_block).strip("\n")
        self._md_block = []
        self._md_fence = False
        self._md_math = False
        if not text.strip():
            return
        self._bubble_has_text = True
        for kind, language, body in markdown_render.split_fences(text):
            if kind == "code":
                self._insert_code_block(body, language)
            else:
                self._insert_html(markdown_render.block(body))


    def _photo_placeholder(self, text: str) -> QtGui.QPixmap:
        """取图期间的占位（灰底一行小字），免得空一块让人不知道发生了什么。"""
        width = max(120, min(_THUMB_WIDTH, self.view.viewport().width() - 40))
        pixmap = QtGui.QPixmap(width, 48)
        pixmap.fill(QtGui.QColor(0, 0, 0, 0))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QtGui.QColor("#242630"))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, width, 48, 6, 6)
        painter.setPen(QtGui.QColor("#8f9bb0"))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return pixmap

    def _photo_key(self, url: str) -> str:
        """给这个网址安排一个文档资源名；还没取过的顺手发起下载。"""
        key = self._photo_keys.get(url)
        if key is None:
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            key = f"noesia-photo://{digest}"
            self._photo_keys[url] = key
            self._photo_sources[key] = url
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource,
                QtCore.QUrl(key),
                self._photo_placeholder("图片加载中…"),
            )
        if url not in self._photos and url not in self._photo_pending:
            self._photo_fetch(url, key)
        return key

    def _photo_fetch(self, url: str, key: str) -> None:
        """取图放后台跑：取不回来只是这块图空着，绝不能拖累正文渲染。"""
        self._photo_pending.add(url)
        try:
            request = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
            request.setHeader(
                QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader, "Noesia Agent"
            )
            request.setTransferTimeout(10000)
            reply = self._net.get(request)
            reply.finished.connect(lambda: self._photo_arrived(reply, url, key))
        except Exception:
            self._photo_pending.discard(url)
            logger.warning("正文图片没发出去：%s", url, exc_info=True)

    def _photo_arrived(self, reply, url: str, key: str) -> None:
        """图取回来了：换成真图重画；取不回来就如实说没加载出来。"""
        self._photo_pending.discard(url)
        stuck = self._follow
        try:
            failed = reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll().data())
            reply.deleteLater()

            pixmap = QtGui.QPixmap()
            broken = failed or not pixmap.loadFromData(data) or pixmap.isNull()
            if broken:
                pixmap = self._photo_placeholder("图片没加载出来")
            else:
                limit = max(120, min(_THUMB_WIDTH, self.view.viewport().width() - 40))
                if pixmap.width() > limit:
                    pixmap = pixmap.scaledToWidth(
                        limit, QtCore.Qt.TransformationMode.SmoothTransformation
                    )
                self._remember_original(url, data)
            if not broken:
                self._photos[url] = pixmap
            while len(self._photos) > _PHOTO_CACHE:
                self._photos.pop(next(iter(self._photos)))
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key), pixmap
            )
            self.view.document().markContentsDirty(0, self.view.document().characterCount())
            self.view.viewport().update()
            if stuck:
                self._scroll_to_end_later()
        except Exception:
            logger.warning("正文图片落地时出错：%s", url, exc_info=True)

    def _adopt_images(self, markup: str) -> str:
        """把正文里的图片备好资源：网络图换成文档资源名，公式图注册好 PNG。

        Qt 的富文本不会去下载 `http(s)` 的图，直接插 `<img src="https://…">` 只剩一块空白；
        换成注册过的资源名，取回来以后才画得出来。被流式切开的那种半截网址到这里已经完整
        （`_open_link` 会压到括号到齐才放行）。公式图（`noesia-math://`）是本地渲好的 PNG，
        插之前注册进文档资源，`<img>` 才有东西可画。
        """
        self._register_math_images(markup)
        if "http" not in markup:
            return markup
        try:
            return self._swap_image_sources(markup)
        except Exception:
            logger.warning("正文图片处理失败，按原文插入", exc_info=True)
            return markup

    def _register_math_images(self, markup: str) -> None:
        """公式图注册进 QTextDocument 文档资源；同名重复注册只是覆盖，没有副作用。"""
        if "noesia-math:" not in markup:
            return
        for src in re.findall(r'src="(noesia-math://[^"]+)"', markup):
            pixmap = QtGui.QPixmap()
            pixmap.loadFromData(markdown_render.math_png(src))
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource,
                QtCore.QUrl(src),
                pixmap,
            )

    def _swap_image_sources(self, markup: str) -> str:
        marker = 'src="'
        pieces: list[str] = []
        index = 0
        while True:
            found = markup.find(marker, index)
            if found < 0:
                pieces.append(markup[index:])
                break
            closing = markup.find('"', found + len(marker))
            if closing < 0:
                pieces.append(markup[index:])
                break
            url = markup[found + len(marker) : closing]
            pieces.append(markup[index : found + len(marker)])
            url = html.unescape(url)
            if url.lower().startswith(("http://", "https://")):
                pieces.append(self._photo_key(url))
            else:
                pieces.append(url)
            pieces.append('"')
            index = closing + 1
        return "".join(pieces)

    def _remember_original(self, url: str, data: bytes) -> None:
        """记着原图字节（只留最近几张）：点开看原图、存原图都靠它。"""
        if not data:
            return
        self._originals[url] = data
        while len(self._originals) > _ORIGINAL_CACHE:
            self._originals.pop(next(iter(self._originals)))

    @staticmethod
    def _is_photo_format(fmt: QtGui.QTextCharFormat) -> bool:
        """是不是一张能看、能下载的原图。

        抬头和按钮上的小图标也是图片字符（`noesia-icon://`），但它们不是原图；
        一律当成图片算，鼠标扫过一行工具抬头就会冒出下载按钮。
        """
        if not fmt.isImageFormat():
            return False
        name = fmt.toImageFormat().name()
        return name.startswith("noesia-image://") or name.startswith("noesia-photo://")

    def _point_at_photo(self, pos: QtCore.QPoint) -> None:
        """鼠标压在能点的地方给手型：图片、链接、折叠段抬头都是。

        以前只看图片，别处一律 `unsetCursor()`——那会把链接的手型也一并擦掉，鼠标压在
        「📎 图片 · xx.jpg · 点击打开」这种行上，看着跟普通文字一模一样，没人知道能点。
        """
        if self._photo_at(pos) or self.view.anchorAt(pos):
            self.view.viewport().setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        else:
            self.view.viewport().unsetCursor()

    def _photo_rect_for(self, position: int, image: QtGui.QTextImageFormat) -> QtCore.QRect | None:
        """这张图画在视口里的哪一块：位置取图片字符自己的光标矩形，宽高取画出来的尺寸。

        尺寸只认**文档里画出来的大小**（插入时 setWidth/setHeight 的那个），不能拿资源里
        那张原图的像素宽：vision_analyze 那张截图原图 1736 宽、画出来 288 宽，用原图宽算，
        这块矩形就横穿整行——下载按钮被挤到行尾，跟代码块的复制按钮叠在一起，谁都点不到。
        """
        key = image.name()
        cursor = QtGui.QTextCursor(self.view.document())
        cursor.setPosition(position)
        line = self.view.cursorRect(cursor)
        width = int(image.width())
        if width <= 0:
            pixmap = self.view.document().resource(
                QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key)
            )
            if isinstance(pixmap, QtGui.QPixmap) and not pixmap.isNull():
                width = pixmap.width()
            else:
                cached = self._photos.get(self._photo_sources.get(key, ""))
                if not isinstance(cached, QtGui.QPixmap) or cached.isNull():
                    return None
                width = cached.width()
        height = int(image.height()) or line.height()
        if width <= 0 or height <= 0:
            return None
        return QtCore.QRect(line.left(), line.top(), width, height)

    def _photo_at(self, pos: QtCore.QPoint) -> tuple[str, QtCore.QRect] | None:
        """鼠标是不是真落在某张图上：认出块里的图片字符，逐张拿它自己的矩形验一遍。

        别用 `cursorForPosition(pos).charFormat()` 认图：光标会吸到最近的字符边界上，附件行里
        图片前面还多一个换行符，图靠左那一片点下去取回来的是换行符的格式，既不认图也不亮下载
        按钮——图看着 200 宽，只有右半边能点。矩形还顺带挡住「图片右边那片空白」：图片在文档里
        只占一个字符位，字符格式会往左右延伸，不验矩形的话鼠标一出图按钮就被重新摆到图外面去。
        """
        block = self._block_at(pos)
        if block is None or not block.isValid() or not self._block_has_image(block):
            return None
        fragment = block.begin()
        while not fragment.atEnd():
            piece = fragment.fragment()
            if piece.isValid() and self._is_photo_format(piece.charFormat()):
                image = piece.charFormat().toImageFormat()
                rect = self._photo_rect_for(piece.position(), image)
                if rect is not None and rect.contains(pos):
                    return image.name(), rect
            fragment += 1
        return None

    def _show_hover_download(self, key: str, rect: QtCore.QRect) -> None:
        """图上悬停时右上角的下载按钮：点一下直接存原图，不用先打开看原图。"""
        button = self._element_button(
            ("photo", key), "download", "下载", lambda k=key: self._download_photo(k), flash=False
        )
        button.setStyleSheet(
            "QToolButton { border: none; border-radius: 6px;"
            " background: rgba(16, 18, 24, 150); padding: 0; }"
            "QToolButton:hover { background: rgba(16, 18, 24, 225); }"
        )
        button.place_at(rect)
        self._show_element(button)

    def _download_photo(self, key: str) -> None:
        """存这张图的原图。手上有原图字节就直接问存哪儿，没有就先取回来。"""
        url = self._photo_sources.get(key)
        if not url:
            local = self._local_photo(key)
            if local is None:
                self._flash_hint("这张图的原文件找不到了")
                return
            self._save_bytes(*local)
            return
        if url in self._downloading:
            return
        data = self._originals.get(url)
        if data:
            self._save_bytes(_photo_name(url, data), data)
            return
        self._downloading.add(url)
        self._fetch_then_save(url)

    def _local_photo(self, key: str) -> tuple[str, bytes] | None:
        """本地图的名字和字节：文件现读，粘进来的字节直接用。读不到返回空。"""
        held = self._local_photos.get(key)
        if held is None:
            return None
        name, source = held
        try:
            data = source if isinstance(source, bytes) else Path(source).read_bytes()
        except OSError:
            return None
        if not data:
            return None
        if not Path(name).suffix:
            name += _image_extension(data)
        return name, data

    def _save_bytes(self, name: str, data: bytes) -> None:
        """问存哪儿、写盘，成了就在底部报一句文件名。取消就什么也不说。"""
        path = save_photo_bytes(self.window(), data, name)
        if path:
            self._flash_hint(f"已保存 {Path(path).name}")

    def _flash_hint(self, text: str) -> None:
        """底部提示行临时改口说一句，两秒半后回到原话。"""
        if self._hint_label is None:
            return
        self._hint_label.setText(text)
        self._hint_timer.start(2500)

    def _restore_hint(self) -> None:
        if self._hint_label is not None:
            self._hint_label.setText(_HINT_TEXT)

    def _fetch_then_save(self, url: str) -> None:
        """原图字节早被松手了：取回来再问存哪儿。"""
        try:
            request = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
            request.setHeader(
                QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader, "Noesia Agent"
            )
            request.setTransferTimeout(15000)
            reply = self._net.get(request)
            reply.finished.connect(lambda: self._save_after_fetch(reply, url))
        except Exception:
            self._downloading.discard(url)
            logger.warning("取原图失败：%s", url, exc_info=True)
            self._flash_hint("原图没取回来")

    def _save_after_fetch(self, reply, url: str) -> None:
        try:
            ok = reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll().data())
            reply.deleteLater()
            if not ok or not data:
                self._flash_hint("原图没取回来")
                return
            self._remember_original(url, data)
            self._save_bytes(_photo_name(url, data), data)
        except Exception:
            logger.warning("存原图出错：%s", url, exc_info=True)
            self._flash_hint("原图没取回来")
        finally:
            self._downloading.discard(url)

    def _open_photo(self, key: str) -> None:
        """点图：开窗看原图。原图字节还在就直接给，松过手的先拿缩略图顶着、同时去取。"""
        url = self._photo_sources.get(key)
        if not url:
            return
        dialog = PhotoView(_photo_name(url), self.window())
        data = self._originals.get(url)
        if data:
            pixmap = QtGui.QPixmap()
            if pixmap.loadFromData(data) and not pixmap.isNull():
                dialog.set_photo(pixmap)
                try:
                    dialog.exec()
                finally:
                    dialog.deleteLater()
                return
        thumb = self._photos.get(url)
        if thumb is not None:
            dialog.set_photo(thumb, pending=True)
        else:
            dialog.set_waiting("正在取原图…")
        self._refetch_original(url, dialog)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _refetch_original(self, url: str, dialog: PhotoView) -> None:
        """原图字节被松手了：单独再取一次，回来直接换到窗口上。"""
        try:
            request = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
            request.setHeader(
                QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader, "Noesia Agent"
            )
            request.setTransferTimeout(15000)
            reply = self._net.get(request)
            reply.finished.connect(lambda: self._original_arrived(reply, url, dialog))
        except Exception:
            logger.warning("原图没发出去：%s", url, exc_info=True)
            dialog.set_failed("原图取不回来")

    def _original_arrived(self, reply, url: str, dialog: PhotoView) -> None:
        try:
            ok = reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll().data())
            reply.deleteLater()
            pixmap = QtGui.QPixmap()
            if not ok or not data or not pixmap.loadFromData(data) or pixmap.isNull():
                dialog.set_failed("原图没加载出来")
                return
            self._remember_original(url, data)
            dialog.set_photo(pixmap)
        except Exception:
            logger.warning("原图落地出错：%s", url, exc_info=True)

    def _relax_image_line(self, block: "QtGui.QTextBlock | None" = None) -> None:
        """图独自占一整行时，别让 130% 的行距把图上下撑开。

        行距是按字高算的；一段里只有图、没有字，Qt 仍然按「字高 × 1.3」给这一行定高，
        图被塞在中间，上下各留半行——几张图连着排，每两张之间就空出小半张图，
        看着像每条图后面都跟了个空行。
        """
        if block is None:
            block = self.view.document().lastBlock()
        if block is None or not block.isValid():
            return
        text = block.text()
        if "\ufffc" not in text:
            return
        if text.replace("\u2028", "").replace("\ufffc", "").strip():
            return
        fmt = block.blockFormat()
        if fmt.lineHeightType() != QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value:
            return
        fmt.setLineHeight(0, QtGui.QTextBlockFormat.LineHeightTypes.SingleHeight.value)
        QtGui.QTextCursor(block).setBlockFormat(fmt)

    def _insert_inline(self, markup: str) -> None:
        """行内片段：接在当前段里，不另起一段（普通句子不该每行一个大间距）。"""
        if not markup:
            return
        self._bubble_has_text = True
        cursor = QtGui.QTextCursor(self.view.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if self._para_break or self._folds.owns(cursor.position()) or self._is_code_block(cursor.block()):
            cursor.insertBlock()
            cursor.setCharFormat(QtGui.QTextCharFormat())
            cursor.setBlockFormat(_plain_block_format())
        self._para_break = False
        cursor.insertHtml(_keep_edge_spaces(self._adopt_images(markup)))
        self._relax_image_line()
        self._settle_insert()

    def _drop_trailing_blank_line(self) -> None:
        """去掉上一行末尾 `<br/>` 留下的空行。

        说到一半的普通句子是一行一个 `<br/>`（流式才跟得上），末尾那个换行在「下一段是列表」时
        就成了一整行空白——屏幕上回复和列表之间那一大截间距。
        """
        block = self.view.document().lastBlock()
        if not block.text().endswith("\u2028"):
            return
        cursor = QtGui.QTextCursor(block)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock)
        cursor.deletePreviousChar()

    def _insert_media(self, raw_path: str) -> None:
        """附件单独占一行：文件名 + 大小 + 点击打开（文件不存在就如实说找不到）。"""
        self._bubble_has_text = True
        self._insert_html(media_links.media_link_html(raw_path, self._media_roots) + "<br>")

    def _on_anchor(self, url: QtCore.QUrl) -> None:
        """文档里的链接：折叠段的抬头（`#fold:N`）就地开合，其余是附件（用默认程序打开）。"""
        target = url.fragment()
        if target.startswith("clarify:"):
            self._on_clarify_click(target.split(":", 1)[1])
            return
        if target.startswith("fold:"):
            fold = self._folds.get(int(target.split(":", 1)[1]))
            if fold is not None:
                following = self._follow
                self._folds.toggle(fold.key)
                fold.pinned = True
                if following:
                    self._scroll_to_end_later()
            return
        self._open_media(url)

    def _open_media(self, url: QtCore.QUrl) -> None:
        path = url.toLocalFile()
        if not path:
            return
        if not media_links.open_local(Path(path)):
            self.show_error(f"打不开这个文件：{path}")


    def show_notice(self, key: str, text: str, *, ttl_ms: int = 0, level: str = "") -> None:
        """引擎的提示（`notification.show`）：按 key 管，带 `ttl_ms` 的到点自己消失。"""
        self.stack.show_notice(key, text, ttl_ms=ttl_ms, level=level)

    def clear_status_notice(self, key: str) -> None:
        """撤掉输入框上方状态条里的一条（按 key；不碰聊天正文）。"""
        self.stack.clear_notice(key)

    def set_processes(self, rows: list) -> None:
        """还在跑的后台进程：一行一个，右边能停（`process.list` 的原样行）。"""
        self.stack.set_processes(rows)

    def tool_progress_enabled(self) -> bool:
        return self._tool_progress != "off"

    def set_activity(self, text: str) -> None:
        """「正在调用 <工具>…」—工具真的开跑就撤掉，它只是「马上有东西了」的信号。"""
        self.stack.set_activity(text)


    def _question_format(self) -> QtGui.QTextCharFormat:
        """问题本身：比选项亮一点、大一点，扫一眼就知道在问什么。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#e6ecf8"))
        fmt.setFontPointSize(10)
        return fmt

    def _qlabel_format(self) -> QtGui.QTextCharFormat:
        """「问 题」这个小抬头：暗、小、字距拉开，只用来分层。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#7f8a9c"))
        fmt.setFontPointSize(8)
        fmt.setFontLetterSpacing(140)
        return fmt

    def _chip_format(self) -> QtGui.QTextCharFormat:
        """「选一个」「可多选」这种提示：一小截淡灰字，不铺底色。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#8a94a6"))
        fmt.setFontPointSize(8)
        return fmt

    def _badge_format(self) -> QtGui.QTextCharFormat:
        """选项行首的 A/B/C：淡灰小字，同样不铺底色。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#8a94a6"))
        fmt.setFontPointSize(8.5)
        return fmt

    @staticmethod
    def _bare_choice(choice: str) -> tuple[str, bool]:
        """引擎给推荐选项后面加 "(Recommended)"（`mark_recommended`）；桌面把它剥掉只留淡字提示。"""
        suffix = "(Recommended)"
        if choice.endswith(suffix):
            return choice[: -len(suffix)].strip(), True
        return choice, False

    def _spacer_format(self) -> QtGui.QTextCharFormat:
        fmt = QtGui.QTextCharFormat()
        fmt.setFontPointSize(3)
        return fmt

    def _hint_format(self) -> QtGui.QTextCharFormat:
        """「在下面输入框回答我」这种说明：斜体暗字，不跟选项抢。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#93a0b4"))
        fmt.setFontPointSize(9)
        fmt.setFontItalic(True)
        return fmt

    def _answered_format(self, *, picked: bool) -> QtGui.QTextCharFormat:
        """答完之后：选中的绿字加粗，没选的降成暗字（还留在记录里，只是不再可点）。"""
        fmt = QtGui.QTextCharFormat()
        fmt.setFontPointSize(9.5)
        if picked:
            fmt.setForeground(QtGui.QColor("#8fd8a8"))
            fmt.setFontWeight(QtGui.QFont.Weight.DemiBold)
        else:
            fmt.setForeground(QtGui.QColor("#7d8798"))
        return fmt

    def _quiet_link_format(self, href: str, *, color: str) -> QtGui.QTextCharFormat:
        """次要的可点动作（「跳过」「✔ 就这样」）：不抢选项的蓝色。"""
        fmt = self._hint_format()
        fmt.setAnchor(True)
        fmt.setAnchorHref(href)
        fmt.setForeground(QtGui.QColor(color))
        fmt.setFontItalic(False)
        return fmt

    def _link_format(self, href: str) -> QtGui.QTextCharFormat:
        fmt = QtGui.QTextCharFormat(self._question_format())
        fmt.setAnchor(True)
        fmt.setAnchorHref(href)
        fmt.setForeground(QtGui.QColor("#8fc2ff"))
        return fmt

    @staticmethod
    def _choice_rows(question: dict) -> tuple[list[str], bool]:
        choices = [str(choice) for choice in (question.get("choices") or []) if str(choice).strip()]
        return choices, bool(question.get("multi_select")) and bool(choices)

    def show_clarify(self, payload: dict) -> None:
        """引擎提问：一张卡，选项点一下就算答了（桌面端 `clarify` 那张卡）。

        答过的选项变成「✓ 选了什么」留在记录里，整批答完抬头换成「已回答」；没选项的问题
        直接用输入框回（和桌面端一样，下一句打出去就当答案）。
        """
        questions = [
            question for question in (payload.get("questions") or [])
            if isinstance(question, dict) and str(question.get("question") or "").strip()
        ]
        if not questions:
            return
        fold = self._folds.new("clarify", self._clarify_head(questions), open_=True)
        self._clarify_gen += 1
        self._clarify = {
            "gen": self._clarify_gen,
            "qcount": len(questions),
            "request_id": str(payload.get("request_id") or ""),
            "reply_id": payload.get("reply_id"),
            "fold": fold,
            "questions": questions,
            "answers": {},
            "marks": {},
            "choice_marks": {},
            "picks": {},
        }
        for index in range(len(questions)):
            self._draw_clarify_question(index)
        self._folds.append_marked(
            fold, "跳过",
            body_format=self._quiet_link_format(f"#clarify:{self._clarify['gen']}:skip", color="#8d96a8")
        )
        self._settle_insert()

    @staticmethod
    def _clarify_head(questions: list[dict]) -> str:
        """卡片的抬头：说清要你干什么，别用第三人称说「她在问你」。"""
        if len(questions) > 1:
            return f"需要你确认 · {len(questions)} 件事"
        question = questions[0]
        if question.get("choices"):
            return "需要你选几个" if question.get("multi_select") else "需要你选一个"
        return "需要你回答"

    def _draw_clarify_question(self, qindex: int) -> None:
        state = self._clarify
        if state is None:
            return
        question = state["questions"][qindex]
        fold = state["fold"]
        qid = str(question.get("qid") or "")
        text = " ".join(str(question.get("question") or "").split())
        choices, multi = self._choice_rows(question)
        marks = state["marks"].setdefault(qid, [])
        if qindex:
            self._folds.append_marked(fold, " ", body_format=self._spacer_format())
        if state["qcount"] > 1:
            marks.append(
                self._folds.append_marked(fold, f"问题 {qindex + 1}", body_format=self._qlabel_format())
            )
        if not choices:
            marks.append(self._folds.append_marked(fold, text, body_format=self._question_format()))
            marks.append(
                self._folds.append_marked(
                    fold, "✎ 在下面的输入框里敲一句回答我", body_format=self._hint_format()
                )
            )
            return
        marks.append(
            self._folds.append_marked(
                fold, "",
                runs=[
                    (text, self._question_format()),
                    ("   ", self._question_format()),
                    ("可多选" if multi else "选一个", self._chip_format()),
                ],
            )
        )
        picked = state["picks"].get(qid) or set()
        choice_marks = state["choice_marks"].setdefault(qid, [])
        for index, choice in enumerate(choices):
            label, recommended = self._bare_choice(" ".join(choice.split()))
            badge = ("☑" if index in picked else "☐") if multi else chr(ord("A") + index)
            runs = [
                (f"{badge}  ", self._badge_format()),
                (label, self._link_format(f"#clarify:{state['gen']}:{qindex}:{index}")),
            ]
            if recommended:
                runs.append((" (Recommended)", self._hint_format()))
            choice_marks.append(self._folds.append_marked(fold, "", runs=runs))
        if multi:
            marks.append(
                self._folds.append_marked(
                    fold, "✔ 就这样",
                    body_format=self._quiet_link_format(
                        f"#clarify:{state['gen']}:{qindex}:*", color="#9ad0ff"
                    ),
                )
            )

    def _redraw_clarify_question(self, qindex: int) -> None:
        """多选勾一下：把每题的那几行**原地换字**（勾上了就 ☑，去掉就 ☐）。"""
        state = self._clarify
        if state is None:
            return
        question = state["questions"][qindex]
        qid = str(question.get("qid") or "")
        choices, _multi = self._choice_rows(question)
        picked = state["picks"].get(qid) or set()
        marks = state["choice_marks"].get(qid) or []
        for index, choice in enumerate(choices):
            if index >= len(marks):
                break
            label = " ".join(choice.split())
            self._folds.set_mark_text(
                marks[index], f"{'☑' if index in picked else '☐'} {label}",
                body_format=self._link_format(f"#clarify:{state['gen']}:{qindex}:{index}"),
            )

    def _mark_clarify_answered(self, qindex: int, answer: str) -> None:
        """答完之后这一题不再可点：选中的留 ✓，其余的降成暗字。"""
        state = self._clarify
        if state is None:
            return
        question = state["questions"][qindex]
        qid = str(question.get("qid") or "")
        choices, _multi = self._choice_rows(question)
        marks = state["marks"].get(qid) or []
        choice_marks = state["choice_marks"].get(qid) or []
        shown = {item.strip() for item in answer.replace("[", "").replace("]", "").split('", "')}
        shown = {item.strip('" ') for item in shown}
        if choices:
            for index, choice in enumerate(choices):
                if index >= len(choice_marks):
                    break
                label = " ".join(choice.split())
                picked = label in {" ".join(item.split()) for item in shown}
                self._folds.set_mark_text(
                    choice_marks[index],
                    f"{'✓' if picked else '·'} {label}",
                    body_format=self._answered_format(picked=picked),
                )
        else:
            anchor = marks[-1] if marks else None
            if anchor is not None:
                self._folds.insert_after(
                    state["fold"], anchor, f"✓ {answer}", body_format=self._answered_format(picked=True)
                )

    def _answer_clarify(self, qindex: int, answer: str) -> None:
        state = self._clarify
        if state is None:
            return
        question = state["questions"][qindex]
        qid = str(question.get("qid") or "")
        if qid in state["answers"]:
            return
        state["answers"][qid] = answer
        self._mark_clarify_answered(qindex, answer)
        self.clarify_answered.emit(state["request_id"], qid, answer)
        if len(state["answers"]) >= len(state["questions"]):
            self._settle_clarify("已回答")

    def _settle_clarify(self, label: str) -> None:
        state = self._clarify
        if state is None:
            return
        self._folds.retitle(state["fold"], label)
        self._clarify = None

    def expire_clarify(self, request_id: str = "") -> None:
        """引擎说这次提问已经结束（超时 / 那一轮被打断）：把这卡收掉。

        不收的话 `pending_clarify()` 一直为真——用户下一条消息会被当成这张旧卡的答案
        吃掉：输入框清空、话不进气泡、只飘一句「这个问题已经结束，不用再答了」。
        """
        state = self._clarify
        if state is None:
            return
        if request_id and str(state.get("request_id") or "") != str(request_id):
            return
        self._settle_clarify("已结束")

    def _on_clarify_click(self, rest: str) -> None:
        state = self._clarify
        if state is None:
            return
        gen, _, rest = rest.partition(":")
        if gen != str(state.get("gen")):
            return
        if rest == "skip":
            reply_id = state.get("reply_id")
            self._settle_clarify("跳过了")
            if reply_id is not None:
                self.clarify_skipped.emit(str(reply_id))
            return
        head, _, tail = rest.partition(":")
        try:
            qindex = int(head)
        except ValueError:
            return
        if not 0 <= qindex < len(state["questions"]):
            return
        question = state["questions"][qindex]
        choices, multi = self._choice_rows(question)
        if not choices:
            return
        if multi and tail == "*":
            qid = str(question.get("qid") or "")
            picked = sorted(state["picks"].get(qid) or [])
            if not picked:
                return
            answer = json.dumps([choices[index] for index in picked if index < len(choices)], ensure_ascii=False)
            self._answer_clarify(qindex, answer)
            return
        try:
            index = int(tail)
        except ValueError:
            return
        if not 0 <= index < len(choices):
            return
        if multi:
            qid = str(question.get("qid") or "")
            picks = state["picks"].setdefault(qid, set())
            picks.symmetric_difference_update({index})
            self._redraw_clarify_question(qindex)
            return
        self._answer_clarify(qindex, choices[index])

    def pending_clarify(self) -> bool:
        """她还在等一个回答（输入框里的下一句就当答案，和桌面端一样）。"""
        return self._clarify is not None

    def answer_clarify_text(self, text: str) -> bool:
        """用输入框里这句话回答她：填给第一个还没答的问题。"""
        state = self._clarify
        if state is None or not text.strip():
            return False
        for index, question in enumerate(state["questions"]):
            qid = str(question.get("qid") or "")
            if qid in state["answers"]:
                continue
            self._answer_clarify(index, text.strip())
            return True
        return False

    def show_tool_view(
        self,
        view,
        *,
        tool_id: str = "",
        tool_name: str = "",
        pending: bool = False,
        copy_payload: dict[str, str] | None = None,
        delegate_event: dict | None = None,
    ) -> None:
        """画工具行：标题 / 副标题 / 正文 / 状态 / 计数。

        标题、副标题、计数、正文、stderr、diff 都是模型层算好的（`app/tool_view/model.py`），
        这里只按固定分支顺序摆进聊天窗，不改写一个字段。

        `tool_name` / `pending` 用来判断那两条只跟「哪个工具」「跑完没」有关的分支：
        文件编辑跑完又没有 diff 的，整行都不画；diff 计数只给文件编辑挂。

        `copy_payload` 是模型层 `tool_copy_payload(part, view)` 的结果（正文右上角那个
        复制按钮复制的东西）：`{"label", "text"}`，为真时给这一行的正文挂复制按钮。
        """
        if self._tool_progress == "off":
            return
        self._end_reasoning_group()

        is_file_edit = is_file_edit_tool(tool_name)

        label = view.title
        if view.count_label:
            label = f"{label} · {view.count_label}"
        if is_file_edit and view.inline_diff:
            stats = count_diff_line_stats(view.inline_diff)
            if stats["added"] or stats["removed"]:
                label = f"{label} +{stats['added']} −{stats['removed']}"
        elif view.duration_label and not pending:
            label = f"{label} · {view.duration_label}"
        if view.status == "error":
            label = f"✗ {label}"
        elif view.status == "warning":
            label = f"! {label}"

        if is_file_edit and not pending and view.status != "error" and not view.inline_diff:
            if tool_id:
                self._tool_started.pop(tool_id, None)
                if not self._tool_started:
                    self._tool_clock.stop()
                stuck = self._tool_folds.get(tool_id)
                if stuck is not None:
                    self._folds.retitle(stuck, label)
            return

        glyph = self._tool_glyph(view, pending=pending, file_edit=is_file_edit)
        fold = self._tool_folds.get(tool_id) if tool_id else None
        if fold is None:
            fold = self._folds.new("tool", label, open_=bool(view.inline_diff) or delegate_event is not None, glyph=glyph)
            if tool_id:
                self._tool_folds[tool_id] = fold
        elif label != fold.label or self._glyph_url(glyph) != self._glyph_url(fold.glyph):
            fold.glyph = glyph
            self._folds.retitle(fold, label)

        if tool_id:
            if pending:
                self._track_running_tool(tool_id, fold, label, view.icon)
            else:
                self._tool_started.pop(tool_id, None)
                if not self._tool_started:
                    self._tool_clock.stop()

        if delegate_event is not None:
            self._draw_delegate(fold, tool_id, delegate_event, pending=pending)
            self._settle_insert()
            return

        path = view.subtitle if is_file_edit else None

        if (view.terminal_command is not None or view.terminal_exit_code is not None) and not pending:
            if not self._tool_term_drawn.get(fold.key):
                self._tool_term_drawn[fold.key] = True
                self._folds.append_runs(fold, self._terminal_rows(view))

        if view.image_url and self._tool_images.get(fold.key) != view.image_url:
            self._tool_images[fold.key] = view.image_url
            self._append_image(fold, view.image_url)

        if view.search_hits:
            rows: list[list[tuple[str, QtGui.QTextCharFormat]]] = []
            if view.search_query:
                rows.append([("Search ", self._label_format()), (view.search_query, self._body_char())])
            if rows:
                self._folds.append_runs(fold, rows)
            self._append_search(fold, view.search_hits)

        if copy_payload and copy_payload.get("text"):
            self._tool_payloads[fold.key] = (
                copy_payload.get("label") or tool_tr("common.copy"),
                copy_payload["text"],
            )
            self._register_body(fold.key)

        if view.inline_diff:
            self._diff_texts[fold.key] = (path, view.inline_diff)
            self._register_body(fold.key)
            self._folds.append_runs(fold, self._diff_rows(view.inline_diff, path=path))
            if not fold.pinned and not fold.open:
                self._folds.set_open(fold, True)

        self._append_detail(fold, view)
        self._settle_insert()


    ANSI_COLORS = {
        "black": "#3f4450",
        "red": "#e06c75",
        "green": "#98c379",
        "yellow": "#e5c07b",
        "blue": "#61afef",
        "magenta": "#c678dd",
        "cyan": "#56b6c2",
        "white": "#dcdfe4",
        "bright-black": "#7c8798",
        "bright-red": "#ef7f89",
        "bright-green": "#b0d68f",
        "bright-yellow": "#f0d197",
        "bright-blue": "#7fc2f7",
        "bright-magenta": "#d693e8",
        "bright-cyan": "#6fcbd6",
        "bright-white": "#f2f4f8",
    }
    DIFF_COLORS = {
        "add": ("#55a583", "#2c55a583"),
        "del": ("#e75e78", "#2ce75e78"),
        "hunk": ("#7fb0d8", "#141d28"),
        "meta": ("#7c8798", "#101214"),
        "context": (None, None),
    }

    def _append_text(self, fold, text: str, *, ansi: bool) -> None:
        """正文一段：声明了 `renders_ansi` 的工具（terminal / execute_code）按 ANSI 上色。

        没声明的工具不认颜色，但**不能**把转义符原样铺出来——引擎有时把给终端画好的
        一段（带颜色）塞进普通字段，直接把 `\x1b[38;2;…m` 显示在界面上是最糟的结果。
        """
        if not ansi_codes(text):
            self._folds.append(fold, text, body_format=self._tool_body_format)
            return
        if not ansi:
            self._folds.append(fold, strip_ansi(text), body_format=self._tool_body_format)
            return
        rows: list[list[tuple[str, QtGui.QTextCharFormat]]] = []
        for line in text.split("\n"):
            fmt_line: list[tuple[str, QtGui.QTextCharFormat]] = []
            for chunk, bold, color in parse_ansi(line):
                fmt = QtGui.QTextCharFormat(self._tool_body_format)
                if bold:
                    fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                if color and color in self.ANSI_COLORS:
                    fmt.setForeground(QtGui.QColor(self.ANSI_COLORS[color]))
                fmt_line.append((chunk, fmt))
            rows.append(fmt_line or [("", self._tool_body_format)])
        self._folds.append_runs(fold, rows)

    GLYPH_COLORS = {
        "success": "#7c8798",
        "notice": "#7c8798",
        "running": "#7fb0d8",
        "error": "#e06c75",
        "warning": "#e5c07b",
    }
    LABEL_COLOR = "#8f9bb0"
    ACCENT_COLOR = "#7fb0d8"

    GUTTER_COLOR = "#5c6472"

    def _gutter_runs(
        self, old_no: int | None, new_no: int | None, kind: str
    ) -> list[tuple[str, QtGui.QTextCharFormat]]:
        """大预览左侧的双列行号 + 2px 竖条。

        规则：新增行给新行号，删除行给旧行号，上下文行两列都填。
        """
        fmt = QtGui.QTextCharFormat(self._tool_body_format)
        fmt.setForeground(QtGui.QColor(self.GUTTER_COLOR))
        fmt.setFontPointSize(8)
        text = f"{'' if old_no is None else old_no:>4} {'' if new_no is None else new_no:>4} "
        accent = QtGui.QTextCharFormat(self._tool_body_format)
        accent_color = self.DIFF_COLORS.get(kind, (None, None))[0] or "#3a4150"
        accent.setForeground(QtGui.QColor(accent_color))
        return [(text, fmt), ("▏", accent)]

    def _diff_rows(
        self, diff: str, *, path: str | None = None, numbers: bool = False
    ) -> list[list[tuple[str, QtGui.QTextCharFormat]]]:
        """diff 一段：新增/删除/段落头各一种底色，代码正文过 pygments 上色。

        （shiki 高亮 + 行底色）。`path` 用来挑词法分析器；
        段落头不画，行首那个 `+`/`-`/空格也一并去掉，增删只看行底色。
        `numbers=True` 时左侧补双列行号，
        聊天窗里的收缩版不带行号。
        """
        rows: list[list[tuple[str, QtGui.QTextCharFormat]]] = []
        old_no = new_no = 0
        for kind, line in diff_rows(strip_diff_file_headers(strip_ansi(diff))):
            if kind == "hunk":
                match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    old_no = int(match.group(1)) - 1
                    new_no = int(match.group(2)) - 1
                continue
            if kind == "add":
                new_no += 1
            elif kind == "del":
                old_no += 1
            elif kind == "context":
                old_no += 1
                new_no += 1

            fg, bg = self.DIFF_COLORS.get(kind, (None, None))
            base = QtGui.QTextCharFormat(self._tool_body_format)
            if fg:
                base.setForeground(QtGui.QColor(fg))
            if bg:
                base.setBackground(QtGui.QColor(bg))

            gutter: list[tuple[str, QtGui.QTextCharFormat]] = []
            if numbers:
                known = kind in ("add", "del", "context")
                gutter = self._gutter_runs(
                    old_no if known and kind != "add" else None,
                    new_no if known and kind != "del" else None,
                    kind,
                )

            stripped = line.rstrip()
            if not stripped:
                rows.append([*gutter, (" ", base)])
                continue

            code = stripped[1:] if kind in ("add", "del", "context") else stripped
            if kind in ("add", "del", "context") and code.strip() and numbers:
                highlighted = code_highlight(code, path)[0]
                row = [*gutter]
                for chunk, color, bold in highlighted:
                    fmt = QtGui.QTextCharFormat(base)
                    if color:
                        fmt.setForeground(QtGui.QColor(color))
                    if bold:
                        fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                    row.append((chunk, fmt))
                rows.append(row)
            else:
                rows.append([*gutter, (code or " ", base)])
        return rows


    def _icon_pixmap(self, name: str | None, color: str, px: int = 14) -> QtGui.QPixmap | None:
        """把图标名字渲成小图（同一个图标同一颜色只渲一次）。"""
        dpr = max(1.0, float(self.devicePixelRatioF()))
        key = (name, color, px, dpr)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached

        svg = icon_svg(name, color)
        if not svg:
            name = "tools"
            key = (name, color, px, dpr)
            cached = self._icon_cache.get(key)
            if cached is not None:
                return cached
            svg = icon_svg(name, color)
        if not svg:
            return None

        try:
            from PySide6.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
            pixmap = QtGui.QPixmap(max(1, int(round(px * dpr))), max(1, int(round(px * dpr))))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pixmap)
            renderer.render(painter)
            painter.end()
        except Exception:
            return None

        self._icon_cache[key] = pixmap
        return pixmap

    SPIN_PX = 14
    SPIN_COLOR = "#7c8798"

    def _spin_pixmap(self, index: int) -> QtGui.QPixmap | None:
        """把一帧呼吸点阵画成 14px 小图（与图标同尺寸，抬头行高不变）。"""
        frame = index % len(_BREATHE_FRAMES)
        dpr = max(1.0, float(self.devicePixelRatioF()))
        key = ("spin", frame, dpr)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        size = max(1, int(round(self.SPIN_PX * dpr)))
        pixmap = QtGui.QPixmap(size, size)
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(self.SPIN_COLOR))
        radius = 1.2
        gap = 1.2
        step = radius * 2 + gap
        width = step * 2 - gap
        height = step * 4 - gap * 3
        left = (self.SPIN_PX - width) / 2
        top = (self.SPIN_PX - height) / 2
        for row, col in _BREATHE_FRAMES[frame]:
            painter.drawEllipse(
                QtCore.QRectF(left + col * step, top + row * step, radius * 2, radius * 2)
            )
        painter.end()
        self._icon_cache[key] = pixmap
        return pixmap

    def _spin_format(self, index: int):
        fmt = QtGui.QTextImageFormat()
        pixmap = self._spin_pixmap(index)
        if pixmap is None:
            return None
        frame = index % len(_BREATHE_FRAMES)
        url = QtCore.QUrl(f"noesia-icon://spin-{frame}")
        if url.toString() not in self._icon_registered:
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource, url, pixmap
            )
            self._icon_registered.add(url.toString())
        fmt.setName(url.toString())
        fmt.setWidth(self.SPIN_PX)
        fmt.setHeight(self.SPIN_PX)
        fmt.setVerticalAlignment(QtGui.QTextCharFormat.VerticalAlignment.AlignMiddle)
        return fmt

    def _tick_spin(self) -> None:
        """运行中的行：抬头那颗图标每 100ms 换一帧，跟桌面端的呼吸动画一致。"""
        if not self._tool_started:
            self._spin_timer.stop()
            return
        self._spin_frame += 1
        glyph = self._spin_format(self._spin_frame)
        if glyph is None:
            return
        for fold, _base, _started, _icon in list(self._tool_started.values()):
            self._folds.set_glyph(fold, glyph)

    def _track_running_tool(self, tool_id: str, fold, label: str, icon: str | None = None) -> None:
        """记下这一行的起点；同一行后续再更新时保留原起点，免得每秒都从 0 重来。

        顺手记下这个工具自己的图标名：叫停/断线要收尾时，钟得换成它，不能留在那只蓝钟上。
        """
        started = self._tool_started[tool_id][2] if tool_id in self._tool_started else time.monotonic()
        self._tool_started[tool_id] = (fold, label, started, icon)
        spin = self._spin_format(self._spin_frame)
        if spin is not None and fold is not None:
            self._folds.set_glyph(fold, spin)
        if not self._tool_clock.isActive():
            self._tool_clock.start()
        if not self._spin_timer.isActive():
            self._spin_timer.start()

    def _tick_running_tools(self) -> None:
        """每秒重标还在跑的工具行，让「正在运行 …」后面的时长真的在走。"""
        for fold, base, started, _icon in list(self._tool_started.values()):
            self._folds.retitle(fold, f"{base} · {format_duration_seconds(time.monotonic() - started)}")

    def _settle_running_tools(self, word: str) -> None:
        """把还挂着「进行中」钟的工具行收尾：表要停，图标也不能再是那只蓝钟。

        `word` 是结尾说法：自己按停是「已停止」，引擎断了是「没跑完」。不收尾的话，
        行下面已经写着「已停止」，上面那只钟还在跳秒——看着像它其实还在跑。
        """
        for _tool_id, (fold, base, _started, icon) in list(self._tool_started.items()):
            settled = self._glyph_format(icon or "tools", self.GLYPH_COLORS["notice"])
            if settled is not None:
                fold.glyph = settled
            label = base[2:] if base.startswith("正在") else base
            self._folds.retitle(fold, f"{label} · {word}")
        self._tool_started.clear()
        self._tool_clock.stop()
        self._spin_timer.stop()

    @staticmethod
    def _glyph_url(fmt) -> str:
        """抬头图标的「身份证」：同一个图标同一颜色，URL 一样。换了状态就该换 URL。"""
        return fmt.name() if fmt is not None else ""

    def _glyph_format(self, name: str, color: str):
        """把一个图标名 + 颜色包成抬头能插的图（同一个 URL 只往文档里注册一次）。"""
        pixmap = self._icon_pixmap(name, color)
        if pixmap is None:
            return None

        url = QtCore.QUrl(f"noesia-icon://{name}-{color[1:]}")
        if url.toString() not in self._icon_registered:
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource, url, pixmap
            )
            self._icon_registered.add(url.toString())
        fmt = QtGui.QTextImageFormat()
        fmt.setName(url.toString())
        fmt.setWidth(14)
        fmt.setHeight(14)
        fmt.setVerticalAlignment(QtGui.QTextCharFormat.VerticalAlignment.AlignMiddle)
        return fmt

    def _tool_icon_glyph(self, tool_name: str, *, ok: bool = True):
        """只有工具名、没有 ToolView 时（引擎给的摘要行）按工具取图标。

        以前这类行一片空白/一律白纸：浏览器也是纸、读文件也是纸，看着像所有工具都是「打开了个文件」。
        """
        color = self.GLYPH_COLORS["success"] if ok else self.GLYPH_COLORS["error"]
        return self._glyph_format(tool_meta(tool_name).icon or "tools", color)

    def _tool_glyph(self, view, *, pending: bool, file_edit: bool = False):
        """抬头图标：跑着只是颜色变蓝，图标还是它自己的；出错是红的。

        改文件、打补丁、写文件这三种换成按文件后缀取的图标：改 .yaml 显示齿轮，
        改 .css 显示调色板，认不出的后缀给尖括号。读文件那条不换——它本来就是文件图标。
        """
        if pending:
            spin = self._spin_format(self._spin_frame)
            if spin is not None:
                return spin
        status = "running" if pending else (view.status or "success")
        color = self.GLYPH_COLORS.get(status, self.GLYPH_COLORS["success"])
        name = codicon_for_filename(view.subtitle) if file_edit else None
        return self._glyph_format(name or view.icon or "tools", color)

    def _label_format(self) -> QtGui.QTextCharFormat:
        fmt = QtGui.QTextCharFormat(self._tool_body_format)
        fmt.setForeground(QtGui.QColor(self.LABEL_COLOR))
        fmt.setFontWeight(QtGui.QFont.Weight.DemiBold)
        return fmt

    def _body_char(self) -> QtGui.QTextCharFormat:
        return QtGui.QTextCharFormat(self._tool_body_format)

    def _terminal_rows(self, view) -> list[list[tuple[str, QtGui.QTextCharFormat]]]:
        """终端转录：`$ 命令` 一行，右边跟退出码（0 绿、非 0 琥珀）。"""
        base = self._body_char()
        accent = QtGui.QTextCharFormat(base)
        accent.setForeground(QtGui.QColor(self.ACCENT_COLOR))
        row: list[tuple[str, QtGui.QTextCharFormat]] = []
        if view.terminal_command:
            row.append(("$ ", accent))
            row.append((view.terminal_command, base))
        if view.terminal_exit_code is not None:
            code = int(view.terminal_exit_code)
            badge = QtGui.QTextCharFormat(base)
            badge.setForeground(QtGui.QColor("#98c379" if code == 0 else "#e5c07b"))
            row.append((f"   exit {code}", badge))
        return [row] if row else []

    def _append_image(self, fold, url: str) -> None:
        """图片工具直接贴图，宽度上限 18rem。"""
        path = Path(url)
        if not path.is_file():
            self._folds.append(fold, url, body_format=self._tool_body_format)
            return

        pixmap = QtGui.QPixmap(str(path))
        if pixmap.isNull():
            self._folds.append(fold, url, body_format=self._tool_body_format)
            return

        width = min(288, pixmap.width() or 288)
        height = int(pixmap.height() * width / pixmap.width()) if pixmap.width() else width
        resource = QtCore.QUrl(f"noesia-image://{abs(hash(str(path)))}")
        self.view.document().addResource(
            QtGui.QTextDocument.ResourceType.ImageResource, resource, pixmap
        )
        self._local_photos[resource.toString()] = (path.name, path)
        fmt = QtGui.QTextImageFormat()
        fmt.setName(resource.toString())
        fmt.setWidth(width)
        fmt.setHeight(height)
        self._folds.append_runs(fold, [[("", fmt)]])

    def _append_search(self, fold, hits) -> None:
        """搜索结果：一条一个标题（可点）+ 摘要。"""
        rows: list[list[tuple[str, QtGui.QTextCharFormat]]] = []
        for hit in hits:
            title = (hit.title or "").strip() or hit.url
            link = QtGui.QTextCharFormat(self._tool_body_format)
            link.setForeground(QtGui.QColor(self.ACCENT_COLOR))
            link.setAnchor(True)
            link.setAnchorHref(hit.url or "")
            link.setFontUnderline(False)
            rows.append([(title, link)])
            if hit.snippet:
                snippet = QtGui.QTextCharFormat(self._tool_body_format)
                snippet.setForeground(QtGui.QColor("#8f9bb0"))
                rows.append([(hit.snippet, snippet)])
        if rows:
            self._folds.append_runs(fold, rows)

    def _append_detail(self, fold, view) -> None:
        """详情：错误、stdout/stderr 分节、普通正文三条分支。"""
        stamp = (view.status, view.detail or "", view.stdout or "", view.stderr or "")
        if self._tool_detail_seen.get(fold.key) == stamp:
            return
        self._tool_detail_seen[fold.key] = stamp
        if view.status == "error":
            detail = view.detail or ""
            if not detail:
                return
            lines = detail.split("\n")
            summary = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            red = QtGui.QTextCharFormat(self._tool_body_format)
            red.setForeground(QtGui.QColor("#e06c75"))
            rows: list[list[tuple[str, QtGui.QTextCharFormat]]] = []
            if summary:
                rows.append([(summary, red)])
            if body:
                rows.append([(body, red)])
            self._folds.append_runs(fold, rows)
            return

        if view.stdout or view.stderr:
            if view.detail_label:
                self._folds.append_runs(fold, [[(view.detail_label, self._label_format())]])
            if view.stdout:
                if view.stderr:
                    self._folds.append_runs(fold, [[("stdout", self._label_format())]])
                self._append_text(fold, view.stdout, ansi=bool(view.renders_ansi))
            if view.stderr:
                self._folds.append_runs(fold, [[("stderr", self._label_format())]])
                self._append_text(fold, view.stderr, ansi=bool(view.renders_ansi))
            return

        if not view.detail:
            return
        if looks_redundant(view.title, view.detail) or looks_redundant(view.subtitle, view.detail):
            return
        if view.detail_label:
            self._folds.append_runs(fold, [[(view.detail_label, self._label_format())]])
        self._append_text(fold, view.detail, ansi=bool(view.renders_ansi))

    def _draw_delegate(self, fold, tool_id: str, event: dict, *, pending: bool) -> None:
        """子代理卡片：抬头是「几个子代理 · 到哪一步了」，正文**一行一个子代理**。

        照桌面端 `DelegateTool`：每行是它的目标（加模型/用时），下面一行是它最新在干什么。
        活动行原地换字，不往下追加——追加会让卡片被她的回答切开，而且子代理越活跃卡片越长。
        """
        key = tool_id or fold.key
        args = event.get("args")
        result = event.get("result")
        card = self._delegate_cards.get(key)
        if card is None:
            card = {
                "fold": fold,
                "rows": delegate_view.rows(args, None),
                "marks": {},
                "goals": delegate_view.goals(args),
            }
            self._delegate_cards[key] = card
            self._delegate_latest = key
        if result is not None and not pending:
            card["rows"] = delegate_view.rows(args, result)
        self._delegate_sync(key)
        if result is not None and not pending and delegate_view.is_background(result):
            self._track_running_tool(key, fold, delegate_view.running_title(args))

    def _delegate_sync(self, key: str) -> None:
        """把卡片的每一行刷新成最新：已有的行原地改字，缺的行补在它自己目标行下面。"""
        card = self._delegate_cards.get(key)
        if card is None:
            return
        fold, marks = card["fold"], card["marks"]
        for index, row in enumerate(card["rows"]):
            head, activity = delegate_view.row_lines(row)
            slots = marks.setdefault(index, {})
            if slots.get("head") is None:
                slots["head"] = self._folds.append_marked(fold, head, body_format=self._tool_body_format)
            else:
                self._folds.set_mark_text(slots["head"], head, body_format=self._tool_body_format)
            if not activity:
                continue
            if slots.get("activity") is None:
                slots["activity"] = self._folds.insert_after(
                    fold, slots["head"], activity, body_format=self._tool_body_format
                )
            else:
                self._folds.set_mark_text(slots["activity"], activity, body_format=self._tool_body_format)

    def _delegate_card_for(self, event: dict) -> str:
        """`subagent.*` 事件落到哪张卡上：先按目标文字对，对不上就落到最近那张。"""
        goal = " ".join(str(event.get("goal") or "").split())
        if goal:
            for key, card in self._delegate_cards.items():
                for text in card.get("goals") or []:
                    head = " ".join(text.split())[:24]
                    if head and (goal.startswith(head) or text.startswith(goal[:24])):
                        return key
        return self._delegate_latest

    def update_delegate(self, event: dict) -> None:
        """子代理跑起来之后的实时反馈：把**它那一行**换成最新状态和刚调的工具。"""
        key = self._delegate_card_for(event)
        card = self._delegate_cards.get(key) if key else None
        if card is None:
            return
        card["rows"] = delegate_view.apply_event(card["rows"], event)
        self._delegate_sync(key)
        if not str(event.get("kind") or "").endswith("complete"):
            return
        self._tool_started.pop(key, None)
        if not self._tool_started:
            self._tool_clock.stop()
        if self._delegate_latest == key:
            self._delegate_latest = ""
        failed = any(row.get("state") == "failed" for row in card["rows"])
        self._folds.retitle(
            card["fold"], delegate_view.group_title(len(card["rows"]), "有失败" if failed else "已完成")
        )

    def show_tool(
        self,
        name: str,
        summary: str,
        *,
        ok: bool = True,
        body: str = "",
        tool_id: str = "",
    ) -> None:
        """工具行：一行摘要，点开看引擎给的参数与结果（照桌面端那条脚手架行）。

        摘要用引擎的 `context`，不自己解析参数——工具名和命令原文都在引擎那边定好了；
        结果同样照引擎给的字段原样铺进折叠区，界面不改写。
        成功不补「完成」：工具跑完紧接着就是她的回答，多一行反而吵。
        """
        if self._tool_progress == "off":
            return
        self._end_reasoning_group()
        label = summary or name
        if not ok:
            label = f"✗ {label}"
        glyph = self._tool_icon_glyph(name, ok=ok)
        fold = self._tool_folds.get(tool_id) if tool_id else None
        if fold is None:
            fold = self._folds.new("tool", label, open_=False, glyph=glyph)
            if tool_id:
                self._tool_folds[tool_id] = fold
        elif label != fold.label or self._glyph_url(glyph) != self._glyph_url(fold.glyph):
            fold.glyph = glyph
            self._folds.retitle(fold, label)
        if body:
            self._folds.append(fold, body, body_format=self._tool_body_format)
        self._settle_insert()

    def show_info(self, message: str) -> None:
        """引擎/界面的一条普通消息（保存成功、正在切换…）：灰色一行，不叫「错误」。

        桌面端把这类东西放通知，只有请求失败才是错误行——这里照同一条规矩分开，
        不然「已保存 deepseek」也会顶着 [错误] 出现在对话里。
        """
        self._insert_html(
            f"<span style='color:#8f9bb0;font-size:9pt;'>· {html.escape(message)}</span>"
        )

    def set_show_reasoning(self, show: bool) -> None:
        """`display.show_reasoning`：关掉就不画思考段（桌面端用的是同一把开关）。"""
        self._show_reasoning = bool(show)

    def set_tool_progress(self, mode: str) -> None:
        """`display.tool_progress`：引擎配置成 off 时，工具行一行都不画。"""
        self._tool_progress = (mode or "all").strip().lower() or "all"

    def show_reasoning(self, text: str) -> None:
        """思考增量：攒够一段才落盘，落进一个可折叠的「思考中」段。

        增量是碎片的，一行行落会塞进几十个 span、也会把一段思考拆成几十段；
        攒到 160 字落一次，折叠段里仍然是一段一段的正文。
        """
        if not text or not self._show_reasoning:
            return
        self._reasoning_pending += text
        if len(self._reasoning_pending) >= 160:
            self.flush_reasoning()

    def flush_reasoning(self) -> None:
        """把攒着的思考落进那个折叠段（回答开始、本轮结束、出错时都要先刷一下）。"""
        text, self._reasoning_pending = self._reasoning_pending, ""
        if not text.strip() or not self._show_reasoning:
            return
        if self._reason_fold is None:
            self._reason_fold = self._folds.new("reasoning", _THINKING_LABEL, open_=True)
            self._fit_fold_gap(self._reason_fold)
        self._folds.append(self._reason_fold, text.strip())
        self._settle_insert()

    def show_reasoning_block(self, text: str) -> None:
        """不流式的供应商一次给全的思考（`reasoning.available`）：整块落进同一段。

        桌面端对它的处理是「替换式追加」（`appendReasoningDelta(..., replace=true)`）。
        实测（真引擎）：这一帧是在这一轮**正文之后**、`message.complete` 之前到的，
        payload 就是整轮回答的全文快照。所以回答已经落地时直接丢掉——不然回答底下会
        再冒出一段「已思考」，把那篇回答原样斜体铺第二遍。
        """
        if self._bubble_has_text:
            return
        if not text.strip() or not self._show_reasoning:
            return
        self._reasoning_pending += text
        self.flush_reasoning()

    def _end_reasoning_group(self) -> None:
        """这一段思考结束了（正文/工具行来了，或者本轮收尾）：抬头换成「已思考」并收起。

        实时思考是展开给人看的；回答一开始就把它折成一行——不然每轮回答上面都顶着一大块
        灰字，正文得往下翻才看得见。用户自己点开过的（`pinned`）不动他。
        """
        self.flush_reasoning()
        fold, self._reason_fold = self._reason_fold, None
        if fold is None:
            return
        self._folds.retitle(fold, _THOUGHT_DONE)
        if not fold.pinned:
            self._folds.set_open(fold, False)
            self._folds.retitle(fold, fold.label)

    def show_error(self, message: str) -> None:
        self._end_reasoning_group()
        if not self._streaming:
            self._flush_timer.stop()
            self._assistant_pending = ""
        text = (message or "").strip()
        if not text:
            return
        key = _error_key(text)
        if key == self._last_error_key:
            return
        self._last_error_key = key
        self._insert_html(
            f"<span style='color:#ff9a9a;'>[错误] {html.escape(message)}</span>"
        )

    def set_state(self, value: str) -> None:
        """引擎报的状态只改那行字，**不碰忙态**。

        忙态的唯一依据是「引擎手里有没有一轮」（set_busy：提交、message.start、delta、
        turn.done）。以前这里是 `set_busy(value in {"thinking","speaking"})`，引擎只要发来
        一条它不认识的状态（idle / working / tool / ready…），按钮就从「停止」跳回「发送」，
        那一轮从此叫不停——重启后引擎自己续跑时最容易撞上。
        """
        text = {"thinking": "思考中…", "speaking": "说话中…", "listening": "听着…"}.get(value, "")
        if not text:
            text = self._provider_wait
        self._state_label.setText(text)

    def set_provider_wait(self, text: str) -> None:
        """引擎在等供应商（`thinking.delta` 里那几句）：桌面端放底部状态行，不进正文。"""
        self._provider_wait = (text or "").strip()
        self._state_label.setText(self._provider_wait)

    def set_anchor(self, rect: QtCore.QRect | None) -> None:
        """头像窗挪了/缩了：记下新位置；已经吸在她下面的，就跟着一起走。"""
        self._anchor = QtCore.QRect(rect) if rect is not None else None
        if self._snapped and self._anchor is not None:
            self.move(self._anchor.center().x() - self.width() // 2, self._anchor.bottom() + 1)

    def _is_under_anchor(self) -> bool:
        """现在是不是吸在她下缘（下方一格、横向没隔远）。"""
        anchor = self._anchor
        if anchor is None:
            return False
        geo = self.frameGeometry()
        return (
            abs(geo.top() - (anchor.bottom() + 1)) <= SNAP_DISTANCE
            and abs(geo.center().x() - anchor.center().x()) <= anchor.width()
        )

    def _snap_under_anchor(self) -> None:
        """拖到她下方边缘附近就吸过去：只挪位置、大小一点都不动，并且横向对齐中心线。"""
        anchor = self._anchor
        if anchor is None or not self._is_under_anchor():
            self._snapped = False
            return
        self.move(anchor.center().x() - self.width() // 2, anchor.bottom() + 1)
        self._snapped = True

    def show_near(self, anchor: QtCore.QRect | None = None) -> None:
        """贴着头像窗右侧出现；没有锚点就靠在屏幕右侧中部。"""
        screen = QtGui.QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)
        self._snapped = False
        self.set_anchor(anchor)
        saved = self._saved_pos()
        if saved is not None:
            self.move(saved)
            self.show()
            self.raise_()
            self.activateWindow()
            self.input.setFocus()
            self._settle_snap()
            return
        width, height = self.width(), self.height()
        if anchor is not None:
            x = min(anchor.right() + 12, area.right() - width - 8)
            y = max(area.top() + 8, min(anchor.center().y() - height // 2, area.bottom() - height - 8))
        else:
            x = area.right() - width - 40
            y = area.center().y() - height // 2
        self.move(max(area.left() + 8, x), y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self._settle_snap()

    def _settle_snap(self) -> None:
        """摆好之后统一收尾：够近就精确吸到她下面（和拖拽松手同一条路），够远就不吸。

        以前这里只记标志不归位，重开时会留着上次那个固定偏移，跟拖拽松手的结果不一致。
        """
        if self._is_under_anchor():
            self._snap_under_anchor()
        else:
            self._snapped = False


    def _body_format(self) -> QtGui.QTextCharFormat:
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#e8e8ef"))
        return fmt

    def _insert_code_block(self, code: str, language: str) -> None:
        """代码块：一行一块，底色铺满整行。

        为什么不交给 HTML：Qt 的富文本片段导入对块底色很不稳——往已有内容里追加时第一块丢底色，
        段末的底色又会渗到下一段普通文字上，悬停复制的命中判定跟着失真。按块画就没有这些花样：
        块格式给底色与缩进、字符格式给等宽字体和 pygments 的着色，再打一个 `_CodeMark` 供命中。
        """
        self._para_break = False
        block_format = QtGui.QTextBlockFormat()
        block_format.setBackground(QtGui.QColor(markdown_render.CODE_BACKGROUND))
        block_format.setLeftMargin(12)
        block_format.setRightMargin(12)
        block_format.setTopMargin(0)
        block_format.setBottomMargin(0)
        head_format = QtGui.QTextBlockFormat(block_format)
        head_format.setTopMargin(4)
        base = QtGui.QTextCharFormat()
        base.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        base.setFontPointSize(9)
        base.setForeground(QtGui.QColor("#d7dae5"))

        self._drop_trailing_blank_line()
        cursor = QtGui.QTextCursor(self.view.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if not self.view.document().isEmpty():
            cursor.insertBlock()
        cursor.setCharFormat(base)
        cursor.setBlockFormat(head_format)
        anchor = cursor.position()

        lines = code.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            lines = [""]
        colored = code_highlight(code if len(lines) > 1 else lines[0], language)
        for index in range(len(lines)):
            if index:
                cursor.insertBlock(block_format)
                cursor.setCharFormat(base)
            tokens = colored[index] if index < len(colored) else []
            tokens = [token for token in tokens if token[0]]
            if not tokens:
                cursor.insertText(" ", base)
                continue
            for text, color, bold in tokens:
                fmt = QtGui.QTextCharFormat(base)
                if color:
                    fmt.setForeground(QtGui.QColor(color))
                if bold:
                    fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                cursor.insertText(text, fmt)

        tail_format = cursor.blockFormat()
        tail_format.setBottomMargin(4)
        cursor.setBlockFormat(tail_format)

        block = cursor.block()
        while block.isValid() and block.position() >= anchor:
            block.setUserData(_CodeMark())
            block = block.previous()

        self._settle_insert()

    def _insert_html(self, markup: str) -> None:
        if not markup.strip():
            return
        document = self.view.document()
        cursor = QtGui.QTextCursor(document)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        heading = _HEADING_RE.match(markup)
        if heading:
            self._insert_heading(cursor, int(heading.group(1)), heading.group(2))
            return
        first_new = None
        quote_format = None
        if not document.isEmpty():
            if _starts_own_block(markup):
                self._drop_trailing_blank_line()
                if document.lastBlock().textList() is not None:
                    cursor.insertBlock()
                    cursor.setBlockFormat(_plain_block_format())
                first_new = document.blockCount()
            else:
                cursor.insertBlock()
                cursor.setCharFormat(QtGui.QTextCharFormat())
                quote = markup.lstrip().lower().startswith(_QUOTE_TAG)
                block_format = _plain_block_format()
                if quote:
                    block_format.setLeftMargin(12)
                cursor.setBlockFormat(block_format)
                if quote:
                    quote_format = QtGui.QTextCharFormat()
                    quote_format.setForeground(QtGui.QColor(_QUOTE_COLOR))
        self._para_break = False
        start = cursor.position()
        cursor.insertHtml(self._adopt_images(markup))
        if quote_format is not None:
            _paint_range(cursor, start, cursor.position(), quote_format)
        if first_new is not None:
            block = document.findBlockByNumber(first_new)
            while block.isValid():
                if block.textList() is None:
                    QtGui.QTextCursor(block).setBlockFormat(_plain_block_format())
                    self._relax_image_line(block)
                block = block.next()
        self._settle_insert()

    def _insert_heading(self, cursor: QtGui.QTextCursor, level: int, inner: str) -> None:
        """标题：自己开段、自己定字号。

        Qt 的 `insertHtml` 只在**文档第一段**才认 `<h1>`-`<h6>`；别处一律并进当前段，
        headingLevel 也不保留（实测 blocks 不变、lvl=0），标题就成了接在上一句末尾的普通文字。
        所以级别得画在字符格式上：字号按级别给，粗体，段前后各留一点空。
        """
        document = self.view.document()
        if not document.isEmpty():
            self._drop_trailing_blank_line()
            cursor.insertBlock()
        block_format = QtGui.QTextBlockFormat()
        block_format.setTopMargin(MSG_GAP if level <= 2 else MSG_GAP // 2)
        block_format.setBottomMargin(MSG_GAP // 4)
        cursor.setBlockFormat(block_format)
        font = QtGui.QFont(self.view.font())
        font.setPixelSize(_HEADING_PX[level])
        font.setBold(True)
        char_format = QtGui.QTextCharFormat()
        char_format.setFont(font)
        cursor.setCharFormat(char_format)
        self._para_break = False
        start = cursor.position()
        cursor.insertHtml(self._adopt_images(inner))
        _paint_range(cursor, start, cursor.position(), char_format)
        self._settle_insert()

    def _scroll_to_end(self) -> None:
        """滚到最底。

        直接设 `maximum()` 有个坑：刚插进去的字还没排版，拿到的最大滚动量是旧的，
        滚不到新长出来那截（表现就是「卡片展开了但还差一口没到底」）。所以先逼一次排版。

        这个过程会发 `valueChanged`，那是我自己滚的，不是用户往上翻——用 `_self_scroll`
        挡一下，否则每滚一次就把「跟随」状态误判成「用户离开了底部」。
        """
        self.view.document().documentLayout().documentSize()
        bar = self.view.verticalScrollBar()
        self._self_scroll = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._self_scroll = False

    def _at_end(self) -> bool:
        """到底没有。

        留 4px 余量不是随手拍的：Qt 的滚动范围是整数，文档高度常带小数
        （行高 17 加半像素这种），取整后最后一两像素滚不到。按 1px 判就永远
        判不到底，症状是胶囊不消失、新内容不跟随——桌面端用 0.5px 是因为
        浏览器里滚动值是精确的浮点值，这边不能照搬那个数。

        文末动作条那截也算「到底」：按钮是这条消息的一部分（桌面端就占在气泡里），
        文字见底就算到这条消息的底。不然文字一见底就被判「没到底」，动作条永远压在
        折叠线下，要人再手动往下滚才露出来。
        """
        bar = self.view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4 - self._trailing_bar_px()

    def _trailing_bar_px(self) -> int:
        """文末动作条占的那截高（没有就是 0）。见 `_at_end`。

        块的外接矩形不含段前段后距，要自己加上——动作条垫高主要就是那段后距。
        """
        block = self.view.document().lastBlock()
        if not isinstance(block.userData(), _BarMark):
            return 0
        fmt = block.blockFormat()
        height = self.view.document().documentLayout().blockBoundingRect(block).height()
        return int(height + fmt.topMargin() + fmt.bottomMargin())

    def _text_selected(self) -> bool:
        """正文里有选中的字，且焦点还在正文上：这时候不许替人滚。

        桌面端 `hasTranscriptTextSelection` 管的就是这个：选着字的时候视口不许动。
        多判一个焦点——焦点跑到输入框里以后，正文那条选中的高亮早就灰了，
        再按它算就会一直不跟随，看着像「底部不锁」。
        """
        return self.view.hasFocus() and self.view.textCursor().hasSelection()

    def _scroll_to_end_later(self) -> None:
        """等这一轮事件处理完（排版落了）再滚——插完字当拍滚不到底。"""
        QtCore.QTimer.singleShot(0, self._scroll_to_end)

    def _settle_insert(self) -> None:
        """插完正文收尾：原本跟着新内容的继续跟，翻着看的一动不动。

        「不动」靠的是压根没碰视图光标——`setTextCursor()` 会让 Qt 在排版那一拍把视图滚到
        光标处，当拍再 `setValue()` 还回去也没用，下一拍照样被拽下去。所以插入一律用独立
        光标（`QTextCursor(document)`），视图光标始终停在用户自己滚到的位置。

        判跟不跟随看 `_follow` 这个状态位，不看此刻的滚动条位置：流式一快，位置永远落后于
        最大值，按位置判就一块都跟不上。
        """
        if self._restoring:
            return
        if self._text_selected():
            return
        if self._follow:
            self._scroll_to_end_later()
            self._hide_jump()
        else:
            self._show_jump()


    def _jump_pill(self) -> _JumpPill:
        """那颗胶囊——第一次要用才建，之后一直复用。"""
        if self._jump is None:
            pill = _JumpPill(self.view.viewport())
            pill.clicked.connect(self._jump_to_end)
            self._jump = pill
        return self._jump

    def _place_jump(self) -> None:
        """贴着正文底边居中摆。

        每次都按当下的视口算，不留旧坐标：窗口一变、正文一长，按旧高度摆出来的位置就悬在
        半空里了。桌面端那颗是 `bottom: calc(...)` 锚在底边的，怎么变都贴底，这里等价地重算。
        每次滚动也要重摆：Qt 滚一下会把视口里的子件跟着内容拖走（消息条就是每 tick 重摆的），
        不重摆就跟着滚动条上下跑，不在底边。
        """
        if self._jump is None:
            return
        area = self.view.viewport().rect()
        self._jump.move(
            (area.width() - self._jump.width()) // 2,
            area.height() - self._jump.height() - 12,
        )

    def _show_jump(self) -> None:
        """底下还有没看完的：把胶囊摆出来。"""
        if self.view.verticalScrollBar().maximum() <= 0:
            self._hide_jump()
            return
        pill = self._jump_pill()
        self._place_jump()
        if pill.isVisible():
            return
        pill.raise_()
        pill.show()

    def _hide_jump(self) -> None:
        if self._jump is not None and self._jump.isVisible():
            self._jump.hide()

    def _jump_to_end(self) -> None:
        """点了就回最新那一行，并重新跟随新内容。"""
        self._follow = True
        self._scroll_to_end_later()
        self._hide_jump()

    def _user_scroll_now(self, *_args) -> None:
        """这一次滚动是用户自己动的（滚轮 / 拖条 / 点箭头 / 方向键）：标上，这一拍过完就撤。"""
        self._user_scroll = True
        QtCore.QTimer.singleShot(0, self._end_user_scroll)

    def _end_user_scroll(self) -> None:
        self._user_scroll = False

    def _resize_started(self) -> None:
        """窗口在拉伸：这一拍里滚动值怎么变都是 Qt 按新视口挪的，消息流不许跟着动。"""
        self._resizing = True
        QtCore.QTimer.singleShot(0, self._end_resize)

    def _end_resize(self) -> None:
        self._resizing = False

    def _range_changed(self, _minimum: int, _maximum: int) -> None:
        """滚动范围变了（排版长高、窗口拉伸）：按钮这些按新位置再摆一遍。"""
        QtCore.QTimer.singleShot(0, self._place_msg_bars)
        QtCore.QTimer.singleShot(0, self._place_sticky)

    def _set_band(self, height: int) -> None:
        """顶上给黏顶那条让位：只改视口上边距，滚动值同步加同样的量。

        视口矮多少、滚动范围就大多少，值跟着加这么多，屏上的字一个像素都不动：
        等于桌面端把被盖住的内容裁掉，而不是把整条正文往下推。改边距会在同一拍
        触发 rangeChanged / valueChanged：这里裹着 `_self_scroll` 挡住跟随判定，
        重算侧另有 `_sticky_busy` 守卫，嵌套只记脏、值由外层这一次补回去。
        """
        was = self.view.viewportMargins().top()
        height = max(0, int(height))
        if height == was:
            return
        bar = self.view.verticalScrollBar()
        value = bar.value()
        outer = self._self_scroll
        self._self_scroll = True
        try:
            self.view.setViewportMargins(0, height, 0, 0)
            if not self._band_freeze:
                bar.setValue(value + height - was)
        finally:
            self._self_scroll = outer
        strip = max(was, height) + 2
        self.update(
            QtCore.QRect(
                self.view.mapTo(self, QtCore.QPoint(0, 0)),
                QtCore.QSize(self.view.width(), strip),
            )
        )

    def _sticky_px(self) -> int:
        """顶上那条黏顶带有多高：文档边距 + 一行提示词的气泡高 + 消息间距。

        只用眼前这条用户行自己的排版高度算，跟具体哪一条提示词无关，所以钉住期间
        这条带永远是同一个高度。
        """
        stick = int(round(self.view.document().documentMargin()))
        line = 0
        for msg in reversed(self._msgs):
            head = msg.get("head_block")
            if msg.get("role") == "user" and head is not None and head.isValid():
                line = int(round(self.view.document().documentLayout().blockBoundingRect(head).height()))
                break
        if line <= 0:
            line = int(round(QtGui.QFontMetricsF(self._user_text_font()).height()))
        pad = self.FRAME_PAD + _StickyPrompt.EDGE_PX
        return stick + line + 2 * pad + MSG_GAP

    def _sticky_target(self) -> tuple[dict | None, float]:
        """该钉哪一条：`(这一条, 这一轮底边在视口里的位置)`，没有就是 `(None, 0.0)`。

        判据照桌面端 `use-sticky-prompt-clip` 抄：气泡顶翻过 `视口顶 + stickyTop + 1px`
        这条线、且这一轮还没翻出窗口顶（`promptRect.bottom > viewportTop`）才候选，
        文档序靠后那条接手（activePrompt 取 FOLLOWING）。桌面端的提示词是 sticky 的，
        钉住后 rect.top 恒等于钉线、rect.bottom 跟着这一轮的底边走；这边段框不能真
        sticky，就用「框外沿顶 ≤ 钉线」加「这一轮底边 > 0」两件事等价地判，从新到旧
        走到头一个候选即止。
        """
        layout = self.view.document().documentLayout()
        offset = float(self.view.verticalScrollBar().value())
        margin_top = float(self.view.viewportMargins().top())
        stick = float(int(round(self.view.document().documentMargin())))
        pin_line = stick + 1.0
        later = float(self.view.document().size().height())
        for msg in reversed(self._msgs):
            if msg.get("role") != "user":
                continue
            head = msg.get("head_block")
            frame = msg.get("frame")
            if head is None or not head.isValid() or frame is None:
                continue
            fmt = frame.frameFormat()
            border_top = layout.frameBoundingRect(frame).top() + float(fmt.topMargin())
            turn_end = later - offset + margin_top
            if border_top - offset + margin_top <= pin_line and turn_end > 0:
                return msg, turn_end
            later = border_top
        return None, 0.0

    def _place_sticky(self) -> None:
        """顶上那条：重算一遍钉没钉住（带子高度固定，钉住时只换显示哪一条）。

        滚动、范围、拉伸都会走到这里，且必须同步走（拉伸时 Qt 在 setGeometry 里
        同步把滚动值夹到新范围，排到 singleShot 就是「旧黏顶 + 新滚动值」那一帧）。
        重算里自己改滚动值引发的嵌套调用只记脏，值由外层这一次补回去。
        """
        if self._sticky_busy:
            self._sticky_dirty = True
            return
        self._sticky_busy = True
        try:
            for _ in range(4):
                self._sticky_dirty = False
                self._place_sticky_once()
                if not self._sticky_dirty:
                    break
        finally:
            self._sticky_busy = False

    def _place_sticky_once(self) -> None:
        px = self._sticky_px()
        target = None
        bottom = 0.0
        if self.view.height() - px >= 80:
            target, bottom = self._sticky_target()
        strip = max(0, min(px, int(bottom))) if target is not None else 0
        self._set_band(strip)
        if target is None:
            if self._sticky is not None:
                self._sticky.hide()
            return
        if self._sticky is None:
            self._sticky = _StickyPrompt(
                self.view, self.FRAME_EDGE, self._user_name_font(), self._user_text_font()
            )
        name = _user_display_name()
        label = f"{name} "
        room = self._user_line_room(QtGui.QFontMetricsF(self._user_name_font()).horizontalAdvance(label))
        shown = QtGui.QFontMetricsF(self._user_text_font()).elidedText(
            str(target.get("text") or ""), QtCore.Qt.TextElideMode.ElideRight, room
        )
        self._sticky.set_line(name, shown)
        margin = self.view.document().documentMargin()
        frame_h = px - int(round(margin)) - MSG_GAP
        top = min(margin, strip - MSG_GAP - frame_h)
        self._sticky.set_layout(margin, MSG_GAP, top)
        geo = self.view.viewport().geometry()
        self._sticky.setGeometry(geo.left(), 0, geo.width(), strip)
        self._sticky.show()
        self._sticky.raise_()

    def _on_scrolled(self, _value: int) -> None:
        """滚动条动了：到底就锁回来，用户翻走才松手，排版自己挪的不算数。"""
        self._hide_hover()
        self._place_sticky()
        self._place_jump()
        if self._self_scroll:
            return
        if self._resizing:
            return
        if self._at_end():
            # 桌面端（use-stick-to-bottom）的语义是「滚回底部就重新钉住」，钉住就是钉到
            # 真底：动作条那截要跟着露出来，不能钉在文字见底、按钮还压在折叠线下的位置。
            self._follow = True
            self._scroll_to_end_later()
            self._hide_jump()
            return
        if not self._user_scroll:
            # 值被 Qt 挪的（排版长高、窗口拉伸、范围回填）：跟着的人继续跟着，
            # 不许因为这种挪动就脱锁——桌面端判定脱锁只看用户自己的滚动。
            if self._follow and not self._restoring:
                self._scroll_to_end_later()
            return
        self._follow = False
        self._show_jump()


    def set_voice_mode(self, mode: str) -> None:
        """切档并落盘：静音写 enabled=false，另两档写 enabled=true + mode。"""
        if mode not in [value for value, _label in _VOICE_MODES]:
            mode = "plain"
        self._paint_voice_state(mode)
        try:
            from app.speech_prefs import save as save_voice

            if mode == "muted":
                save_voice(enabled=False)
            else:
                save_voice(enabled=True, mode=mode)
            if mode != "interactive":
                self.clear_status_notice("avatar")
            self.clear_status_notice("speech")
        except Exception as exc:
            self._paint_voice_state(_voice_mode())
            self.show_error(f"语音档位没存住：{exc}")
            return
        self.voice_mode_changed.emit(mode)

    def refresh_voice_state(self) -> None:
        """按磁盘上的状态重画图标（启动时、设置里改过之后），不写盘。"""
        self._paint_voice_state(_voice_mode())

    def _paint_voice_state(self, mode: str) -> None:
        self._voice_button.setIcon(_glyph_icon("voice", _VOICE_TINT.get(mode, "#6b6b76")))
        label = next(text for value, text in _VOICE_MODES if value == mode)
        self._voice_button.setToolTip(f"语音：{label}（点开换档：静音 / 纯语音 / 语音互动）")
        for action in self._voice_menu.actions():
            action.setChecked(action.data() == mode)

    def set_approval_mode(self, mode: str) -> None:
        """显示引擎真正在用的档位（图标着色 + tooltip + 菜单勾选）。读不到就保持灰色盾牌。"""
        mode = (mode or "").strip().lower()
        if not mode:
            return
        label = next((text.split(" · ")[0] for value, text in _APPROVAL_MODES if value == mode), mode)
        self._approval_button.setToolTip(f"审批模式：{label}（点开可换，改了立即生效）")
        self._approval_button.setIcon(_glyph_icon("shield", APPROVAL_TINT.get(mode, "#e8e8ef")))
        for action in self._approval_menu.actions():
            action.setChecked(action.data() == mode)

    def set_reasoning_state(self, state: dict) -> None:
        """按引擎回读的当前档位重建菜单：`{current, can_disable}`。

        档位清单照桌面端写死（`REASONING_EFFORTS`，7 档 + 可关时多一项「关闭思考」）：
        引擎没有「这个模型有哪几档」这种接口，`config.get reasoning` 只回当前值，
        自己去问档位就会问出个空清单（然后整个菜单变成一句「没有可调的思考档」）。
        """
        state = state or {}
        current = str(state.get("current") or "").strip().lower()
        can_disable = bool(state.get("can_disable", False))
        pending = bool(state.get("pending", False))
        wire = str(state.get("wire") or "").strip().lower()
        self._reasoning_menu.clear()
        group = self._reasoning_group

        def add(value: str, text: str) -> None:
            action = self._reasoning_menu.addAction(text)
            action.setCheckable(True)
            action.setActionGroup(group)
            action.setData(value)
            action.setChecked(not pending and value == current)
            action.triggered.connect(
                lambda _checked=False, chosen=value: self.reasoning_requested.emit(chosen)
            )

        names = dict(_REASONING_LEVELS)
        names[_REASONING_OFF[0]] = _REASONING_OFF[1]

        if can_disable:
            add(*_REASONING_OFF)
            self._reasoning_menu.addSeparator()
        for value, text in _REASONING_LEVELS:
            add(value, text)
        label = next((text for value, text in _REASONING_LEVELS if value == current), "")
        if current == _REASONING_OFF[0]:
            label = _REASONING_OFF[1]
        self._reasoning_button.setEnabled(True)
        if pending:
            self._reasoning_button.setToolTip("推理强度：等会话回报当前档（发一句话后显示）")
        elif wire and wire != current:
            self._reasoning_button.setToolTip(f"推理强度：{label}（这条路实际按「{names.get(wire, wire)}」发送）")
        else:
            self._reasoning_button.setToolTip(f"推理强度：{label or '引擎默认'}")


    def speech_preparing(self) -> None:
        self.speech_bar.show_preparing()

    def speech_playing(self) -> None:
        self.speech_bar.show_playing()

    def speech_progress(self, position_ms: int, duration_ms: int) -> None:
        self.speech_bar.set_progress(position_ms, duration_ms)

    def speech_done(self) -> None:
        self.speech_bar.dismiss()

    def stop_streaming(self) -> None:
        """叫住这一轮：手上这段先收尾（半条回答留着），之后飘回来的字丢掉，末尾留一个「已停止」。"""
        if not (self._busy or self._streaming or self._assistant_pending or self._tool_started):
            return
        already_stopped = self._interrupted
        if self._streaming:
            self.end_assistant()
        self._interrupted = True
        self._clarify = None
        self._settle_running_tools("已停止")
        if already_stopped:
            return
        self._insert_html(
            "<span style='color:rgba(255,255,255,120); font-size:11px;'>已停止</span>"
        )

    def append_review_summary(self, text: str) -> None:
        """后台自我回顾（记忆/技能被改了）的总结：一条常驻灰字，落在对话里。

        不是弹一下就没的提示——错过了就等于这次自我改进没发生过。但长会话里回顾可能几十上百条，
        逐条进文档会把 `QTextDocument` 和内存一起喂大：留够看的量，之后只说一句「后面省略了」。
        """
        text = (text or "").strip()
        if not text:
            return
        self._review_count += 1
        if self._review_count > MAX_REVIEW_SUMMARIES:
            if not self._review_folded:
                self._review_folded = True
                self._insert_html(
                    "<div style='color:rgba(255,255,255,110); font-size:11px; margin-top:6px;'>"
                    f"💾 回顾总结已经贴满 {MAX_REVIEW_SUMMARIES} 条，后面的不再逐条贴在这里</div>"
                )
            return
        if self._streaming:
            self._review_pending.append(text)
            return
        self._insert_html(
            "<div style='color:rgba(255,255,255,135); font-size:11px; margin-top:6px;'>"
            f"💾 {html.escape(text)}</div>"
        )

    def abandon_turn(self, reason: str = "引擎断开") -> None:
        """引擎没了：还在计时的工具行当场停表、标成没跑完，末尾留一行灰字。

        不这么做的话界面看着像还在跑——工具行后面秒数一直跳、按钮还是「发送」，
        其实那个引擎进程已经没了，一个字都不会再来（2026-10-26 就是这样）。
        """
        running = bool(self._tool_started)
        self._settle_running_tools("没跑完")
        if not (running or self._streaming or self._assistant_pending or self._busy):
            return
        if self._interrupted:
            return
        self._interrupted = True
        self._clarify = None
        if self._streaming:
            self.end_assistant()
        self.set_provider_wait("")
        self._insert_html(
            f"<span style='color:rgba(255,255,255,120); font-size:11px;'>{html.escape(reason)} · 这一轮没跑完</span>"
        )

    def set_busy(self, busy: bool) -> None:
        """一轮在跑：发送键换成「停止」（框里有字就换成「排队」）；停的是引擎那一轮，说过的话仍在上下文里。

        这里**不放开** `_interrupted`：让按钮回「发送」只是为了你能接着打字，
        那一刻引擎手里上一轮的话还可能在路上。放闸是 `settle_turn()` 的事。
        """
        self._busy = bool(busy)
        self._sync_send_button()
        if not self._busy:
            self.set_activity("")
            self.set_provider_wait("")

    def open_turn_gate(self) -> None:
        """引擎明确开了一轮新回答（`message.start`）：上一轮按停/断线留下的闸到此为止。

        闸是给「按停之后还飘在半路的残片」用的。引擎都亲口说开新一轮了，残片不可能再来；
        再拦着，这一整轮回答会被原地吞掉——气泡停在用户那句，「思考中…」转完就没有下文，
        要等到下一轮才恢复（重连之后就是这样）。
        """
        self._interrupted = False

    def settle_turn(self) -> None:
        """这一轮彻底完了（引擎的 turn.done）：可以重新收字了，排在后面的那句也该发了。

        被中断的轮次走不到 message.complete，只有这一帧能划出「残片到这儿为止」的界线。
        """
        self._interrupted = False
        pending, self._rewind_after_stop = self._rewind_after_stop, None
        if pending is not None:
            self._emit_rewind(pending[0], pending[1], pending[2])
            return
        self._drain_queue()

    def _send_label(self) -> str:
        """发送键上写什么——按「此刻按下它会做什么」定，跟桌面端一致。

        框里有字：发送（忙的时候是交给引擎排队，这轮跑完自己接上）；
        框里空着、又在跑：停止。
        """
        typing = bool(self.input.toPlainText().strip() or self._attachments)
        if self._busy:
            if self._rewind_waiting:
                return "停止"
            return "排队" if typing else "停止"
        if self._rewind_waiting:
            return "重发中"
        return "发送"

    def _sync_send_button(self) -> None:

        if not hasattr(self, "_send_button"):
            return
        label = self._send_label()
        if label == "排队":
            self._send_button.setIcon(QtGui.QIcon())
            self._send_button.setText("排队")
            self._send_button.setFixedWidth(52)
        else:
            self._send_button.setIcon(
                svg_icon("stop" if label == "停止" else "arrow-up", "#2b1520", 15)
            )
            self._send_button.setText("")
            self._send_button.setFixedWidth(32)
        self._send_button.setToolTip(
            "这一轮还在跑：点了先排队，它跑完接着发" if label == "排队"
            else "叫住这一轮（已进行的对话仍留在上下文里）" if label == "停止"
            else "发送"
        )


    def _enqueue(self, text: str, attachments: list[dict], from_input: bool = True,
                 source: str = "typed") -> None:
        """这一轮还在跑：这句话先排队，等它完了再发出去。

        正文里不提前出现你要说的话——和桌面端一样，「排着队」和「已经交给引擎」是两回事。
        前面那条要是被你按停了，后面这条就成了「说了却没发生」的假记录。

        `from_input=False` 是语音那条路：语音模式下输入框藏着，里面可能压着草稿，
        清掉等于替人扔了东西，所以不许动它。
        """
        entry = {"text": text, "attachments": attachments, "source": source}
        chip = QtWidgets.QFrame(self._queue_bar)
        chip.setObjectName("attachchip")
        chip.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Preferred
        )
        line = QtWidgets.QHBoxLayout(chip)
        line.setContentsMargins(6, 2, 2, 2)
        line.setSpacing(6)
        mark = QtWidgets.QLabel("排队中", chip)
        line.addWidget(mark)
        shown = text or f"{len(attachments)} 个附件"
        preview = QtWidgets.QLabel(shown if len(shown) <= 40 else shown[:40] + "…", chip)
        preview.setToolTip(text or "、".join(str(held.get("name") or "") for held in attachments))
        line.addWidget(preview)
        close = QtWidgets.QToolButton(chip)
        close.setObjectName("icon")
        close.setText("×")
        close.setFixedSize(16, 16)
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.setToolTip("不发了")
        close.clicked.connect(lambda _checked=False, target=entry: self._drop_queued(target))
        line.addWidget(close)
        self._queue_col.addWidget(chip)
        self._queued.append(entry)
        self._queue_chips.append(chip)
        self._queue_bar.show()
        if from_input:
            self.input.clear()
            self._clear_attachments()
        self._sync_send_button()

    def _drop_queued(self, entry: dict) -> None:
        """撤掉一条还没轮到的。按对象认，不按文字认：同一句话排两次是两条。"""
        index = next((i for i, held in enumerate(self._queued) if held is entry), None)
        if index is None:
            return
        self._queued.pop(index)
        chip = self._queue_chips.pop(index)
        chip.setParent(None)
        chip.deleteLater()
        if not self._queued:
            self._queue_bar.hide()

    def _clear_queue(self) -> None:
        if not hasattr(self, "_queue_chips"):
            return
        for chip in self._queue_chips:
            chip.setParent(None)
            chip.deleteLater()
        self._queue_chips.clear()
        self._queued.clear()
        self._queue_bar.hide()

    def _drain_queue(self) -> None:
        """这一轮真的完了：把排在最前面的那条交给引擎——到这一刻它才进正文。"""
        if self._busy or not self._queued:
            return
        entry = self._queued.pop(0)
        chip = self._queue_chips.pop(0)
        chip.setParent(None)
        chip.deleteLater()
        if not self._queued:
            self._queue_bar.hide()
        text = str(entry.get("text") or "")
        attachments = list(entry.get("attachments") or [])
        self.append_user(text, attachments)
        if attachments:
            self.attachment_picked.emit(attachments)
        self.submitted.emit(text, str(entry.get("source") or "typed"))


    def _show_attach_menu(self) -> None:
        """「＋」弹菜单：位置照桌面端——贴着按钮往上弹，别盖住输入框。"""
        menu = self._attach_menu
        menu.adjustSize()
        anchor = self._attach_button.mapToGlobal(QtCore.QPoint(0, 0))
        menu.exec(QtCore.QPoint(anchor.x() - 4, anchor.y() - menu.sizeHint().height() - 6))

    def _pick_files(self) -> None:
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(self, "选文件", "", "所有文件 (*.*)")
        self._attach_paths([Path(raw) for raw in paths])

    def _pick_images(self) -> None:
        pattern = " ".join(f"*{suffix}" for suffix in sorted(IMAGE_SUFFIXES))
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(self, "选图片", "", f"图片 ({pattern})")
        self._attach_paths([Path(raw) for raw in paths])

    def _pick_folders(self) -> None:
        """文件夹没有 attach 接口：引擎认 `@folder:` 引用，直接插进输入框。"""
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选文件夹")
        if folder:
            self.insert_text(f"@folder:{folder}")

    def _add_url(self) -> None:
        dialog = self._url_dialog()
        accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        url = dialog.textValue().strip()
        dialog.deleteLater()
        if not accepted:
            return
        if url:
            self.insert_text(f"@url:{url}")

    def _url_dialog(self) -> QtWidgets.QInputDialog:
        """输入链接的小框。人物窗本身是置顶无边框窗，对话框不带同一个标志就会被压在下面点不着。"""
        dialog = QtWidgets.QInputDialog(self.window())
        dialog.setWindowTitle("加链接")
        dialog.setLabelText("链接（http/https）：")
        dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        return dialog

    def _paste_image(self) -> None:
        """「粘贴图片」：图当场读进内存放在待发条上，不落盘、也不交给引擎（取消就当没发生）。"""
        if not self._attach_from_clipboard():
            self.show_error("剪贴板里没有图片")

    def _show_snippets(self) -> None:
        dialog = self._snippets_dialog()
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _snippets_dialog(self) -> QtWidgets.QDialog:
        dialog = QtWidgets.QDialog(self.window(), QtCore.Qt.WindowType.Window)
        dialog.setWindowTitle("提示词片段")
        dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        outer = QtWidgets.QVBoxLayout(dialog)
        outer.addWidget(QtWidgets.QLabel("选择一个起始提示词放入输入框。", dialog))
        for label, description, body in PROMPT_SNIPPETS:
            button = QtWidgets.QPushButton(f"{label}\n{description}", dialog)
            button.setObjectName("snippet")
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(46)
            button.clicked.connect(
                lambda _checked=False, text=body, window=dialog: (self.insert_text(text), window.accept())
            )
            outer.addWidget(button)
        return dialog

    def insert_text(self, text: str) -> None:
        """往输入框里补一段（@folder: / @url: / 提示词片段都用它）。

        插入前看住两端：草稿末尾正贴着字的话先补一个换行，`@folder:` 粘在上一句尾巴上
        就成了「帮我看看@folder:…」，引擎按整串找引用找不到，用户看着还以为是引擎不认。
        """
        self.input.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        body = self.input.toPlainText()
        if body and not body[-1].isspace():
            text = "\n" + text
        self.input.insertPlainText(text if text.endswith(("\n", " ")) else text + " ")
        self.input.setFocus()

    def _attach_paths(self, paths) -> bool:
        for path in paths:
            self._add_attachment(Path(path))
        return bool(paths)

    def _add_attachment(self, path: Path) -> None:
        """按扩展名分两路：图片走 `image.attach`，其它走 `file.attach`（都只交路径）。"""
        if not path.is_file():
            self.show_error(f"找不到这个文件：{path.name}")
            return
        kind = "image" if path.suffix.lower() in IMAGE_SUFFIXES else "file"
        self._push_attachment({"kind": kind, "name": path.name, "path": str(path)})

    def _push_attachment(self, item: dict) -> None:
        """加一条待发送附件：立刻画出来，也立刻让引擎开始收（发送只发文字）。"""
        chip = QtWidgets.QFrame(self._attach_bar)
        chip.setObjectName("attachchip")
        chip.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Preferred
        )
        line = QtWidgets.QHBoxLayout(chip)
        line.setContentsMargins(6, 2, 2, 2)
        line.setSpacing(6)
        thumb = QtWidgets.QLabel(chip)
        pixmap = self._attachment_pixmap(item)
        if pixmap is not None:
            thumb.setPixmap(pixmap.scaled(26, 26, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
        else:
            thumb.setText("📄")
        line.addWidget(thumb)
        name = QtWidgets.QLabel(item["name"], chip)
        name.setToolTip(item.get("path") or item["name"])
        line.addWidget(name)
        close = QtWidgets.QToolButton(chip)
        close.setObjectName("icon")
        close.setText("×")
        close.setFixedSize(16, 16)
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.setToolTip("不发了")
        close.clicked.connect(lambda _checked=False, target=item: self._drop_attachment(target))
        line.addWidget(close)
        self._attach_row.insertWidget(self._attach_row.count() - 1, chip)
        self._attachments.append(item)
        self._attach_chips.append(chip)
        self._attach_bar.show()
        self._sync_send_button()

    def _drop_attachment(self, item: dict) -> None:
        """撤掉一条（还没发出去）。按对象认，别按文件名认——同名两张图是两回事。"""
        index = next((i for i, held in enumerate(self._attachments) if held is item), None)
        if index is None:
            return
        self._attachments.pop(index)
        chip = self._attach_chips.pop(index)
        chip.setParent(None)
        chip.deleteLater()
        if not self._attachments:
            self._attach_bar.hide()
        self._sync_send_button()

    def drop_attachment_by_name(self, name: str) -> None:
        """引擎说这个附件没收下：从条上撤掉，别让人以为发出去了。"""
        item = next((held for held in self._attachments if held["name"] == name), None)
        if item is not None:
            self._drop_attachment(item)

    def _clear_attachments(self) -> None:
        for chip in self._attach_chips:
            chip.setParent(None)
            chip.deleteLater()
        self._attach_chips.clear()
        self._attachments.clear()
        self._attach_bar.hide()
        self._sync_send_button()

    @staticmethod
    def _attachment_pixmap(item: dict):
        """图片给 QPixmap，非图片/读不出来给 None（那就只显示文件名）。"""
        if item.get("kind") != "image":
            return None
        if item.get("data"):
            pixmap = QtGui.QPixmap()
            return pixmap if pixmap.loadFromData(item["data"]) else None
        path = item.get("path") or ""
        if not path or not Path(path).is_file():
            return None
        pixmap = QtGui.QPixmap(path)
        return None if pixmap.isNull() else pixmap

    @staticmethod
    def _is_paste_key(event) -> bool:
        if event.key() == QtCore.Qt.Key.Key_Insert:
            return bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        return event.key() == QtCore.Qt.Key.Key_V and bool(
            event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier
        )

    def _attach_from_clipboard(self) -> bool:
        """剪贴板里是图/文件就接成附件；是文字就交回去正常粘贴。"""
        clipboard = QtWidgets.QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            return False
        paths = self._drop_paths(mime)
        if paths:
            return self._attach_paths(paths)
        image = clipboard.image() if mime.hasImage() else None
        if image is None or image.isNull():
            return False
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")  # pyright: ignore[reportCallIssue, reportArgumentType]
        data = bytes(buffer.data().data())
        buffer.close()
        if not data:
            return False
        self._clipboard_seq += 1
        self._push_attachment(
            {"kind": "image", "name": f"粘贴图片-{self._clipboard_seq}.png", "data": data}
        )
        return True

    @staticmethod
    def _drop_paths(mime) -> list[Path]:
        if mime is None or not mime.hasUrls():
            return []
        found: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file():
                found.append(path)
        return found

    def _stop_turn(self) -> None:
        """叫住这一轮。Enter 在忙的时候是「停止」，语音打断也走这一个口子——同一件事只写一遍。"""
        if not self._busy:
            return
        self.stop_requested.emit()

    @QtCore.Slot()
    def _fit_input_height(self) -> None:
        """输入框按内容长高：一行时跟原来的单行框一样高，写到换行才长，删掉再缩回去。

        软换行也算行数——窗口只有 360 宽，一句话很快就绕行了，只按换行符数会一直不
        长高，字被截在框外。量的是排版后的真实高度（逐块 `blockBoundingRect`），
        不是数换行符，也不是 `document().size()`（那个在纯文本控件里给的是块数不是像素）。
        """
        fm = self.input.fontMetrics()
        line = max(1, fm.lineSpacing())
        pad = 12
        doc = self.input.document()
        layout = doc.documentLayout()
        total = 0.0
        if layout is not None:
            block = doc.begin()
            while block.isValid():
                total += layout.blockBoundingRect(block).height()
                block = block.next()
        shown = int(round(total / line)) if total > 0 else 1
        shown = max(1, min(INPUT_MAX_LINES, shown))
        height = shown * line + pad
        if self.input.height() != height:
            self.input.setFixedHeight(height)
        self.input.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if shown >= INPUT_MAX_LINES
            else QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def _emit(self) -> None:
        message = self.input.toPlainText().strip()
        attachments = list(self._attachments)
        if self._busy:
            if self._rewind_waiting:
                self._stop_turn()
            elif message or attachments:
                self._enqueue(message, attachments)
            else:
                self._stop_turn()
            return
        if self._rewind_waiting:
            return

        if not message and not attachments:
            return
        self.input.clear()
        self._sync_send_button()
        self._clear_attachments()
        self.append_user(message, attachments)
        if attachments:
            self.attachment_picked.emit(attachments)
        self.submitted.emit(message, "typed")


    def _init_voice_input(self) -> None:
        """建录音器。音频接口在这台机器上用不了就把按钮关掉，不影响打字。"""
        self._voice_mode_on = False
        self._recorder = None
        self._voice_prefs = None
        self._global_ptt = global_hotkey.GlobalHotkey(self)
        self._hold_armed = False
        self._hold_seen_down = False
        self._hold_started_at = 0.0
        self._hold_lost_at = 0.0
        self._ptt_watch = QtCore.QTimer(self)
        self._ptt_watch.setInterval(150)
        self._ptt_watch.timeout.connect(self._hold_watch)
        self._rehook_watch = QtCore.QTimer(self)
        self._rehook_watch.setInterval(1000)
        self._rehook_watch.timeout.connect(self._global_ptt.rearm)
        self._global_ptt.pressed.connect(self._hold_begin)
        self._global_ptt.released.connect(self._hold_end)
        self._voice_draft_focus = False
        self._voice_focus_backup: dict = {}
        self._audio_released = False
        self._ptt_button = QtWidgets.QToolButton(self._inputbox)
        self._ptt_button.setObjectName("icon")
        self._ptt_button.setIcon(_glyph_icon("keycap"))
        self._ptt_button.setIconSize(QtCore.QSize(14, 14))
        self._ptt_button.setFixedSize(22, 22)
        self._ptt_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._ptt_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._ptt_button.clicked.connect(self._ask_ptt_key)
        self._ptt_button.hide()
        self._refresh_ptt_button()
        try:
            from app import voice_input_prefs

            recorder = VoiceRecorder(self)
            recorder.level.connect(self._on_voice_level)
            recorder.failed.connect(self._on_voice_failed)
            recorder.device_lost.connect(self._on_voice_device_lost)
            remembered = voice_input_prefs.device_guid()
            if remembered:
                recorder.preload(remembered)
            self._recorder = recorder
            self._voice_prefs = voice_input_prefs
        except Exception:  # noqa: BLE001
            logger.warning("[语音] 录音器起不来", exc_info=True)
            self._mic_button.setEnabled(False)
            self._mic_button.setToolTip("这台机器的音频接口用不了，语音输入先关着")

    def _toggle_voice_input(self) -> None:
        self._set_voice_input(not self._voice_mode_on)

    def _set_voice_input(self, on: bool) -> None:
        """切「打字 / 说话」。只切可见性，输入框和它的内容一直在。"""
        if self._recorder is None or on == self._voice_mode_on:
            return
        self._voice_mode_on = on
        if on:
            self._voice_draft_focus = self.input.hasFocus()
            self._voice_orb.set_state(STATE_IDLE)
            self.input.hide()
            for button in (self._attach_button, self._approval_button,
                           self._reasoning_button, self._voice_button, self._send_button):
                button.hide()
            self._box_row.setContentsMargins(8, 8, 4, 8)
            self._set_inputbox_voice(True)
            self._voice_orb.show()
            self._float_mode_button(True)
            self._voice_focus_backup = {}
            for child in self.findChildren(QtWidgets.QWidget):
                policy = child.focusPolicy()
                if policy != QtCore.Qt.FocusPolicy.NoFocus:
                    self._voice_focus_backup[child] = policy
                    child.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.setFocus()
            self._mic_button.setIcon(_glyph_icon("keyboard", "#ffffff"))
            self._mic_button.setToolTip(
                f"语音已开：按住球或 {self._ptt_label()} 说话，松手就发"
                "（点一下回到打字，右键换麦克风）"
            )
            try:
                self._recorder.open_stream()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[语音] 打开录音流失败", exc_info=True)
                self._voice_flash(f"麦克风打不开：{exc}")
            self.voice_input_opened.emit()
            self._apply_global_ptt()
            self._ptt_watch.start()
            self._rehook_watch.start()
        else:
            self._ptt_watch.stop()
            self._rehook_watch.stop()
            self._global_ptt.stop()
            try:
                self._recorder.cancel()
                self._recorder.close_stream()
            except Exception:  # noqa: BLE001
                logger.warning("[语音] 关录音流失败", exc_info=True)
            self._voice_orb.hide()
            self._voice_orb.set_state(STATE_IDLE)
            for button in (self._attach_button, self._approval_button,
                           self._reasoning_button, self._voice_button, self._send_button):
                button.show()
            self._box_row.setContentsMargins(8, 0, 4, 0)
            self._set_inputbox_voice(False)
            self._float_mode_button(False)
            for child, policy in self._voice_focus_backup.items():
                try:
                    child.setFocusPolicy(policy)
                except RuntimeError:
                    pass
            self._voice_focus_backup = {}
            self._mic_button.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.view.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.input.show()
            QtCore.QTimer.singleShot(0, self._fit_input_height)
            self._mic_button.setIcon(_glyph_icon("mic"))
            self._mic_button.setToolTip("语音输入（右键选麦克风）")
            if self._voice_draft_focus or self.input.toPlainText():
                self.input.setFocus()

    def _float_mode_button(self, floating: bool) -> None:
        """语音模式下把「切回打字」那颗按钮从布局里摘出来，浮到输入框右上角。

        语音模式里整条输入区只剩球居中，按钮留在布局里会被挤到球右边偏下的位置；
        浮起来贴角更像个「关闭」开关，也不占球的横向空间。
        """
        button = self._mic_button
        if floating:
            self._box_row.removeWidget(button)
            button.setParent(self._inputbox)
            button.show()
            self._ptt_button.show()
            self._place_mode_button()
            self._inputbox.installEventFilter(self)
            return
        self._ptt_button.hide()
        self._inputbox.removeEventFilter(self)
        button.setParent(self._inputbox)
        self._box_row.addWidget(button)
        button.show()

    def _place_mode_button(self) -> None:
        """贴着输入框右上角摆，留 6px 内边距；「按住说话的键」键帽排在它下面。"""
        button = self._mic_button
        button.move(max(0, self._inputbox.width() - button.width() - 6), 6)
        ptt = getattr(self, "_ptt_button", None)
        if ptt is not None:
            ptt.move(
                max(0, self._inputbox.width() - ptt.width() - 6),
                6 + button.height() + 4,
            )

    def _set_inputbox_voice(self, on: bool) -> None:
        """切输入条的样子：语音模式下它只是球的托盘，不要框也不要底色。"""
        self._inputbox.setProperty("voice", on)
        style = self._inputbox.style()
        style.unpolish(self._inputbox)
        style.polish(self._inputbox)

    def _voice_hold_start(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        try:
            endpoint = recorder.current()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[语音] 取麦克风失败", exc_info=True)
            self._voice_flash(f"没法确认用哪只麦克风：{exc}")
            return
        if endpoint is None:
            self._voice_flash("这台电脑现在找不到能用的麦克风，右键能选别的设备")
            return
        self._voice_orb.set_state(STATE_RECORDING)
        try:
            started = recorder.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[语音] 开始录音失败", exc_info=True)
            self._voice_orb.set_state(STATE_IDLE)
            self._voice_flash(f"录音起不来：{exc}")
            return
        if not started:
            self._voice_orb.set_state(STATE_IDLE)
            self.show_notice("voice-input", "麦克风还在准备，稍等一下再说", ttl_ms=2500, level="warn")
            return
        self.voice_record_started.emit()

    def _voice_hold_end(self) -> None:
        recorder = self._recorder
        if recorder is None or self._voice_orb.state() != STATE_RECORDING:
            return
        pcm = recorder.stop()
        if pcm is None:
            self._voice_flash("说话太短了，按住多说一会儿")
            return
        if peak_of(pcm) <= SILENT_PEAK:
            self._voice_flash(f"「{recorder.endpoint_name()}」没收到声音，看看是不是戴歪了")
            return
        try:
            path = self._voice_temp_path()
            write_wav(pcm, path)
        except OSError as exc:
            self._voice_flash(f"录音存不下来：{exc}")
            return
        self._voice_orb.set_state(STATE_THINKING)
        self.voice_transcribe_requested.emit(str(path))

    def _voice_flash(self, message: str) -> None:
        """麦克风这边出问题：球闪红一下，状态条留一句话。"""
        self.show_notice("voice-input", message, ttl_ms=5000, level="error")
        self._voice_orb.set_state(STATE_ERROR)

        def _back_to_idle() -> None:
            if self._voice_orb.state() == STATE_ERROR:
                self._voice_orb.set_state(STATE_IDLE)

        QtCore.QTimer.singleShot(1600, _back_to_idle)

    def _voice_temp_path(self) -> Path:
        root = Path(__file__).resolve().parents[2] / "data" / "tmp" / "voice"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{int(time.time() * 1000)}.wav"

    @QtCore.Slot(float)
    def _on_voice_level(self, value: float) -> None:
        self._voice_orb.set_level(value)

    @QtCore.Slot(str)
    def _on_voice_failed(self, message: str) -> None:
        self._voice_flash(message or "麦克风用不了")

    @QtCore.Slot(str)
    def _on_voice_device_lost(self, replacement: str) -> None:
        """正在用的麦克风拔了/断开了：录音器已经自己回退，这里只告诉人一声。"""
        if replacement:
            self.show_notice("voice-input", f"麦克风断开了，已经换到「{replacement}」", ttl_ms=6000, level="warn")
        else:
            self.show_notice("voice-input", "麦克风断开了，现在没有能用的录音设备", ttl_ms=8000, level="error")
            self._voice_orb.set_state(STATE_ERROR)

    def apply_voice_transcript(self, text: str, source_path: str = "") -> None:
        """识别回来了：直接发出去，人留在语音模式接着往下说。

        不走输入框：语音模式下它是藏着的，里面可能还压着没发出去的草稿，
        清掉等于替她扔了东西，合并着发又等于替她拍了板。
        """
        self._discard_voice_file(source_path)
        text = (text or "").strip()
        if not text:
            self._voice_flash("这句没听清，再说一次")
            return
        self._voice_orb.set_state(STATE_IDLE)
        if self._busy:
            if self._streaming:
                self.end_assistant()
            self.append_user(text, [])
            self.submitted.emit(text, "voice")
            return
        self.append_user(text, [])
        self.submitted.emit(text, "voice")

    def voice_transcribe_failed(self, message: str, source_path: str = "") -> None:
        self._discard_voice_file(source_path)
        self._voice_flash(message or "识别失败了，再试一次")

    def release_audio(self) -> None:
        """把麦克风还回去。关窗和退出两条路都会走到，只认真做一次。"""
        if getattr(self, "_audio_released", True):
            return
        self._audio_released = True
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.cancel()
            recorder.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("[语音] 收尾失败", exc_info=True)

    @staticmethod
    def _discard_voice_file(path: str) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.debug("[语音] 删不掉临时录音 %s", path, exc_info=True)


    def _ptt_sequence(self) -> QtGui.QKeySequence:
        """按住说话用的键；没配过或配坏了就当空格。"""
        stored = ""
        if self._voice_prefs is not None:
            try:
                stored = self._voice_prefs.hotkey()
            except Exception:  # noqa: BLE001
                stored = ""
        if stored:
            sequence = QtGui.QKeySequence(stored)
            if sequence.count() == 1 and sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText):
                return sequence
        return QtGui.QKeySequence(QtCore.Qt.Key.Key_Space)

    def _ptt_label(self) -> str:
        """给人看的键名：空格 / F2 / Ctrl+空格。"""
        return _key_name(self._ptt_sequence())


    def _global_ptt_sequence(self) -> QtGui.QKeySequence:
        """系统级按住说话的键；没配过或配坏了就用 Shift+空格。"""
        stored = ""
        if self._voice_prefs is not None:
            try:
                stored = self._voice_prefs.global_hotkey()
            except Exception:  # noqa: BLE001
                stored = ""
        if stored:
            sequence = QtGui.QKeySequence(stored)
            if sequence.count() == 1 and sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText):
                return sequence
        return QtGui.QKeySequence("Shift+Space")

    def _global_ptt_label(self) -> str:
        return _key_name(self._global_ptt_sequence())

    def _apply_global_ptt(self) -> None:
        """把配置里的系统级键装进钩子。窗口在后台也生效。"""
        self._global_ptt.stop()
        native = _to_native(self._global_ptt_sequence())
        if native is None:
            return
        mods, vk = native
        ok, why = global_hotkey.probe(mods, vk)
        if not ok:
            logger.warning("[语音] 系统级快捷键 %s 被占用：%s", self._global_ptt_label(), why)
            self.show_notice(
                "voice-input",
                f"系统级 {self._global_ptt_label()} {why}，点键帽换一个",
                ttl_ms=8000,
                level="warn",
            )
            return
        self._global_ptt.set_sequence(mods, vk)
        self._global_ptt.start()
        if not self._global_ptt.running:
            logger.warning("[语音] 系统级快捷键装不上（错误码 %s）", self._global_ptt._last_error)
            self.show_notice(
                "voice-input",
                f"系统级 {self._global_ptt_label()} 没装上：窗口在后台时按它不会开始说话（在窗口里照常）",
                ttl_ms=8000,
                level="warn",
            )

    def _refresh_ptt_button(self) -> None:
        """外形永远是那颗键盘图标，只换提示文字。"""
        button = getattr(self, "_ptt_button", None)
        if button is None:
            return
        button.setToolTip(f"按住说话的键：{self._ptt_label()}（点一下改）")

    def _ptt_matches(self, event) -> bool:
        """这个按下算不算按住说话：主键和修饰键都要对上。"""
        sequence = self._ptt_sequence()
        if sequence.count() != 1:
            return False
        expected = sequence[0]  # pyright: ignore[reportIndexIssue]
        actual = event.keyCombination()
        return (
            expected.key() == actual.key()
            and expected.keyboardModifiers() == actual.keyboardModifiers()
        )

    def _ptt_key_is(self, event) -> bool:
        """这个松开是不是按住说话那个主键。松手只认主键——那时候修饰键可能已经先松了。"""
        key = event.keyCombination().key()
        return any(
            seq.count() == 1 and seq[0].key() == key  # pyright: ignore[reportIndexIssue]
            for seq in (self._ptt_sequence(), self._global_ptt_sequence())
        )

    def _hold_key_down(self) -> bool:
        """按住说话那个组合现在还完整按着吗——问系统键态，不问事件。"""
        return _sequence_down(self._ptt_sequence()) or _sequence_down(
            self._global_ptt_sequence()
        )

    def _hold_begin(self) -> None:
        """按下：事件说按了就开始。这里不查键态——键盘驱动慢半拍时查它是自找漏录。"""
        if not self._voice_mode_on:
            return
        if self._voice_orb.state() != STATE_RECORDING:
            self._hold_armed = True
            self._hold_seen_down = False
            self._hold_lost_at = 0.0
            self._hold_started_at = time.monotonic()
            self._voice_hold_start()

    def _hold_end(self) -> None:
        """松开：事件说松了就收。收到松开就不用再靠定时器兜底了。"""
        self._hold_armed = False
        self._hold_lost_at = 0.0
        if self._voice_orb.state() == STATE_RECORDING:
            self._voice_hold_end()

    def _hold_watch(self) -> None:
        """兜底：按下说了话却等不到松开，把它收掉。

        三个出口，按可信度排：
        1. 键态读到过、现在读不到 → 真丢了松开事件（系统截走 keyup、切窗口）；
        2. 从没读到过键态的环境（远程会话、某些驱动）不做这个判断，
           否则会把正常说的半句话掐掉；
        3. 所以再加一道纯时间的上限：正常人一口气说不到两分钟，
           到了还没收就一定有东西丢了，不能让它一直开着话筒。
        """
        if not self._hold_armed or self._voice_orb.state() != STATE_RECORDING:
            self._hold_lost_at = 0.0
            return
        now = time.monotonic()
        if now - self._hold_started_at >= _HOLD_MAX_SECONDS:
            self._hold_lost_at = 0.0
            self._hold_armed = False
            self._voice_hold_end()
            return
        if self._hold_key_down():
            self._hold_seen_down = True
            self._hold_lost_at = 0.0
            return
        if not self._hold_lost_at:
            self._hold_lost_at = now
        elif self._hold_seen_down and now - self._hold_lost_at >= 0.4:
            self._hold_lost_at = 0.0
            self._hold_armed = False
            self._voice_hold_end()

    def _ask_ptt_key(self) -> None:
        """改按住说话的键：窗口内和系统级都在这个小窗里，点哪条改哪条。"""
        dialog = _PttKeyDialog(
            self._ptt_sequence(), self._global_ptt_sequence(), self.window()
        )
        accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        window_sequence = dialog.window_sequence()
        global_sequence = dialog.global_sequence()
        dialog.deleteLater()
        if not accepted:
            return
        if self._voice_prefs is not None:
            try:
                if window_sequence is not None:
                    portable = window_sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText)
                    if portable:
                        self._voice_prefs.remember_hotkey("" if portable == "Space" else portable)
                if global_sequence is not None:
                    portable = global_sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText)
                    if portable:
                        self._voice_prefs.remember_global_hotkey(
                            "" if portable == "Shift+Space" else portable
                        )
            except (OSError, voice_input_prefs.ConfigError) as exc:
                logger.warning("[语音] 记不住快捷键：%s", exc)
                self.show_notice("voice-input", f"快捷键没存上：{exc}", ttl_ms=5000, level="warn")
        self._refresh_ptt_button()
        self._apply_global_ptt()
        self._mic_button.setToolTip(
            f"语音已开：按住球、{self._ptt_label()}（窗口内）或 "
            f"{self._global_ptt_label()}（系统级）说话，松手就发"
            "（点一下回到打字，右键换麦克风）"
        )
        self.show_notice("voice-input", f"按住「{self._ptt_label()}」说话", ttl_ms=3000)

    def _show_mic_menu(self, pos: QtCore.QPoint) -> None:
        """右键：把所有能录的设备列出来，勾上当前那只。"""
        recorder = self._recorder
        if recorder is None:
            return
        menu = QtWidgets.QMenu(self._mic_button)
        menu.setObjectName("modelmenu")
        menu.setStyleSheet(STYLE)

        current = recorder.current()
        current_guid = current.guid if current else ""

        endpoints = recorder.devices()
        group = QtGui.QActionGroup(menu)
        group.setExclusive(True)
        if not endpoints:
            empty = menu.addAction("没找到能用的麦克风")
            empty.setEnabled(False)
        else:
            for endpoint in endpoints:
                label = endpoint.name + ("（蓝牙）" if endpoint.bluetooth else "")
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(endpoint.guid == current_guid)
                action.setActionGroup(group)
                action.triggered.connect(
                    lambda _checked=False, guid=endpoint.guid: self._choose_mic(guid)
                )
        menu.addSeparator()
        follow = menu.addAction("跟随系统默认")
        follow.setCheckable(True)
        follow.setActionGroup(group)
        follow.setChecked(not recorder.chosen_guid())
        follow.triggered.connect(lambda _checked=False: self._choose_mic(""))
        menu.exec(self._mic_button.mapToGlobal(pos))
        menu.deleteLater()

    def _choose_mic(self, guid: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        recorder.choose(guid)
        if self._voice_prefs is not None:
            try:
                self._voice_prefs.remember(guid)
            except OSError as exc:
                logger.warning("[语音] 记不住选的麦克风：%s", exc)
        if guid:
            endpoint = audio_devices.find(guid)
            name = endpoint.name if endpoint else "所选麦克风"
            self.show_notice("voice-input", f"麦克风换成「{name}」", ttl_ms=3000)
        else:
            self.show_notice("voice-input", "麦克风跟随系统默认", ttl_ms=3000)



    def _max_size(self) -> tuple[int, int]:
        """别让面板大到超出屏幕：宽 90% 屏宽、高 85% 屏高。"""
        screen = QtGui.QGuiApplication.screenAt(self.frameGeometry().center())
        screen = screen or QtGui.QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)
        return int(area.width() * 0.9), int(area.height() * 0.85)

    def _restore_geometry(self) -> None:
        max_w, max_h = self._max_size()
        saved = self._settings.value("panel/size")
        if isinstance(saved, QtCore.QSize):
            size = fit_free_size(
                saved.width(),
                saved.height(),
                min_width=MIN_PANEL_WIDTH,
                min_height=MIN_PANEL_HEIGHT,
                max_width=max_w,
                max_height=max_h,
            )
        else:
            size = fit_free_size(
                PANEL_WIDTH,
                PANEL_HEIGHT,
                min_width=MIN_PANEL_WIDTH,
                min_height=MIN_PANEL_HEIGHT,
                max_width=max_w,
                max_height=max_h,
            )
        self.resize(size)
        self._sync_popup_width()

    def _save_geometry(self) -> None:
        try:
            self._settings.setValue("panel/size", self.size())
            self._settings.setValue("panel/pos", self.pos())
            self._settings.sync()
        except Exception:
            logger.warning("聊天窗位置没存下来：%s", self._settings.fileName(), exc_info=True)

    def _saved_pos(self) -> QtCore.QPoint | None:
        """上次的位置；换过显示器/分辨率后可能已在屏幕外，那就夹回屏幕里再用。"""
        saved = self._settings.value("panel/pos")
        if not isinstance(saved, QtCore.QPoint):
            return None
        screen = QtGui.QGuiApplication.screenAt(saved) or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return None
        area = screen.availableGeometry()
        x = max(area.left(), min(saved.x(), area.right() - self.width() + 1))
        y = max(area.top(), min(saved.y(), area.bottom() - self.height() + 1))
        return QtCore.QPoint(x, y)

    def _sync_popup_width(self) -> None:
        """菜单宽按自己最长那一行算：Qt 本来就是内容多宽、菜单多宽，这里只让它重算一次。"""
        if hasattr(self, "_model_menu"):
            self._model_menu.adjustSize()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        self._resize_started()
        super().resizeEvent(event)
        if hasattr(self, "_model_menu"):
            self._sync_popup_width()
        self._place_background()
        self._place_jump()
        QtCore.QTimer.singleShot(0, self._refit_user_lines)
        QtCore.QTimer.singleShot(0, self._place_msg_bars)
        QtCore.QTimer.singleShot(0, self._place_sticky)

    def _refit_user_lines(self) -> None:
        """窗口宽度变了：只重截眼前这几条，其余滚到时再补（见 `_fit_visible_user_lines`）。"""
        width = self.view.viewport().width()
        if width == self._user_line_width:
            return
        self._user_line_width = width
        self._fit_visible_user_lines()
        title = getattr(self, "_title_label", None)
        if title is not None:
            title.setText(TITLE_FULL if self.width() >= TITLE_FULL_MIN_WIDTH else TITLE_SHORT)

    def _fit_visible_user_lines(self) -> None:
        """眼前这几条按当前宽度重截；离开视口的不动，滚回来时再补。

        每次改一条都要重排它后面那段文档，文档上百条时挨个改一遍就是几百次重排——
        窗口每动一格都要重来一遍，拉伸才会越拉越卡。
        """
        width = self.view.viewport().width()
        for block in self.view.visible_blocks():
            msg = getattr(block.userData(), "msg", None)
            if msg is None or msg.get("role") != "user":
                continue
            if msg.get("fit_width") == width:
                continue
            self._fit_user_line(msg)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_popup_width()


    def _hit_edges(self, local: QtCore.QPoint):
        return edges_at(local, self.size(), margin=RESIZE_MARGIN)

    def _sync_edge_cursor(self, local: QtCore.QPoint) -> None:
        """只在贴边那圈给缩放光标，一离开就还回去。

        光标是父级继承的：`setCursor` 设在面板上会传给里面所有控件，鼠标从图/输入框
        那边进来时面板收不到 move，双箭头就一路顶着——所以每个能收到 move 的地方都要
        对一次位置。
        """
        inside = QtCore.QRect(QtCore.QPoint(0, 0), self.size()).contains(local)
        edge = self._hit_edges(local) if inside else None
        if edge:
            if self._edge_cursor_shape != edge:
                self.setCursor(cursor_for(edge))
                self._edge_cursor_shape = edge
        elif self._edge_cursor_shape is not None or self.testAttribute(
            QtCore.Qt.WidgetAttribute.WA_SetCursor
        ):
            self.unsetCursor()
            self._edge_cursor_shape = None

    def _begin_resize(self, local: QtCore.QPoint, global_pos: QtCore.QPoint) -> bool:
        edge = self._hit_edges(local)
        if not edge:
            return False
        self._resize_edge = edge
        self._resize_start_size = self.size()
        self._resize_start_global = global_pos
        self._drag_offset = None
        self.setCursor(cursor_for(edge))
        self._edge_cursor_shape = edge
        return True

    def _handle_input_drag(self, event) -> bool:
        """拖文件/图进输入框：接成附件，不往正文里塞一串路径。

        两条路径都要接：拖放的目标可能是视口（QAbstractScrollArea 的正常派发），
        也可能是控件本身（某些平台/合成源直接发给控件）。
        """
        kind = event.type()
        if kind in (
            QtCore.QEvent.Type.DragEnter,
            QtCore.QEvent.Type.DragMove,
        ):
            if self._drop_paths(event.mimeData()):
                event.acceptProposedAction()
                return True
        elif kind is QtCore.QEvent.Type.Drop:
            paths = self._drop_paths(event.mimeData())
            if paths:
                event.acceptProposedAction()
                self._attach_paths(paths)
                return True
        return False

    def _mark_input_focus(self, focused: bool) -> None:
        """高亮画在输入框外壳上（#inputbox[focused]），框里的输入控件自己是无边框的。"""
        box = getattr(self, "_inputbox", None)
        if box is None:
            return
        box.setProperty("focused", "true" if focused else "false")
        box.style().unpolish(box)
        box.style().polish(box)

    def _sync_input_ph(self) -> None:
        """提示文字：输入框里有字、或者输入法正组着字，就让开。
        组字那半截不在 document 里，只看 document 会漏掉，中文打一半时提示压在拼音上。"""
        inp = getattr(self, "input", None)
        if inp is None:
            return
        inp.set_placeholder_visible(
            inp.document().isEmpty() and not getattr(self, "_ime_composing", False)
        )


    def _hover_at(self, pos: QtCore.QPoint) -> None:
        """鼠标在正文里移动：先判出它落在哪个区域，换了区域才换按钮。

        区域判定和按钮摆放分成两件事：这里只问「在哪」，下面 _open_zone 只负责
        「那个区域该亮哪颗」。所以每条分支里都没有「收掉」这种补救动作——
        上一个区域的按钮在换区时整体收掉，一次。
        """
        if self._over_hover_button():
            return
        self._show_msg_bar(self._msg_at_pos(pos))
        zone, block = self._zone_at(pos)
        if zone != self._zone:
            self._hide_hover()
            self._zone = zone
        if zone is not None and block is not None:
            self._open_zone(zone, block)

    def _zone_at(self, pos: QtCore.QPoint) -> tuple[tuple | None, QtGui.QTextBlock | None]:
        """鼠标此刻落在哪个区域。只认区域，不碰按钮。"""
        block = self._block_at(pos)
        if block is None or not block.isValid():
            return None, None
        if self._code_fence_at(block) is not None:
            return ("code", block.blockNumber()), block
        if self._block_has_image(block):
            hit = self._photo_at(pos)
            return (("photo",) + hit) if hit else None, block
        mark = block.userData()
        if isinstance(mark, disclosure._BodyMark):
            return ("body", mark.key), block
        return None, block

    def _open_zone(self, zone: tuple, block: QtGui.QTextBlock) -> None:
        """给当前区域亮它自己那颗按钮。别的区域的按钮在这条路径上根本不出现。"""
        kind = zone[0]
        if kind == "code":
            fence = self._code_fence_at(block)
            if fence is not None:
                first, last, text = fence
                self._show_hover_copy(
                    text, tool_tr("assistant.tool.copyCode"), first, last,
                    key=("code", first.position()),
                )
        elif kind == "photo":
            self._show_hover_download(zone[1], zone[2])
        elif kind == "body":
            mark = block.userData()
            if isinstance(mark, disclosure._BodyMark):
                self._open_body_zone(block, mark.key)

    def _open_body_zone(self, block: QtGui.QTextBlock, key: int) -> None:
        """工具输出正文：能复制就给复制，另外有 diff 才给「看完整 diff」。"""
        first, last = self._marked_run(block, key)
        payload = self._tool_payloads.get(key)
        if payload is None and key in self._diff_texts:
            payload = (tool_tr("assistant.tool.copyContent"), self._diff_texts[key][1])
        if payload and payload[1].strip():
            self._show_hover_copy(
                payload[1], payload[0], first, last, key=("body", key),
                margin=self.BODY_BUTTON_MARGIN,
            )
        if key in self._diff_texts:
            self._show_hover_expand(key, first, last, margin=self.BODY_BUTTON_MARGIN)

    def _hook_tool_scrolls(self) -> None:
        """工具正文那几块是独立控件，盖在正文上：鼠标事件得从它们那儿转进来，否则悬停不亮。"""
        for fold in self._folds.all():
            scroll = getattr(fold, "scroll", None)
            if scroll is None or id(scroll) in self._hooked_scrolls:
                continue
            self._hooked_scrolls.add(id(scroll))
            scroll.installEventFilter(self)
            scroll.viewport().installEventFilter(self)
            scroll.verticalScrollBar().installEventFilter(self)

    def _sync_elements(self) -> None:
        """文档里新出现的代码块、图片，各建一颗自己的按钮：创建跟着元素走，不跟着鼠标走。"""
        document = self.view.document()
        end = document.characterCount() - 1
        block = document.findBlock(min(self._el_scan_pos, end))
        while block.isValid():
            self._register_element(block)
            block = block.next()
        self._el_scan_pos = max(0, end)
        self._hook_tool_scrolls()
        self._place_msg_bars()

    def _register_element(self, block: QtGui.QTextBlock) -> None:
        mark = block.userData()
        if isinstance(mark, _CodeMark):
            first = block
            while isinstance(first.previous().userData(), _CodeMark):
                first = first.previous()
            self._element_button(
                ("code", first.position()),
                "copy",
                tool_tr("assistant.tool.copyCode"),
                lambda target=first: self._copy_code_run(target),
                done_tooltip=tool_tr("common.copied"),
            )
            return
        if not self._block_has_image(block):
            return
        fragment = block.begin()
        while not fragment.atEnd():
            piece = fragment.fragment()
            if piece.isValid() and self._is_photo_format(piece.charFormat()):
                name = piece.charFormat().toImageFormat().name()
                button = self._element_button(
                    ("photo", name), "download", "下载",
                    lambda key=name: self._download_photo(key), flash=False,
                )
                button.setStyleSheet(
                    "QToolButton { border: none; border-radius: 6px;"
                    " background: rgba(16, 18, 24, 150); padding: 0; }"
                    "QToolButton:hover { background: rgba(16, 18, 24, 225); }"
                )
            fragment += 1

    def _register_body(self, key: int) -> None:
        """工具正文有了内容：给它建自己的复制/看 diff 按钮，位置绑在同那段正文上。"""
        if ("body", key) in self._el_buttons and ("expand", key) in self._el_buttons:
            return
        payload = self._tool_payloads.get(key)
        if payload is not None and str(payload[1]).strip():
            self._element_button(
                ("body", key), "copy", payload[0],
                lambda target=key: self._copy_body(target),
                done_tooltip=tool_tr("common.copied"),
            )
        if key in self._diff_texts:
            self._element_button(
                ("expand", key), "expand", DIFF_PREVIEW_LABEL,
                lambda target=key: self._open_diff_preview(target), flash=False,
            )

    def _copy_body(self, key: int) -> None:
        payload = self._tool_payloads.get(key)
        if payload is not None:
            self._copy_to_clipboard(payload[1])
            return
        if key in self._diff_texts:
            self._copy_to_clipboard(self._diff_texts[key][1])

    def _copy_code_run(self, first: QtGui.QTextBlock) -> None:
        """复制这段代码围栏里的正文。"""
        fence = self._code_fence_at(first)
        if fence is not None:
            self._copy_to_clipboard(fence[2])

    def _element_button(
        self, key: tuple, icon: str, tooltip: str, slot: Callable[[], None],
        *, done_tooltip: str = "", flash: bool = True,
    ) -> HoverActionButton:
        """一个元素自己的按钮：同一个键只建一次，往后只是换上这次的动作用。"""
        button = self._el_buttons.get(key)
        if button is None:
            button = HoverActionButton(
                self.view.viewport(), icon, tooltip, slot, done_tooltip, flash=flash
            )
            self._el_buttons[key] = button
            return button
        button.retarget(icon, tooltip, slot, done_tooltip)
        return button

    def _show_element(self, button: HoverActionButton) -> None:
        """亮这颗；同一批里要一起亮的（比如工具正文的复制 + 看 diff）互不遮挡。"""
        if button not in self._shown_buttons:
            self._shown_buttons.append(button)
        button.show()
        button.raise_()

    def _hide_elements(self) -> None:
        for button in self._shown_buttons:
            button.hide()
        self._shown_buttons.clear()

    @classmethod
    def _block_has_image(cls, block: QtGui.QTextBlock) -> bool:
        """这一块里有没有原图字符。没有就不去问图片区域，普通正文不跑图片的代码。"""
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and cls._is_photo_format(frag.charFormat()):
                return True
            it += 1
        return False

    def _over_hover_button(self) -> bool:
        """鼠标此刻是不是压在某颗悬停按钮上（压在按钮上时正文会收到 Leave，那不是「离开」）。"""
        pos = self.view.viewport().mapFromGlobal(QtGui.QCursor.pos())
        for button in (*self._el_buttons.values(), *self._bar_buttons):
            if button is not None and button.isVisible() and button.geometry().contains(pos):
                return True
        return False

    def _block_at(self, pos: QtCore.QPoint) -> QtGui.QTextBlock | None:
        if not self.view.viewport().rect().contains(pos):
            return None
        return self.view.cursorForPosition(pos).block()

    @staticmethod
    def _is_code_block(block: QtGui.QTextBlock) -> bool:
        """代码块：插进去时打过 `_CodeMark`（见 `_insert_code_block`）。"""
        return isinstance(block.userData(), _CodeMark)

    def _code_fence_at(self, block: QtGui.QTextBlock):
        """把鼠标所在的那段代码围栏整块圈出来（往上、往下连着同样是代码的块）。"""
        if not self._is_code_block(block):
            return None
        first = block
        while first.previous().isValid() and self._is_code_block(first.previous()):
            first = first.previous()
        last = block
        while last.next().isValid() and self._is_code_block(last.next()):
            last = last.next()
        cache_key = (first.position(), last.position())
        text = self._fence_cache.get(cache_key)
        if text is None:
            lines: list[str] = []
            current = first
            while True:
                lines.append(current.text())
                if current == last or not current.next().isValid():
                    break
                current = current.next()
            text = markdown_render.code_text("\n".join(lines)).strip("\n")
            if len(self._fence_cache) > 64:
                self._fence_cache.clear()
            self._fence_cache[cache_key] = text
        if not text.strip():
            return None
        return first, last, text

    def _marked_run(self, block: QtGui.QTextBlock, key: int):
        """同一段折叠正文的连续块：从第一块到最后一块。"""
        first = block
        while True:
            previous = first.previous()
            mark = previous.userData()
            if not previous.isValid() or not isinstance(mark, disclosure._BodyMark) or mark.key != key:
                break
            first = previous
        last = block
        while True:
            following = last.next()
            mark = following.userData()
            if not following.isValid() or not isinstance(mark, disclosure._BodyMark) or mark.key != key:
                break
            last = following
        return first, last

    def _block_rect(self, first: QtGui.QTextBlock, last: QtGui.QTextBlock) -> QtCore.QRect:
        layout = self.view.document().documentLayout()
        top = layout.blockBoundingRect(first)
        bottom = layout.blockBoundingRect(last)
        widest = top.width()
        current = first
        while current.isValid():
            widest = max(widest, layout.blockBoundingRect(current).width())
            if current == last:
                break
            current = current.next()
        rect = QtCore.QRect(
            int(min(top.left(), bottom.left())),
            int(top.top()),
            int(widest),
            int(bottom.bottom() - top.top()),
        )
        rect.translate(-self.view.horizontalScrollBar().value(), -self.view.verticalScrollBar().value())
        return rect

    def _show_hover_copy(self, text: str, tooltip: str, first, last, edge: bool = False,
                         key: tuple | None = None, margin: int = 4) -> None:
        """复制按钮：点完写剪贴板、换 ✓、1.5 秒还原。"""
        button = self._element_button(
            key or ("copy", first.position()),
            "copy",
            tooltip,
            lambda value=text: self._copy_to_clipboard(value),
            done_tooltip=tool_tr("common.copied"),
        )
        self._show_element(button)
        self._place_hover(button, first, last, edge=edge, margin=margin)

    def _show_hover_expand(self, key: int, first, last, margin: int = 4) -> None:
        """放大按钮：只开预览窗，不换 ✓（那不是一次复制）。放在复制按钮左边。"""
        button = self._element_button(
            ("expand", key),
            "expand",
            DIFF_PREVIEW_LABEL,
            lambda target=key: self._open_diff_preview(target),
            flash=False,
        )
        self._show_element(button)
        self._place_hover(button, first, last,
                          offset_x=-(HoverActionButton.BUTTON_PX + 2), margin=margin)


    def _begin_message(self, role: str, start: int, text: str, row_id, frame=None) -> None:
        """给刚插进来的那条消息的开头块打标记，并记进清单。

        `start` 是插入**前**的文末字符位：插入时若新开了一段，这条消息就从下一段开始
        （`next().position() == start + 1` 正好说明段界开在这里）。
        """
        document = self.view.document()
        if frame is not None:
            block = frame.firstCursorPosition().block()
        else:
            block = document.findBlock(start)
            following = block.next()
            if following.isValid() and following.position() == start + 1:
                block = following
        msg = {"role": role, "text": text, "row_id": row_id}
        if frame is not None:
            msg["frame"] = frame
        msg["mark"] = _MsgMark(msg)
        msg["head_block"] = block
        block.setUserData(msg["mark"])
        gap = self._msg_gap(block)
        head_format = block.blockFormat()
        head_format.setTopMargin(0 if frame is not None else gap)
        QtGui.QTextCursor(block).setBlockFormat(head_format)
        if frame is not None:
            self._apply_frame_gap(frame, gap)
        self._msgs.append(msg)
        if role == "assistant":
            self._stream_msg = msg

    def _fit_fold_gap(self, fold) -> None:
        """折叠行刚好是这一轮的第一块时，段前距按两条消息之间算。

        折叠段新建时只拿到段内距（写在 `Folds` 里的那一档），可「思考」经常在助手消息
        开始之前就先流出来，于是它成了贴在用户气泡下面的第一块，段内距就成了 4px。
        """
        block = fold.head_block
        if block is None or not block.isValid():
            return
        probe = block.previous()
        while probe.isValid() and not probe.isVisible():
            probe = probe.previous()
        mark = probe.userData() if probe.isValid() else None
        boundary = isinstance(mark, (_BarMark, _PhotoMark)) or (
            isinstance(mark, _MsgMark) and mark.msg.get("role") == "user"
        )
        if not boundary:
            return
        gap = self._msg_gap(block)
        fmt = block.blockFormat()
        if int(fmt.topMargin()) != gap:
            fmt.setTopMargin(gap)
            QtGui.QTextCursor(block).setBlockFormat(fmt)

    def _starts_after_fold(self, block) -> bool:
        """这一段是不是接在本轮自己的折叠行（思考 / 工具）下面。

        每一轮都以你那条消息开头，所以「前一个可见块不是消息块」就说明它属于这一轮：
        答案文字接着自己那行「已思考」往下写，间距按段内算，不按两条消息之间算。
        """
        probe = block.previous()
        while probe.isValid() and not probe.isVisible():
            probe = probe.previous()
        if not probe.isValid():
            return False
        return not isinstance(probe.userData(), (_MsgMark, _BarMark, _PhotoMark))

    def _msg_gap(self, block: QtGui.QTextBlock) -> int:
        """两条消息之间留多少：可见空隙一律 MSG_GAP，气泡内边距与动作条占的份先补回来。"""
        if self._starts_after_fold(block):
            return MSG_GAP_TIGHT
        probe = block.previous()
        while probe.isValid() and not probe.isVisible():
            probe = probe.previous()
        if not probe.isValid():
            own = block.userData()
            if isinstance(own, _MsgMark) and own.msg.get("role") == "user":
                return ChatView.BUBBLE_PAD_Y
            return MSG_GAP_TIGHT
        mark = probe.userData()
        if isinstance(mark, _BarMark):
            gap = MSG_GAP - self.BAR_GAP
        elif isinstance(mark, _PhotoMark):
            return MSG_GAP - ChatView.BUBBLE_PAD_Y
        elif isinstance(mark, _MsgMark) and mark.msg.get("role") == "user":
            gap = MSG_GAP + ChatView.BUBBLE_PAD_Y
        else:
            gap = MSG_GAP
        own = block.userData()
        if isinstance(own, _MsgMark) and own.msg.get("role") == "user":
            gap += ChatView.BUBBLE_PAD_Y
        return gap

    def _rewind_later_users(self, msg: dict) -> int:
        """这条用户消息后面还有几条用户消息（= 重发时会被一起删掉的问答轮数）。"""
        count = 0
        seen = False
        for item in self._msgs:
            if item is msg:
                seen = True
                continue
            if seen and item.get("role") == "user":
                count += 1
        return count

    def _previous_user(self, msg: dict) -> dict | None:
        """这条消息之前最近的一条用户消息（截断点由它定）。"""
        found = None
        for item in self._msgs:
            if item is msg:
                break
            if item.get("role") == "user":
                found = item
        return found

    def _rewind_empties(self, msg: dict) -> bool:
        """从这条截断会不会把整场对话都清掉（它就是第一句用户消息）。"""
        return self._previous_user(msg) is None

    def _emit_rewind(self, msg: dict, text: str, regenerate: bool) -> None:
        """请引擎从这条截断重来：本地先记着「在等落地」，落地前不许再发一次。

        确认在调用方做完（用户那条会弹窗说清要删掉什么）；到这一步只剩「发请求」。
        """
        self._rewind_waiting = True
        self._sync_send_button()
        self.resubmit_requested.emit(
            msg, text, regenerate, self._rewind_empties(msg), self._rewind_later_users(msg) > 0
        )

    def apply_rewind(self, msg: dict, text: str) -> None:
        """引擎已经按这条消息截断过历史了：窗口回到那一刻，再把正文重新贴一遍。

        附件照原样贴回去：那条消息本来就带着图和文件，重跑后气泡还得是原来那么大。
        """
        self._rewind_waiting = False
        self._truncate_from(msg)
        self.append_user(text, attachments=msg.get("attachments") or ())
        if self.input.toPlainText().strip() == text.strip():
            self.input.clear()
        self._sync_send_button()

    def cancel_rewind(self) -> None:
        """这次截断重发没成立（被拒 / 换了会话）：正文留在框里，发送键恢复原样。"""
        self._rewind_waiting = False
        self._row_stamp_pending = None
        self._sync_send_button()

    def _truncate_from(self, msg: dict) -> None:
        """把这个窗口从某条消息起的内容抹掉（引擎侧已经截断，界面跟上）。"""
        self._destroy_msg_bars(msg)
        self._destroy_elements()
        for index, item in enumerate(self._msgs):
            if item is msg:
                self._msgs = self._msgs[:index]
                break
        mark = msg.get("mark")
        document = self.view.document()
        target = None
        probe = document.begin()
        while probe.isValid():
            try:
                hit = probe.userData() is mark
            except RuntimeError:
                hit = False
            if hit:
                target = probe
                break
            probe = probe.next()
        if target is None:
            return
        cursor = QtGui.QTextCursor(document)
        start = target.position()
        prev = target.previous()
        prev_mark = None
        if prev.isValid():
            try:
                prev_mark = prev.userData()
            except RuntimeError:
                prev_mark = None
        cursor.setPosition(start)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End, QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._reseat_tail(prev_mark)
        self._stream_msg = None
        self._streaming = False
        self._flush_timer.stop()
        self._assistant_pending = ""
        self._md_block = []
        self._md_fence = False
        self._reason_fold = None
        self._tool_started.clear()
        self._tool_folds.clear()
        self._tool_clock.stop()
        self._folds.drop_invalid()
        self._fence_cache.clear()

    def _reseat_tail(self, prev_mark: QtGui.QTextBlockUserData | None) -> None:
        """截断删到文末，Qt 会把切点前那块并进文末那块：块标记会被丢掉，被删那轮的标记
        还可能搬上来。并出来的就是文末这一块，收拾回原样——它原来带谁的标记就给谁贴个
        新的回去（旧的已经跟着合并没了，句柄改指这块），原来没标记或挂着被删那轮搬来的
        脏标记就清掉：脏标记会把「首条顶到第一行」的折叠挡掉，还会让悬停把这块认成已经
        删掉的消息。"""
        slot = self.view.document().lastBlock()
        owner: dict | None = None
        if isinstance(prev_mark, (_MsgMark, _BarMark)):
            owner = prev_mark.msg
        elif isinstance(prev_mark, _PhotoMark):
            owner = prev_mark.owner
        fresh = None
        if owner is not None and any(owner is item for item in self._msgs):
            if isinstance(prev_mark, _BarMark):
                fresh = _BarMark(owner)
                owner["bar_block"] = slot
            elif isinstance(prev_mark, _MsgMark):
                fresh = _MsgMark(owner)
                owner["mark"] = fresh
                owner["head_block"] = slot
            else:
                fresh = _PhotoMark(owner)
        elif isinstance(prev_mark, _CodeMark):
            fresh = _CodeMark()
        # 传 None 就是摘掉用户数据（Qt 文档：data 为 0 即移除）；PySide 的桩没把参数写成可空。
        slot.setUserData(cast(QtGui.QTextBlockUserData, fresh))

    def stamp_user_row(self, row_id: int) -> None:
        """引擎回了这次提交的用户消息行号：重新生成/编辑重发靠它定位截断点。"""
        for msg in reversed(self._msgs):
            if msg.get("role") == "user" and msg.get("row_id") is None:
                msg["row_id"] = int(row_id)
                return
        self._row_stamp_pending = int(row_id)

    def bind_turn_receipt(self, receipt: dict) -> None:
        """这一轮真正落库的行号：截断重发之后新贴的那条就等它。

        引擎在「这一轮开始算隔离计算」那条路上回 `prompt.submit` 时不带新消息的行号，
        只带活下来的旧行号；新行号要等这一轮跑完的 `persisted_turn` 回执。只认提交回包的话，
        重跑出来的那条消息永远没有行号——「刷新 / 重新发送」用过一次就再也不出现了。
        """
        if not isinstance(receipt, dict):
            return
        ids = receipt.get("user_row_ids")
        if isinstance(ids, list):
            ints = [int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)]
        else:
            single = receipt.get("user_row_id")
            ints = [int(single)] if isinstance(single, int) and not isinstance(single, bool) else []
        if not ints:
            return
        waiting = [m for m in self._msgs if m.get("role") == "user" and m.get("row_id") is None]
        if not waiting:
            return
        for msg, rid in zip(waiting, ints[-len(waiting):]):
            msg["row_id"] = rid
        self._row_stamp_pending = None

    def is_busy(self) -> bool:
        return bool(self._busy)

    def queued_count(self) -> int:
        return len(self._queued)

    def _place_hover(self, button: HoverActionButton, first, last, offset_x: int = 0,
                     below: bool = False, bottom_align: bool = False,
                     anchor_right: bool = False, edge: bool = False,
                     margin: int = 4) -> None:
        rect = self._block_rect(first, last)
        if rect.height() <= 0:
            button.hide()
            return
        if not below and rect.top() < 0:
            button.hide()
            return
        usable = self._usable_width()
        if edge:
            rect.setRight(usable)
        else:
            rect.setRight(min(rect.right(), usable))
        button.place_at(rect, offset_x=offset_x, below=below, bottom_align=bottom_align,
                        anchor_right=anchor_right, margin=margin)
        button.show()
        button.raise_()

    BAR_PX = 24
    BAR_GAP = 8

    def _mount_msg_bar(self, msg: dict) -> None:
        """这条消息写完了：在它文末落一块动作条。

        桌面端把按钮条写在气泡里、靠排版占位；这里同样插一个空段落把动作条那一段占住，
        按钮贴着这一块摆——位置由排版给，不用再去算「最后一行在哪」。
        """
        if msg.get("role") == "user":
            if "bar_buttons" in msg:
                return
            buttons = [
                HoverActionButton(self.view.viewport(), icon, tip, slot)
                for icon, tip, slot in self._bar_specs(msg)
            ]
            msg["bar_buttons"] = buttons
            msg["bar_block"] = None
            self._bar_buttons.extend(buttons)
            self._show_msg_bar(None)
            self._place_msg_bars()
            return
        if msg.get("bar_block") is not None:
            return
        document = self.view.document()
        cursor = QtGui.QTextCursor(document)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        fmt = QtGui.QTextBlockFormat()
        fmt.setTopMargin(0)
        cursor.insertBlock(fmt)
        block = cursor.block()
        block.setUserData(_BarMark(msg))
        line = self.view.document().documentLayout().blockBoundingRect(block).height()
        fmt.setBottomMargin(max(0.0, self.BAR_PX - line) + self.BAR_GAP)
        cursor.setBlockFormat(fmt)
        msg["bar_block"] = block
        buttons = [
            HoverActionButton(self.view.viewport(), icon, tip, slot)
            for icon, tip, slot in self._bar_specs(msg)
        ]
        msg["bar_buttons"] = buttons
        self._bar_buttons.extend(buttons)
        self._show_msg_bar(None)
        self._place_msg_bars()
        if (self._follow or self._at_end()) and not self._restoring:
            self._scroll_to_end_later()

    def _bar_specs(self, msg: dict) -> list[tuple[str, str, Callable[[], None]]]:
        """一条回答上摆「复制 + 刷新」，你发的那条摆「复制 + 重新发送」。"""
        if msg.get("role") == "user":
            return [
                ("copy", "复制", lambda: self._copy_message(msg)),
                ("resend", "重新发送", lambda: self._resend_message(msg)),
            ]
        return [
            ("copy", "复制", lambda: self._copy_message(msg)),
            ("refresh", "刷新", lambda: self._refresh_message(msg)),
        ]

    def _place_msg_bars(self) -> None:
        """按钮贴着各自那条消息的动作块：只摆眼前这几块。

        光标问出可见块之后，谁带消息标记就摆谁——不再拿整张消息表去对位置。
        """
        usable = self._usable_width()
        pad = (self.BAR_PX - HoverActionButton.BUTTON_PX) // 2
        seen: set[int] = set()
        for block in self.view.visible_blocks():
            msg = self._msg_of_block(block)
            if msg is None or id(msg) in seen:
                continue
            seen.add(id(msg))
            buttons = msg.get("bar_buttons") or []
            if not buttons:
                continue
            box = self._msg_corner(msg, usable, pad)
            if box is None:
                for button in buttons:
                    button.hide()
                continue
            total = len(buttons) * HoverActionButton.BUTTON_PX + (len(buttons) - 1) * 4
            start = max(0, min(int(box.right()) - total, usable - 8 - total))
            if "frame" in msg:
                # 有框的那条：按钮和框里的字站同一行，不挂到框底下去。
                top = box.top() + (box.height() - HoverActionButton.BUTTON_PX) // 2
            else:
                top = int(box.bottom()) - HoverActionButton.BUTTON_PX
            top = max(0, top)
            for button in buttons:
                button.move(start, top)
                start += HoverActionButton.BUTTON_PX + 4

    @staticmethod
    def _msg_of_block(block: QtGui.QTextBlock) -> dict | None:
        """这一块属于哪条消息：块自己带着标记，不扫表。"""
        try:
            mark = block.userData()
            if isinstance(mark, _MsgMark):
                return mark.msg
            if isinstance(mark, _BarMark):
                return mark.msg
            if isinstance(mark, _PhotoMark):
                return mark.owner
        except RuntimeError:
            return None
        return None

    def _resolve_head(self, msg: dict) -> QtGui.QTextBlock | None:
        """这条消息的开头块：句柄和标记对不上就按标记重新找回来。

        截断重贴会动文档，存下的 QTextBlock 句柄可能指到别的块上——位置只认
        「块自己带着这条消息的标记」，不认旧句柄。
        """
        head = msg.get("head_block")
        mark = msg.get("mark")
        if head is not None and head.isValid():
            try:
                if head.userData() is mark:
                    return head
            except RuntimeError:
                pass
        if mark is None:
            return head if head is not None and head.isValid() else None
        probe = self.view.document().begin()
        while probe.isValid():
            try:
                if probe.userData() is mark:
                    msg["head_block"] = probe
                    return probe
            except RuntimeError:
                pass
            probe = probe.next()
        return None

    def _resolve_bar(self, msg: dict) -> QtGui.QTextBlock | None:
        """这条回答的动作块：句柄没指到带自己标记的块，就顺着开头块往后找回。"""
        block = msg.get("bar_block")
        if block is not None and block.isValid():
            try:
                mark = block.userData()
            except RuntimeError:
                mark = None
            if isinstance(mark, _BarMark) and mark.msg is msg:
                return block
        head = msg.get("head_block")
        if head is None or not head.isValid():
            return None
        probe = head.next()
        while probe.isValid():
            try:
                ud = probe.userData()
            except RuntimeError:
                ud = None
            if isinstance(ud, _BarMark) and ud.msg is msg:
                msg["bar_block"] = probe
                return probe
            if isinstance(ud, _MsgMark):
                return None
            probe = probe.next()
        return None

    def _msg_corner(self, msg: dict, usable: int, pad: int) -> QtCore.QRect | None:
        """这条消息在视口里的那块矩形（排版给的），按钮贴着它摆。

        有框的那条：横向取框的左右两边，纵向取框里第一行字自己的上下沿——按钮跟字站
        同一行，不会被框的高度带偏。
        """
        layout = self.view.document().documentLayout()
        offset = self.view.verticalScrollBar().value()
        if msg.get("role") == "user":
            head = self._resolve_head(msg)
            if head is None:
                return None
            frame = msg.get("frame")
            if frame is not None:
                box = layout.frameBoundingRect(frame).translated(0, -offset)
                fmt = frame.frameFormat()
                box = box.adjusted(
                    fmt.leftMargin(), fmt.topMargin(), -fmt.rightMargin(), -fmt.bottomMargin()
                )
                line = layout.blockBoundingRect(head).translated(0, -offset)
                return QtCore.QRect(
                    int(box.left()),
                    int(line.top()),
                    max(1, int(box.width())),
                    max(1, int(line.height())),
                )
            box = layout.blockBoundingRect(head).translated(0, -offset)
            return QtCore.QRect(
                int(box.left()), int(box.top()), max(1, int(box.width())), int(box.height())
            )
        block = self._resolve_bar(msg)
        if block is None:
            return None
        top = layout.blockBoundingRect(block).top() - offset
        return QtCore.QRect(0, int(top), usable - 4, self.BAR_PX - pad)

    def _usable_width(self) -> int:
        """可视宽：滚动条已显示时视口已经让过它；还没显示但内容要溢出时也要先让出来。"""
        bar = self.view.verticalScrollBar()
        room = self.view.viewport().width()
        if not bar.isVisible() and bar.maximum() > bar.minimum():
            room -= bar.sizeHint().width()
        return room

    def _msg_at_pos(self, pos: QtCore.QPoint) -> dict | None:
        """鼠标压在哪条消息上：问光标落在哪一块，再顺着块往前找它的开头。

        往前找而不是往后：鼠标压在消息的正文/工具块上要认成这条消息，压在两条消息之间
        的空白（第一条上面那种）就该是谁都不认，不能顺手把下一条算进来。
        """
        block = self._block_at(pos)
        while block is not None and block.isValid():
            msg = self._msg_of_block(block)
            if msg is not None:
                return msg
            block = block.previous()
        return None

    def _show_msg_bar(self, msg: dict | None) -> None:
        """只亮鼠标所在那条的动作条，其余收起来。"""
        if msg is self._bar_msg:
            return
        self._bar_msg = msg
        for item in self._msgs:
            for button in item.get("bar_buttons") or []:
                if item is msg:
                    button.show()
                    button.raise_()
                else:
                    button.hide()
        self._place_msg_bars()

    def _drop_msg_bar(self, msg: dict) -> None:
        for button in msg.get("bar_buttons") or []:
            button.hide()
            button.deleteLater()
        msg.pop("bar_buttons", None)
        msg.pop("bar_block", None)
        self._bar_buttons = [b for item in self._msgs for b in (item.get("bar_buttons") or [])]
        self._bar_msg = None

    def _destroy_elements(self) -> None:
        """元素按钮跟着元素走：文档被清或被截断，它们一起退场。"""
        for button in self._el_buttons.values():
            button.hide()
            button.deleteLater()
        self._el_buttons.clear()
        self._shown_buttons.clear()
        self._el_scan_pos = 0
        if self._sticky is not None:
            self._sticky.hide()

    def _destroy_msg_bars(self, keep_from: dict | None = None) -> None:
        """清窗口或截断时把动作条一并带走：它们是这一轮的对象，不留到下一轮。"""
        seen = keep_from is None
        for item in list(self._msgs):
            if item is keep_from:
                seen = True
            if seen:
                self._drop_msg_bar(item)

    def _copy_message(self, msg: dict) -> None:
        """复制这条消息的正文。"""
        text = str(msg.get("text") or "")
        if text:
            self._copy_to_clipboard(text)

    def _refresh_message(self, msg: dict) -> None:
        """刷新：拿这条回答前面那句原话重跑；后面还有问答就先问一句再删。"""
        user_msg = self._previous_user(msg)
        if user_msg is None or user_msg.get("row_id") is None:
            return
        if self._rewind_later_users(user_msg) > 0 and not ConfirmDialog.ask(
            self.window(),
            "恢复到此检查点？",
            "此提示之后的所有消息将从对话中移除，并从此处重新运行该提示。",
            "恢复并重新运行",
        ):
            return
        text = str(user_msg.get("text") or "")
        if self._busy:
            self._rewind_after_stop = (user_msg, text, True)
            self._stop_turn()
            return
        self._emit_rewind(user_msg, text, True)

    def _resend_message(self, msg: dict) -> None:
        """重新发送：从这条起把后面的删掉，按原话重跑。"""
        if msg.get("row_id") is None:
            return
        if not ConfirmDialog.ask(
            self.window(),
            "恢复到此检查点？",
            "此提示之后的所有消息将从对话中移除，并从此处重新运行该提示。",
            "恢复并重新运行",
        ):
            return
        text = str(msg.get("text") or "")
        if self._busy:
            self._rewind_after_stop = (msg, text, False)
            self._stop_turn()
            return
        self._emit_rewind(msg, text, False)

    def _hide_hover(self) -> None:
        self._hide_elements()
        self._zone = None

    def _copy_to_clipboard(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)

    def _open_diff_preview(self, key: int) -> None:
        source = self._diff_texts.get(key)
        if not source:
            return
        path, diff = source
        stats = count_diff_line_stats(diff)
        window = file_preview.DiffPreviewWindow(
            self,
            path or "完整 diff",
            self._diff_rows(diff, path=path, numbers=True),
            f"+{stats['added']} −{stats['removed']}",
        )
        window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        window.show()
        window.raise_()
        window.activateWindow()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """frame 铺满整个窗口，边缘那圈空白上的鼠标事件在这儿接住。"""
        scroll_owner = obj if isinstance(obj, ToolScroll) else None
        if scroll_owner is None and isinstance(obj.parent(), ToolScroll):
            scroll_owner = obj.parent()
        if scroll_owner is not None:
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseMove:
                self._hover_at(obj.mapTo(self.view.viewport(), event.position().toPoint()))
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Wheel):
                if kind is QtCore.QEvent.Type.Leave and self._over_hover_button():
                    return super().eventFilter(obj, event)
                self._hide_hover()
            return super().eventFilter(obj, event)
        if obj is self.view.viewport():
            kind = event.type()
            if kind is QtCore.QEvent.Type.Resize:
                QtCore.QTimer.singleShot(0, self._place_msg_bars)
            if kind is QtCore.QEvent.Type.MouseMove:
                point = event.position().toPoint()
                self._sync_edge_cursor(self.view.viewport().mapTo(self, point))
                self._hover_at(point)
                self._point_at_photo(point)
            elif kind is QtCore.QEvent.Type.MouseButtonPress:
                self._press_pos = event.position().toPoint()
            elif kind is QtCore.QEvent.Type.MouseButtonRelease and (
                event.button() is QtCore.Qt.MouseButton.LeftButton
            ):
                start = self._press_pos
                point = event.position().toPoint()
                still = start is not None and (point - start).manhattanLength() <= 5
                if still:
                    hit = self._photo_at(point)
                    if hit:
                        QtCore.QTimer.singleShot(0, lambda k=hit[0]: self._open_photo(k))
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Wheel):
                if kind is QtCore.QEvent.Type.Leave and self._over_hover_button():
                    return super().eventFilter(obj, event)
                self._hide_hover()
                self._show_msg_bar(None)
                self.view.viewport().unsetCursor()
                if kind is QtCore.QEvent.Type.Wheel:
                    self._user_scroll_now()
            elif kind is QtCore.QEvent.Type.Resize:
                self._resize_started()
                self._place_jump()
                self._place_msg_bars()
            return super().eventFilter(obj, event)
        if obj is self.view:
            kind = event.type()
            if kind is QtCore.QEvent.Type.KeyPress:
                if event.key() in _SCROLL_KEYS:
                    self._user_scroll_now()
                return super().eventFilter(obj, event)
            if kind is QtCore.QEvent.Type.Wheel:
                # 黏顶带占着视口上边那一条时，滚轮落在正文外面：事件目标是 view，
                # 先标上「用户自己滚的」松开跟随，再原样转给视口去滚。
                self._hide_hover()
                self._user_scroll_now()
                QtWidgets.QApplication.sendEvent(self.view.viewport(), event)
                return True
            return super().eventFilter(obj, event)
        if obj is getattr(self, "input", None):
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseMove:
                self._sync_edge_cursor(self.input.mapTo(self, event.position().toPoint()))
            if kind is QtCore.QEvent.Type.FocusIn:
                self._mark_input_focus(True)
            elif kind is QtCore.QEvent.Type.FocusOut:
                self._mark_input_focus(False)
            elif kind is QtCore.QEvent.Type.InputMethod:
                self._ime_composing = bool(event.preeditString())
                self._sync_input_ph()
            elif kind is QtCore.QEvent.Type.KeyPress:
                if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and not getattr(
                    self, "_ime_composing", False
                ):
                    if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                        return super().eventFilter(obj, event)
                    self._emit()
                    return True
                if self._is_paste_key(event):
                    mime = QtWidgets.QApplication.clipboard().mimeData()
                    plain_text = bool(
                        mime is not None
                        and not mime.hasUrls()
                        and mime.hasText()
                        and mime.text().strip()
                    )
                    if not plain_text and self._attach_from_clipboard():
                        event.accept()
                        return True
            elif kind in (
                QtCore.QEvent.Type.DragEnter,
                QtCore.QEvent.Type.DragMove,
                QtCore.QEvent.Type.Drop,
            ):
                if self._handle_input_drag(event):
                    return True
            elif kind is QtCore.QEvent.Type.Resize:
                self._fit_input_height()
            return super().eventFilter(obj, event)
        if getattr(self, "input", None) is not None and obj is self.input.viewport():
            if self._handle_input_drag(event):
                return True
            return super().eventFilter(obj, event)
        if obj is getattr(self, "_inputbox", None) and event.type() is QtCore.QEvent.Type.Resize:
            if self._mic_button.parent() is self._inputbox:
                self._place_mode_button()
            return super().eventFilter(obj, event)
        if obj is getattr(self, "_frame", None):
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseButtonPress:
                if event.button() is QtCore.Qt.MouseButton.LeftButton and self._begin_resize(
                    event.position().toPoint(), event.globalPosition().toPoint()
                ):
                    return True
            elif kind is QtCore.QEvent.Type.MouseMove and not (
                event.buttons() & QtCore.Qt.MouseButton.LeftButton
            ):
                self._sync_edge_cursor(event.position().toPoint())
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() is QtCore.Qt.MouseButton.LeftButton:
            if self._begin_resize(event.position().toPoint(), event.globalPosition().toPoint()):
                return
            if event.position().y() < 48:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        pos = event.globalPosition().toPoint()
        if (
            self._resize_edge
            and self._resize_start_size is not None
            and self._resize_start_global is not None
        ):
            max_w, max_h = self._max_size()
            size = size_from_drag_free(
                self._resize_edge,
                self._resize_start_size,
                pos - self._resize_start_global,
                min_width=MIN_PANEL_WIDTH,
                min_height=MIN_PANEL_HEIGHT,
                max_width=max_w,
                max_height=max_h,
            )
            self.setGeometry(rect_after_resize(self.geometry(), self._resize_edge, size))
            return
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._snapped = False
            self.move(pos - self._drag_offset)
            return
        self._sync_edge_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        resized = self._resize_edge is not None
        dragged = self._drag_offset is not None
        self._resize_edge = None
        self._resize_start_size = None
        self._resize_start_global = None
        self._drag_offset = None
        self.unsetCursor()
        self._edge_cursor_shape = None
        if dragged and not resized:
            self._snap_under_anchor()
        if resized or dragged:
            self._save_geometry()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self._global_ptt.stop()
        self._hold_end()
        self._save_geometry()
        super().closeEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802
        self._save_geometry()
        super().hideEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if self._voice_mode_on and self._ptt_matches(event):
            event.accept()
            if not event.isAutoRepeat():
                self._hold_begin()
            return
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if self._voice_mode_on and self._ptt_key_is(event):
            event.accept()
            if not event.isAutoRepeat():
                self._hold_end()
            return
        super().keyReleaseEvent(event)
