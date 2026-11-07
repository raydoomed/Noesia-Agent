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
from typing import Callable
from urllib.parse import unquote, urlparse

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

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
from app.hover_actions import DIFF_PREVIEW_LABEL, HoverActionButton, icon_pixmap
from app.approval_dialog import ConfirmDialog
from app.photo_view import PhotoView
from app.speech_bar import SpeechBar
from app.status_stack import StatusStack
from app.voice_input import SILENT_PEAK, VoiceRecorder, peak_of, write_wav
from app.voice_orb import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_THINKING, VoiceOrb
from app.tool_view.i18n import tr as tool_tr

logger = logging.getLogger(__name__)

# 引擎的错误文案常带动态片段（路径、端口、请求 id、时间戳）：整句相等去重会失效，
# 同一条错每次换个数字就刷满屏幕。按「去掉噪声后的正文」当指纹比较。
_ERROR_NOISE = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*|/(?:[\w.\-]+/)+[\w.\-]+|\b[0-9a-f]{8,}\b|\d+|\s+")


MAX_REVIEW_SUMMARIES = 100  # 一场对话里最多逐条贴这么多回顾灰字：超了就不再往文档里堆

# 正文区自己会拿来滚动的键：按下就算「用户自己在翻」，不能当成 Qt 的自动调整。
_SCROLL_KEYS = frozenset({
    QtCore.Qt.Key_Up,
    QtCore.Qt.Key_Down,
    QtCore.Qt.Key_PageUp,
    QtCore.Qt.Key_PageDown,
    QtCore.Qt.Key_Home,
    QtCore.Qt.Key_End,
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
        # cursorRect 宽只有 1px（就光标那么宽），直接拿它画会把字裁光：只取 y 和行高，宽度给满
        rect = self.cursorRect()
        rect.setWidth(max(0, self.viewport().width() - rect.x() - 2))
        painter.drawText(rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self._ph_text)
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
        icon = icon_pixmap("arrow-down", color.name(), self.ICON_PX)
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


# 这些标签 Qt 导入时会自己开新段（再 `insertBlock()` 就凭空多一个空段 = 一整行空白）。
# 实测（offscreen）：只有 列表 / 表格 / 分隔线 会开新段；`<h1>`-`<h6>`、`<blockquote>`、
# `<p>`、`<pre>` 一律并进当前段，连 headingLevel 都不留——所以它们得自己开段、自己画样式。
_SELF_BLOCK_TAGS = ("<ul", "<ol", "<table", "<hr")
_HEADING_RE = re.compile(r"^\s*<h([1-6])[^>]*>(.*)</h\1>\s*$", re.S | re.I)
_HEADING_PX = {1: 20, 2: 18, 3: 16, 4: 15, 5: 14, 6: 14}
_QUOTE_TAG = "<blockquote"
_QUOTE_COLOR = "#b9c0cf"  # 引用的字比正文淡一点：Qt 不认 <blockquote>，缩进和颜色得自己给


def _starts_own_block(markup: str) -> bool:
    """这段 HTML 是不是「Qt 会自己开段」的块级元素。"""
    return markup.lstrip().lower().startswith(_SELF_BLOCK_TAGS)


def _paint_range(cursor: QtGui.QTextCursor, start: int, end: int, char_format: QtGui.QTextCharFormat) -> None:
    """把字符格式刷到 [start, end) 这段已插入的文字上，刷完把光标放回末尾。"""
    if end <= start:
        return
    cursor.setPosition(start)
    cursor.setPosition(end, QtGui.QTextCursor.KeepAnchor)
    cursor.mergeCharFormat(char_format)
    cursor.clearSelection()
    cursor.movePosition(QtGui.QTextCursor.End)


def _plain_block_format() -> QtGui.QTextBlockFormat:
    """普通段落：只留 5px 上间距，不带上一段的底色/缩进（代码块的底色否则会渗下来）。"""
    block_format = QtGui.QTextBlockFormat()
    block_format.setTopMargin(5)
    # 行距 1.3：13px 的字配上这个松紧，一行一行读着不挤也不散
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
    if not core:  # 整段都是空格
        return "&nbsp;" * len(markup)
    return lead.replace(" ", "&nbsp;") + core + trail.replace(" ", "&nbsp;")


_PHOTO_CACHE = 24  # 最多记着多少张取回来的图：只挡重复下载，不是显示上限
_ORIGINAL_CACHE = 6  # 另外记着几张原图（字节），点开就能看清、能存；再多就别占内存了
_THUMB_WIDTH = 200  # 正文里的图按缩略图显示：看得清大概，又不把气泡撑满


def _photo_name(url: str) -> str:
    """从网址里挑个像文件名的做保存时的默认名。"""
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or "图片"
    if "." not in tail:
        tail += ".jpg"
    return tail


def save_photo_bytes(parent: QtWidgets.QWidget, data: bytes, name: str) -> bool:
    """问存哪儿、把原图字节写下去。存成了返回真，取消或者写失败返回假。"""
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        parent,
        "保存图片",
        name,
        "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;所有文件 (*)",
    )
    if not path:
        return False
    try:
        Path(path).write_bytes(data)
    except OSError as err:
        QtWidgets.QMessageBox.warning(parent, "存不下来", str(err))
        return False
    return True


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
        return True  # `![冰岛` —— 连说明都没写完
    opened = rest.find("(", closing)
    if opened < 0:
        return False  # `[1]` 这种引用标记，不是链接
    return ")" not in rest[opened:]  # `![说明](地址` —— 就等最后那个括号


from app.user_state import window_settings
from app.env_keys import env_file
from app.tool_view.ansi import has_ansi_codes as ansi_codes, parse_ansi
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

PANEL_WIDTH = 420  # 默认（第一次打开）大小；之后记在 QSettings 里
PANEL_HEIGHT = 460
MIN_PANEL_WIDTH = 280
MIN_PANEL_HEIGHT = 240
# 顶栏左边写的名字：窗窄到放不下长的那条，就换成短的，别让字顶到右边的胶囊
TITLE_FULL = "Noesia Agent"
TITLE_SHORT = "Noesia"
TITLE_FULL_MIN_WIDTH = 430

# USER.md 里的名字：文件没动就不重读（每条用户消息都要用它）
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


RESIZE_MARGIN = 12  # 距边缘多少像素内按下算缩放
SNAP_DISTANCE = 24  # 拖到离她下缘这么近就自动吸过去
# 不可用的模型项：置灰（setEnabled(False) 只是不能选，颜色还得自己给，
# 因为下拉列表整体设了 color，禁用态不会自动变淡）

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


# 审批图标按档位着色：不看 tooltip 也能一眼看出「现在问不问」，off 用红色提醒
APPROVAL_TINT = {"manual": "#ffcf8a", "smart": "#e8e8ef", "off": "#ff9a9a"}

# 思考段的三档抬头，照引擎中文语言包（assistant.thread.thinking / thought / thoughtBriefly / thoughtFor）
_THINKING_LABEL = "思考中"

# 审批档位的文案，照引擎中文语言包（shell.approvalMode：label · description）
_APPROVAL_MODES = (
    ("manual", "手动 · 执行需要审批的操作前询问"),
    ("smart", "智能 · 自动评估操作，并在需要时询问"),
    ("off", "关闭 · 不显示审批提示，直接运行"),
)

# 输入框最多长到几行，再多就在框里滚动。
# 窗口只有 360 宽时一行放不下几个字，行数给多了整条输入区会吃掉聊天区。
INPUT_MAX_LINES = 5

# 思考档位清单，
# 中文标签照 `shell.modelOptions`。`none` 不是一档，是「关掉思考」，只在模型允许关时才摆。
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


def _format_elapsed(seconds: int) -> str:
    """照桌面端 `formatElapsed`：不到一分钟写「12s」，再长写「1:05」。"""
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _thought_label(seconds: float | None) -> str:
    """思考段收尾后的抬头：已思考 / 思考了片刻 / 思考了 3s（跟桌面端同一套分档）。"""
    if seconds is None:
        return "已思考"
    if seconds < 1:
        return "思考了片刻"
    return f"思考了 {_format_elapsed(int(seconds))}"


def _glyph_pixmap(kind: str, color: str, size: int = 14) -> QtGui.QPixmap:
    """画 18px 网格里的线条图标（2x 采样，缩放后边缘不发虚）。

    自己画而不是塞 emoji：emoji 在各家 Windows 上字形/色板都不一样，线条图标
    跟这套深色界面更配，也不用带图片资源。
    """
    scale = 2
    pix = QtGui.QPixmap(size * scale, size * scale)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.scale(scale * size / 18.0, scale * size / 18.0)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
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
        # 厚圈 + 八根短齿：13~16px 上都还看得出是齿轮，细齿会糊成一朵太阳
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
        for radius in (2.4, 4.4):  # 两道声波
            painter.drawArc(
                QtCore.QRectF(9.2 - radius, 9.0 - radius, radius * 2, radius * 2),
                -50 * 16,
                100 * 16,
            )
    elif kind == "mic":
        # 话筒：头 + 支架弧 + 杆。语音输入用这个，跟上面 voice（喇叭）不是一回事
        painter.drawRoundedRect(QtCore.QRectF(6.2, 2.4, 5.6, 8.2), 2.8, 2.8)
        painter.drawArc(QtCore.QRectF(4.8, 5.4, 8.4, 7.4), 180 * 16, 180 * 16)
        painter.drawLine(QtCore.QPointF(9.0, 12.8), QtCore.QPointF(9.0, 15.4))
    elif kind == "keyboard":
        # 键盘：外框 + 一排按键 + 空格键。14px 里塞两排会糊成一片，只留一排
        painter.drawRoundedRect(QtCore.QRectF(2.6, 4.6, 12.8, 8.8), 1.6, 1.6)
        for x in (5.2, 7.8, 10.4, 13.0):
            painter.drawPoint(QtCore.QPointF(x, 8.2))
        painter.drawLine(QtCore.QPointF(7.0, 10.6), QtCore.QPointF(11.0, 10.6))
    elif kind == "keycap":
        # 一个键帽：表示「按住说话的键」，跟整把键盘那颗切换键区分开
        painter.drawRoundedRect(QtCore.QRectF(3.2, 5.0, 11.6, 8.0), 2.2, 2.2)
        painter.drawLine(QtCore.QPointF(7.2, 9.0), QtCore.QPointF(10.8, 9.0))
    else:  # spark：思考档位
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


# 输入框那排的语音三档：静音（不念）/ 纯语音（现在的朗读）/ 语音互动（数字人实时说话）。
# 纯语音和语音互动是同一档位里的两个选项，永远互斥。
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


# 「＋」和粘贴按扩展名分：这些当图片走 `image.attach`，其余走 `file.attach`。
# 表跟引擎的 `cli._IMAGE_EXTENSIONS` 同口径——界面进程 import 不到引擎包，只能自己维护一份；
# 引擎那边加格式时 media_links 里必须一起加，否则新格式会被界面当普通文件送出去（模型就看不见图了）。
IMAGE_SUFFIXES = media_links.IMAGE_SUFFIXES  # 口径只留一份（定义在 media_links）：贴图与「图片/文件」分类不能两套

# 附件在消息里是独占一行的标记：`@image:<路径>`（引擎存历史时补在正文后）、`@file:<工作区相对路径>`
# （发文件时拼在正文前，跟桌面 buildContextText 一致），带空格的值用反引号/引号包住；历史里还可能夹
# 一行 `[screenshot]` 占位。这些是给界面看的标记、不是正文，显示时必须摘出来还原成附件卡片。
# 规则照引擎 session_history.py（界面进程 import 不到引擎包，只能照抄一份）。
_REF_LINE = re.compile(r"@(image|file|folder|url):(`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)\Z")
_SCREENSHOT_PLACEHOLDER = "[screenshot]"

# 引擎以「用户消息」/「回答」的形式回灌给模型的机器行：display_kind 标了种类。
# 这些原文是给模型看的英文脚手架，界面上一个字都不能照搬——照搬的结果就是他截的那张图：
# 一段 `[System note: Your previous turn was interrupted mid-run …]` 像是我说的话，
# 三句 `Your request was not processed…` 像是我的回答。
_NOTICE_LABELS = {
    "auto_continue": "上次那一轮被中断了，引擎重启后自己接着跑完",
    "failed_turn": "这一轮没跑成：引擎没处理，要的话把话重发一次",
}
# 子代理完成那条不是固定文案：正文交给 delegate_view 解析成中文说明行
_DELEGATION_DISPLAY_KIND = "async_delegation_complete"
# 事故恢复注记末尾会带上「原来那句话是什么」，摘出来接在中文说明后面
_AUTO_CONTINUE_REQUEST = "The interrupted request was:]"

# 引擎把 @file:/@folder:/@url: 展开出来的内容补在正文后面这一段里（那是喂给模型看的，不是用户写的话）。
# 显示时切开：标记之后整段丢掉，里面提到的引用只补回「正文里没有的」那几条。
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
            if Path(ref).exists():  # 图没了就只剩正文，不留一串死路径
                items.append({"kind": "image", "name": Path(ref).name, "path": ref})
        elif kind == "url":
            items.append({"kind": "url", "name": ref})
        else:
            items.append({"kind": kind, "name": Path(ref).name or ref})
    return "\n".join(lines).strip(), items



# 「＋」里的「提示词片段…」——三条起始提示，文案跟桌面端中文包一致
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
    text = sequence.toString(QtGui.QKeySequence.NativeText)
    if text in ("Space", ""):
        return "空格"
    # Qt 管 Windows 键叫 Meta，中文界面上没人认得这个称呼
    return text.replace("Meta", "Win").replace("Space", "空格")


def _to_native(sequence: QtGui.QKeySequence) -> tuple[int, int] | None:
    """Qt 的键序列 → (修饰位, 虚拟键码)。钩子只认这两个。"""
    if sequence.count() != 1:
        return None
    combo = sequence[0]
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
    # 小键盘是另一套键码，先认掉
    if flags & QtCore.Qt.KeyboardModifier.KeypadModifier:
        return (mods, 0x60 + ord(name) - 0x30) if name in "0123456789" else None
    vk = _qt_key_to_vk(name)
    return (mods, vk) if vk is not None else None


# Qt 的键值只在字母、数字、空格这几段恰好等于 Win32 虚拟键码。标点、
# 方向键、Delete 全都不同号：硬套会把 Ctrl+/ 当成 VK_HELP(47)、Ctrl+. 当成
# VK_DELETE(46)，按下去没反应还顺手把 Ctrl+Delete 吞掉。下面是固定对应关系。
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
_VK_TO_QT_NAME[0x0D] = "Return"   # Enter 和 Return 同码，取常用的那个名


# 修饰键在系统里成对出现（左右键码不同），任一按着就算这个修饰键按着
# 一次按住最多这么久。正常人一口气说话到不了两分钟；到了还没收尾就是松开事件丢了，
# 这是不依赖键态、不依赖事件的最后一道出口，保证话筒不会一直开着。
_HOLD_MAX_SECONDS = 120.0

# 位用的是 `_to_native` 拼出来的那套（global_hotkey.MOD_*），不是 Qt 的枚举：
# PySide6 的 KeyboardModifier 连 int() 都不给转，混着比永远对不上（还会在心跳里抛 TypeError）。
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
    if 0x60 <= vk <= 0x69:                       # 小键盘 0-9
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
        # 改之前的原样留着：没动的那些要原封不动带回去
        self._before: dict[str, QtGui.QKeySequence | None] = {
            "window": window_current,
            "global": global_current,
        }
        self._pending: dict[str, QtGui.QKeySequence | None] = dict(self._before)
        self._target = "window"
        self._drag_offset: QtCore.QPoint | None = None
        self._caps: dict[str, QtWidgets.QLabel] = {}
        self._rows: dict[str, QtWidgets.QFrame] = {}

        # 改键必须靠低层钩子收键：Win+空格、Alt+Tab 这类系统保留组合会被系统
        # 直接接走，Qt 的 keyPressEvent 根本不来，只靠窗口事件等于没反应
        self._capture = global_hotkey.GlobalHotkey(self)
        self._capture.captured.connect(self._on_captured)

        # 跟审批卡片、引擎卡片同一套：无边框圆角、压在最上面、不进任务栏
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)  # 圆角外不留一块方底
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setStyleSheet(_PTT_DIALOG_QSS)

        card = QtWidgets.QFrame(self)
        card.setObjectName("pttcard")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 18)
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
        close.setCursor(QtCore.Qt.PointingHandCursor)
        close.setFixedSize(20, 20)
        close.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.reject)
        head.addWidget(close)
        lay.addLayout(head)
        lay.addSpacing(10)

        # 两条键槽：点哪条，接下来按的键就记给哪条
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
        cancel.setCursor(QtCore.Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._ok = QtWidgets.QPushButton("确定", card)
        self._ok.setObjectName("primary")
        self._ok.setCursor(QtCore.Qt.PointingHandCursor)
        self._ok.clicked.connect(self.accept)
        row.addWidget(self._ok)
        lay.addLayout(row)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.adjustSize()
        self.setFixedWidth(340)

        # 按钮不吃焦点：空格、回车都要落回弹窗自己身上，不然按空格等于点「取消」
        cancel.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._ok.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._set_target("window")

    def _make_row(self, card: QtWidgets.QWidget, key: str, name: str) -> QtWidgets.QFrame:
        """一条键槽：左边是哪里的键，右边是键名。整条可点。"""
        frame = QtWidgets.QFrame(card)
        frame.setObjectName("pttrow")
        frame.setCursor(QtCore.Qt.PointingHandCursor)
        line = QtWidgets.QHBoxLayout(frame)
        line.setContentsMargins(10, 4, 10, 4)
        line.setSpacing(12)

        tag = QtWidgets.QLabel(name, frame)
        tag.setObjectName("pttrowname")
        tag.setMinimumWidth(52)
        line.addWidget(tag)

        cap = QtWidgets.QLabel(card)
        cap.setObjectName("pttcap")
        cap.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
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

        # 两条设成同一个键：按下去分不清该走哪个，这是真冲突不是规则
        if window is not None and globe is not None and window == globe:
            return "block", "两条键不能一样，按下去分不清是哪一个"

        native = _to_native(globe) if globe is not None else None
        if native is None:
            # 转成虚拟键码这一步失败的键（界面上的名字翻不出键码）装不上钩子，
            # 这里不拦就会存进去，然后系统级按住说话静默失效
            return "block", "这个键当系统级用不了，换成字母、数字、F 键或标点"
        mods, vk = native
        if mods == 0:
            # 这是钩子的物理限制，不是规则：不带修饰键就分不清「要说话」和「在打字」
            return "block", "系统级的键必须带修饰键（Ctrl / Alt / Shift / Win），不然分不清是在打字还是要说话"
        ok, why = global_hotkey.probe(mods, vk)
        if ok:
            return "", ""
        # 被占用就别存：存下去会把那个程序的快捷键抢掉，这是坏行为不是本事
        return "block", why

    def _refresh(self) -> None:
        """确定键能不能按、下面那行提示说什么。"""
        level, warn = self._conflict()
        changed = self._pending != self._before
        self._ok.setEnabled(changed and level != "block")
        if warn:
            # 冲突优先说：哪怕这次一个字没改，当前这个键也用不了
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
        self.setFocus()   # 一打开就能直接按键
        # 改键期间把系统级的钩子停了：不然按一下 Shift+空格 当场就开始录音
        panel = self.parent()
        running = getattr(panel, "_global_ptt", None)
        if running is not None:
            running.stop()
        self._capture.start_capture()

    def done(self, result: int) -> None:
        """改键结束（确定、取消、Esc、点外面关掉都走这里）先卸钩子。"""
        self._capture.stop_capture()
        super().done(result)
        # 语音模式还开着就把系统级键按新配置重新装上
        panel = self.parent()
        if getattr(panel, "_voice_mode_on", False):
            apply = getattr(panel, "_apply_global_ptt", None)
            if apply is not None:
                apply()

    def _on_captured(self, mods: int, vk: int) -> None:
        """钩子收到一个键：跟窗口事件走同一套——填进当前那行，再看冲不冲突。"""
        # 弹窗还开着但人已经切到别的程序去了：那边按的键不算，不然会把
        # 不相干的键绑进来。不用 isActiveWindow()——无边框窗口上它恒为 False，
        # 会把正常改键也一起拦死，得直接问系统现在前台是谁。
        if ctypes.windll.user32.GetForegroundWindow() != int(self.winId()):
            return
        sequence = _from_native(mods, vk)
        if sequence is None:
            return
        # 回车留给「确定」，不当成要绑的键
        if vk in (0x0D, 0x0A):
            return
        self._pending[self._target] = sequence
        self._paint_cap(self._target)
        self._refresh()

    # 无边框卡片得自己处理拖动，不然挡住内容也挪不开；按钮会吃掉自己的按下事件
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == QtCore.Qt.Key_Escape:
            self.reject()
            return
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            # 回车留给「确定」：它不能当按住说话的键
            if self._pending != self._before and self._conflict()[0] != "block":
                self.accept()
            return
        if key in (
            QtCore.Qt.Key_Shift,
            QtCore.Qt.Key_Control,
            QtCore.Qt.Key_Alt,
            QtCore.Qt.Key_Meta,
        ):
            return   # 光按修饰键不算，等真正的键
        if event.isAutoRepeat():
            return
        sequence = QtGui.QKeySequence(event.keyCombination())
        if not sequence.toString(QtGui.QKeySequence.PortableText):
            return   # 这个键翻不成名字，当没按
        self._pending[self._target] = sequence
        self._paint_cap(self._target)
        self._refresh()

    def window_sequence(self) -> QtGui.QKeySequence | None:
        """窗口内确认下来的键。"""
        return self._pending["window"]

    def global_sequence(self) -> QtGui.QKeySequence | None:
        """系统级确认下来的键。"""
        return self._pending["global"]


# ---- 面板的透明度 ----
# 这里原本想挂系统磨玻璃（DWM acrylic / Mica），实测在无边框 + 逐像素透明的 Qt 窗上
# 都不生效：系统只铺一层固定的灰（Mica=32、Acrylic=84），既不随窗口挪动，也不是背后
# 的画面。要“看得出透明、跟着桌面走”，只能让面板自己真透明 —— 代价是没有模糊。


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

    submitted = QtCore.Signal(str, str)  # (正文, 来源)：来源区分打字和语音，语音结果不算干净语料
    model_chosen = QtCore.Signal(str)  # 下拉框选了模型 → 交给窗口发 SET_MODEL
    models_refresh_requested = QtCore.Signal()  # 菜单里的「刷新模型」→ 让窗口重问一次引擎
    # 「设置」：还没配模型（或下拉框里全是灰的）时的明确入口，不能让人对着一个空下拉框猜
    model_settings_requested = QtCore.Signal()
    # 「新会话」：人设（SOUL.md）是引擎在建 agent 时读进系统提示的，换了人设要新会话才生效
    new_session_requested = QtCore.Signal()
    approval_requested = QtCore.Signal(str)  # 输入框旁的审批档位：点了哪一档就发哪个
    stop_requested = QtCore.Signal()  # 跑着的时候按「停止」，叫住这一轮
    speech_stop_requested = QtCore.Signal()  # 语音进度条上那个停止：只掐朗读，不动这一轮
    voice_mode_changed = QtCore.Signal(str)  # 语音档位换了（静音/纯语音/互动）：旧的语音提示要跟着撤
    # 语音输入：录完一段，把 WAV 路径发出去让引擎识别；结果由窗口那边回灌给 apply_voice_transcript
    voice_transcribe_requested = QtCore.Signal(str)
    # 按下说话：窗口那边先掐掉正在念的那句，不然麦克风收的是她自己的声音
    voice_record_started = QtCore.Signal()
    # 进了语音模式：窗口那边问一句识别模型在不在（引擎顺手预热）
    voice_input_opened = QtCore.Signal()
    reasoning_requested = QtCore.Signal(str)  # 思考档位：选哪一档发哪个（空串 = 不指定）
    attachment_picked = QtCore.Signal(list)  # 按发送那一刻才把附件交给引擎（选的时候只留在界面上）
    attachment_failed = QtCore.Signal(str)  # 某个附件没传上去：从待发送条里撤掉
    clarify_answered = QtCore.Signal(str, str, str)  # (request_id, qid, 答案)：她要的那个选择
    clarify_skipped = QtCore.Signal(str)  # reply_id：这次提问整个跳过
    process_kill_requested = QtCore.Signal(str)  # 后台进程那一行点了「停止」
    # 重新生成 / 编辑重发：把要重来的那条和正文交给窗口，由它去引擎把这段历史截掉再发
    # (消息, 正文, 是否只是重新生成, 是否整场清空, 后面还有别的问答要一起删)
    resubmit_requested = QtCore.Signal(object, str, bool, bool, bool)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        settings: QtCore.QSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(MIN_PANEL_WIDTH, MIN_PANEL_HEIGHT)
        self.setMouseTracking(True)
        self._settings = settings or window_settings()
        self._streaming = False
        self._drag_offset: QtCore.QPoint | None = None
        self._anchor: QtCore.QRect | None = None
        self._snapped = False  # 吸在她下面没有：有的话她挪一步它跟一步  # 头像窗当前在屏幕上的位置（吸附用）
        # 缩放状态：命中的边、按下时的尺寸与全局坐标
        self._resize_edge = None
        self._resize_start_size: QtCore.QSize | None = None
        self._resize_start_global: QtCore.QPoint | None = None

        frame = QtWidgets.QFrame(self)
        frame.setObjectName("frame")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(TITLE_FULL, frame)
        title.setObjectName("title")
        state = QtWidgets.QLabel("", frame)
        state.setObjectName("state")
        dot = QtWidgets.QLabel("●", frame)
        dot.setObjectName("dot")
        dot.setToolTip("在线")
        hint_label = QtWidgets.QLabel("Enter 发送 · Esc 收起", frame)
        hint_label.setObjectName("hint")
        header.addWidget(title)
        header.addWidget(dot)
        header.addWidget(state)
        header.addStretch(1)
        # 「新会话」放顶栏、不发在底部：底部最右是「发送」，两颗键上下正对，错一下就清了上下文
        new_session = QtWidgets.QPushButton("新会话", frame)
        new_session.setObjectName("mini")
        new_session.setToolTip("开一段新对话：换回历史里不带的干净上下文（改了人设、SOUL.md 之后用它生效）")
        new_session.setCursor(QtCore.Qt.PointingHandCursor)
        new_session.clicked.connect(self.new_session_requested.emit)
        layout.addLayout(header)
        self._state_label = state
        self._title_label = title

        # 模型切换：并进顶栏一行（名字 · 在 ······ 模型胶囊 · 设置），不再单独占一行
        combo = QtWidgets.QToolButton(frame)
        combo.setObjectName("model")
        combo.setToolTip("点开按供应商分组选模型（右键头像 → 模型 也能选）；「设置」里填密钥")
        combo.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        combo.setCursor(QtCore.Qt.PointingHandCursor)
        combo.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        combo.setText("模型")  # 引擎还没回报清单时的占位，有值后会被覆盖；空着就是个没字的方块
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
        set_model.setFixedSize(26, 20)  # 只留图标，省下的宽度给「新会话」
        set_model.setToolTip("设置：选模型；没有可用的就告诉你密钥该放在哪个文件")
        set_model.setCursor(QtCore.Qt.PointingHandCursor)
        set_model.clicked.connect(self.model_settings_requested.emit)
        header.addWidget(set_model)
        self._model_button = combo
        self._model_menu = menu
        self._models: list[dict] = []
        self._models_refreshing = False  # 刷新请求发出去了、清单还没回来
        self._current_model_id = ""  # 菜单里勾着的那一个（刷新时重画要保留）
        self._refresh_button: QtWidgets.QPushButton | None = None  # 菜单底部那颗「刷新模型」

        # 当前这条回答里是否已经落下正文（用来掐掉开头的空行）
        self._bubble_has_text = False
        # 一轮是否在跑：在跑时「发送」变成「停止」
        self._busy = False
        self._interrupted = False  # 这一轮被叫停过：之后还飘回来的字一律不进气泡
        self._assistant_pending = ""  # 还没落进窗口的半行（`MEDIA:` 可能被流式切两半）
        self._last_error_key = ""  # 上一条错误的指纹：同一条只留一条，别刷屏
        self._review_count = 0  # 这场对话里贴过多少条回顾灰字
        self._review_folded = False  # 超限之后只提示一次
        self._reasoning_pending = ""  # 思考增量先攒着，成段再落（见 flush_reasoning）
        self._md_block: list[str] = []  # 攒着的结构行（列表 / 表格 / 代码块）
        self._para_break = False  # 空行记的账：下一段来时再开新段（当场开就是白留一行）
        self._md_fence = False  # 是否在 ``` 围栏代码块里
        self._media_roots: tuple = ()  # 相对路径从哪些目录找（由窗口按配置塞进来）
        # 正文里的网络图片：Qt 自己不下载，得先取回来注册成文档资源才画得出来
        self._photos: dict[str, QtGui.QPixmap] = {}  # 网址 → 取回来的图
        self._photo_keys: dict[str, str] = {}  # 网址 → 文档里的资源名
        self._photo_sources: dict[str, str] = {}  # 资源名 → 网址：点图时要知道点的是哪张
        self._originals: dict[str, bytes] = {}  # 网址 → 原图字节：留着才能看原图、存原图
        self._photo_pending: set[str] = set()  # 正在取的，别重复发
        self._press_pos: QtCore.QPoint | None = None  # 正文里按下的位置：松手时判断是点击还是拖选
        self._net = QtNetwork.QNetworkAccessManager(self)
        # 思考段 / 工具行都是「可折叠的一段」（见 app/disclosure.py）
        self._reason_fold: disclosure.Fold | None = None  # 正在攒的思考段
        self._reason_started = 0.0  # 这段思考从什么时候开始（算「思考了 3s」）
        self._tool_folds: dict[str, disclosure.Fold] = {}  # tool_id → 工具行，回来时补结果
        self._tool_images: dict[str, str] = {}  # 每个工具块贴过的图：同一张只贴一次
        self._tool_term_drawn: dict[str, bool] = {}  # 终端转录画过没：跑着/跑完两帧都带同一条命令
        self._tool_detail_seen: dict[str, tuple] = {}  # 详情画过没：同样的正文不重复贴
        # 子代理卡片：tool_id → 卡片状态（fold / 已经贴过的活动行），`subagent.*` 事件往上刷
        self._delegate_cards: dict[str, dict] = {}
        self._delegate_latest: str = ""  # 最近一张还在跑的子代理卡片
        self._clarify: dict | None = None  # 引擎正在问的那个问题（一张卡、可点选项）
        # 还在跑的工具行要显示跑了多久：画第一笔时记下起点，之后每秒重标一次。
        self._tool_started: dict[str, tuple[object, str, float, str | None]] = {}
        self._tool_clock = QtCore.QTimer(self)
        self._tool_clock.setInterval(1000)
        self._tool_clock.timeout.connect(self._tick_running_tools)
        self._icon_cache: dict[tuple, QtGui.QPixmap] = {}  # 抬头小图标：同一图标同一颜色只渲一次
        self._icon_registered: set[str] = set()  # 已登记进文档资源的图标 URL
        self._fence_cache: dict[tuple[int, int], str] = {}  # 代码围栏整块文本：同一块只拼一次
        self._image_seq = 0  # 附件图片的资源名序号：名字+尺寸会撞
        self._clarify_gen = 0  # 提问卡的代际：旧卡的锚点靠它认出自己已作废
        # 桌面端照着引擎的两把开关画：show_reasoning 关掉就不画思考；
        # tool_progress=off 时引擎本来就不发 tool 帧，这里再挡一道是不让它露出来
        self._show_reasoning = True
        self._tool_progress = "all"
        self._provider_wait = ""  # 引擎在等供应商时那句中英文提示（底部状态行）

        # QTextBrowser 而不是 QTextEdit：附件行是 <a href="file://...">，只有它会在点击时发 anchorClicked
        self.view = ChatView(frame)
        self.view.setObjectName("view")
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)  # 不自己跳转：点开是「用默认程序打开文件」，不是窗口内导航
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._on_anchor)
        # 正文一滚，贴在某一行上的按钮就对不上字了：直接收掉，鼠标再过来时重摆
        self.view.scrolled.connect(self._hide_hover)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        # 横向不出条：正文一律按视口宽折行（代码块也是，见 markdown_render._unwrap_pre），
        # 差一两像素的取整误差也不许在底下弹一条——这个位置以前就因为这个被嫌弃过。
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.view, 1)

        # 折叠段的字体：抬头是一行暗色小字（可点），正文再暗一档；工具正文用等宽
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

        # 悬停才出现的小动作：两个**各自独立**的按钮——复制（点完换 ✓）与「看完整 diff」
        # （只开窗，不冒充复制成功）。靠 CSS 的 group-hover，Qt 没有，所以自己盯鼠标摆位。
        self._hover_copy: HoverActionButton | None = None
        self._zone: tuple | None = None  # 鼠标当前在哪个区域，换区才换按钮
        self._hover_expand: HoverActionButton | None = None
        self._hover_download: HoverActionButton | None = None
        # 消息级悬停条：复制整段 / 引用 / 编辑重发 / 重新生成（按角色与位置决定露哪几个）
        self._msgs: list[dict] = []  # 这个窗口里的消息，按出现顺序（截断/清屏时跟着动）
        self._stream_msg: dict | None = None  # 正在流式写的那条回答
        # 悬停条只有一颗按钮，两种角色（用户/回答）轮着用它
        self._msg_buttons: list[HoverActionButton] = []  # 悬停条：回答那条是「复制 / 刷新」，用户那条一颗
        self._reserve_tail_space()
        self._msg_band: QtCore.QRect | None = None  # 悬停条所在那一带（鼠标在里面就别收）
        self._msg_shown: dict | None = None  # 悬停条现在属于哪条消息（窗口一缩放就得重新摆）
        self._edge_cursor_shape = None  # 现在贴在面板上的边缘光标对应哪条边（None = 没设过）
        self._rewind_waiting = False  # 截断重发已经交给引擎，等它真接下来（这期间不许再发）
        self._rewind_after_stop: tuple[dict, str] | None = None  # 流式中点了重新发送：先叫停，停稳了再重来
        self._row_stamp_pending: int | None = None  # 截断重发：行号比正文先回，先存着等新消息贴上去
        self._jump: _JumpPill | None = None  # 没看完时底下那颗「滚动到底部」
        # 跟不跟随新内容：状态位，不是「此刻位置」（流式一快，位置永远追不上最大值，
        # 按位置判就会一直判成「翻着看」，然后彻底不再跟）。用户往上翻才熄，回到底才亮。
        self._follow = True
        self._self_scroll = False  # 自己滚的这会儿，valueChanged 不算用户意图
        # 用户真在动手滚（滚轮 / 拖条 / 点箭头 / 方向键）：只有这种位置变化才算「往上翻」。
        # Qt 自己在 show()、排版变高、窗口缩放时也会挪位置，那不是用户的意思，不能拿它熄跟随。
        self._user_scroll = False
        self._range_changing = False  # 排版刚变过、滚动条范围跟着变：这期间的位移是 Qt 挪的
        self._restoring = False  # 正在整窗重铺历史：逐条插入不滚、不摆胶囊，铺完一次到位
        self._diff_texts: dict[int, tuple[str | None, str]] = {}  # 折叠段 key → (路径, 整段 diff)
        self._tool_payloads: dict[int, tuple[str, str]] = {}  # 折叠段 key → (复制按钮文案, 待复制的文本)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.installEventFilter(self)  # 方向键 / PageUp 也是用户自己滚的
        bar = self.view.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.sliderMoved.connect(self._user_scroll_now)  # 拖着滚动条
        bar.actionTriggered.connect(self._user_scroll_now)  # 点箭头 / 点条 / 翻页动作
        bar.rangeChanged.connect(self._range_changed)  # 范围变了 = 刚刚重新排版
        self.view.document().contentsChanged.connect(self._replace_msg_actions)  # 流式还在长，条得跟着尾巴走

        # 输入框正上方那一叠状态（桌面端 composer 的 status-stack）：引擎的提示、还在跑的后台
        # 进程、正在生成的工具调用。没有内容时整块藏着，不占高度。
        self.stack = StatusStack(frame)
        self.stack.kill_requested.connect(self.process_kill_requested.emit)
        layout.addWidget(self.stack)

        # 念出来那条进度：常驻在输入框正上方，没人念的时候自己藏起来（和桌面端同一个位置）
        self.speech_bar = SpeechBar(frame)
        self.speech_bar.stop_requested.connect(self.speech_stop_requested.emit)
        layout.addWidget(self.speech_bar)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        # 审批 / 思考放进输入框里做成图标：窗口拉到最窄时它们各只占一个图标宽，
        # 不会再挤掉输入区域（原来在框外，窄了就被压成「批 sma」「思考 默」）。
        self._inputbox = box = QtWidgets.QFrame(frame)
        box.setObjectName("inputbox")
        box_row = QtWidgets.QHBoxLayout(box)
        box_row.setContentsMargins(9, 0, 4, 0)
        box_row.setSpacing(2)

        # 「＋」加附件：图片、文档都行。选完立刻交给引擎上传，发送时只发文字。
        self._attach_button = QtWidgets.QToolButton(box)
        self._attach_button.setObjectName("icon")
        self._attach_button.setIcon(_glyph_icon("plus"))
        self._attach_button.setIconSize(QtCore.QSize(14, 14))
        self._attach_button.setFixedSize(22, 22)
        self._attach_button.setToolTip("加附件（图片、文档；也可以直接粘贴或拖进来）")
        self._attach_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._attach_menu = QtWidgets.QMenu(self._attach_button)
        self._attach_menu.setObjectName("modelmenu")  # 跟审批/档位菜单同一套样式
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
        hint.setEnabled(False)  # 只是提示，不是能点的项
        self._attach_button.clicked.connect(self._show_attach_menu)
        box_row.addWidget(self._attach_button)

        # 输入区在最左，两个图标键贴右边（按钮放输入框右侧，不放左侧）
        # 多行输入：Enter 发送、Shift+Enter 换行；写到换行才长高，最多 5 行，再多内部滚动。
        self.input = _Input(box)
        self.input.setObjectName("input")
        self.input.set_placeholder("和 Noesia 说点什么…")  # 自己画，见 _Input 的说明
        self.input.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.input.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.input.setTabChangesFocus(True)            # Tab 用来换焦点，不往正文里塞制表符
        self.input.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.input.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # 文档默认有 4px 边距，会让字比外壳往里缩一格；单行框时对齐的是 padding，这里抹平
        self.input.document().setDocumentMargin(0)
        self.input.installEventFilter(self)  # 焦点高亮 / Enter 发送 / 粘贴接附件
        # 拖放要装在视口上：多行控件是 QAbstractScrollArea，拖进来的目标其实是 viewport，
        # 挂在控件本身收不到（单行框不是滚动区，所以以前挂在上面就够）
        self.input.viewport().installEventFilter(self)
        self.input.setAcceptDrops(True)
        self.input.textChanged.connect(self._fit_input_height)
        box_row.addWidget(self.input, 1)
        self._ime_composing = False   # 输入法正在组字：这时回车是确认候选词，不是发送
        self.input.textChanged.connect(self._sync_input_ph)
        self.input.textChanged.connect(self._sync_send_button)  # 框里有字，「停止」变「排队」
        self._sync_input_ph()
        self._fit_input_height()

        # 语音模式的输入区：跟输入框抢同一个位置，同一时刻只露一个。
        # 切模式只做 hide/show，不销毁输入框——草稿、光标位置、焦点都在控件自己身上，
        # 藏起来不会丢，切回来还是原样。
        # 球按居中摆：语音模式下其余按钮都藏起来，整条输入区就剩这一颗球。
        self._box_row = box_row
        self._voice_orb = VoiceOrb(box)
        self._voice_orb.hide()
        self._voice_orb.pressed.connect(self._voice_hold_start)
        self._voice_orb.released.connect(self._voice_hold_end)
        box_row.addWidget(self._voice_orb, 1, QtCore.Qt.AlignCenter)

        # 审批档位：写一句话之前先知道「危险命令要不要问我」。档位写在 tooltip 上。
        self._approval_button = QtWidgets.QToolButton(box)
        self._approval_button.setObjectName("icon")
        self._approval_button.setIcon(_glyph_icon("shield"))
        self._approval_button.setIconSize(QtCore.QSize(14, 14))
        self._approval_button.setFixedSize(22, 22)
        self._approval_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._approval_button.setToolTip("审批档位：还没问到引擎")
        self._approval_menu = QtWidgets.QMenu(self._approval_button)
        self._approval_menu.setObjectName("modelmenu")
        self._approval_menu.setStyleSheet(STYLE)
        # 一档只勾一个：照桌面端那组 radio（`DropdownMenuRadioGroup`）。不分组的话每点一次就多一个勾，
        # 点过三档就是三个勾——看起来「三档都选上了」。
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
        self._approval_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        box_row.addWidget(self._approval_button)

        # 思考档位：菜单项不写死，等引擎按当前模型/路由算完再填（这个模型没有的档不摆）。
        self._reasoning_button = QtWidgets.QToolButton(box)
        self._reasoning_button.setObjectName("icon")
        self._reasoning_button.setIcon(_glyph_icon("spark"))
        self._reasoning_button.setIconSize(QtCore.QSize(14, 14))
        self._reasoning_button.setFixedSize(22, 22)
        self._reasoning_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._reasoning_button.setEnabled(False)  # 档位是引擎回读的，没读到之前别让人点
        self._reasoning_button.setToolTip("推理强度：等引擎回读当前档位")
        self._reasoning_menu = QtWidgets.QMenu(self._reasoning_button)
        # 组建一次复用（跟审批那组一样）：每轮新建会把旧组一直挂在菜单下面
        self._reasoning_group = QtGui.QActionGroup(self._reasoning_menu)
        self._reasoning_group.setExclusive(True)
        self._reasoning_menu.setObjectName("modelmenu")
        self._reasoning_menu.setStyleSheet(STYLE)
        self._reasoning_button.setMenu(self._reasoning_menu)
        self._reasoning_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        box_row.addWidget(self._reasoning_button)

        # 念出来（TTS）：和审批/思考并排，三档——静音 / 纯语音 / 语音互动。
        # 纯语音和语音互动互斥，就是同一档位里的两个选项，所以不存在两个同时生效的情况。
        # 状态存在 speech_prefs 里，和音色、Key 一个地方；点一下立刻生效，不影响正在跑的这轮。
        self._voice_button = QtWidgets.QToolButton(box)
        self._voice_button.setObjectName("icon")
        self._voice_button.setIconSize(QtCore.QSize(14, 14))
        self._voice_button.setFixedSize(22, 22)
        self._voice_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._voice_menu = QtWidgets.QMenu(self._voice_button)
        # 三档互斥：QAction 不带互斥，点过静音又点纯语音就是两个勾
        self._voice_group = QtGui.QActionGroup(self._voice_menu)
        self._voice_group.setExclusive(True)
        for value, label in _VOICE_MODES:
            action = self._voice_menu.addAction(label)
            action.setCheckable(True)
            action.setActionGroup(self._voice_group)
            action.setData(value)
            action.triggered.connect(lambda _checked=False, mode=value: self.set_voice_mode(mode))
        self._voice_button.setMenu(self._voice_menu)
        self._voice_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        box_row.addWidget(self._voice_button)
        self.set_voice_mode(_voice_mode())

        # 语音输入：点一下在「打字 / 说话」之间来回切，右键选麦克风。
        # 跟左边那个 TTS 三档是两件事——那个管「念不念出来」，这个管「怎么输入」。
        self._mic_button = QtWidgets.QToolButton(box)
        self._mic_button.setObjectName("icon")
        self._mic_button.setIcon(_glyph_icon("mic"))
        self._mic_button.setIconSize(QtCore.QSize(14, 14))
        self._mic_button.setFixedSize(22, 22)
        self._mic_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._mic_button.setToolTip("语音输入（右键选麦克风）")
        self._mic_button.clicked.connect(self._toggle_voice_input)
        self._mic_button.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._mic_button.customContextMenuRequested.connect(self._show_mic_menu)
        box_row.addWidget(self._mic_button)
        self._init_voice_input()

        self._send_button = send = QtWidgets.QPushButton(frame)
        send.setFixedSize(32, 32)
        send.setIconSize(QtCore.QSize(15, 15))
        send.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        send.setFlat(True)
        # 粉紫钮下面一层柔光：设计稿里的 box-shadow，Qt 只能靠图形效果给
        glow = QtWidgets.QGraphicsDropShadowEffect(send)
        glow.setBlurRadius(11)
        glow.setColor(QtGui.QColor(196, 110, 158, 97))
        glow.setOffset(0, 3)
        send.setGraphicsEffect(glow)
        send.setObjectName("send")
        send.setCursor(QtCore.Qt.PointingHandCursor)
        # 待发送附件：一条缩略图/文件名，空着就整条隐藏，不占高度
        self._attachments: list[dict] = []
        self._clipboard_seq = 0  # 粘进来的图按序命名（粘贴图片-1.png …）
        self._attach_chips: list[QtWidgets.QFrame] = []
        self._attach_bar = QtWidgets.QFrame(frame)
        self._attach_bar.setObjectName("attachbar")
        self._attach_row = QtWidgets.QHBoxLayout(self._attach_bar)
        self._attach_row.setContentsMargins(6, 3, 6, 3)
        self._attach_row.setSpacing(6)
        self._attach_row.addStretch(1)  # 附件靠左排，别让一条撑满整行
        self._attach_bar.hide()
        # 排队中：这一轮还在跑时你发的话先排在这儿，等它完了自动发。
        # 和桌面端一样，排队的东西不进正文——正文里出现的必须是已经交给引擎的。
        self._queued: list[dict] = []
        self._queue_chips: list[QtWidgets.QFrame] = []
        self._queue_bar = QtWidgets.QFrame(frame)
        self._queue_bar.setObjectName("attachbar")
        self._queue_col = QtWidgets.QVBoxLayout(self._queue_bar)
        self._queue_col.setContentsMargins(6, 3, 6, 3)
        self._queue_col.setSpacing(3)
        self._queue_bar.hide()
        layout.addWidget(self._queue_bar)
        layout.addWidget(self._attach_bar)

        row.addWidget(box, 1)
        row.addWidget(send)
        layout.addLayout(row)

        # 最底一行只留快捷键提示：这一带正对着「发送」，不放任何可点的东西
        meta_row = QtWidgets.QHBoxLayout()
        meta_row.setContentsMargins(4, 5, 4, 0)
        meta_row.setSpacing(6)
        meta_row.addWidget(hint_label)
        meta_row.addStretch(1)
        layout.addLayout(meta_row)

        send.clicked.connect(self._emit)
        # 回车发送放 eventFilter 里判：多行控件没有 returnPressed，还要区分 Shift+回车和输入法组字

        # 边框这一圈（14px 内容边距之外的 frame 空白）由 panel 接管：拖它缩放
        self._frame = frame
        frame.setMouseTracking(True)
        frame.installEventFilter(self)
        self._init_background(frame)
        self._restore_geometry()

    def _init_background(self, frame) -> None:
        """右下角一张她的立绘当底纹：压在所有内容下面，淡到只当作画面。

        路径必须走 `repo_root()`：`project_root()` 在 `NOESIA_HOME` 指到
        `data/NoesiaHome` 时会跟着指过去，那儿没有 assets。
        """
        from paths import repo_root

        self._bg_pixmap = _dimmed(
            QtGui.QPixmap(
                str(repo_root() / "assets" / "transparent" / "noesia_background.png")
            ),
            0.72,
        )
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
        height = int(frame.height() * 0.42)
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

    # ---- 对外接口 ----

    def set_models(self, items: list[dict], current: str, *, switching: bool = False) -> None:
        """填模型菜单：一级按供应商分组，二级是那家的模型；按钮上显示当前模型名。

        清单一到就说明刷新结束了，所以顺手把「刷新中…」收回去。
        """
        self._models = list(items)
        self._current_model_id = current  # 刷新途中重画菜单要用它勾回当前这条
        self._models_refreshing = False
        self._repaint_model_menu()
        text = current_label(self._models, current, fallback="选模型")
        self._model_button.setText(("切换中…" if switching else text) + "  ▾")
        # 清单空但引擎说了在跑哪个模型时也要能点：菜单里至少还有「刷新模型」
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
        if button is None:  # 菜单还没画过（没开过、清单也没到过），只能整张建一次
            self._repaint_model_menu()
            return
        button.setText("刷新中…" if self._models_refreshing else "刷新模型")
        # 不 setEnabled(False)：菜单里正亮着的那颗控件一禁用，QMenu 就把高亮甩回第一行
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
                if sub is not None and sub.isVisible():
                    keep_open_sub = self._menu_row_key(action)
                    inner = sub.activeAction()  # 鼠标停在这家子菜单的哪一个模型上
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
            if sub is not None:
                # 子菜单自己不知道往哪弹：按这一行的右上角算，跟 Qt 平时弹出的位置一致
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
        self.view.clear()
        self._streaming = False
        self._msgs = []  # 窗口里的消息清单跟着清：悬停条不该再指向已经不存在的段落
        self._row_stamp_pending = None  # 上一场没落地的行号不能带进新的一场
        self._reserve_tail_space()  # clear() 把根框架格式冲掉了，文末那一格要重新铺
        self._stream_msg = None
        self._rewind_waiting = False
        self._rewind_after_stop = None
        self._row_stamp_pending = None
        self._hide_msg_actions()
        self._assistant_pending = ""
        self._reasoning_pending = ""
        self._md_block = []
        self._md_fence = False
        self._para_break = False
        self._reason_fold = None
        self._reason_started = 0.0
        self._tool_folds = {}
        self._clarify = None
        self._clear_queue()  # 换会话了：上一段还没发出去的那几句跟着作废，别串到新会话里
        self.stack.clear_all()
        # 清屏顺带把语音球退回待机：不然它会一直停在「识别中」转圈
        self._voice_orb.set_state(STATE_IDLE)
        self._tool_started.clear()
        self._tool_clock.stop()
        self._provider_wait = ""
        self._folds.reset()
        self._diff_texts = {}
        self._tool_payloads = {}
        self._tool_images = {}  # 每个工具块已经贴过的图：同一张不重复贴
        self._tool_term_drawn = {}
        self._tool_detail_seen = {}
        self._delegate_cards = {}
        self._delegate_latest = ""
        self._interrupted = False
        # `view.clear()` 把文档资源也丢了：图标/图片的「已登记」记账得跟着清，
        # 不清的话记账说登记过、文档里其实没有，清屏之后这些图永远画不出来。
        self._icon_registered.clear()
        self._photos.clear()
        self._photo_keys.clear()
        self._photo_sources.clear()
        self._originals.clear()
        self._photo_pending.clear()
        self._fence_cache.clear()
        self._review_count = 0      # 一场对话里的回顾条数跟着新会话重来
        self._review_folded = False
        # 清屏要真的回到「刚开窗」的样子，否则新会话会继承上一场的残留：
        # 正文记账没清 → 新回答多出整行空白；错误指纹没清 → 同一个错再不显示；
        # 跟着最新没复位 → 新会话不自动贴底；忙态没清 → 按钮停在上一次的「停止」。
        self._bubble_has_text = False
        self._last_error_key = ""
        self._follow = True
        # 按钮文案跟着真实忙态走：这里不硬置 False——引擎自己续跑时窗口刚铺完历史，
        # 硬置会把 on_session_ready 刚设上的「停止」又抹回「发送」。
        self._sync_send_button()
        self.speech_bar.dismiss()
        self._hide_hover()
        # 清屏后文档只剩末尾那一格，旧位置算出来的「跳到最新」胶囊会留在一个不该有它的地方
        # （它只在滚动事件里翻显隐，清屏本身不触发滚动）。
        self._hide_jump()

    def has_transcript(self) -> bool:
        """屏幕上有没有真东西（正文或工具行）。铺历史前用它判断要不要动这个窗口。"""
        if self._folds.all():
            return True
        if self._streaming:
            return True
        return bool(self.view.toPlainText().strip())

    def set_history(self, messages) -> None:
        """界面打开：把上一段对话直接铺出来（不是让用户去输入什么）。

        内容来自引擎的会话库，界面只负责显示——所以这里是整窗重画，不留第二份历史。

        但这个窗口里已经铺了正文/工具行时**绝不能**重画：引擎的 `session.resume` 会在
        开窗、重连、切模型之后陆续回来，一旦重画，刚显示出来的工具行（历史里没有它们，
        历史只有 role/text）就整片消失——屏幕上只剩用户和回答，工具痕迹全没了。
        """
        if self.has_transcript():
            return
        self.clear_conversation("历史重铺")
        self._restoring = True
        try:
            for item in messages or ():
                text = str(item.get("text") or "")
                if not text.strip():
                    continue
                kind = str(item.get("display_kind") or "")
                if kind == _DELEGATION_DISPLAY_KIND:
                    # 引擎把子代理的结果以「用户消息」回灌给模型：原文是给模型看的英文脚手架，
                    # 界面上只留一行中文说明，不能伪装成「你」说的话。
                    self.append_notice_lines(delegate_view.completion_lines(text))
                    continue
                if kind in _NOTICE_LABELS:
                    # 事故恢复注记 / 失败轮的收尾行：同样只留中文说明。
                    # auto_continue 那句末尾带着原来问的是什么，摘出来一起显示。
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
        self._follow = True  # 重建出来的历史，从底部开始看
        self._scroll_to_end()

    def append_notice_lines(self, lines) -> None:
        """灰色说明行：引擎回灌给模型的机器消息（子代理完成之类），不把英文原文铺出来。"""
        for line in lines or ():
            self._insert_html(f"<span style='color:#8f9bb0;'>{html.escape(str(line))}</span>")

    def append_user(self, message: str, attachments=(), row_id: int | None = None) -> None:
        self._last_error_key = ""  # 新的一轮：同样的错误再出现就该再说一次
        message, refs = split_attachment_refs(message)  # 历史里的 @image:/@file:/[screenshot] 标记不能当正文显示
        items = [*refs, *attachments]
        start = self.view.document().characterCount() - 1  # 插入前的文末位置：这条消息从这儿开始
        self._insert_html(
            f"<span style='color:#c9a6e0;font-size:11px;'><b>{html.escape(_user_display_name())}</b></span>"
            f"<br><span style='color:rgba(214,198,222,168);font-size:12.5px;'>{html.escape(message)}</span>"
        )
        self._begin_message("user", start, message, row_id)
        if row_id is None and self._row_stamp_pending is not None:
            # 这一条是截断重发刚贴回来的：行号早就回了，补上（不然它再也删不掉/刷不了）
            self._msgs[-1]["row_id"] = self._row_stamp_pending
            self._row_stamp_pending = None
        for item in items:
            self._append_user_attachment(item)
        if not self._restoring:
            # 自己刚说的话必须看得见：正翻着历史也一样，发完立刻回到底并继续跟随
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
        # 名字+尺寸会撞（两张同分辨率截图都叫 image.png）：后一张 addResource 会把前一张盖掉。
        # 序号让每个附件天生一个资源名，历史里不会再串图。
        key = f"noesia-image://user-{self._image_seq}"
        self.view.document().addResource(
            QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key), pixmap
        )
        width = min(200, pixmap.width() or 200)
        self._insert_html(f"<br><img src='{key}' width='{width}'>")

    def begin_assistant(self) -> None:
        self._end_reasoning_group()  # 思考在前、回答在后：先把那段思考收成「思考了 3s」
        self._flush_md()
        start = self.view.document().characterCount() - 1
        self._insert_html("<span style='color:#d9b8cd;font-size:11px;'><b>Noesia</b></span><br>")
        self._begin_message("assistant", start, "", None)
        self._streaming = True
        self._bubble_has_text = False
        # 注意：这里**不能**放开 `_interrupted`。按停之后引擎还飘在半路的 token 会先到，
        # 到达时正是由它触发新的一段——在这儿放闸，那段残片就正好被写进新气泡，
        # 于是「512`（」这种半截回答挂在你新消息下面。放闸只认 turn.done（settle_turn）。

    def append_chunk(self, chunk: str) -> None:
        if self._interrupted:
            return  # 已经叫停了：引擎可能还飘回来几个 token，不再往气泡里写
        if not self._streaming:
            self.begin_assistant()
            self._bubble_has_text = False
        self._assistant_pending += chunk
        if self._stream_msg is not None:
            self._stream_msg["text"] += chunk  # 整段复制拿的是原文，不是窗口里折起来的样子
        self._pump_assistant(final=False)

    def end_assistant(self) -> None:
        self._end_reasoning_group()
        self._pump_assistant(final=True)
        self._flush_md()  # 最后那段结构（列表/表格/代码块）没等到空行也要落  # 收尾：缓冲里那半行吐干净
        self._drop_trailing_blank_line()  # 末尾的 `<br/>` 空行不留在下一段前面
        self._assistant_pending = ""
        self._streaming = False  # 段落由下一次 _insert_html 的 insertBlock 分隔
        self._stream_msg = None

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
        # 代码块里绝不能抢画半行：`inline()` 会把 `'1'   + '2'` 压成 `'1' + '2'`、把行尾并进上一段，
        # 这些残片还会用文档默认的黑字黑底画出来——屏幕上就是一条条纯黑横杠（用户报的「一条条黑的」）。
        # 围栏自己没到齐时（"`" / "``"）同理，等它齐了再按围栏判断。
        stripped = data.strip()
        hold_partial = (
            self._md_fence  # 围栏里：半行照正文画会漏字、还会用默认黑字黑底画成黑杠
            or (not self._md_block and "```".startswith(stripped))  # 围栏自己还没到齐（"`" / "``"）
            or data.count("**") % 2  # 粗体标记还被切在两半里：先画就会漏出 `**`
            or data.rstrip().endswith("*")  # 标记只到了一半（行尾一个 `*`），等它配对
            or data.count("`") % 2  # 行内代码没闭合，同理
            or _open_link(data)  # 图片/链接的括号还没到齐：先画出来就是那行原始 markdown
            or markdown_render.is_structural_prefix(data)  # 结构标记只到了一半（`-`、`1.`、`##`）
        )
        if (
            data
            and not hold_partial
            and not media_links.might_be_media(data)
            and not markdown_render.is_structural(data)
        ):
            # 半行先照行内语法画出来（流式要跟得上）；剩下的等换行到了再接着画
            # 结构行（表格 / 列表 / 标题）不能这么画：半行照行内画会把 "| 2026-…” 这种
            # 原始 markdown 漏进上一段，表格还会多出一行残缺的行
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
        self._flush_md()  # 附件单独占一行：先把没落盘的段落收掉
        self._insert_media(path)

    # ---- markdown：正文照桌面端那样渲染，不按纯文本铺 ----

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
        if not line.strip():
            # 空行是分段：先把结构段落盘，再断开当前这段（否则两段会连成一句）
            self._flush_md()
            self._break_paragraph()
            return
        if markdown_render.is_structural(line):
            self._md_block.append(line)
            if len(self._md_block) >= 80:  # 长表格不会一直憋着不显示
                self._flush_md()
            return
        self._flush_md()  # 结构段结束，先落下
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
        if not text.strip():
            return
        self._bubble_has_text = True
        for kind, language, body in markdown_render.split_fences(text):
            if kind == "code":
                self._insert_code_block(body, language)
            else:
                self._insert_html(markdown_render.block(body))

    # ---- 正文里的网络图片：Qt 不下载，得自己取回来注册成文档资源 ----

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
            # 有些图床不认没有 UA 的请求
            request.setHeader(
                QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader, "Noesia Agent"
            )
            request.setTransferTimeout(10000)  # 网络不通时别一直挂着
            reply = self._net.get(request)
            reply.finished.connect(lambda: self._photo_arrived(reply, url, key))
        except Exception:
            self._photo_pending.discard(url)
            logger.warning("正文图片没发出去：%s", url, exc_info=True)

    def _photo_arrived(self, reply, url: str, key: str) -> None:
        """图取回来了：换成真图重画；取不回来就如实说没加载出来。"""
        self._photo_pending.discard(url)
        # 占位是个矮灰条，真图比它高一大截：换上去正文会长高。
        # 本来跟着新内容看的，图一落地正文就变高，得继续跟到底，不能把人晾在半空。
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
                # 正文里只放缩略图：想看仔细、想存原图，点开或用右上角的下载
                limit = max(120, min(_THUMB_WIDTH, self.view.viewport().width() - 40))
                if pixmap.width() > limit:
                    pixmap = pixmap.scaledToWidth(
                        limit, QtCore.Qt.TransformationMode.SmoothTransformation
                    )
                self._remember_original(url, data)  # 原图字节留着：点开能看清、能存
            if not broken:
                # 失败不记账：换个网络还能救回来，别把「没加载出来」当成结果缓存住
                self._photos[url] = pixmap
            # 长会话里一路看下去不能把每张图都攥着：超过上限就先松掉最早的。
            # 已经画进文档的那几张不受影响（文档自己是资源的持有者），这里只是不再挡重复下载。
            while len(self._photos) > _PHOTO_CACHE:
                self._photos.pop(next(iter(self._photos)))
            self.view.document().addResource(
                QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key), pixmap
            )
            self.view.document().markContentsDirty(0, self.view.document().characterCount())
            self.view.viewport().update()
            if stuck:
                # 排版是下一拍才算完的，等它算完再贴底，不然拿到的还是旧的最大值
                self._scroll_to_end_later()
        except Exception:
            logger.warning("正文图片落地时出错：%s", url, exc_info=True)

    def _adopt_images(self, markup: str) -> str:
        """把正文里的网络图片换成文档资源名。

        Qt 的富文本不会去下载 `http(s)` 的图，直接插 `<img src="https://…">` 只剩一块空白；
        换成注册过的资源名，取回来以后才画得出来。被流式切开的那种半截网址到这里已经完整
        （`_open_link` 会压到括号到齐才放行）。
        """
        if "http" not in markup:
            return markup
        try:
            return self._swap_image_sources(markup)
        except Exception:
            # 图这块出事不能连累正文：原样插进去，最多是这块图显示不出来
            logger.warning("正文图片处理失败，按原文插入", exc_info=True)
            return markup

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
            # HTML 里的 `src` 是转义过的：markdown-it 会把地址中的 `&` 写成 `&amp;`，
            # 原样拿去请求时服务端收到的是 `w=300&amp;h=200`，参数名变成 `amp;h`，
            # 签名/尺寸校验直接失败——带 `&` 的 CDN 图整张取不回来，重试也没用。
            url = html.unescape(url)
            if url.lower().startswith(("http://", "https://")):
                pieces.append(self._photo_key(url))
            else:
                pieces.append(url)
            pieces.append('"')  # 值的收尾引号（marker 里只有开引号）
            index = closing + 1
        return "".join(pieces)

    def _remember_original(self, url: str, data: bytes) -> None:
        """记着原图字节（只留最近几张）：点开看原图、存原图都靠它。"""
        if not data:
            return
        self._originals[url] = data
        while len(self._originals) > _ORIGINAL_CACHE:
            self._originals.pop(next(iter(self._originals)))

    def _image_at(self, pos: QtCore.QPoint) -> str:
        """这一点落在哪张图上？返回资源名，不是图就返回空。"""
        fmt = self.view.cursorForPosition(pos).charFormat()
        return fmt.toImageFormat().name() if self._is_photo_format(fmt) else ""

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

    def _photo_rect_at(self, pos: QtCore.QPoint, key: str) -> QtCore.QRect | None:
        """这张图在视口里占的那块地方；鼠标不在图上就返回 None。

        别用 `cursorForPosition` 的横坐标来回推：图片只占一个字符位，紧挨着图片右边的空白
        也可能被算成「图片那个字」，而且返回的位置比图片右边还靠右，退一个图宽也退不回来。
        横坐标改用所在行块的左边界（图片在正文里独家占一行、左对齐），纵坐标和高度用光标
        矩形——这两个是准的。
        """
        # 宽度以**文档里真正画着的那张图**为准，不是 `_photos` 缓存：缓存是 24 张的 FIFO，
        # 长会话里老图被挤掉后，`_photos` 里查不到——以前就在这里返回 None，
        # 于是那些看得见的图点不开、也没有悬停下载（图还在文档里，文档才是持有者）。
        pixmap = self.view.document().resource(
            QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key)
        )
        if not isinstance(pixmap, QtGui.QPixmap) or pixmap.isNull():
            pixmap = self._photos.get(self._photo_sources.get(key, ""))
        if pixmap is None or pixmap.isNull():
            return None
        cursor = self.view.cursorForPosition(pos)
        line = self.view.cursorRect(cursor)
        block = self.view.document().documentLayout().blockBoundingRect(cursor.block())
        left = int(block.left() - self.view.horizontalScrollBar().value())
        rect = QtCore.QRect(left, line.top(), pixmap.width(), line.height())
        return rect if rect.contains(pos) else None

    def _photo_at(self, pos: QtCore.QPoint) -> tuple[str, QtCore.QRect] | None:
        """鼠标是不是真落在某张图上。

        不能只看 `_image_at`：图片在文档里只占一个字符位，那个字符格式会往图片左右延伸，
        紧挨着图片的空白也算「在图片上」。于是鼠标一出图，按钮就被重新摆到图外面去。
        这里拿矩形再验一次，图画到哪儿就只认到哪儿。
        """
        key = self._image_at(pos)
        if not key:
            return None
        rect = self._photo_rect_at(pos, key)
        return (key, rect) if rect is not None else None

    def _show_hover_download(self, key: str, rect: QtCore.QRect) -> None:
        """图上悬停时右上角的下载按钮：点一下直接存原图，不用先打开看原图。"""
        if self._hover_download is None:
            self._hover_download = HoverActionButton(
                self.view.viewport(),
                "download",
                "下载原图",
                lambda: None,
                flash=False,  # 存完不换成 ✓：存到哪个目录已经由对话框说过了
            )
            # 图片深浅不定，垫一层半透明底，压在浅色照片上也看得清
            self._hover_download.setStyleSheet(
                "QToolButton { border: none; border-radius: 6px;"
                " background: rgba(16, 18, 24, 150); padding: 0; }"
                "QToolButton:hover { background: rgba(16, 18, 24, 225); }"
            )
        self._hover_download.retarget(
            "download", "下载原图", lambda k=key: self._download_photo(k)
        )
        self._hover_download.place_at(rect)
        self._hover_download.show()

    def _hide_download(self) -> None:
        if self._hover_download is not None:
            self._hover_download.hide()

    def _download_photo(self, key: str) -> None:
        """存这张图的原图。手上还有原图字节就直接问存哪儿，没有就先取回来。"""
        url = self._photo_sources.get(key)
        if not url:
            return
        data = self._originals.get(url)
        if data:
            save_photo_bytes(self.window(), data, _photo_name(url))
            return
        self._fetch_then_save(url)

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
            logger.warning("取原图失败：%s", url, exc_info=True)

    def _save_after_fetch(self, reply, url: str) -> None:
        try:
            ok = reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll().data())
            reply.deleteLater()
            if not ok or not data:
                return
            self._remember_original(url, data)
            save_photo_bytes(self.window(), data, _photo_name(url))
        except Exception:
            logger.warning("存原图出错：%s", url, exc_info=True)

    def _open_photo(self, key: str) -> None:
        """点图：开窗看原图。原图字节还在就直接给，松过手的先拿缩略图顶着、同时去取。"""
        url = self._photo_sources.get(key)
        if not url:
            return  # 用户自己贴的附件走的是另一套，这里不管
        dialog = PhotoView(_photo_name(url), self.window())
        data = self._originals.get(url)
        if data:
            pixmap = QtGui.QPixmap()
            if pixmap.loadFromData(data) and not pixmap.isNull():
                dialog.set_photo(pixmap)
                try:
                    dialog.exec()
                finally:
                    dialog.deleteLater()   # 带 parent 的对话框 exec 完只是藏起来，不销毁
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
            dialog.deleteLater()   # 带 parent 的对话框不销毁，每开一次面板下就多一个

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

    def _insert_inline(self, markup: str) -> None:
        """行内片段：接在当前段里，不另起一段（普通句子不该每行一个大间距）。"""
        if not markup:
            return
        self._bubble_has_text = True
        cursor = QtGui.QTextCursor(self.view.document())  # 独立光标：不挪视图，滚动位置就不动
        cursor.movePosition(QtGui.QTextCursor.End)
        if self._para_break or self._folds.owns(cursor.position()) or self._is_code_block(cursor.block()):
            # 上一段是折叠段藏起来的正文：另起一段，别把回答接在工具结果后面
            # （折叠着的时候更糟——回答会一起被藏起来）
            # 上一段是代码块也一样：代码块一行一段，接着写会把这段说明并进最后一行代码，
            # 看起来像代码，复制出来也带上它（它此时还没有自己的块）。
            cursor.insertBlock()
            cursor.setCharFormat(QtGui.QTextCharFormat())
            cursor.setBlockFormat(_plain_block_format())
        self._para_break = False
        cursor.insertHtml(_keep_edge_spaces(self._adopt_images(markup)))
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
        cursor.movePosition(QtGui.QTextCursor.EndOfBlock)
        cursor.deletePreviousChar()

    def _insert_media(self, raw_path: str) -> None:
        """附件单独占一行：文件名 + 大小 + 点击打开（文件不存在就如实说找不到）。"""
        self._bubble_has_text = True
        # 末尾补一个 <br>：不然后面的话会接在这行附件后面（insertHtml 不会自己换行）
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
                self._folds.toggle(fold.key)  # 抬头上的箭头跟着翻
                fold.pinned = True  # 他自己点了：之后再跑完一轮也别自动收起/展开
                if following:  # 展开后内容比原来长：本来跟着底部看，就继续贴住
                    self._scroll_to_end_later()
            return
        self._open_media(url)

    def _open_media(self, url: QtCore.QUrl) -> None:
        path = url.toLocalFile()
        if not path:
            return
        if not media_links.open_local(Path(path)):
            self.show_error(f"打不开这个文件：{path}")

    # ---- 输入框上方那一叠状态（桌面端 composer 的 status-stack） ----

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

    # ---- 引擎在问「你要选哪个」 ----

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
            # 代际：旧卡的锚点还留在文档里，靠它认出「这张卡已经作废」，
            # 否则点旧卡的选项会拿新卡的题目作答、以新 request_id 发出去。
            "gen": self._clarify_gen,
            "qcount": len(questions),
            "request_id": str(payload.get("request_id") or ""),
            "reply_id": payload.get("reply_id"),
            "fold": fold,
            "questions": questions,
            "answers": {},
            "marks": {},
            "choice_marks": {},  # 每题自己那几行选项：抬头和「跳过」不掺进来，按下标改字才不会错位
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
        if qindex:  # 第 2 题起：上面留一个 3pt 的空行，别让两题黏成一块
            self._folds.append_marked(fold, " ", body_format=self._spacer_format())
        if state["qcount"] > 1:  # 一题时抬头已经说清了，多题才需要「问题 1」这种序号
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
        # 题面 + 同一行末尾的小药丸提示（桌面 `singleSelectHint` / `multiSelectHint` 就在这一行右侧）
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
        if multi:  # 我们按题确认，就放在这题选项下面
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
            return  # 不是这一张卡（新问题已经顶上来）
        self._settle_clarify("已结束")

    def _on_clarify_click(self, rest: str) -> None:
        state = self._clarify
        if state is None:
            return
        gen, _, rest = rest.partition(":")
        if gen != str(state.get("gen")):
            return  # 旧卡上的锚点：这一问已经被新问题顶掉，点了不能再当成新问题的答案
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
            # 跑着的时候给的时长是 0，会被格式成「1ms」并一直停在那儿；
            # 这段时间自己按秒走，结果回来再换成后端报的真实时长。
            label = f"{label} · {view.duration_label}"
        if view.status == "error":
            label = f"✗ {label}"
        elif view.status == "warning":
            label = f"! {label}"

        if is_file_edit and not pending and view.status != "error" and not view.inline_diff:
            # 编辑类工具成功了却没 diff：正文没东西可画。但跑起来那一帧已经建过行、挂上秒表了，
            # 这里必须收尾——只 return 不收表，「编辑 x.py · 1247s」会一直往上跳，看着像还在跑。
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
            # 子代理卡片默认展开：正要「看清它在跑什么」的时候，收着等于没有
            fold = self._folds.new("tool", label, open_=bool(view.inline_diff) or delegate_event is not None, glyph=glyph)
            if tool_id:
                self._tool_folds[tool_id] = fold
        elif label != fold.label or self._glyph_url(glyph) != self._glyph_url(fold.glyph):
            # 图标得跟着状态换：跑着是蓝钟，跑完/出错就换成这个工具自己的图标（灰/红/黄）。
            # 只在建行时插一次的话，跑过的行会永远挂着那只钟——看着像还在跑。
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
            # 子代理卡片：抬头是「几个任务 · 什么状态」，正文是任务行 + 实时记录 + 运行中的动作
            self._draw_delegate(fold, tool_id, delegate_event, pending=pending)
            self._settle_insert()  # 子代理卡片也是正文：不贴底就跟不上它继续长
            return

        # ---- 正文：顺序固定（终端转录 → 图 → 搜索 → diff → 详情）----
        path = view.subtitle if is_file_edit else None

        # 终端转录等跑完再画：跑着的帧和跑完的帧带的是同一条命令，两边都画就是两行一模一样的
        if (view.terminal_command is not None or view.terminal_exit_code is not None) and not pending:
            if not self._tool_term_drawn.get(fold.key):
                self._tool_term_drawn[fold.key] = True
                self._folds.append_runs(fold, self._terminal_rows(view))

        if view.image_url and self._tool_images.get(fold.key) != view.image_url:
            # 同一个工具块会画两次（刚调起来一次、跑完一次）：图只贴一次。
            # 不挡的话界面上就是上下两张一模一样的图（一次分析看两张截图）。
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

        if view.inline_diff:
            self._diff_texts[fold.key] = (path, view.inline_diff)
            self._folds.append_runs(fold, self._diff_rows(view.inline_diff, path=path))
            # `defaultOpen = Boolean(inlineDiff) && !hideCodeDiffs`：带 diff 的工具行默认展开，
            # 用户自己点过就按用户的来（`pinned`）。
            if not fold.pinned and not fold.open:
                self._folds.set_open(fold, True)

        self._append_detail(fold, view)
        self._settle_insert()

    # ---- 工具正文的画法：ANSI 上色、diff 上色 ----

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
    # 增删靠底色认。深绿深红那种实色在这个底上跟背景几乎没差别，等于没标；
    # 改成高饱和色淡一层叠上去（`#AARRGGBB`，前两位是不透明度），一眼能分出加还是删。
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

    GUTTER_COLOR = "#5c6472"  # 行号列 `text-muted-foreground/55`

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
        # 兜底：diff 里可能还带着终端转义（引擎那份是给终端画的）。
        # 留着会让逐行分类把 `\x1b[..m+xxx` 当上下文，颜色全丢、转义原样显示。
        # hunk 之前那段文件头（`diff --git`、`index`、`--- a/…`、`+++ b/…`、`a/… → b/…`）
        # 砍掉 diff 文件头：路径抬头已经写着，画出来只剩噪音。
        for kind, line in diff_rows(strip_diff_file_headers(strip_ansi(diff))):
            if kind == "hunk":
                # `@@ -120,7 +120,9 @@`：行号从这一段自己报的起点算，不能从 0 一直累加，
                # 不然「看完整 diff」左栏的号是错的，照着改文件会改错地方。
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

            # 行首那个 `+`/`-`/空格是给解析器看的记号，不是内容：
            # 去掉，增删靠行底色认，左边不留一列加减号。
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
                # 聊天窗这条路每行只能上一种颜色（内容排在独立滚动区里，按行排版），
                # 语法色在这里只会让整行偏一个色，看不出增删——整行直接给 diff 色。
                rows.append([*gutter, (code or " ", base)])
        return rows

    # ---- 抬头左边的小图标 ----

    def _icon_pixmap(self, name: str | None, color: str, px: int = 14) -> QtGui.QPixmap | None:
        """把图标名字渲成小图（同一个图标同一颜色只渲一次）。"""
        key = (name, color, px)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached

        svg = icon_svg(name, color)
        if not svg:
            # 名字不认识（以后新加的工具、旧包里没有这个图标）不该退化成「这一行没有图标」——
            # 那看起来像界面坏了。退回扳手，至少看得出这是一行工具。
            name = "tools"
            key = (name, color, px)
            cached = self._icon_cache.get(key)
            if cached is not None:
                return cached
            svg = icon_svg(name, color)
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
        except Exception:
            return None

        self._icon_cache[key] = pixmap
        return pixmap

    def _track_running_tool(self, tool_id: str, fold, label: str, icon: str | None = None) -> None:
        """记下这一行的起点；同一行后续再更新时保留原起点，免得每秒都从 0 重来。

        顺手记下这个工具自己的图标名：叫停/断线要收尾时，钟得换成它，不能留在那只蓝钟上。
        """
        started = self._tool_started[tool_id][2] if tool_id in self._tool_started else time.monotonic()
        self._tool_started[tool_id] = (fold, label, started, icon)
        if not self._tool_clock.isActive():
            self._tool_clock.start()

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
        # 图默认按基线摆，整块会吊在文字上方；改成按行居中，和文字落在一条线上
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
        # 同一个工具块会画两次（开始 / 跑完），同样的正文只画一次，不然详情会一模一样地叠两遍
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
        # 跳过与标题/副标题重复的详情，省得同一句话念两遍
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
            card["rows"] = delegate_view.rows(args, result)  # 状态/用时/模型换成引擎报的
        self._delegate_sync(key)
        if result is not None and not pending and delegate_view.is_background(result):
            # 后台派发：工具这一帧已经「完成」了，但子代理还在跑——抬头按秒走，
            # 等 `subagent.complete` 回来才收尾。
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
        # 这一张卡收尾了：停掉它的秒表，抬头换成最终状态，不再当「最近在跑的那张」
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
            return  # 引擎配置成不看工具行
        self._end_reasoning_group()
        label = summary or name
        if not ok:
            label = f"✗ {label}"
        # 这条路径没有 ToolView（引擎只给了摘要），图标也得按工具取：不能一律白纸，也不能空着。
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
        if not self._reason_started:
            self._reason_started = time.monotonic()
        if len(self._reasoning_pending) >= 160:
            self.flush_reasoning()

    def flush_reasoning(self) -> None:
        """把攒着的思考落进那个折叠段（回答开始、本轮结束、出错时都要先刷一下）。"""
        text, self._reasoning_pending = self._reasoning_pending, ""
        if not text.strip() or not self._show_reasoning:
            return
        if self._reason_fold is None:
            # 边想边看：桌面端实时的思考段是展开的，跑完也保持展开
            self._reason_fold = self._folds.new("reasoning", _THINKING_LABEL, open_=True)
        self._folds.append(self._reason_fold, text.strip())
        self._settle_insert()

    def show_reasoning_block(self, text: str) -> None:
        """不流式的供应商一次给全的思考（`reasoning.available`）：整块落进同一段。

        桌面端对它的处理是「替换式追加」（`appendReasoningDelta(..., replace=true)`）。
        实测（真引擎）：这一帧是在这一轮**正文之后**、`message.complete` 之前到的，
        payload 就是整轮回答的全文快照。所以回答已经落地时直接丢掉——不然回答底下会
        再冒出一段「思考了片刻」，把那篇回答原样斜体铺第二遍。
        """
        if self._bubble_has_text:
            return  # 这一轮的正文已经写完了：这是整轮快照，不是新的一段思考
        if not text.strip() or not self._show_reasoning:
            return
        if not self._reason_started:
            self._reason_started = time.monotonic()
        self._reasoning_pending += text
        self.flush_reasoning()

    def _end_reasoning_group(self) -> None:
        """这一段思考结束了（正文/工具行来了，或者本轮收尾）：抬头换成「思考了 3s」并收起。

        实时思考是展开给人看的；回答一开始就把它折成一行——不然每轮回答上面都顶着一大块
        灰字，正文得往下翻才看得见。用户自己点开过的（`pinned`）不动他。
        """
        self.flush_reasoning()
        fold, self._reason_fold = self._reason_fold, None
        started, self._reason_started = self._reason_started, 0.0
        if fold is None:
            return
        self._folds.retitle(fold, _thought_label(time.monotonic() - started if started else None))
        if not fold.pinned:
            self._folds.set_open(fold, False)
            # 收起之后再改一次抬头：箭头得跟着翻成 ▸（retitle 读的是 fold.open）
            self._folds.retitle(fold, fold.label)

    def show_error(self, message: str) -> None:
        self._end_reasoning_group()
        # 注意**不要**动 `_streaming`：回答正在流的时候引擎也会回错误帧（config.get、
        # process.list 之类），把它清掉，下一段 chunk 就会另起一条「Noesia」——
        # 一条完整回答被劈成两条，中间夹一行红字，复制/刷新只作用到后半截。
        if not self._streaming:
            self._assistant_pending = ""  # 没在流的时候清掉上一轮的残片；正在流就留着，那是这轮没渲染完的半句
        text = (message or "").strip()
        if not text:
            return
        key = _error_key(text)
        if key == self._last_error_key:
            return  # 同一条错误只留一条：引擎会为每个请求、每次重试各回一遍
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
            text = self._provider_wait  # 状态覆盖不了「在等供应商」那行时，把它留着
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
            self._snapped = False  # 离得远：不吸，之后她挪也不跟
            return
        # 只认她的中心线：不去挤屏幕边缘，否则一挤就对不齐了
        self.move(anchor.center().x() - self.width() // 2, anchor.bottom() + 1)
        self._snapped = True  # 吸上了：之后她挪/缩，聊天窗一直跟着走

    def show_near(self, anchor: QtCore.QRect | None = None) -> None:
        """贴着头像窗右侧出现；没有锚点就靠在屏幕右侧中部。"""
        screen = QtGui.QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)
        self._snapped = False  # 摆放之前先不认「吸着」，摆完再按实际位置判断
        self.set_anchor(anchor)
        saved = self._saved_pos()
        if saved is not None:  # 上次放过的地方就是它该在的地方，别再贴到右边去
            self.move(saved)
            self.show()
            self.raise_()
            self.activateWindow()
            self.input.setFocus()
            # 摆好、显示出来才量得准：上次就是吸在她下面的，这次接着跟（精确对齐，不留偏移）
            self._settle_snap()
            return
        width, height = self.width(), self.height()  # 面板可缩放，定位要按当前尺寸算
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

    # ---- 内部 ----

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
        self._para_break = False  # 代码块自己会开段，空行记的账在这里结清
        block_format = QtGui.QTextBlockFormat()
        block_format.setBackground(QtGui.QColor(markdown_render.CODE_BACKGROUND))
        block_format.setLeftMargin(10)
        block_format.setRightMargin(8)
        # 上下边距只留给首尾两块：每块都带的话，块与块之间那段留白不画底色，
        # 一整段代码就断成一条一条的色带。
        block_format.setTopMargin(0)
        block_format.setBottomMargin(0)
        head_format = QtGui.QTextBlockFormat(block_format)
        head_format.setTopMargin(4)
        base = QtGui.QTextCharFormat()
        base.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        base.setFontPointSize(9)
        base.setForeground(QtGui.QColor("#d7dae5"))

        self._drop_trailing_blank_line()  # 上一行末尾的 `<br/>` 空行，紧接着代码块时就是白留一行
        cursor = QtGui.QTextCursor(self.view.document())  # 独立光标：不挪视图，滚动位置就不动
        cursor.movePosition(QtGui.QTextCursor.End)
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
                cursor.insertText(" ", base)  # 空行也要占一行的高度
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
            # 空片段也是片段：硬插一次会留下一个空段（14px 行高 + 5px 间距），
            # 屏幕上就是一句回复和下面的列表之间那一大截空白。
            return
        document = self.view.document()
        cursor = QtGui.QTextCursor(document)  # 独立光标：不挪视图，滚动位置就不动
        cursor.movePosition(QtGui.QTextCursor.End)
        heading = _HEADING_RE.match(markup)
        if heading:
            self._insert_heading(cursor, int(heading.group(1)), heading.group(2))
            return
        first_new = None
        quote_format = None
        if not document.isEmpty():
            if _starts_own_block(markup):
                # 列表 / 表格 / 分隔线：Qt 自己开新段，这里再开一次就是凭空多一个空段
                self._drop_trailing_blank_line()
                # 光标停在列表项里时插新的列表，Qt 会当成同一个列表接着排：
                # 无序后面跟有序，`二` 和 `甲` 会落进同一个块（实测 text 变成 "二\u2028甲"）。
                # 先退到一个不属于任何列表的空段，再让 Qt 自己开。
                if document.lastBlock().textList() is not None:
                    cursor.insertBlock()
                    cursor.setBlockFormat(_plain_block_format())
                first_new = document.blockCount()
            else:
                cursor.insertBlock()  # 行内片段不会自己断开，得显式开段
                # 新段不能沿用上一段的格式：上一段是代码块时，底色会跟着过来，整段说明都画成代码
                cursor.setCharFormat(QtGui.QTextCharFormat())
                quote = markup.lstrip().lower().startswith(_QUOTE_TAG)
                block_format = _plain_block_format()
                if quote:
                    block_format.setLeftMargin(12)  # 引用：缩进一点，跟正文分开
                cursor.setBlockFormat(block_format)
                if quote:
                    quote_format = QtGui.QTextCharFormat()
                    quote_format.setForeground(QtGui.QColor(_QUOTE_COLOR))
        self._para_break = False
        start = cursor.position()
        cursor.insertHtml(self._adopt_images(markup))
        if quote_format is not None:
            # insertHtml 会把手上的字符格式换成片段自己的，颜色得插完再刷到这段范围上
            _paint_range(cursor, start, cursor.position(), quote_format)
        if first_new is not None:
            # 段间距只由块格式给（5px），所以 Qt 新开的这几段也要设成普通段落
            block = document.findBlockByNumber(first_new)
            while block.isValid():
                # 列表项不能重设块格式：Qt 把「属于哪个 QTextList」存在 objectIndex 里，
                # 重设就把块从列表里摘出来，圆点/序号/缩进全没了。它们的间距由列表样式给。
                if block.textList() is None:
                    QtGui.QTextCursor(block).setBlockFormat(_plain_block_format())
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
        block_format.setTopMargin(12 if level <= 2 else 9)
        block_format.setBottomMargin(3)
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
        _paint_range(cursor, start, cursor.position(), char_format)  # insertHtml 会换掉手上的格式
        self._settle_insert()

    def _scroll_to_end(self) -> None:
        """滚到最底。

        直接设 `maximum()` 有个坑：刚插进去的字还没排版，拿到的最大滚动量是旧的，
        滚不到新长出来那截（表现就是「卡片展开了但还差一口没到底」）。所以先逼一次排版。

        这个过程会发 `valueChanged`，那是我自己滚的，不是用户往上翻——用 `_self_scroll`
        挡一下，否则每滚一次就把「跟随」状态误判成「用户离开了底部」。
        """
        self.view.document().documentLayout().documentSize()  # 逼一次排版：拿到的 maximum 才是新的
        bar = self.view.verticalScrollBar()
        self._self_scroll = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._self_scroll = False

    def _at_end(self) -> bool:
        bar = self.view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

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
            return  # 整窗重铺历史：几千条逐条滚会让 singleShot 堆成一座山，铺完在 set_history 末尾滚一次
        if self._follow:
            self._scroll_to_end_later()  # 排版还没落，当拍滚不到新长出来那截
            self._hide_jump()
        else:
            self._show_jump()

    # ---- 没看完：底下那颗「滚动到底部」 ----

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
            # 一屏装得下，谈不上「没看完」
            self._hide_jump()
            return
        pill = self._jump_pill()
        self._place_jump()  # 可见不等于摆在点子上
        if pill.isVisible():
            return
        pill.raise_()  # 正文里有图，别让图盖住它
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

    def _range_changed(self, _minimum: int, _maximum: int) -> None:
        """滚动范围变了：这一拍里跟着来的位移是 Qt 按新排版挪的，不是用户翻的。"""
        self._range_changing = True
        QtCore.QTimer.singleShot(0, self._end_range_change)

    def _end_range_change(self) -> None:
        self._range_changing = False

    def _on_scrolled(self, _value: int) -> None:
        """滚动条动了：到没到底既决定胶囊的存亡，也决定还跟不跟随新内容。"""
        self._hide_hover()
        if self._self_scroll:
            return  # 是我自己滚到底的，不算用户的意思
        if self._at_end():
            self._follow = True
            self._hide_jump()
            return
        if self._range_changing and not self._user_scroll:
            # 位置是 Qt 自己挪的（首帧显示、排版变高、窗口缩放），不是用户往上翻：
            # 跟随状态不变；本来就在跟随的话，把差的那一截补回去。
            if self._follow and not self._restoring:
                self._scroll_to_end_later()
            return
        self._follow = False  # 用户真的往上翻了
        self._show_jump()

    # ---- 念出来（TTS）开关 ----

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
                # 互动档那条提示（输入框上方状态条）跟着档位一起撤：不撤的话切回纯语音还挂着，
                # 看着像没切过来。
                self.clear_status_notice("avatar")
            # 语音那条提示不分档位一起撤：切到静音以后「缺 Key」这句话就没有意义了，
            # 留着会一直挂在输入框上方（用户实际报的就是这个）。
            self.clear_status_notice("speech")
        except Exception as exc:  # 存不下来也别让界面卡住
            self.show_error(f"语音档位没存住：{exc}")
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
            # 菜单一律只写档名：箭头会让整列看起来像「最小→低、中→低……」一团乱。
            # 「这条路由实际发哪档」只放在按钮提示里说一句，菜单保持干净。
            add(value, text)
        label = next((text for value, text in _REASONING_LEVELS if value == current), "")
        if current == _REASONING_OFF[0]:
            label = _REASONING_OFF[1]
        self._reasoning_button.setEnabled(True)
        if pending:
            # 恢复的会话在首条消息之前不知道档位：菜单照列，但一项都不勾、也不拿默认值冒充
            self._reasoning_button.setToolTip("推理强度：等会话回报当前档（发一句话后显示）")
        elif wire and wire != current:
            # 这条路没有这一档时，引擎会降档发出去：提示里说明，菜单上不画箭头
            self._reasoning_button.setToolTip(f"推理强度：{label}（这条路实际按「{names.get(wire, wire)}」发送）")
        else:
            self._reasoning_button.setToolTip(f"推理强度：{label or '引擎默认'}")

    # ---- 念出来的那条进度（只关声音，不动这一轮对话）----

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
            # 本来就没有一轮在跑：不关闸。关了以后没有 turn.done 来开它，
            # 接下来整场对话都收不到字（语音模式「先说话再发」就撞这个）。
            return
        already_stopped = self._interrupted
        if self._streaming:
            self.end_assistant()
        self._interrupted = True
        # 这一轮已经作废：手里那张「等你选一个」的卡不会再有人来接答案。
        # 不清的话它一直挂在「等回答」上，你下一句话会被当成这张卡的答案吃掉、
        # 不当新一轮发出去——屏幕上就是发了没反应（2026-11-03 查到的）。
        self._clarify = None
        # 按停的那一刻，手上还在跑的工具行也要收尾：表停、钟换成它自己的图标、标成「已停止」。
        # 不然行下面写着已停止，上面那只钟还在跳秒，看着像根本没停住。
        self._settle_running_tools("已停止")
        if already_stopped:
            return  # 同一轮连按两下，只留一个标记
        # 引擎不会替我们标注（那句 "Operation interrupted…" 是取消元数据，前面已经拦掉了），
        # 这里自己补一行灰字，让人一眼看出「这条不是说完的，是被我叫住的」。
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
            self.end_assistant()
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
            return  # 本来就没在跑：别在空会话里硬塞一条
        if self._interrupted:
            return  # 已经写过「已停止」，别叠第二条
        self._interrupted = True
        self._clarify = None  # 这一轮废了：那张等回答的卡也跟着作废（原因同 stop_streaming）
        if self._streaming:
            self.end_assistant()
        self.set_provider_wait("")  # 变量和底部那行字一起清：只清变量会永远挂着「等待供应商…」
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
            # 这一轮结束了：「正在调用 X…」那句是靠 tool.start 撤掉的，
            # 按停时那个工具的 start 永远不会来了，不清就一直挂在输入框上方，看着像还在跑
            self.set_activity("")
            # 「在等供应商，2s 后第 1/3 次」同理：只有异常路径清过它，正常/失败收尾之前都不清，
            # 于是上一轮的重试倒计时会一直挂在这轮的错误提示旁边，两句话互相矛盾。
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
            # 刚叫停的那一轮是为了「从这条重来」：别再把它排队的那句接上
            self._emit_rewind(pending[0], pending[1], False)
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
                return "停止"  # 这次重发正在跑：正文已经交给引擎，这个键就是停止，别再排一条一样的
            return "排队" if typing else "停止"
        if self._rewind_waiting:
            return "重发中"  # 请求发出去了还没落地：这会儿按下去也不会再发一次（见 _emit）
        return "发送"

    def _sync_send_button(self) -> None:

        if not hasattr(self, "_send_button"):
            return  # 按钮还没建（__init__ 里文本框先连接）：等 set_busy 时再来一次
        label = self._send_label()
        if label == "排队":
            # 排队时按钮上就写「排队」：只换个图标看不出来点下去是排队还是发送
            self._send_button.setIcon(QtGui.QIcon())
            self._send_button.setText("排队")
            self._send_button.setFixedWidth(52)
        else:
            icon = icon_pixmap("stop" if label == "停止" else "arrow-up", "#2b1520", 15)
            self._send_button.setIcon(QtGui.QIcon(icon) if icon is not None else QtGui.QIcon())
            self._send_button.setText("")
            self._send_button.setFixedWidth(32)
        self._send_button.setToolTip(
            "这一轮还在跑：点了先排队，它跑完接着发" if label == "排队"
            else "叫住这一轮（已进行的对话仍留在上下文里）" if label == "停止"
            else "发送"
        )

    # ---- 排队：这一轮还在跑时你说的话，先排着，轮次完了自动发 ----

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
        close.setCursor(QtCore.Qt.PointingHandCursor)
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
            return  # 还没建队列区（__init__ 早期清屏）：没东西可清
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

    # ---- 附件：「＋」菜单、粘贴、拖进来，三条路进同一个地方 ----

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
        dialog.deleteLater()   # 带 parent 的对话框不销毁，每开一次面板下就多一个
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
            dialog.deleteLater()   # 带 parent 的对话框 exec 完只是藏起来，不销毁

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
            button.setCursor(QtCore.Qt.PointingHandCursor)
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
        self.input.moveCursor(QtGui.QTextCursor.End)
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
            thumb.setPixmap(pixmap.scaled(26, 26, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
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
        close.setCursor(QtCore.Qt.PointingHandCursor)
        close.setToolTip("不发了")
        close.clicked.connect(lambda _checked=False, target=item: self._drop_attachment(target))
        line.addWidget(close)
        self._attach_row.insertWidget(self._attach_row.count() - 1, chip)  # 排在那根弹簧前面
        self._attachments.append(item)
        self._attach_chips.append(chip)
        self._attach_bar.show()
        self._sync_send_button()  # 只挂了附件也算有内容：忙的时候那个键该是「排队」

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
        self._sync_send_button()  # 撤掉最后一条：忙的时候那个键该从「排队」变回「停止」

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
        self._sync_send_button()  # 附件撤光了：忙的时候那个键该变回「停止」

    @staticmethod
    def _attachment_pixmap(item: dict):
        """图片给 QPixmap，非图片/读不出来给 None（那就只显示文件名）。"""
        if item.get("kind") != "image":
            return None
        if item.get("data"):
            # 粘进来的图只有字节，没有文件：内存里直接解出来画缩略图
            pixmap = QtGui.QPixmap()
            return pixmap if pixmap.loadFromData(item["data"], "PNG") else None
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
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
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
        pad = 12                      # 上下各 6px，跟样式表 #input 的 padding 对上
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
            QtCore.Qt.ScrollBarAsNeeded
            if shown >= INPUT_MAX_LINES
            else QtCore.Qt.ScrollBarAlwaysOff
        )

    def _emit(self) -> None:
        message = self.input.toPlainText().strip()
        attachments = list(self._attachments)
        if self._busy:
            if self._rewind_waiting:
                self._stop_turn()  # 这次重发正在跑：这个键是停止（正文已经在引擎那边了，不重复发）
            elif message or attachments:
                self._enqueue(message, attachments)  # 这一轮跑完再发：正文里先不出现
            else:
                self._stop_turn()  # 框里空着：这会儿这个键就是「停止」
            return
        if self._rewind_waiting:
            return  # 上一次截断重发还没落地：这会儿再发会把同一段贴两遍

        if not message and not attachments:  # 只粘了图/只选了文件也能发（跟桌面一样）
            return
        self.input.clear()
        self._sync_send_button()
        self._clear_attachments()
        self.append_user(message, attachments)
        if attachments:
            # 到这一刻才交给引擎：没点发送的附件既不会落盘、也不会被模型看见
            self.attachment_picked.emit(attachments)
        self.submitted.emit(message, "typed")

    # ---- 语音输入 ----

    def _init_voice_input(self) -> None:
        """建录音器。音频接口在这台机器上用不了就把按钮关掉，不影响打字。"""
        self._voice_mode_on = False
        self._recorder = None
        self._voice_prefs = None
        # 系统级按住说话：窗口在后台也收得到，靠低层键盘钩子（细节见 global_hotkey.py）
        self._global_ptt = global_hotkey.GlobalHotkey(self)
        # 安全桌面（Ctrl+Alt+Del）那一下钩子收不到 keyup，修饰键记账会残留，
        # 此后所有组合都匹配不上。隔几秒按真实键态重记一次。
        # 兜底：事件可能丢（系统截走 keyup、切窗口、安全桌面），每 150ms 问一次真实键态。
        # 按下和松开仍然由事件立刻触发，这个定时器只负责发现「已经该收了但没人来说」。
        self._hold_armed = False   # 按下了但还没收到松开：只有这种才需要定时器兜底
        self._hold_seen_down = False  # 这个环境读不读得到键态，读得到兜底才可信
        self._hold_started_at = 0.0   # 这一轮按下的时刻，用来兜住「永远等不到松开」
        self._hold_lost_at = 0.0   # 定时器第一次发现键不在手上的时刻
        self._ptt_watch = QtCore.QTimer(self)
        self._ptt_watch.setInterval(150)
        self._ptt_watch.timeout.connect(self._hold_watch)
        self._global_ptt.pressed.connect(self._hold_begin)
        self._global_ptt.released.connect(self._hold_end)
        self._voice_draft_focus = False
        # 语音模式下把子控件的焦点策略全收走，退出时按原样还回去：
        # 面板靠 keyPressEvent 接空格，但只要任何一个按钮拿着焦点，空格就先被它吃掉
        # （「新会话」那颗按钮就是——按一下直接开新会话）。
        self._voice_focus_backup: dict = {}
        self._audio_released = False
        # 「按住说话的键」：单独一颗键帽外形的按钮，点开在弹窗里改；外形不随键名变
        self._ptt_button = QtWidgets.QToolButton(self._inputbox)
        self._ptt_button.setObjectName("icon")
        self._ptt_button.setIcon(_glyph_icon("keycap"))
        self._ptt_button.setIconSize(QtCore.QSize(14, 14))
        self._ptt_button.setFixedSize(22, 22)
        self._ptt_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._ptt_button.setFocusPolicy(QtCore.Qt.NoFocus)
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
                recorder.preload(remembered)  # 只记住选择，不在这里开流
            self._recorder = recorder
            self._voice_prefs = voice_input_prefs
        except Exception:  # noqa: BLE001 - 音频初始化失败不该让整个面板起不来
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
            # 记下切走前焦点在不在输入框，切回来要还给她
            self._voice_draft_focus = self.input.hasFocus()
            self._voice_orb.set_state(STATE_IDLE)
            self.input.hide()
            # 整条输入区只留球：＋、审批、思考、喇叭全收起来，球居中才有「轮到说话」的样子
            for button in (self._attach_button, self._approval_button,
                           self._reasoning_button, self._voice_button, self._send_button):
                button.hide()
            self._box_row.setContentsMargins(9, 7, 4, 7)  # 球别贴着上下边框
            self._set_inputbox_voice(True)
            self._voice_orb.show()
            self._float_mode_button(True)
            # 空格要留给「按住说话」：面板自己拿着焦点，**所有**子控件都不许吃空格。
            # 只关麦克风按钮和聊天记录区是不够的——头部那排按钮（尤其「新会话」）都是
            # 默认可聚焦的，鼠标点一下面板恰好点在它上面，之后按空格就是「按下那个按钮」。
            self._voice_focus_backup = {}
            for child in self.findChildren(QtWidgets.QWidget):
                policy = child.focusPolicy()
                if policy != QtCore.Qt.NoFocus:
                    self._voice_focus_backup[child] = policy
                    child.setFocusPolicy(QtCore.Qt.NoFocus)
            self.setFocus()
            self._mic_button.setIcon(_glyph_icon("keyboard", "#ffffff"))
            self._mic_button.setToolTip(
                f"语音已开：按住球或 {self._ptt_label()} 说话，松手就发"
                "（点一下回到打字，右键换麦克风）"
            )
            try:
                self._recorder.open_stream()
            except Exception as exc:  # noqa: BLE001 - 开流崩了别让整条输入区跟着塌
                logger.warning("[语音] 打开录音流失败", exc_info=True)
                self._voice_flash(f"麦克风打不开：{exc}")
            self.voice_input_opened.emit()
            self._apply_global_ptt()  # 进了语音模式才挂系统级钩子：不开语音时不碰键盘
            self._ptt_watch.start()
        else:
            self._ptt_watch.stop()
            self._global_ptt.stop()  # 回到打字：钩子立刻卸掉，别占着键
            try:
                self._recorder.cancel()
                self._recorder.close_stream()
            except Exception:  # noqa: BLE001 - 收流失败也得把界面退回打字，不能卡在语音布局里
                logger.warning("[语音] 关录音流失败", exc_info=True)
            self._voice_orb.hide()
            self._voice_orb.set_state(STATE_IDLE)
            for button in (self._attach_button, self._approval_button,
                           self._reasoning_button, self._voice_button, self._send_button):
                button.show()
            self._box_row.setContentsMargins(9, 0, 4, 0)
            self._set_inputbox_voice(False)
            self._float_mode_button(False)
            # 焦点策略按进语音模式时记下的原样还回去，打字模式的 Tab 导航才不受影响
            for child, policy in self._voice_focus_backup.items():
                try:
                    child.setFocusPolicy(policy)
                except RuntimeError:  # 控件连 C++ 对象一起没了（菜单重建之类）
                    pass
            self._voice_focus_backup = {}
            self._mic_button.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.view.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.input.show()
            # 藏起来的时候量不到宽度，显示之后重新按当前宽度算行数
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
        # 属性变了 Qt 不会自己重算样式，得手动 unpolish/polish 一遍
        style = self._inputbox.style()
        style.unpolish(self._inputbox)
        style.polish(self._inputbox)

    def _voice_hold_start(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        try:
            endpoint = recorder.current()
        except Exception as exc:  # noqa: BLE001 - 枚举设备崩了也得让人看到，不能按了没反应
            logger.warning("[语音] 取麦克风失败", exc_info=True)
            self._voice_flash(f"没法确认用哪只麦克风：{exc}")
            return
        if endpoint is None:
            # 先分清「没麦」和「在开」：两种都说成「还在准备」会让人一直等下去
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
            # 流还在打开（蓝牙要先唤醒链路，几百毫秒）：这次按下不算数，球退回去
            self._voice_orb.set_state(STATE_IDLE)
            self.show_notice("voice-input", "麦克风还在准备，稍等一下再说", ttl_ms=2500, level="warn")
            return
        # 真开始录了才掐朗读：她正在念的时候开口，这段录音里全是她自己的声音
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
            # 整段都是数字静音：设备没在收音，跟「没说话」是两回事，得分开说
            self._voice_flash(f"「{recorder.endpoint_name()}」没收到声音，看看是不是戴歪了")
            return
        try:
            path = self._voice_temp_path()   # mkdir 也可能失败（只读盘/被占/满了），一起接住
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
            # 只收自己那条：这 1.6 秒里人可能已经重新按住开录了，
            # 无条件置 IDLE 会把录音中的球按回去，松手时就再也收不到「停」。
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
        self._voice_orb.set_state(STATE_IDLE)  # 球先回待机，能接着按住说下一句
        if self._busy:
            # 语音是插话，不是排队：这一轮不能掐死。掐了插进去的这句跟着一起作废——
            # 日志实测 `/steer ... as new user message` 之后 9 毫秒就
            # `Turn ended: reason=interrupted_by_user`，屏幕上就是一个回答都没有。
            # 所以只把手上这段收尾、把你的话插进去，这句话照常发出去（不带 queued），
            # 引擎按「纠正」递进这一轮，她接着往下说，后面的字进新的一段。
            # 闸也不关：这一轮还在跑，关了就把它剩下的字也丢了。
            if self._streaming:
                self.end_assistant()
            self.append_user(text, [])
            self.submitted.emit(text, "voice")
            return
        self.append_user(text, [])
        # 标成 voice：这一条是模型听出来的，可能有错字，不能拿去当学词的语料
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
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住程序退出
            logger.debug("[语音] 收尾失败", exc_info=True)

    @staticmethod
    def _discard_voice_file(path: str) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.debug("[语音] 删不掉临时录音 %s", path, exc_info=True)

    # ---------------------------------------------------------- 按住说话的键

    def _ptt_sequence(self) -> QtGui.QKeySequence:
        """按住说话用的键；没配过或配坏了就当空格。"""
        stored = ""
        if self._voice_prefs is not None:
            try:
                stored = self._voice_prefs.hotkey()
            except Exception:  # noqa: BLE001 - 配置读坏了也得能按住说话
                stored = ""
        if stored:
            sequence = QtGui.QKeySequence(stored)
            # Qt 拿到不认识的字符串不报错，只是转回来是空的：这种当没配过
            if sequence.count() == 1 and sequence.toString(QtGui.QKeySequence.PortableText):
                return sequence
        return QtGui.QKeySequence(QtCore.Qt.Key_Space)

    def _ptt_label(self) -> str:
        """给人看的键名：空格 / F2 / Ctrl+空格。"""
        return _key_name(self._ptt_sequence())

    # ---- 系统级按住说话 ----

    def _global_ptt_sequence(self) -> QtGui.QKeySequence:
        """系统级按住说话的键；没配过或配坏了就用 Shift+空格。"""
        stored = ""
        if self._voice_prefs is not None:
            try:
                stored = self._voice_prefs.global_hotkey()
            except Exception:  # noqa: BLE001 - 配置读坏了也得能按住说话
                stored = ""
        if stored:
            sequence = QtGui.QKeySequence(stored)
            if sequence.count() == 1 and sequence.toString(QtGui.QKeySequence.PortableText):
                return sequence
        return QtGui.QKeySequence(QtCore.Qt.SHIFT | QtCore.Qt.Key_Space)

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
            # 不替人换键：被占就是这个键用不了，说清楚让他自己改
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
            # 探过之后还装不上（钩子被系统拒）：不能只写日志——用户按这颗键会一点反应都没有，
            # 还以为自己按错了。说清楚「后台用不了、窗口里照常」。
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
        expected = sequence[0]
        actual = event.keyCombination()
        return (
            expected.key() == actual.key()
            and expected.keyboardModifiers() == actual.keyboardModifiers()
        )

    def _ptt_key_is(self, event) -> bool:
        """这个松开是不是按住说话那个主键。松手只认主键——那时候修饰键可能已经先松了。"""
        key = event.keyCombination().key()
        return any(
            seq.count() == 1 and seq[0].key() == key
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
            self._hold_armed = True    # 只记「按下了、还没收到松开」
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
            self._hold_seen_down = True   # 这个环境读得到键态，下面那条才可信
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
        dialog.deleteLater()   # 带 parent 的对话框不销毁，每开一次面板下就多一个
        if not accepted:
            return
        if self._voice_prefs is not None:
            try:
                # 存 PortableText：换系统语言也认得回来；空格存空串跟默认值同写法
                if window_sequence is not None:
                    portable = window_sequence.toString(QtGui.QKeySequence.PortableText)
                    if portable:
                        self._voice_prefs.remember_hotkey("" if portable == "Space" else portable)
                if global_sequence is not None:
                    portable = global_sequence.toString(QtGui.QKeySequence.PortableText)
                    if portable:
                        # 默认就是 Shift+Space，选回它也存空串
                        self._voice_prefs.remember_global_hotkey(
                            "" if portable == "Shift+Space" else portable
                        )
            except (OSError, voice_input_prefs.ConfigError) as exc:
                logger.warning("[语音] 记不住快捷键：%s", exc)
                self.show_notice("voice-input", f"快捷键没存上：{exc}", ttl_ms=5000, level="warn")
        self._refresh_ptt_button()
        self._apply_global_ptt()  # 换了系统级键要当场重装，别等下次开语音
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
        group = QtGui.QActionGroup(menu)  # 下面「跟随系统默认」也要进这一组，所以先建好
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
        # 跟上面那些麦克风同一组：选了具体设备就不该同时勾着「跟随系统默认」
        follow.setActionGroup(group)
        follow.setChecked(not recorder.chosen_guid())
        follow.triggered.connect(lambda _checked=False: self._choose_mic(""))
        menu.exec(self._mic_button.mapToGlobal(pos))
        menu.deleteLater()   # 带 parent 的菜单不销毁，每右键一次就多挂一整套

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

    # ---- 交互：拖动 / Esc 收起 ----

    # ---- 尺寸上限与位置 ----

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
            self._settings.setValue("panel/pos", self.pos())  # 下次打开回到你上次放的地方
            self._settings.sync()
        except Exception:  # 目录只读/被杀软锁住：位置丢了就丢了，不能把窗口带崩
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
        super().resizeEvent(event)
        if hasattr(self, "_model_menu"):
            self._sync_popup_width()
        title = getattr(self, "_title_label", None)
        if title is not None:
            title.setText(TITLE_FULL if self.width() >= TITLE_FULL_MIN_WIDTH else TITLE_SHORT)
        self._place_background()
        self._place_jump()  # 窗口一变，贴底的胶囊跟着重新贴底
        self._replace_msg_actions()  # 正文重排后，悬停条也得跟着落回它那一行

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        # 隐藏时的 resize 不会立刻派发，显示出来时再对一次宽度
        super().showEvent(event)
        self._sync_popup_width()

    # ---- 交互：拖动 / 边缘缩放 / Esc 收起 ----

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
                event.acceptProposedAction()   # 接住：光标才会变成「可以放这儿」
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

    # ---- 悬停小动作：代码块复制 / 工具行看完整 diff ----

    def _hover_at(self, pos: QtCore.QPoint) -> None:
        """鼠标在正文里移动：先判出它落在哪个区域，换了区域才换按钮。

        区域判定和按钮摆放分成两件事：这里只问「在哪」，下面 _open_zone 只负责
        「那个区域该亮哪颗」。所以每条分支里都没有「收掉」这种补救动作——
        上一个区域的按钮在换区时整体收掉，一次。
        """
        if self._over_hover_button():
            return  # 鼠标压在悬停条上：别在它下面把按钮收掉（那一下连提示都弹不出来）
        if self._in_msg_band(pos):
            return  # 走到这条消息末尾下方那一格（按钮就停在那儿）：保持不动，一收就永远点不到
        zone, block = self._zone_at(pos)
        if zone == self._zone:
            return  # 没换区域：按钮就在原地，鼠标动一下不必重做
        self._hide_hover()  # 换区：上一个区域的按钮一次收干净
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
        # 图片块带着工具正文的标记，但它自己是一张图：先按图片处理
        if self._block_has_image(block):
            hit = self._photo_at(pos)
            return (("photo",) + hit) if hit else None, block
        mark = block.userData()
        if isinstance(mark, disclosure._BodyMark):
            return ("body", mark.key), block
        msg = self._message_at(block)
        if msg is not None:
            # 一条消息占好几段，用消息本身认，别用块号——那样在一条里上下挪会一直换区、按钮直闪
            return ("msg", id(msg)), block
        return None, block

    def _open_zone(self, zone: tuple, block: QtGui.QTextBlock) -> None:
        """给当前区域亮它自己那颗按钮。别的区域的按钮在这条路径上根本不出现。"""
        kind = zone[0]
        if kind == "code":
            fence = self._code_fence_at(block)
            if fence is not None:
                first, last, text = fence
                self._show_hover_copy(text, tool_tr("assistant.tool.copyCode"), first, last, edge=True)
        elif kind == "photo":
            self._show_hover_download(zone[1], zone[2])
        elif kind == "body":
            mark = block.userData()
            if isinstance(mark, disclosure._BodyMark):
                self._open_body_zone(block, mark.key)
        elif kind == "msg":
            msg = self._message_at(block)
            if msg is not None:
                self._show_msg_actions(msg, block)

    def _open_body_zone(self, block: QtGui.QTextBlock, key: int) -> None:
        """工具输出正文：能复制就给复制，另外有 diff 才给「看完整 diff」。"""
        first, last = self._marked_run(block, key)
        payload = self._tool_payloads.get(key)
        if payload is None and key in self._diff_texts:
            # 没算出 payload（例如只有 diff）就拿 diff 当复制内容，文案用「复制内容」
            payload = (tool_tr("assistant.tool.copyContent"), self._diff_texts[key][1])
        if payload and payload[1].strip():
            self._show_hover_copy(payload[1], payload[0], first, last, edge=True)
        if key in self._diff_texts:
            self._show_hover_expand(key, first, last)

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

    def _in_msg_band(self, pos: QtCore.QPoint) -> bool:
        """鼠标是否还在这条消息末尾那一带（最后一行 + 下面留给按钮的那一格）。"""
        return self._msg_band is not None and self._msg_band.contains(pos)

    def _over_hover_button(self) -> bool:
        """鼠标此刻是不是压在某颗悬停按钮上（压在按钮上时正文会收到 Leave，那不是「离开」）。"""
        pos = self.view.viewport().mapFromGlobal(QtGui.QCursor.pos())
        for button in (self._hover_copy, self._hover_expand, self._hover_download, *self._msg_buttons):
            if button is not None and button.isVisible() and button.geometry().contains(pos):
                return True
        return False

    def _block_at(self, pos: QtCore.QPoint) -> QtGui.QTextBlock | None:
        if not self.view.rect().contains(pos):
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
        cache_key = (first.blockNumber(), last.blockNumber())
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
            if len(self._fence_cache) > 64:   # 只记最近看过的几块，别让缓存自己长大
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
        rect = QtCore.QRect(
            int(min(top.left(), bottom.left())),
            int(top.top()),
            int(max(top.width(), bottom.width())),
            int(bottom.bottom() - top.top()),
        )
        rect.translate(-self.view.horizontalScrollBar().value(), -self.view.verticalScrollBar().value())
        return rect

    def _show_hover_copy(self, text: str, tooltip: str, first, last, edge: bool = False) -> None:
        """复制按钮：点完写剪贴板、换 ✓、1.5 秒还原。"""
        if self._hover_copy is None:
            self._hover_copy = HoverActionButton(
                self.view.viewport(),
                "copy",
                tooltip,
                lambda: None,
                tool_tr("common.copied"),
            )
        self._hover_copy.retarget(
            "copy",
            tooltip,
            lambda value=text: self._copy_to_clipboard(value),
            tool_tr("common.copied"),
        )
        self._place_hover(self._hover_copy, first, last, edge=edge)

    def _show_hover_expand(self, key: int, first, last) -> None:
        """放大按钮：只开预览窗，不换 ✓（那不是一次复制）。放在复制按钮左边。"""
        if self._hover_expand is None:
            self._hover_expand = HoverActionButton(
                self.view.viewport(),
                "expand",
                DIFF_PREVIEW_LABEL,
                lambda: None,
                flash=False,
            )
        self._hover_expand.retarget(
            "expand",
            DIFF_PREVIEW_LABEL,
            lambda target=key: self._open_diff_preview(target),
        )
        self._place_hover(self._hover_expand, first, last,
                          offset_x=-(HoverActionButton.BUTTON_PX + 2), edge=True)

    # ---- 消息级动作：复制整段 / 引用 / 编辑重发 / 重新生成 ----

    def _begin_message(self, role: str, start: int, text: str, row_id) -> None:
        """给刚插进来的那条消息的开头块打标记，并记进清单。

        `start` 是插入**前**的文末字符位：插入时若新开了一段，这条消息就从下一段开始
        （`next().position() == start + 1` 正好说明段界开在这里）。
        """
        document = self.view.document()
        block = document.findBlock(start)
        following = block.next()
        if following.isValid() and following.position() == start + 1:
            block = following
        msg = {"role": role, "text": text, "row_id": row_id}
        msg["mark"] = _MsgMark(msg)
        block.setUserData(msg["mark"])
        # 两条消息的按钮都挂在末尾下方，所以每条后面都留出一格
        if self._msgs:
            head_format = block.blockFormat()
            head_format.setTopMargin(HoverActionButton.BUTTON_PX + 10)
            QtGui.QTextCursor(block).setBlockFormat(head_format)
        if role == "user":
            # 你说的话和她在同一列左起：靠气泡和字号分出谁在说，不靠左右分栏
            user_format = block.blockFormat()
            user_format.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            user_format.setLeftMargin(ChatView.BUBBLE_PAD_X)
            user_format.setRightMargin(ChatView.BUBBLE_PAD_X)
            # 下边距留够：气泡往下画 8px，下面那行（「思考中」之类）还要再让 8px 才不贴脸
            user_format.setBottomMargin(ChatView.BUBBLE_PAD_Y + 10)
            # topMargin 留着不动：上一条是回答时，这块里已经有 34px 的按钮位，盖掉按钮就压字上了
            QtGui.QTextCursor(block).setBlockFormat(user_format)
        self._msgs.append(msg)
        if role == "assistant":
            self._stream_msg = msg

    def _message_at(self, block) -> dict | None:
        """从光标所在块往上找：这条消息是从哪一段开始的。

        「这块自己就有按钮」的才不认领：代码块（`_CodeMark`）和折叠段正文（`_BodyMark`）
        各管各的（复制代码 / 看完整 diff）。往上走的时候遇到它们**只是跳过**：一条回答里
        夹着工具行、代码块是常态，后面接着写的正文照样属于这条消息。以前遇到就停，于是
        代码块/工具行**下面**那几段永远认领不到消息——滚到最底下时看见的正是那几段，
        复制/刷新整条都不出现。
        """
        if isinstance(block.userData(), (_CodeMark, disclosure._BodyMark)):
            return None
        probe = block
        while probe.isValid():
            mark = probe.userData()
            if isinstance(mark, _MsgMark):
                return mark.msg
            probe = probe.previous()
        return None

    def _msg_head_block(self, msg: dict, near=None):
        """这条消息开头那一块（从鼠标所在块往上找；找不到再从头往后扫一遍）。"""
        mark = msg.get("mark")
        if mark is None:
            return None
        if near is None or not near.isValid():
            probe = self.view.document().begin()
            while probe.isValid():
                if probe.userData() is mark:
                    return probe
                probe = probe.next()
            return None
        probe = near
        while probe.isValid():
            if probe.userData() is mark:
                return probe
            probe = probe.previous()
        return self._msg_head_block(msg, None)

    def _msg_tail_block(self, msg: dict):
        """这条消息的最后一段：下一条消息开头的前一段；最后一条就取文末那一段。

        「文末」不能照字面用 `lastBlock()`：一轮正在跑的时候，文末是**这一轮刚流出来的
        思考段/工具行**，它们不属于任何一条消息。照字面取的结果就是按钮挂在思考段下面
        几十像素的空白处（他截图里那两颗按钮离自己的话两行远）。
        所以往回退：折叠正文、折叠抬头、代码块都要跳过，落到这条消息自己最后一行字上。
        """
        document = self.view.document()
        following = None
        seen = False
        for item in self._msgs:
            if item is msg:
                seen = True
                continue
            if seen:
                following = item
                break
        if following is None:
            tail = self._skip_blank_back(document.lastBlock())
        else:
            head = self._msg_head_block(following, None)
            tail = self._skip_blank_back(head.previous()) if head is not None else None
        mine = self._msg_head_block(msg, None)
        heads = {f.head_block.position() for f in self._folds.all() if f.head_block is not None}
        while tail is not None and tail.isValid() and mine is not None and tail is not mine:
            mark = tail.userData()
            if not isinstance(mark, (_CodeMark, disclosure._BodyMark)) and tail.position() not in heads:
                break
            previous = tail.previous()
            if not previous.isValid():
                break
            tail = self._skip_blank_back(previous)
        return tail

    @staticmethod
    def _skip_blank_back(block):
        """消息末尾常留一个空段用来间隔：往回收掉，落在这条消息真正有字的最后一行。"""
        while block.isValid() and block.previous().isValid() and not block.text().strip():
            block = block.previous()
        return block

    def _reserve_tail_space(self) -> None:
        """文末留出悬停条那一格：最后一条回答的按钮挂在正文下方 6px，

        不留这一格的话，滚到最底下时下面就是视口边缘——`tail_visible` 那条判断永远过不去，
        按钮就永远不出现（往上滚到别的消息才看得见）。`view.clear()` 会把根框架格式冲掉，
        所以清窗口之后要重新铺一次。
        """
        root = self.view.document().rootFrame()
        fmt = root.frameFormat()
        fmt.setBottomMargin(HoverActionButton.BUTTON_PX + 12)
        root.setFrameFormat(fmt)

    def _ensure_msg_buttons(self, count: int) -> None:
        """按这条消息要露几个动作准备几颗按钮（回答两颗：复制、刷新；用户一颗：重新发送/停止）。

        按需建、只增不减：换到别的消息时是 retarget 复用，不销毁重来。
        """
        while len(self._msg_buttons) < count:
            self._msg_buttons.append(
                HoverActionButton(self.view.viewport(), "copy", "复制", lambda: None)
            )

    def _msg_action_specs(
        self, msg: dict,
    ) -> list[tuple[str, str, Callable[[dict], None], bool]]:
        """这条消息该露哪几个动作，顺序照桌面端从左往右；第四个值是可不可点。

        回答那条：复制 + 刷新。桌面端那条「刷新」在**每一条回答**上都摆着，只有末尾那条能点，
        其余灰着（`ActionBarPrimitive.Reload` 自己按 isLast 禁用）；还有朗读和表情回应，
        那两个能力我们还没有，不摆空壳。
        用户那条：一颗，最新那条在跑时是停止，其余时候是重新发送。
        """
        if not str(msg.get("text") or "").strip():
            return []
        if msg.get("role") == "user":
            if self._busy and self._latest_user() is msg:
                return [("stop", "停止", lambda _m: self._stop_turn(), True)]
            if msg.get("row_id") is not None:
                return [("resend", "重新发送", self._resend_from, True)]
            return []
        return [
            ("copy", "复制", self._copy_message, True),
            ("refresh", "刷新", self._request_regenerate, self._can_regenerate(msg)),
        ]

    def _show_msg_actions(self, msg: dict, block) -> None:
        specs = self._msg_action_specs(msg)
        if not specs:
            self._hide_msg_actions()
            return
        self._ensure_msg_buttons(len(specs))
        if self._msg_shown is not msg:
            # 悬停条换到别的消息了：上一行留下的提示是独立小窗，不会自己走
            QtWidgets.QToolTip.hideText()
        self._msg_shown = msg
        # 条子挂在消息**最后一行**上：用户那条并排在这行右端（右边空着），回答另起一行挂在下面，
        # 都不压字。末尾那一行必须整行在屏幕里，否则不挂——不然按钮会漂在窗口中间。
        head = self._msg_head_block(msg, block) or block
        tail = self._msg_tail_block(msg) or head
        # 尾巴要落在「真正的正文」上：工具行、代码块属于它们自己，别把整条消息的按钮挂到那儿
        while (
            tail is not None
            and tail.isValid()
            and tail is not head
            and isinstance(tail.userData(), (_CodeMark, disclosure._BodyMark))
        ):
            previous = tail.previous()
            if not previous.isValid():
                break
            tail = previous
        tail_rect = self._block_rect(tail, tail)
        view_height = self.view.viewport().height()
        tail_visible = (
            tail_rect.height() > 0
            and tail_rect.bottom() >= 0
            and tail_rect.bottom() + 6 + HoverActionButton.BUTTON_PX <= view_height
        )
        if not tail_visible:
            self._hide_msg_actions()
            return
        inline = msg.get("role") == "user"
        band_height = tail_rect.height() + HoverActionButton.BUTTON_PX + 10
        self._msg_band = QtCore.QRect(0, tail_rect.top(), self.view.viewport().width(), band_height)
        buttons = self._msg_buttons[: len(specs)]
        if len(buttons) < len(specs):  # _ensure_msg_buttons() 刚建过，这里只是让类型检查看得见
            return
        # 按钮簇：你说的那条摆进气泡右下角（垫一层气泡色把字尾盖住）；
        # 她的回答另起一行贴右侧。都右对齐、从右往左排。
        step = HoverActionButton.BUTTON_PX + 12
        bubble = self.view.bubble_rect(head) if inline else None
        for index, (icon, tip, slot, usable) in enumerate(specs):
            button = buttons[index]
            button.retarget(icon, tip, lambda m=msg, s=slot: s(m))
            button.set_enabled(usable)
            button.set_backdrop(bubble is not None)
            if bubble is not None:
                button.place_at(bubble.toRect(), offset_x=-index * step, bottom_align=True)
                button.show()
                button.raise_()
                continue
            self._place_hover(
                button, tail, tail, offset_x=-index * step, below=True, anchor_right=True
            )

    def _hide_msg_actions(self) -> None:
        # 内容重铺、历史变动时都会从这里收掉悬停条：区域记忆一并作废，
        # 否则鼠标还停在同一条消息里，再动也不会重新亮出来
        self._zone = None
        self._msg_band = None
        self._msg_shown = None
        for button in self._msg_buttons:
            button.hide()

    def _replace_msg_actions(self) -> None:
        """窗口缩放/正文重新排版后，悬停条得按新的行位置重摆——不然按钮留在老地方压着别的字。"""
        msg = self._msg_shown
        if msg is None or not self._msg_buttons:
            return
        head = self._msg_head_block(msg, None)
        if head is None:
            self._hide_msg_actions()
            return
        self._show_msg_actions(msg, head)

    def _resend_from(self, msg: dict) -> None:
        """重新发送：从这条起把后面全删掉，立刻重跑这句话（桌面端那颗 discard）。

        桌面端先弹一句「此提示之后的所有消息将从对话中移除」再动手——删的是真对话历史，
        收不回来。确认之后就按原来的正文重跑，不回到输入框让人改（那是编辑框的事）。
        """
        if msg.get("row_id") is None:
            return
        if not ConfirmDialog.ask(
            self.window(),
            "恢复到此检查点？",
            "此提示之后的所有消息将从对话中移除，并从此处重新运行该提示。",
            "恢复并重新运行",
        ):
            return  # 取消就是不动：历史一条都不删
        self._hide_msg_actions()
        text = str(msg.get("text") or "")
        if self._busy:
            # 这一轮还在流：桌面端是先叫停再恢复，停稳了（settle_turn）才真发出去
            self._rewind_after_stop = (msg, text)
            self._stop_turn()
            return
        self._emit_rewind(msg, text, False)

    def _can_regenerate(self, msg: dict) -> bool:
        """只有最后一条回答能重新生成：截断点由它前面那条用户消息定。"""
        if not self._msgs or self._msgs[-1] is not msg:
            return False
        before = self._previous_user(msg)
        return bool(before and before.get("row_id") is not None)

    def _latest_user(self) -> dict | None:
        """窗口里最后一条用户消息（桌面端拿它决定那颗按钮是停止还是重新发送）。"""
        for item in reversed(self._msgs):
            if item.get("role") == "user":
                return item
        return None

    def _previous_user(self, msg: dict) -> dict | None:
        found = None
        for item in self._msgs:
            if item is msg:
                break
            if item.get("role") == "user":
                found = item
        return found

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

    def _request_regenerate(self, msg: dict) -> None:
        """刷新：拿这条回答前面那句原话重跑一遍（桌面端的 RefreshCw 不问、直接来）。"""
        user_msg = self._previous_user(msg)
        if user_msg is None:
            return
        self._emit_rewind(user_msg, str(user_msg.get("text") or ""), True)

    def apply_rewind(self, msg: dict, text: str) -> None:
        """引擎已经按这条消息截断过历史了：窗口回到那一刻，再把正文重新贴一遍。"""
        self._rewind_waiting = False
        self._truncate_from(msg)
        self.append_user(text)
        if self.input.toPlainText().strip() == text.strip():
            # 这段正文已经进了对话，输入框别再留一份（不然看着像没发出去）
            self.input.clear()
        self._sync_send_button()  # 等待态结束：按钮从「重发中」恢复

    def cancel_rewind(self) -> None:
        """这次截断重发没成立（被拒 / 换了会话）：正文留在框里，发送键恢复原样。"""
        self._rewind_waiting = False
        self._row_stamp_pending = None  # 那一下的行号作废：别落到之后别的消息上
        self._sync_send_button()

    def _truncate_from(self, msg: dict) -> None:
        """把这个窗口从某条消息起的内容抹掉（引擎侧已经截断，界面跟上）。"""
        mark = msg.get("mark")
        document = self.view.document()
        target = None
        probe = document.begin()
        while probe.isValid():
            if probe.userData() is mark:
                target = probe
                break
            probe = probe.next()
        if target is None:
            return
        cursor = QtGui.QTextCursor(document)
        cursor.setPosition(target.position())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End, QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        for index, item in enumerate(self._msgs):
            if item is msg:
                self._msgs = self._msgs[:index]
                break
        self._stream_msg = None
        self._streaming = False
        self._assistant_pending = ""
        self._md_block = []
        self._md_fence = False
        self._reason_fold = None
        # 工具行的状态跟着一起抹：秒表还在 tick 一个已经不在文档里的标签，
        # fold 留着还会让 `has_transcript()` 永远为真，界面就以为这里有工具过程可看。
        self._tool_started.clear()
        self._tool_folds.clear()
        self._tool_clock.stop()
        self._folds.drop_invalid()
        self._hide_msg_actions()

    def stamp_user_row(self, row_id: int) -> None:
        """引擎回了这次提交的用户消息行号：重新生成/编辑重发靠它定位截断点。"""
        for msg in reversed(self._msgs):
            if msg.get("role") == "user" and msg.get("row_id") is None:
                msg["row_id"] = int(row_id)
                return
        # 截断重发时这个回包比正文先到（窗口要等第一段文字才动），新消息还没贴上去：
        # 先记着，等 `append_user` 贴的时候再落。不留这一步，那条消息永远没有行号，
        # 「刷新 / 重新发送」用过一次就再也不出现了。
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
        # 回执按顺序给这一轮的用户行（含 steer 行）：只取末尾还没绑上的那几条
        for msg, rid in zip(waiting, ints[-len(waiting):]):
            msg["row_id"] = rid
        self._row_stamp_pending = None  # 已经有真行号了，之前存的别留给下一条

    def is_busy(self) -> bool:
        return bool(self._busy)

    def queued_count(self) -> int:
        return len(self._queued)

    def _place_hover(self, button: HoverActionButton, first, last, offset_x: int = 0,
                     below: bool = False, bottom_align: bool = False,
                     anchor_right: bool = False, edge: bool = False) -> None:
        rect = self._block_rect(first, last)
        if rect.height() <= 0:
            button.hide()
            return
        # 按钮不压滚动条：右边界一律收到可用宽度以内
        bar = self.view.verticalScrollBar()
        usable = self.view.viewport().width() - (bar.width() if bar.isVisible() else 0)
        if edge:
            # 代码块、工具输出的底色整行铺满：贴内容的右边界，不是字的末尾
            rect.setRight(usable)
        else:
            rect.setRight(min(rect.right(), usable))
        button.place_at(rect, offset_x=offset_x, below=below, bottom_align=bottom_align,
                        anchor_right=anchor_right)
        button.show()
        button.raise_()

    def _hide_copy(self) -> None:
        if self._hover_copy is not None:
            self._hover_copy.hide()

    def _hide_expand(self) -> None:
        if self._hover_expand is not None:
            self._hover_expand.hide()

    def _hide_hover(self) -> None:
        self._hide_copy()
        self._hide_expand()
        self._hide_download()
        self._hide_msg_actions()

    def _copy_to_clipboard(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)

    def _copy_message(self, msg: dict) -> None:
        """复制这条回答的正文（桌面端那颗复制图标；用户自己说的话在桌面端没有复制）。"""
        text = str(msg.get("text") or "")
        if text:
            self._copy_to_clipboard(text)

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
        window.show()
        window.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """frame 铺满整个窗口，边缘那圈空白上的鼠标事件在这儿接住。"""
        if obj is self.view.viewport():
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseMove:
                point = event.position().toPoint()
                self._sync_edge_cursor(self.view.viewport().mapTo(self, point))
                self._hover_at(point)
                self._point_at_photo(point)
            elif kind is QtCore.QEvent.Type.MouseButtonPress:
                self._press_pos = event.position().toPoint()
            elif kind is QtCore.QEvent.Type.MouseButtonRelease and (
                event.button() is QtCore.Qt.LeftButton
            ):
                start = self._press_pos
                point = event.position().toPoint()
                # 拖过一段的是在选字，不是点图；按下没挪动才算「点这张图」
                still = start is not None and (point - start).manhattanLength() <= 5
                if still:
                    hit = self._photo_at(point)
                    if hit:
                        # 松手这会儿还在处理事件，延到下一拍再开窗
                        QtCore.QTimer.singleShot(0, lambda k=hit[0]: self._open_photo(k))
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Wheel):
                if kind is QtCore.QEvent.Type.Leave and self._over_hover_button():
                    return super().eventFilter(obj, event)  # 压在悬停按钮上：不算离开正文
                self._hide_hover()
                self.view.viewport().unsetCursor()
                if kind is QtCore.QEvent.Type.Wheel:
                    self._user_scroll_now()  # 真滚动发生在下面放行之后
            elif kind is QtCore.QEvent.Type.Resize:
                self._place_jump()  # 正文区尺寸变了（输入框长高、窗口拉宽），胶囊重新贴底
                self._replace_msg_actions()  # 行重新折过了，悬停条跟着重摆
                if self._follow and not self._restoring:
                    self._scroll_to_end_later()  # 窗口变小、正文变高：跟着看的话别停在半空
            return super().eventFilter(obj, event)
        if obj is self.view and event.type() is QtCore.QEvent.Type.KeyPress:
            # 方向键 / PageUp 翻正文：同样算用户自己滚的
            if event.key() in _SCROLL_KEYS:
                self._user_scroll_now()
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
                # 组字中的回车是「确认候选词」，不是发送——不然打「nihao」按回车会直接发出去
                self._ime_composing = bool(event.preeditString())
                self._sync_input_ph()  # 组字也算有内容：提示立刻让开，别压着拼音
            elif kind is QtCore.QEvent.Type.KeyPress:
                if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and not getattr(
                    self, "_ime_composing", False
                ):
                    if event.modifiers() & QtCore.Qt.ShiftModifier:
                        return super().eventFilter(obj, event)  # 放行：插一个换行
                    self._emit()
                    return True                                 # 吞掉，别让它再插一个换行
                if self._is_paste_key(event):
                    # Ctrl+V / Shift+Insert：剪贴板是图或文件就接成附件；是纯文字就放回去让它自己粘。
                    # 注意 `text/uri-list`（资源管理器 Ctrl+C）在 Qt 里 hasText() 也为真，
                    # 所以「有 urls」要先按文件算，不能让纯文字那条把它挡掉。
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
                self._fit_input_height()   # 宽度变了软换行的位置就变，行数要重算
            return super().eventFilter(obj, event)
        if getattr(self, "input", None) is not None and obj is self.input.viewport():
            # 拖放通常落在这儿：多行控件是滚动区，拖进来的目标其实是 viewport
            if self._handle_input_drag(event):
                return True
            return super().eventFilter(obj, event)
        if obj is getattr(self, "_inputbox", None) and event.type() is QtCore.QEvent.Type.Resize:
            # 语音模式下那颗「切回打字」的按钮是浮在右上角的，输入框一变宽就得重摆
            if self._mic_button.parent() is self._inputbox:
                self._place_mode_button()
            return super().eventFilter(obj, event)
        if obj is getattr(self, "_frame", None):
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseButtonPress:
                if event.button() is QtCore.Qt.LeftButton and self._begin_resize(
                    event.position().toPoint(), event.globalPosition().toPoint()
                ):
                    return True
            elif kind is QtCore.QEvent.Type.MouseMove and not (
                event.buttons() & QtCore.Qt.LeftButton
            ):
                self._sync_edge_cursor(event.position().toPoint())
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() is QtCore.Qt.LeftButton:
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
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            # 拖的过程只跟手，不吸——吸住会从指头底下溜走，松手时才归位
            self._snapped = False  # 自己动手拖过，就不再算「吸在她下面」
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
            self._snap_under_anchor()  # 松手这一下才吸：拖的时候它得跟着手走
        if resized or dragged:
            self._save_geometry()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self._global_ptt.stop()  # 程序关掉必须把钩子卸了，不然键盘一直被我们截着
        self._hold_end()           # 正在录就收尾，麦克风不能跟着程序一起留着
        self._save_geometry()
        super().closeEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802
        self._save_geometry()  # Esc 收起、切走都算「上次关掉的位置」
        super().hideEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if self._voice_mode_on and self._ptt_matches(event):
            event.accept()
            if not event.isAutoRepeat():
                self._hold_begin()
            return
        if event.key() == QtCore.Qt.Key_Escape:
            self.hide()  # 只收起，进程和对话都留着
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if self._voice_mode_on and self._ptt_key_is(event):
            event.accept()
            if not event.isAutoRepeat():
                self._hold_end()
            return
        super().keyReleaseEvent(event)
