"""用户叫什么：从 memories/USER.md 里挑出名字。

引擎起名那一轮会把名字写成一条 `Name: 「某某」`，模型自己记也照这个形状，所以名字一律在引号里；
引号前还得挨着 `Name:` / 「名字」「叫」这类词，随口一句引号不算。档案还没写、读不到、或者挑不出来，
一律退回「你」——宁可叫「你」，不能叫错。按文件 mtime 做缓存，USER.md 不改就不重读。
"""

from __future__ import annotations

import re

from app.env_keys import env_file

_CACHE: dict[str, object] = {"stamp": None, "name": ""}

#: 档案里还没写名字时怎么称呼：读不到就用它。调用方想区分「有名字/没名字」时拿它比对——
#: 别自己写死「你」，两处对不上就会在界面上叫错人。
FALLBACK_NAME = "你"


#: 名字一律写在引号里：`Name: 「某某」`、`用户的名字叫「某某」`。引号内才是名字。
_QUOTED = re.compile(r"[「【《]([^」】》]{1,40})[」】》]")

#: 引号前面紧挨着这些词的，是旧名字（「以前叫『某某』」「此前记录为『某某』」），跳过。
_STALE = re.compile(r"以前|此前|之前|原来|原本|原名|曾|旧名")

#: 引号前面得挨着这些词才算名字，否则只是随手一个引号（「她说『好的』」「迷上了『咖啡』」）。
_NAMED = re.compile(
    r"(?:name\s*(?:is|[:：])|名字\s*[:：]?|名字(?:是|叫)|称呼\s*[他她我]?|叫我?|称为)\s*$",
    re.IGNORECASE,
)

#: 判断旧名字时往前看几个字。
_LOOKBEHIND = 8


def profile_name(text: str) -> str:
    """从 USER.md 正文里挑出名字：逐行看引号，跳过旧名字，最后出现的那个当前名字算数。

    名字一律用引号包着写（引导填名字、模型自己记，都是这个形状），所以只看引号内的内容；引号前面
    得挨着 `Name:`、「名字」「叫」这类词，随口一句引号（「她说『好的』」）不算。哪一个是当前的由
    引号前面的词决定，不是位置：写着「以前叫『某某』」「此前记录为『某某』」的是旧名字，跳过。
    行与行之间靠后的是新写的，所以后面覆盖前面。超过八个字当句子，不算名字。
    """
    name = ""
    for line in text.splitlines():
        for found in _QUOTED.finditer(line):
            head = line[max(0, found.start() - _LOOKBEHIND):found.start()]
            if _STALE.search(head) or not _NAMED.search(head):
                continue
            candidate = found.group(1).strip()
            if 1 <= len(candidate) <= 8:
                name = candidate
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
