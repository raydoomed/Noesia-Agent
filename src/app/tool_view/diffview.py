"""把一段 unified diff 切成「哪种行」。

只做分类和切行——上色、行号槽、折叠都由画的那一层决定。分类依据就是行的第一个字符：
`+` 新增、`-` 删除、`@@` 段落头、`diff --git` / `index` / `+++` / `---` 这类是元信息，其余是上下文。
"""

from __future__ import annotations

import re

ADD = "add"
DEL = "del"
HUNK = "hunk"
META = "meta"
CONTEXT = "context"

META_PREFIXES = ("diff --git ", "index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")

# hunk 之前的这一段「文件头」只是把路径再说一遍，
# 而工具行的抬头本来就写着路径；绝对路径到了这里还会变成 `a/<绝对路径>` 这种难看的写法。
DIFF_HEADER_PREFIXES = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "similarity ",
    "rename ",
    "new file",
    "deleted file",
)
_ARROW_HEADER_RE = re.compile(r"^\S.*→\s*\S+$")


def _is_arrow_header_line(line: str) -> bool:
    trimmed = line.strip()
    return "→" in trimmed and bool(_ARROW_HEADER_RE.match(trimmed)) and not trimmed[:1] in "+-@"


def strip_diff_file_headers(text: str) -> str:
    """砍掉第一个 `@@` 之前那段文件头。

    引擎给的 diff 会带 `a/<路径> → b/<路径>` 这行箭头，git 的还会带 `diff --git` / `index` /
    `--- a/…` / `+++ b/…`。这些都只重复路径，画出来是噪音（`a/` 不是标记，是 git 的路径前缀）。
    """
    if not text:
        return ""
    lines = text.split("\n")
    start = 0
    while start < len(lines):
        line = lines[start]
        if line.startswith("@@"):
            break
        if line.strip() == "" or _is_arrow_header_line(line) or line.startswith(DIFF_HEADER_PREFIXES):
            start += 1
            continue
        break
    return "\n".join(lines[start:])


def diff_rows(text: str) -> list[tuple[str, str]]:
    """`[(kind, 原始行)]`，行尾的 `\\r` 去掉，空文本给空列表。"""
    if not text:
        return []

    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.rstrip("\r")
        if stripped.startswith("@@"):
            rows.append((HUNK, stripped))
        elif stripped.startswith("+++") or stripped.startswith("---"):
            rows.append((META, stripped))
        elif stripped.startswith(META_PREFIXES):
            rows.append((META, stripped))
        elif stripped.startswith("+"):
            rows.append((ADD, stripped))
        elif stripped.startswith("-"):
            rows.append((DEL, stripped))
        else:
            rows.append((CONTEXT, stripped))
    return rows


def looks_like_diff(text: str) -> bool:
    """值不值得当 diff 画：至少有一行 `@@` 或一对 `+++`/`---`，或者以 `diff --git` 开头。"""
    if not text.strip():
        return False
    head = text.lstrip()
    if head.startswith("diff --git ") or "\n@@ " in text or head.startswith("@@ "):
        return True
    rows = diff_rows(text)
    has_add = any(kind == ADD for kind, _ in rows)
    has_del = any(kind == DEL for kind, _ in rows)
    has_meta = any(kind == META for kind, _ in rows)
    return has_meta and (has_add or has_del)
