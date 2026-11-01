"""聊天输入面板：悬浮窗旁的一个小窗，负责「打字 → 发送 → 显示流式回复」。

无边框 + 置顶 + 半透明圆角，和头像窗同一套视觉；不进任务栏，Esc 收起。
只做 UI，不知道 IPC 细节：发出去走 `submitted` 信号，收回来由 `append_chunk` 之类的方法喂。
"""

from __future__ import annotations

import json

import html
import re
import logging
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app import audio_devices, disclosure, file_preview, markdown_render, media_links
from app.hover_actions import DIFF_PREVIEW_LABEL, HoverActionButton
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


def _error_key(text: str) -> str:
    """同一条错误 → 同一个指纹：去掉路径、数字、空白的差别再比。"""
    return _ERROR_NOISE.sub("#", (text or "").strip().lower())


class _CodeMark(QtGui.QTextBlockUserData):
    """「这一块是代码块」——复制按钮靠它命中，不再靠底色猜（底色的 HTML 行为不稳）。"""


# 这些标签 Qt 导入时会自己开新段（再 `insertBlock()` 就凭空多一个空段 = 一整行空白）。
# `<p>` 不在里面：它会把内容并进当前段，得自己开段。
_SELF_BLOCK_TAGS = ("<ul", "<ol", "<table", "<h1", "<h2", "<h3", "<h4", "<h5", "<h6", "<blockquote", "<hr")


def _starts_own_block(markup: str) -> bool:
    """这段 HTML 是不是「Qt 会自己开段」的块级元素。"""
    return markup.lstrip().lower().startswith(_SELF_BLOCK_TAGS)


def _plain_block_format() -> QtGui.QTextBlockFormat:
    """普通段落：只留 5px 上间距，不带上一段的底色/缩进（代码块的底色否则会渗下来）。"""
    block_format = QtGui.QTextBlockFormat()
    block_format.setTopMargin(5)
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

from app.user_state import window_settings
from app.tool_view.ansi import has_ansi_codes as ansi_codes, parse_ansi
from app.tool_view.diffview import diff_rows, strip_diff_file_headers
from app.tool_view.code import highlight_code as code_highlight
from app.tool_view import delegate as delegate_view
from app.tool_view.fmt import format_duration_seconds
from app.tool_view.helpers import is_file_edit_tool, strip_ansi
from app.tool_view.icons import icon_svg
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
RESIZE_MARGIN = 12  # 距边缘多少像素内按下算缩放
SNAP_DISTANCE = 24  # 拖到离她下缘这么近就自动吸过去
# 不可用的模型项：置灰（setEnabled(False) 只是不能选，颜色还得自己给，
# 因为下拉列表整体设了 color，禁用态不会自动变淡）

STYLE = """
#frame {
    background-color: rgba(22, 22, 28, 235);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 14px;
}
#title { color: #ffd7e6; font-size: 14px; font-weight: 600; }
#hint  { color: rgba(255, 255, 255, 110); font-size: 11px; }
#state { color: #9ad0ff; font-size: 11px; }
/* 输入框上方那一叠状态（引擎提示 / 后台进程 / 正在调用）：一行一个，暗底小字 */
#statusrow {
    background-color: rgba(255, 255, 255, 12);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 8px;
}
#statusnote { color: #cfd6e4; font-size: 11px; }
#statuswarn { color: #ffd166; font-size: 11px; }
#statusbusy { color: #9ad0ff; font-size: 11px; }
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
#model:hover { border: 1px solid rgba(154, 208, 255, 140); }
#model:disabled { color: rgba(255, 255, 255, 90); }
#modelmenu {
    background-color: #1b1b22;
    border: 1px solid rgba(255, 255, 255, 40);
    color: #e8e8ef;
    font-size: 12px;
}
#modelmenu::item { padding: 4px 22px 4px 10px; }
#modelmenu::item:selected { background-color: rgba(154, 208, 255, 60); }
#modelmenu::item:disabled { color: #6b6b76; }
/* 菜单项的颜色也得写在 ::item 上，写在 QMenu 上会盖掉 setForeground 的灰 */
#modelmenu QMenu::item { color: #e8e8ef; }
#modelmenu QMenu::item:disabled { color: #6b6b76; }
#view {
    background: transparent;
    border: none;
    color: #e8e8ef;
    font-size: 13px;
}
/* 输入框外壳：审批/思考两个图标在框里面，窗口拉窄也不会把输入框挤没 */
#inputbox {
    background-color: rgba(255, 255, 255, 18);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 8px;
}
#inputbox[focused="true"] { border: 1px solid rgba(154, 208, 255, 160); }
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
#send {
    background-color: rgba(154, 208, 255, 60);
    border: none;
    border-radius: 8px;
    color: #ffffff;
    padding: 7px 14px;
    font-size: 13px;
}
#send:hover { background-color: rgba(154, 208, 255, 110); }
/* 念出来那条进度：浅蓝细条，和输入框一个调子；没人念的时候整条藏起来不占地方 */
#speechbar {
    background: rgba(154, 208, 255, 22);
    border: 1px solid rgba(154, 208, 255, 60);
    border-radius: 8px;
}
#speechtitle { color: #cfe6ff; font-size: 12px; }
#speechclock { color: rgba(207, 230, 255, 150); font-size: 11px; }
#speechprogress { background: rgba(255, 255, 255, 25); border: none; border-radius: 2px; }
#speechprogress::chunk { background: rgba(154, 208, 255, 200); border-radius: 2px; }
#speechbar QToolButton { color: rgba(207, 230, 255, 200); font-size: 11px; }
/* 输入框里的图标键：审批盾牌 / 思考火花，低调，别跟「发送」抢眼 */
#icon {
    background: transparent;
    border: none;
    border-radius: 5px;
    padding: 0px;
}
#icon:hover { background-color: rgba(154, 208, 255, 60); }
#icon::menu-indicator { image: none; }
#mini {
    background-color: rgba(255, 255, 255, 14);
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 6px;
    color: rgba(255, 255, 255, 190);
    font-size: 11px;
    padding: 2px 8px;
}
#mini:hover { border: 1px solid rgba(154, 208, 255, 140); color: #ffffff; }
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
_VOICE_TINT = {"muted": "#6b6b76", "plain": "#9ad0ff", "interactive": "#b39dff"}


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

# 引擎以「用户消息」回灌给模型的机器行：display_kind 标了种类，界面上画成灰说明行
_NOTICE_DISPLAY_KINDS = {"async_delegation_complete"}

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


class ChatPanel(QtWidgets.QWidget):
    """打字对话窗。"""

    submitted = QtCore.Signal(str)
    model_chosen = QtCore.Signal(str)  # 下拉框选了模型 → 交给窗口发 SET_MODEL
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
        title = QtWidgets.QLabel("Noesia Agent", frame)
        title.setObjectName("title")
        state = QtWidgets.QLabel("", frame)
        state.setObjectName("state")
        hint = QtWidgets.QLabel("Enter 发送 · Esc 收起", frame)
        hint.setObjectName("hint")
        header.addWidget(title)
        header.addWidget(state)
        header.addStretch(1)
        new_session = QtWidgets.QPushButton("新会话", frame)
        new_session.setObjectName("mini")
        new_session.setToolTip("开一段新对话：换回历史里不带的干净上下文（改了人设、SOUL.md 之后用它生效）")
        new_session.setCursor(QtCore.Qt.PointingHandCursor)
        new_session.clicked.connect(self.new_session_requested.emit)
        header.addWidget(new_session)
        header.addWidget(hint)
        layout.addLayout(header)
        self._state_label = state

        # 模型切换：这里必须是「能点、能选」的真控件，不能只是一行状态文字
        model_row = QtWidgets.QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(6)
        model_caption = QtWidgets.QLabel("模型", frame)
        model_caption.setObjectName("modelrow")
        model_row.addWidget(model_caption)

        combo = QtWidgets.QToolButton(frame)
        combo.setObjectName("model")
        combo.setToolTip("点开按供应商分组选模型（右键头像 → 模型 也能选）；「设置」里填密钥")
        combo.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        combo.setCursor(QtCore.Qt.PointingHandCursor)
        combo.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        menu = QtWidgets.QMenu(combo)
        menu.setObjectName("modelmenu")
        menu.setToolTipsVisible(True)
        menu.setStyleSheet(STYLE)
        combo.setMenu(menu)
        model_row.addWidget(combo, 1)
        set_model = QtWidgets.QPushButton("设置", frame)
        set_model.setObjectName("mini")
        set_model.setToolTip("选模型；没有可用的就告诉你密钥该放在哪个文件")
        set_model.setCursor(QtCore.Qt.PointingHandCursor)
        set_model.clicked.connect(self.model_settings_requested.emit)
        model_row.addWidget(set_model)
        layout.addLayout(model_row)
        self._model_button = combo
        self._model_menu = menu
        self._models: list[dict] = []
        self._current = ""

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
        self._inline_open = False  # 当前段落里有没有已经画过的行内文字（空行要断开）
        self._para_break = False  # 空行记的账：下一段来时再开新段（当场开就是白留一行）
        self._md_fence = False  # 是否在 ``` 围栏代码块里
        self._media_roots: tuple = ()  # 相对路径从哪些目录找（由窗口按配置塞进来）
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
        # 桌面端照着引擎的两把开关画：show_reasoning 关掉就不画思考；
        # tool_progress=off 时引擎本来就不发 tool 帧，这里再挡一道是不让它露出来
        self._show_reasoning = True
        self._tool_progress = "all"
        self._provider_wait = ""  # 引擎在等供应商时那句中英文提示（底部状态行）

        # QTextBrowser 而不是 QTextEdit：附件行是 <a href="file://...">，只有它会在点击时发 anchorClicked
        self.view = QtWidgets.QTextBrowser(frame)
        self.view.setObjectName("view")
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)  # 不自己跳转：点开是「用默认程序打开文件」，不是窗口内导航
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._on_anchor)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        layout.addWidget(self.view, 1)

        # 折叠段的字体：抬头是一行暗色小字（可点），正文再暗一档；工具正文用等宽
        head_format = QtGui.QTextCharFormat()
        head_format.setForeground(QtGui.QColor("#8f9bb0"))
        head_format.setFontPointSize(9)
        body_format = QtGui.QTextCharFormat()
        body_format.setForeground(QtGui.QColor("#6f7a8c"))
        body_format.setFontPointSize(9)
        body_format.setFontItalic(True)
        self._tool_body_format = QtGui.QTextCharFormat()
        self._tool_body_format.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        self._tool_body_format.setFontPointSize(8.5)
        self._tool_body_format.setForeground(QtGui.QColor("#7c8798"))
        self._folds = disclosure.Folds(self.view.document(), head_format, body_format)

        # 悬停才出现的小动作：两个**各自独立**的按钮——复制（点完换 ✓）与「看完整 diff」
        # （只开窗，不冒充复制成功）。靠 CSS 的 group-hover，Qt 没有，所以自己盯鼠标摆位。
        self._hover_copy: HoverActionButton | None = None
        self._hover_expand: HoverActionButton | None = None
        self._diff_texts: dict[int, tuple[str | None, str]] = {}  # 折叠段 key → (路径, 整段 diff)
        self._tool_payloads: dict[int, tuple[str, str]] = {}  # 折叠段 key → (复制按钮文案, 待复制的文本)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.verticalScrollBar().valueChanged.connect(lambda _value: self._hide_hover())

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
        self.input = QtWidgets.QLineEdit(box)
        self.input.setObjectName("input")
        self.input.setPlaceholderText("和 Noesia 说点什么…")
        self.input.installEventFilter(self)  # 焦点时把高亮画在整条外壳上
        self.input.setAcceptDrops(True)  # 拖文件进来：DragEnter/Drop 也在同一个 eventFilter 里接
        box_row.addWidget(self.input, 1)

        # 语音模式的输入区：跟输入框抢同一个位置，同一时刻只露一个。
        # 切模式只做 hide/show，不销毁 QLineEdit——草稿、光标位置、焦点都在控件自己身上，
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
        for value, label in _VOICE_MODES:
            action = self._voice_menu.addAction(label)
            action.setCheckable(True)
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

        self._send_button = send = QtWidgets.QPushButton("发送", frame)
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
        layout.addWidget(self._attach_bar)

        row.addWidget(box, 1)
        row.addWidget(send)
        layout.addLayout(row)

        send.clicked.connect(self._emit)
        self.input.returnPressed.connect(self._emit)

        # 边框这一圈（14px 内容边距之外的 frame 空白）由 panel 接管：拖它缩放
        self._frame = frame
        frame.setMouseTracking(True)
        frame.installEventFilter(self)
        self._restore_geometry()

    # ---- 对外接口 ----

    def set_models(self, items: list[dict], current: str, *, switching: bool = False) -> None:
        """填模型菜单：一级按供应商分组，二级是那家的模型；按钮上显示当前模型名。"""
        self._models = list(items)
        self._current = current
        fill_menu(self._model_menu, self._models, current, self.model_chosen.emit)
        text = current_label(self._models, current, fallback="选模型")
        self._model_button.setText(("切换中…" if switching else text) + "  ▾")
        self._model_button.setEnabled(bool(self._models) and not switching)
        if not self._models:
            self._model_button.setToolTip("模型清单还没到；点「设置」可以先配一家")
        elif not any(group["available"] for group in group_models(self._models) if group["provider"]):
            self._model_button.setToolTip("这些供应商都还没配密钥：点「设置」填一个")
        else:
            self._model_button.setToolTip("点开按供应商分组选模型（右键头像 → 模型 也能选）")

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
        self._assistant_pending = ""
        self._reasoning_pending = ""
        self._md_block = []
        self._md_fence = False
        self._para_break = False
        self._reason_fold = None
        self._reason_started = 0.0
        self._tool_folds = {}
        self._clarify = None
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
        self.speech_bar.dismiss()
        self._hide_hover()

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
        for item in messages or ():
            text = str(item.get("text") or "")
            if not text.strip():
                continue
            if str(item.get("display_kind") or "") in _NOTICE_DISPLAY_KINDS:
                # 引擎把子代理的结果以「用户消息」回灌给模型：原文是给模型看的英文脚手架，
                # 界面上只留一行中文说明，不能伪装成「你」说的话。
                self.append_notice_lines(delegate_view.completion_lines(text))
                continue
            if str(item.get("role") or "") == "user":
                self.append_user(text)
            else:
                self.append_chunk(text)
                self.end_assistant()
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_notice_lines(self, lines) -> None:
        """灰色说明行：引擎回灌给模型的机器消息（子代理完成之类），不把英文原文铺出来。"""
        for line in lines or ():
            self._insert_html(f"<span style='color:#8f9bb0;'>{html.escape(str(line))}</span>")

    def append_user(self, message: str, attachments=()) -> None:
        self._last_error_key = ""  # 新的一轮：同样的错误再出现就该再说一次
        message, refs = split_attachment_refs(message)  # 历史里的 @image:/@file:/[screenshot] 标记不能当正文显示
        items = [*refs, *attachments]
        self._insert_html(
            f"<span style='color:#9ad0ff;'><b>你</b></span><br>{html.escape(message)}"
        )
        for item in items:
            self._append_user_attachment(item)

    def _append_user_attachment(self, item: dict) -> None:
        """你发的附件留在气泡里：图片给缩略图，其它给一行文件名。"""
        pixmap = self._attachment_pixmap(item)
        if pixmap is None:
            self._insert_html(f"<br><span style='color:#9aa4b2;'>📎 {html.escape(item['name'])}</span>")
            return
        key = f"noesia-image://user-{abs(hash((item['name'], pixmap.width(), pixmap.height())))}"
        self.view.document().addResource(
            QtGui.QTextDocument.ResourceType.ImageResource, QtCore.QUrl(key), pixmap
        )
        width = min(200, pixmap.width() or 200)
        self._insert_html(f"<br><img src='{key}' width='{width}'>")

    def begin_assistant(self) -> None:
        self._end_reasoning_group()  # 思考在前、回答在后：先把那段思考收成「思考了 3s」
        self._flush_md()
        self._insert_html("<span style='color:#ffd7e6;'><b>Noesia</b></span><br>")
        self._streaming = True
        self._bubble_has_text = False
        self._interrupted = False  # 新的一段：重新开始收字

    def append_chunk(self, chunk: str) -> None:
        if self._interrupted:
            return  # 已经叫停了：引擎可能还飘回来几个 token，不再往气泡里写
        if not self._streaming:
            self.begin_assistant()
            self._bubble_has_text = False
        self._assistant_pending += chunk
        self._pump_assistant(final=False)

    def end_assistant(self) -> None:
        self._end_reasoning_group()
        self._pump_assistant(final=True)
        self._flush_md()  # 最后那段结构（列表/表格/代码块）没等到空行也要落  # 收尾：缓冲里那半行吐干净
        self._drop_trailing_blank_line()  # 末尾的 `<br/>` 空行不留在下一段前面
        self._assistant_pending = ""
        self._streaming = False  # 段落由下一次 _insert_html 的 insertBlock 分隔

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

        不看 `_inline_open`：紧随列表/表格之后的那段普通文字也要断开，否则会被并进最后一个列表项。
        但这一轮还什么都没写时不算（回复开头的空行不该在名字底下顶出一行空白）。
        """
        self._inline_open = False
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
        self._inline_open = False
        for kind, language, body in markdown_render.split_fences(text):
            if kind == "code":
                self._insert_code_block(body, language)
            else:
                self._insert_html(markdown_render.block(body))

    def _insert_inline(self, markup: str) -> None:
        """行内片段：接在当前段里，不另起一段（普通句子不该每行一个大间距）。"""
        if not markup:
            return
        self._bubble_has_text = True
        self._inline_open = True
        cursor = self.view.textCursor()
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
        cursor.insertHtml(_keep_edge_spaces(markup))
        self.view.setTextCursor(cursor)
        self._scroll_to_end()

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
                at_end = self._at_end()
                self._folds.toggle(fold.key)  # 抬头上的箭头跟着翻
                fold.pinned = True  # 他自己点了：之后再跑完一轮也别自动收起/展开
                if at_end:  # 展开后内容比原来长：本来贴着底部看，就继续贴住
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
        self._clarify = {
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
            fold, "跳过", body_format=self._quiet_link_format("#clarify:skip", color="#8d96a8")
        )
        self._scroll_to_end_later()  # 卡刚插进来，滚到最底才看得全

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
                (label, self._link_format(f"#clarify:{qindex}:{index}")),
            ]
            if recommended:
                runs.append((" (Recommended)", self._hint_format()))
            choice_marks.append(self._folds.append_marked(fold, "", runs=runs))
        if multi:  # 我们按题确认，就放在这题选项下面
            marks.append(
                self._folds.append_marked(
                    fold, "✔ 就这样", body_format=self._quiet_link_format(f"#clarify:{qindex}:*", color="#9ad0ff")
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
                body_format=self._link_format(f"#clarify:{qindex}:{index}"),
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
                picked = label in shown
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

    def _on_clarify_click(self, rest: str) -> None:
        state = self._clarify
        if state is None:
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
        # 编辑类工具成功了却没 diff，等于「没有可看的东西」——整行不画。
        if is_file_edit and not pending and view.status != "error" and not view.inline_diff:
            return

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

        glyph = self._tool_glyph(view, pending=pending)
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
        self._scroll_to_end()

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
    DIFF_COLORS = {
        "add": ("#9ed07f", "#16281a"),
        "del": ("#e08a92", "#2b1618"),
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
        diff 行首那个 `+`/`-`/空格保留，并按类别上色。
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

            marker, code = stripped[0], stripped[1:]
            if kind in ("add", "del", "context") and code.strip():
                highlighted = code_highlight(code, path)[0]
                row = [*gutter, (marker, base)]
                for chunk, color, bold in highlighted:
                    fmt = QtGui.QTextCharFormat(base)
                    if color:
                        fmt.setForeground(QtGui.QColor(color))
                    if bold:
                        fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                    row.append((chunk, fmt))
                rows.append(row)
            else:
                rows.append([*gutter, (stripped, base)])
        return rows

    # ---- 抬头左边的小图标 ----

    def _icon_pixmap(self, name: str | None, color: str, px: int = 13) -> QtGui.QPixmap | None:
        """把 Phosphor path 渲成小图（同一个图标同一颜色只渲一次）。"""
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
        fmt.setWidth(13)
        fmt.setHeight(13)
        return fmt

    def _tool_icon_glyph(self, tool_name: str, *, ok: bool = True):
        """只有工具名、没有 ToolView 时（引擎给的摘要行）按工具取图标。

        以前这类行一片空白/一律白纸：浏览器也是纸、读文件也是纸，看着像所有工具都是「打开了个文件」。
        """
        color = self.GLYPH_COLORS["success"] if ok else self.GLYPH_COLORS["error"]
        return self._glyph_format(tool_meta(tool_name).icon or "tools", color)

    def _tool_glyph(self, view, *, pending: bool):
        """抬头图标：跑着是「进行中」的钟，出错是红的，其余是工具自己的图标。"""
        status = "running" if pending else (view.status or "success")
        color = self.GLYPH_COLORS.get(status, self.GLYPH_COLORS["success"])
        name = "watch" if status == "running" else (view.icon or "tools")
        return self._glyph_format(name, color)

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
        self._scroll_to_end()

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
        self._scroll_to_end()

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
        self._streaming = False
        self._assistant_pending = ""
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
        text = {"thinking": "思考中…", "speaking": "说话中…", "listening": "听着…"}.get(value, "")
        if not text:
            text = self._provider_wait  # 状态覆盖不了「在等供应商」那行时，把它留着
        self._state_label.setText(text)
        self.set_busy(value in {"thinking", "speaking"})

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
        block_format.setTopMargin(4)
        block_format.setBottomMargin(4)
        base = QtGui.QTextCharFormat()
        base.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        base.setFontPointSize(9)
        base.setForeground(QtGui.QColor("#d7dae5"))

        self._drop_trailing_blank_line()  # 上一行末尾的 `<br/>` 空行，紧接着代码块时就是白留一行
        cursor = self.view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        if not self.view.document().isEmpty():
            cursor.insertBlock()
        cursor.setCharFormat(base)
        cursor.setBlockFormat(block_format)
        anchor = cursor.position()

        lines = code.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            lines = [""]
        colored = code_highlight(code if len(lines) > 1 else lines[0], language)
        for index, line in enumerate(lines):
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

        block = cursor.block()
        while block.isValid() and block.position() >= anchor:
            block.setUserData(_CodeMark())
            block = block.previous()

        end = QtGui.QTextCursor(self.view.document())
        end.movePosition(QtGui.QTextCursor.End)
        self.view.setTextCursor(end)
        self._scroll_to_end()

    def _insert_html(self, markup: str) -> None:
        if not markup.strip():
            # 空片段也是片段：硬插一次会留下一个空段（14px 行高 + 5px 间距），
            # 屏幕上就是一句回复和下面的列表之间那一大截空白。
            return
        document = self.view.document()
        cursor = self.view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        first_new = None
        if not document.isEmpty():
            if _starts_own_block(markup):
                # 列表 / 表格 / 标题：Qt 自己开新段，这里再开一次就是凭空多一个空段
                self._drop_trailing_blank_line()
                first_new = document.blockCount()
            else:
                cursor.insertBlock()  # 行内片段不会自己断开，得显式开段
                # 新段不能沿用上一段的格式：上一段是代码块时，底色会跟着过来，整段说明都画成代码
                cursor.setCharFormat(QtGui.QTextCharFormat())
                cursor.setBlockFormat(_plain_block_format())
        self._para_break = False
        cursor.insertHtml(markup)
        if first_new is not None:
            # 段间距只由块格式给（5px），所以 Qt 新开的这几段也要设成普通段落
            block = document.findBlockByNumber(first_new)
            while block.isValid():
                QtGui.QTextCursor(block).setBlockFormat(_plain_block_format())
                block = block.next()
        self.view.setTextCursor(cursor)
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        """滚到最底。

        直接设 `maximum()` 有个坑：刚插进去的字还没排版，拿到的最大滚动量是旧的，
        滚不到新长出来那截（表现就是「卡片展开了但还差一口没到底」）。所以先逼一次排版。
        """
        self.view.document().documentLayout().documentSize()  # 逼一次排版：拿到的 maximum 才是新的
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _at_end(self) -> bool:
        bar = self.view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_to_end_later(self) -> None:
        """等这一轮事件处理完（排版落了）再滚——插完字当拍滚不到底。"""
        QtCore.QTimer.singleShot(0, self._scroll_to_end)

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
        already_stopped = self._interrupted
        if self._streaming:
            self.end_assistant()
        self._interrupted = True
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
        self._scroll_to_end()

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
                self._scroll_to_end()
            return
        if self._streaming:
            self.end_assistant()
        self._insert_html(
            "<div style='color:rgba(255,255,255,135); font-size:11px; margin-top:6px;'>"
            f"💾 {html.escape(text)}</div>"
        )
        self._scroll_to_end()

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
        if self._streaming:
            self.end_assistant()
        self._provider_wait = ""
        self._insert_html(
            f"<span style='color:rgba(255,255,255,120); font-size:11px;'>{html.escape(reason)} · 这一轮没跑完</span>"
        )
        self._scroll_to_end()

    def set_busy(self, busy: bool) -> None:
        """一轮在跑：发送键变「停止」；停的是引擎那一轮，已经说过的话仍留在上下文里。"""
        self._busy = bool(busy)
        if self._busy:
            # 新一轮开始：上一轮被叫停留下的那道"别再收字"的闸必须放开，
            # 否则按过一次停止之后，后面每一条回答都会被静默丢掉（2026-10-25 就是这个 bug）。
            self._interrupted = False
        self._send_button.setText("停止" if self._busy else "发送")
        self._send_button.setToolTip("叫住这一轮（已进行的对话仍留在上下文里）" if self._busy else "")

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
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        url = dialog.textValue().strip()
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
        self._snippets_dialog().exec()

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
        """往输入框里补一段（@folder: / @url: / 提示词片段都用它）。"""
        self.input.setText((self.input.text() + text).strip())
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

    def _emit(self) -> None:
        if self._busy:  # 在跑的时候这个键是「停止」
            self._stop_turn()
            return
        message = self.input.text().strip()
        attachments = list(self._attachments)
        if not message and not attachments:  # 只粘了图/只选了文件也能发（跟桌面一样）
            return
        self.input.clear()
        self._clear_attachments()
        self.append_user(message, attachments)
        if attachments:
            # 到这一刻才交给引擎：没点发送的附件既不会落盘、也不会被模型看见
            self.attachment_picked.emit(attachments)
        self.submitted.emit(message)

    # ---- 语音输入 ----

    def _init_voice_input(self) -> None:
        """建录音器。音频接口在这台机器上用不了就把按钮关掉，不影响打字。"""
        self._voice_mode_on = False
        self._recorder = None
        self._voice_prefs = None
        self._voice_draft_focus = False
        self._audio_released = False
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
            self._box_row.setContentsMargins(9, 10, 4, 10)  # 球别贴着上下边框
            self._set_inputbox_voice(True)
            self._voice_orb.show()
            self._float_mode_button(True)
            self._mic_button.setIcon(_glyph_icon("keyboard", "#ffffff"))
            self._mic_button.setToolTip("语音已开：按住球说话，松手就发（点一下回到打字，右键换麦克风）")
            try:
                self._recorder.open_stream()
            except Exception as exc:  # noqa: BLE001 - 开流崩了别让整条输入区跟着塌
                logger.warning("[语音] 打开录音流失败", exc_info=True)
                self._voice_flash(f"麦克风打不开：{exc}")
            self.voice_input_opened.emit()
        else:
            self._recorder.cancel()
            self._recorder.close_stream()
            self._voice_orb.hide()
            self._voice_orb.set_state(STATE_IDLE)
            for button in (self._attach_button, self._approval_button,
                           self._reasoning_button, self._voice_button, self._send_button):
                button.show()
            self._box_row.setContentsMargins(9, 0, 4, 0)
            self._set_inputbox_voice(False)
            self._float_mode_button(False)
            self.input.show()
            self._mic_button.setIcon(_glyph_icon("mic"))
            self._mic_button.setToolTip("语音输入（右键选麦克风）")
            if self._voice_draft_focus or self.input.text():
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
            self._place_mode_button()
            button.show()
            self._inputbox.installEventFilter(self)
            return
        self._inputbox.removeEventFilter(self)
        button.setParent(self._inputbox)
        self._box_row.addWidget(button)
        button.show()

    def _place_mode_button(self) -> None:
        """贴着输入框右上角摆，留 6px 内边距。"""
        button = self._mic_button
        button.move(max(0, self._inputbox.width() - button.width() - 6), 6)

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
        path = self._voice_temp_path()
        try:
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
        QtCore.QTimer.singleShot(1600, lambda: self._voice_orb.set_state(STATE_IDLE))

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
        # 人开口就是要打断：还在跑就先终止这一轮，跟 Enter 变「停止」走同一条路
        # （停朗读、半条回答收尾、工具行标「已停止」、发 interrupt 全在里面）。
        self._stop_turn()
        self.append_user(text, [])
        self.submitted.emit(text)

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
        if not endpoints:
            empty = menu.addAction("没找到能用的麦克风")
            empty.setEnabled(False)
        else:
            group = QtGui.QActionGroup(menu)
            group.setExclusive(True)
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
        follow.setChecked(not recorder.chosen_guid())
        follow.triggered.connect(lambda _checked=False: self._choose_mic(""))
        menu.exec(self._mic_button.mapToGlobal(pos))

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
        """菜单跟着面板变宽：模型名长的时候（`mimo-v2.6-pro-ultraspeed`）别被截成「…」。"""
        if hasattr(self, "_model_menu"):
            self._model_menu.setMinimumWidth(max(240, self.width() - 60))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_model_menu"):
            self._sync_popup_width()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        # 隐藏时的 resize 不会立刻派发，显示出来时再对一次宽度
        super().showEvent(event)
        self._sync_popup_width()

    # ---- 交互：拖动 / 边缘缩放 / Esc 收起 ----

    def _hit_edges(self, local: QtCore.QPoint):
        return edges_at(local, self.size(), margin=RESIZE_MARGIN)

    def _begin_resize(self, local: QtCore.QPoint, global_pos: QtCore.QPoint) -> bool:
        edge = self._hit_edges(local)
        if not edge:
            return False
        self._resize_edge = edge
        self._resize_start_size = self.size()
        self._resize_start_global = global_pos
        self._drag_offset = None
        self.setCursor(cursor_for(edge))
        return True

    def _mark_input_focus(self, focused: bool) -> None:
        """高亮画在输入框外壳上（#inputbox[focused]），框里的 QLineEdit 自己是无边框的。"""
        box = getattr(self, "_inputbox", None)
        if box is None:
            return
        box.setProperty("focused", "true" if focused else "false")
        box.style().unpolish(box)
        box.style().polish(box)

    # ---- 悬停小动作：代码块复制 / 工具行看完整 diff ----

    def _hover_at(self, pos: QtCore.QPoint) -> None:
        """鼠标在正文里移动：代码块（或工具正文）给复制按钮，工具 diff 另给「看完整 diff」。

        两个按钮各管各的：复制点完换 ✓，放大只开窗。以前共用一个小部件，结果放大也换成 ✓、
        再也看不到复制——那是错的。
        """
        block = self._block_at(pos)
        if block is None or not block.isValid():
            self._hide_hover()
            return

        fence = self._code_fence_at(block)
        if fence is not None:
            first, last, text = fence
            self._show_hover_copy(text, tool_tr("assistant.tool.copyCode"), first, last)
            self._hide_expand()
            return

        mark = block.userData()
        if isinstance(mark, disclosure._BodyMark):
            first, last = self._marked_run(block, mark.key)
            payload = self._tool_payloads.get(mark.key)
            if payload is None and mark.key in self._diff_texts:
                # 没算出 payload（例如只有 diff）就拿 diff 当复制内容，文案用「复制内容」
                payload = (tool_tr("assistant.tool.copyContent"), self._diff_texts[mark.key][1])
            if payload and payload[1].strip():
                self._show_hover_copy(payload[1], payload[0], first, last)
            else:
                self._hide_copy()
            if mark.key in self._diff_texts:
                self._show_hover_expand(mark.key, first, last)
            else:
                self._hide_expand()
            return

        self._hide_hover()

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
        lines: list[str] = []
        current = first
        while True:
            lines.append(current.text())
            if current == last or not current.next().isValid():
                break
            current = current.next()
        text = markdown_render.code_text("\n".join(lines)).strip("\n")
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

    def _show_hover_copy(self, text: str, tooltip: str, first, last) -> None:
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
        self._place_hover(self._hover_copy, first, last)

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
        self._place_hover(self._hover_expand, first, last, offset_x=-(HoverActionButton.BUTTON_PX + 2))

    def _place_hover(self, button: HoverActionButton, first, last, offset_x: int = 0) -> None:
        rect = self._block_rect(first, last)
        if rect.height() <= 0:
            button.hide()
            return
        button.place_at(rect, offset_x=offset_x)
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
        window.show()
        window.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """frame 铺满整个窗口，边缘那圈空白上的鼠标事件在这儿接住。"""
        if obj is self.view.viewport():
            kind = event.type()
            if kind is QtCore.QEvent.Type.MouseMove:
                self._hover_at(event.position().toPoint())
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Wheel):
                self._hide_hover()
            return super().eventFilter(obj, event)
        if obj is getattr(self, "input", None):
            kind = event.type()
            if kind is QtCore.QEvent.Type.FocusIn:
                self._mark_input_focus(True)
            elif kind is QtCore.QEvent.Type.FocusOut:
                self._mark_input_focus(False)
            elif kind is QtCore.QEvent.Type.KeyPress and self._is_paste_key(event):
                # Ctrl+V / Shift+Insert：剪贴板是图或文件就接成附件（QLineEdit 自己不认图，
                # 默认粘贴会把图直接丢掉）；是纯文字就放回去让它自己粘。
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
            elif kind in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.Drop):
                paths = self._drop_paths(event.mimeData())
                if paths:
                    event.acceptProposedAction()
                    if kind is QtCore.QEvent.Type.Drop:
                        self._attach_paths(paths)
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
                self.setCursor(cursor_for(self._hit_edges(event.position().toPoint())))
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
        self.setCursor(cursor_for(self._hit_edges(event.position().toPoint())))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        resized = self._resize_edge is not None
        dragged = self._drag_offset is not None
        self._resize_edge = None
        self._resize_start_size = None
        self._resize_start_global = None
        self._drag_offset = None
        self.unsetCursor()
        if dragged and not resized:
            self._snap_under_anchor()  # 松手这一下才吸：拖的时候它得跟着手走
        if resized or dragged:
            self._save_geometry()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self._save_geometry()
        super().closeEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802
        self._save_geometry()  # Esc 收起、切走都算「上次关掉的位置」
        super().hideEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if event.key() == QtCore.Qt.Key_Escape:
            self.hide()  # 只收起，进程和对话都留着
            return
        super().keyPressEvent(event)
