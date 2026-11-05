"""把可能含密钥的文本打码。

工具预览和审批弹窗都要显示命令原文，两处必须共用一套规则：只在一处打码，另一处就会把
`curl -H "Authorization: Bearer ***"` 原样摆到用户脸上。所以命令文本的出口只有
`tool_view.model.shell_command()`（工具行、展开正文、`$` 转录都从那儿取）和
`approval_dialog`，两边都调 `redact()`。
"""

from __future__ import annotations

import re

# 顺序有讲究：带语境的先用（Bearer / --flag / 变量名），最后才扫裸长串。
# 原来的单条 `(?<=[=:])\s*[A-Za-z0-9_-]{24,}` 接不住三种最常见写法：
# 值前面是空格的 `Authorization: Bearer xxx`、`--api-key xxx`、带 `.` 的 JWT。
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWT：`eyJ...` 开头、三段用 `.` 连。必须排在长串规则前面，否则只打掉第一段。
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{6,}"), "***"),
    # Authorization: Bearer/Basic <值>（引号里也一样）
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1\2***"),
    # --api-key / --token / --secret / --password <值>，`=` 或空格分隔都认
    (
        re.compile(
            r"(?i)(--(?:api[-_]?key|access[-_]?key|token|secret|password|passwd|auth)"
            r"(?:=|\s+))[\"']?[^\s\"']{8,}[\"']?"
        ),
        r"\1***",
    ),
    # 引号里带空格的值（`DB_PASSWORD="p@ss w0rd 1234"`）：整段吃掉，不然只打到第一个空格
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:key|token|secret|passw|credential|auth)[\w.-]*\s*[=:]\s*)"
            r"([\"'])[^\"']*\2"
        ),
        r"\1\2***\2",
    ),
    # NAME=<值> / NAME: <值>：名字像密钥就一直打到空格为止（值里允许 / . 这些字符）
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:key|token|secret|passw|credential|auth)[\w.-]*\s*[=:]\s*)"
            r"[\"']?[^\s\"']{8,}[\"']?"
        ),
        r"\1***",
    ),
    # 兜底：`sk-` 开头的厂商密钥
    (re.compile(r"\bsk-[\w-]{6,}"), "***"),
    # 兜底：`=`/`:` 后面的长串。带 `/` 的不碰——那是路径和 URL，打了码反而看不出跑的是什么
    (re.compile(r"(?<=[=:])\s*[A-Za-z0-9_-]{24,}"), "***"),
)


def redact(text: str) -> str:
    """把像密钥的片段换成 `***`。纯展示用，不要拿它做安全边界。"""
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out
