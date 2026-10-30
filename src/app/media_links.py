"""把模型输出里的 `MEDIA:<路径>` 变成能点的附件行。

模型（按提示词约定）会把生成的文件写成一行 `MEDIA:D:/x/y.png`。原样丢进聊天窗就是一串没人看得懂的
路径，用户没法点开——这里负责挑出这一行、去文件系统确认它真的在，再换成「📎 文件名 · 大小」的可点链接。

规矩：
- 只认「一行以 `MEDIA:` 开头」（允许前置 `-`/`*` 或反引号、引号、`file://`）。路径取到行尾——Windows
  路径里常有空格，按空白切会切坏。
- **文件存在才给可点链接**；不存在就如实写「找不到文件」，不假装成功。
- 相对路径按调用方给的头几个目录（工作目录等）依次找。
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

# 行首可选的项目符号 / 反引号 / 空格，然后是 MEDIA:，剩下整行都是路径（去掉包裹的引号、反引号）
_MEDIA_LINE = re.compile(r"^\s*(?:[-*+]\s*)?`?(?:media:\s*)(?P<path>.+?)`?\s*$", re.IGNORECASE)
_LEAD = re.compile(r"^\s*(?:[-*+]\s*)?`?(?P<rest>.*)$", re.IGNORECASE)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tif", ".tiff"})
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi"})


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    for quote in ('"', "'", "`", "<", ">"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    if text.lower().startswith("file:///"):
        text = text[8:]
    elif text.lower().startswith("file://"):
        text = text[7:]
    return text.strip()


def might_be_media(fragment: str) -> bool:
    """这段还没写完的半行，将来会不会变成 `MEDIA:` 行？

    流式输出里 `MEDIA:` 常被切成两三块，这种半行要压着不显示；普通文字立刻显示，不拖节奏。
    """
    head = _LEAD.match(fragment or "").group("rest") if _LEAD.match(fragment or "") else fragment
    head = head.strip().strip("`\"'<").lower()
    if not head:
        return True  # 纯空白：等下一块
    keyword = "media:"
    return keyword.startswith(head) or head.startswith(keyword)


def split_media_line(line: str) -> tuple[str, str | None]:
    """一行拆成（要照常显示的文字, 路径或 None）。"""
    body = (line or "").rstrip("\r")
    match = _MEDIA_LINE.match(body)
    if match is None:
        return body, None
    path = _clean(match.group("path"))
    if not path:
        return body, None
    return "", path


def resolve(raw: str, roots: tuple[Path, ...] | list[Path] = ()) -> Path:
    """把路径字符串变成 Path：展开变量和 ~，相对路径按 roots 找。"""
    text = _clean(raw)
    candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    if candidate.is_absolute():
        return candidate
    for root in roots or ():
        guess = Path(root) / candidate
        if guess.exists():
            return guess
    return candidate


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.1f} GB"


def kind_label(suffix: str) -> str:
    if suffix in IMAGE_SUFFIXES:
        return "图片"
    if suffix in AUDIO_SUFFIXES:
        return "音频"
    if suffix in VIDEO_SUFFIXES:
        return "视频"
    return "文件"


def media_link_html(raw: str, roots: tuple[Path, ...] | list[Path] = ()) -> str:
    """一行可点（或如实说找不到）的附件说明。链接用 file:// URL，点击交给调用方打开。"""
    path = resolve(raw, roots)
    name = path.name or str(path)
    if path.exists() and path.is_file():
        size = human_size(path.stat().st_size)
        url = html.escape(path.as_uri(), quote=True)
        label = html.escape(f"📎 {kind_label(path.suffix.lower())} · {name} · {size} · 点击打开")
        return f'<a href="{url}" style="color:#9ad0ff;text-decoration:none;">{label}</a>'
    label = html.escape(f"📎 {name}（找不到文件）")
    return f'<span style="color:#ff9a9a;">{label}</span>'


def open_local(path: Path) -> bool:
    """用系统默认程序打开这个文件。返回是否真的发起（文件不存在就不发）。"""
    from PySide6 import QtCore, QtGui

    if not path.exists():
        return False
    return QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
