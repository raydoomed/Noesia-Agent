"""用户叫什么：从 memories/USER.md 里读 `Name: Ray` 那一行。

引擎起名那一轮会把名字写成一条 `Name: Ray`，所以这里只认这种键值行（中英文冒号都认）。
档案还没写、读不到、或者行不成样子，一律退回「你」——宁可叫「你」，不能叫错。
按文件 mtime 做缓存，USER.md 不改就不重读。
"""

from __future__ import annotations

import re

from app.env_keys import env_file

_CACHE: dict[str, object] = {"stamp": None, "name": ""}

#: 档案里还没写名字时怎么称呼：读不到就用它。调用方想区分「有名字/没名字」时拿它比对——
#: 别自己写死「你」，两处对不上就会在界面上叫错人。
FALLBACK_NAME = "你"


def _from_profile(text: str) -> str:
    """从 USER.md 正文里挑出名字：只认 `Name: X` / `名字：X` 那一行，到第一个标点为止。

    USER.md 由引擎侧的记忆工具自己整理，`Name:` 后面可能跟着整段档案，所以不能假定这行只有
    名字：截到第一个标点，太长或带空格就退回空串（调用方会叫「你」），不准截半个句子当名字。
    """
    found = re.search(
        r"^[ \t]*(?:name|名字)[ \t]*[:：][ \t]*(.+?)[ \t]*$", text, re.MULTILINE | re.IGNORECASE
    )
    if not found:
        return ""
    head = re.split(r"[，。、；：！？,.;:!?\s（(\[【]", found.group(1).strip(), maxsplit=1)[0]
    return head if 1 <= len(head) <= 8 else ""


def user_display_name() -> str:
    """用户那条冒什么名：USER.md 里记着名字就用名字，没有就叫「你」。"""
    path = env_file().with_name("memories") / "USER.md"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return FALLBACK_NAME
    if _CACHE["stamp"] == stamp:
        return str(_CACHE["name"] or FALLBACK_NAME)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        text = ""
    name = _from_profile(text)
    _CACHE["stamp"] = stamp
    _CACHE["name"] = name
    return name or FALLBACK_NAME
