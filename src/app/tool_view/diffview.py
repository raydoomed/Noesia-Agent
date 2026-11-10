"""把一段 unified diff 切成「哪种行」。

只做分类和切行——上色、行号槽、折叠都由画的那一层决定。分类依据就是行的第一个字符：
`+` 新增、`-` 删除、`@@` 段落头、`diff --git` / `index` / `+++` / `---` 这类是元信息，其余是上下文。
"""

from __future__ import annotations

ADD = "add"
DEL = "del"
HUNK = "hunk"
META = "meta"
CONTEXT = "context"

META_PREFIXES = ("diff --git ", "index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")

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


def _is_arrow_header_line(line: str) -> bool:
    trimmed = line.strip()
    if trimmed[:1] in "+-@":
        return False
    return "→" in trimmed and trimmed.startswith(("a/", "b/", "a\\", "b\\"))


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
    seen_hunk = False
    for line in text.splitlines():
        stripped = line.rstrip("\
")
        if stripped.startswith("@@"):
            seen_hunk = True
            rows.append((HUNK, stripped))
        elif not seen_hunk and (
            stripped.startswith("+++")
            or stripped.startswith("---")
            or stripped.startswith(META_PREFIXES)
        ):
            rows.append((META, stripped))
        elif stripped.startswith("+"):
            rows.append((ADD, stripped))
        elif stripped.startswith("-"):
            rows.append((DEL, stripped))
        else:
            rows.append((CONTEXT, stripped))
    return rows


