"""用户叫什么：只认 memories/USER.md 里那条 `Name: 「某某」`。

起名卡和设置页「记忆」里的名字框都只写这一条，所以档案里名字只有一处、一种写法；正文里写的任何
东西（「现用名/昵称『某某』」「会叫『宝宝』」）一概不看——聊天时她怎么称呼他是模型自己的事，
界面文案不跟着猜。读不到就叫「你」：宁可叫「你」，不能叫错。按文件 mtime 做缓存，不改就不重读。
"""

from __future__ import annotations

import re

from app.env_keys import env_file

_CACHE: dict[str, object] = {"stamp": None, "name": ""}

#: 档案里还没写名字时怎么称呼：读不到就用它。调用方想区分「有名字/没名字」时拿它比对——
#: 别自己写死「你」，两处对不上就会在界面上叫错人。
FALLBACK_NAME = "你"


#: 名字那一行：行首（可带 `-`、可带粗体）写 `Name:` / `名字:` / `姓名:` / `称呼:`，冒号后面就是名字。
NAME_LINE = re.compile(
    r"^[ \t]*(?:[-*·][ \t]*)?(?:\*\*)?[ \t]*(?:name|名字|姓名|称呼)[ \t]*(?:\*\*)?[ \t]*[:：][ \t]*"
    r"(?:\*\*[ \t]*)?(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: 名字外面可能包着引号/书名号（手改过的未必带），剥掉再比。
_BRACKETS = "「」『』《》\"'“”‘’"

#: 模板占位：`_(pick something)_` 这类不算名字。
_PLACEHOLDER = re.compile(r"^_\(.*\)_$")


def clean_name(value: str) -> str:
    """名字收拾成能写进档案的样子：去掉两端引号/书名号、换行和条目分隔符 `§`。"""
    return " ".join(value.strip().strip(_BRACKETS).replace("§", " ").split())


def format_name(value: str) -> str:
    """名字在档案里那行的写法：`Name: 「某某」`。写盘和设置页用同一处。"""
    return f"Name: 「{clean_name(value)}」"


def is_name_entry(entry: str) -> bool:
    """整条都是名字行才算（正文里顺带一句的不算），改名字时用它把旧的那条挑出来。"""
    found = NAME_LINE.match(entry)
    return bool(found) and found.end() == len(entry)


def profile_name(text: str) -> str:
    """挑出名字：只认名字行，正文一概不看；有多条时取最后一条（档案是往下追加的）。"""
    name = ""
    for found in NAME_LINE.finditer(text):
        value = found.group(1).strip().strip(_BRACKETS).strip()
        if not value or len(value) > 40 or _PLACEHOLDER.match(value):
            continue
        name = " ".join(value.split())
    return name


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
    name = profile_name(text)
    _CACHE["stamp"] = stamp
    _CACHE["name"] = name
    return name or FALLBACK_NAME
