"""工具行里的「目标」：URL / 路径的识别与取首项。

差异：`Math.imul` 的 int32 截断、`charCodeAt` 的 UTF-16 码元、`toString(36)` 都手工复现。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .helpers import is_windows_absolute_path, source_field

_URL_SCHEME_RE = re.compile(r'^https?://', re.IGNORECASE)
_FILE_URL_RE = re.compile(r'^file://', re.IGNORECASE)
_LOCAL_PATH_RE = re.compile(r'^(?:/|\.{1,2}/|~/).+')


def looks_like_url(value: str) -> bool:
    return bool(_URL_SCHEME_RE.match(value))


def looks_like_path(value: str) -> bool:
    return (
        bool(_FILE_URL_RE.match(value))
        or bool(_LOCAL_PATH_RE.match(value))
        or is_windows_absolute_path(value)
    )


# 显式 http(s) 的 URL；遇到空白、引号、尖括号、右括号、右方括号即止
URL_PATTERN = re.compile(r"""https?://[^\s'"<>)\]]+""", re.IGNORECASE)


def find_first_url(*sources: Any) -> str:
    for src in sources:
        if isinstance(src, str):
            m = URL_PATTERN.search(src)

            if m:
                return m.group(0)
        elif isinstance(src, (dict, list, tuple)):
            values = src.values() if isinstance(src, dict) else src

            for value in values:
                found = find_first_url(value)

                if found:
                    return found

    return ''


def hostname_of(value: str) -> str:
    """移植 upstream: targets.ts hostnameOf。

    差异：Node 的 `URL` 对 IPv6 主机保留方括号，urlsplit().hostname 会剥掉；
    非法 URL 两边都原样返回入参。
    """
    try:
        url = urlsplit(value)

        if not url.scheme or not url.netloc or url.hostname is None:
            raise ValueError(value)

        pathname = url.path or '/'

        return f'{url.hostname}{pathname if pathname != "/" else ""}'
    except (ValueError, TypeError):
        return value
