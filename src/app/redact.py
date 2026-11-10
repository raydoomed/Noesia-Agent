"""把可能含密钥的文本打码。

工具预览和审批弹窗都要显示命令原文，两处必须共用一套规则：只在一处打码，另一处就会把
`curl -H "Authorization: Bearer ***"` 原样摆到用户脸上。所以命令文本的出口只有
`tool_view.model.shell_command()`（工具行、展开正文、`$` 转录都从那儿取）和
`approval_dialog`，两边都调 `redact()`。
"""

from __future__ import annotations

import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{6,}"), "***"),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)(?=[A-Za-z0-9._~+/=-]{12,}(?![A-Za-z0-9._~+/=-]))(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{12,}"), r"\1\2***"),
    (
        re.compile(
            r"(?i)(--(?:api[-_]?key|access[-_]?key|token|secret|password|passwd|auth)"
            r"(?:=|\s+))[\"']?[^\s\"']{8,}[\"']?"
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:key|token|secret|passw|credential|auth)[\w.-]*\s*[=:]\s*)"
            r"([\"'])[^\"']*\2"
        ),
        r"\1\2***\2",
    ),
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:key|token|secret|passw|credential|auth)[\w.-]*\s*[=:]\s*)"
            r"[\"']?[^\s\"']{8,}[\"']?"
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[\w-]{6,}"), "***"),
    (re.compile(r"(?<=[=:])\s*[A-Za-z0-9_-]{24,}"), "***"),
)


def redact(text: str) -> str:
    """把像密钥的片段换成 `***`。纯展示用，不要拿它做安全边界。"""
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out
