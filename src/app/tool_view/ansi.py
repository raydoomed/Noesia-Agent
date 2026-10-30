"""ANSI SGR → 带颜色的片段，只留渲染要用的部分。

只认 16 色 + 粗体，**真彩色（`38;2;r;g;b`）不认**——这是既定行为：参数里的
`2`/`218`/`165`/`32` 都在同一条 CSI 里，被一起忽略，文字用默认前景色。我们照这个行为做，
所以在终端里看到的 `\\x1b[38;2;…m` 会消失，剩下的文字按默认色画出来。

其它 CSI（光标移动等）一律丢掉——终端转义不是给人看的字。
"""

from __future__ import annotations

import re

# CSI = ESC '[' 参数 终止字节；只有终止字节是 'm' 的才带颜色语义
CSI_RE = re.compile(r"\x1b\[([\d;]*)([\x40-\x7e])")
# OSC / SS3 等其它转义序列，整条丢掉
OTHER_ESCAPE_RE = re.compile(r"\x1b[@-Z\\-_]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

FG_BY_CODE = {
    30: "black",
    31: "red",
    32: "green",
    33: "yellow",
    34: "blue",
    35: "magenta",
    36: "cyan",
    37: "white",
    90: "bright-black",
    91: "bright-red",
    92: "bright-green",
    93: "bright-yellow",
    94: "bright-blue",
    95: "bright-magenta",
    96: "bright-cyan",
    97: "bright-white",
}


def has_ansi_codes(text: str) -> bool:
    """有转义才走解析，没转义就当普通字符串。"""
    return "\x1b[" in text


def parse_ansi(text: str) -> list[tuple[str, bool, str | None]]:
    """`[(文字, 是否粗体, 颜色名或 None)]`，相邻同样式的片段已合并。"""
    if not text:
        return []

    cleaned = OTHER_ESCAPE_RE.sub("", text)
    segments: list[list] = []
    cursor = 0
    bold = False
    fg: str | None = None

    def push(chunk: str) -> None:
        if not chunk:
            return
        if segments and segments[-1][1] == bold and segments[-1][2] == fg:
            segments[-1][0] += chunk
            return
        segments.append([chunk, bold, fg])

    for match in CSI_RE.finditer(cleaned):
        start = match.start()
        if start > cursor:
            push(cleaned[cursor:start])

        if match.group(2) == "m":
            raw = match.group(1)
            codes = [0 if part == "" else int(part) if part.isdigit() else 0 for part in raw.split(";")]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code == 0:
                    bold = False
                    fg = None
                elif code == 1:
                    bold = True
                elif code == 22:
                    bold = False
                elif code == 39:
                    fg = None
                elif code in FG_BY_CODE:
                    fg = FG_BY_CODE[code]
                elif code == 38:
                    # 256 色 / 真彩色：跳过尾部参数，不做渲染。
                    # 不跳过的话 `38;2;218;165;32` 里的 `32` 会被当成绿色，整段染错。
                    nxt = codes[index + 1] if index + 1 < len(codes) else None
                    if nxt == 5:
                        index += 2
                    elif nxt == 2:
                        index += 4
                index += 1

        cursor = match.end()

    if cursor < len(cleaned):
        push(cleaned[cursor:])

    return [(chunk, is_bold, color) for chunk, is_bold, color in segments]
