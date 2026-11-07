"""代码上色：桌面端用 shiki，这边用 pygments（随项目已有的那份），只做「按词法给颜色」这一件事。

做法是：先把 diff 里每一行的代码正文高亮，再叠一层
新增/删除的底色。这里同样分两步——`highlight_code` 给出每行的彩色片段，底色由画的那一层决定。

拿不到词法分析器时一律回落成「无颜色的整行」，**不猜语言、不自己写正则**去染色。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pygments.lexer import Lexer

PYGMENTS_STYLE = "one-dark"

_LEXER_CACHE: dict[str, "Lexer | None"] = {}
_COLOR_CACHE: dict | None = None


def _lexer_for(path: str | None) -> "Lexer | None":
    """按文件名挑词法分析器，挑不到再按语言名挑；都挑不到返回 None（调用方回落成纯文本）。

    聊天里的围栏写的是**语言名**（```python），不是文件名——只按文件名找会一律 ClassNotFound，
    于是所有带语言标注的代码块都成了单色（用户以为高亮坏了）。
    """
    if not path:
        return None
    name = Path(path).name
    if name in _LEXER_CACHE:
        return _LEXER_CACHE[name]

    lexer = None
    try:
        from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_for_filename(name, stripnl=False, ensurenl=False)
        except ClassNotFound:
            try:
                lexer = get_lexer_by_name(name, stripnl=False, ensurenl=False)
            except ClassNotFound:
                lexer = None
    except Exception:
        lexer = None

    _LEXER_CACHE[name] = lexer
    return lexer


def _style_color_map() -> dict:
    global _COLOR_CACHE
    if _COLOR_CACHE is not None:
        return _COLOR_CACHE

    colors: dict = {}
    try:
        from pygments.styles import get_style_by_name

        style = get_style_by_name(PYGMENTS_STYLE)
        for token, attrs in style:
            color = (attrs or {}).get("color")
            if color:
                colors[token] = "#" + color
    except Exception:
        colors = {}

    _COLOR_CACHE = colors
    return colors


def highlight_code(text: str, path: str | None) -> list[list[tuple[str, str | None, bool]]]:
    """把一段代码切成按行的彩色片段：`[[(文字, 颜色, 粗体), …], …]`。

    行数与 `text.splitlines()` 严格对齐——diff 要按行叠底色，少一行就串位。
    """
    lines = text.splitlines() or [""]
    plain: list[list[tuple[str, str | None, bool]]] = [[(line, None, False)] for line in lines]
    lexer = _lexer_for(path)
    if lexer is None or not text.strip():
        return plain

    colors = _style_color_map()
    if not colors:
        return plain

    try:
        from pygments import lex
        from pygments.token import Token

        tokens = list(lex(text, lexer))
    except Exception:
        return plain

    rows: list[list[tuple[str, str | None, bool]]] = [[]]
    for token, value in tokens:
        if not value:
            continue
        color = colors.get(token)
        bold = token in Token.Generic.Heading or token in Token.Generic.Strong
        chunks = value.split("\n")
        for index, chunk in enumerate(chunks):
            if index:
                rows.append([])
            if chunk:
                rows[-1].append((chunk, color, bold))

    while len(rows) < len(lines):
        rows.append([])
    if len(rows) > len(lines):
        rows = rows[: len(lines)]

    return [[chunk for chunk in row] or [("", None, False)] for row in rows]
