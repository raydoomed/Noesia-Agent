"""把可能含密钥的文本打码。

工具预览和审批弹窗都要显示命令原文，两处必须共用一套规则：只在一处打码，另一处就会把
`curl -H "Authorization: Bearer sk-..."` 原样摆到用户脸上。
"""

from __future__ import annotations

import re

SECRET_IN_TEXT = re.compile(r"(?i)(sk-[\w-]{6,}|(?<=[=:])\s*[A-Za-z0-9_\-]{24,})")


def redact(text: str) -> str:
    """把像密钥的片段换成 `***`。纯展示用，不要拿它做安全边界。"""
    return SECRET_IN_TEXT.sub("***", text)
