"""正文渲染：把引擎推来的 markdown 变成 Qt 能画的 HTML。

**为什么要这一层**：桌面端的正文段就是 markdown 渲染（`assistant-ui/thread` 里的
`TimelineMarkdownText`，代码块交给高亮器）。我们照同一条路——用现成的 markdown 库解析，
再拿 pygments 上色；不自己写「哪些符号算标题、算列表」这种规则。

两种落法，对应流式的两种情形：
- `inline()`：普通一行字。马上画出来（流式输出才跟得上），只处理行内语法（粗体、行内代码、链接）。
- `block()`：列表 / 表格 / 代码块 / 标题这类要连着好几行才算数的结构，整段一次画。
"""

from __future__ import annotations

import hashlib
import html
import io
import re
import struct
from functools import lru_cache
from typing import Any

_STRUCTURAL = re.compile(r"^(\s{2,}|[-*+]\s|\d+[.)]\s|>|\||#{1,6}\s)")

_STRUCTURAL_PREFIX = re.compile(r"^\s*(?:[|>`]|[-*+]|\d+[.)]?|#{1,6})$")

CODE_BACKGROUND = "#14ece6ef"

#
_CODE_STYLE = (
    f"background-color:{CODE_BACKGROUND};color:#d7dae5;font-family:Consolas,'Cascadia Mono',monospace;"
    "font-size:9pt;margin:0 0 0 6px;"
)

_TAG = re.compile(r"<[^>]+>")


@lru_cache(maxsize=1)
def _parser() -> Any:
    """markdown 解析器（只建一次）。表格、删除线打开——引擎的说明里常见这两样。"""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    for rule in ("table", "strikethrough"):
        try:
            md.enable(rule)
        except Exception:  # noqa: BLE001
            pass
    md.options["highlight"] = _highlight
    return md


def _nbsp(line: str) -> str:
    """把一行高亮 HTML 里的文本空格换成 `&nbsp;`（标签内的一律不动）。

    HTML 会把连续空格压成一个，缩进就没了；而 `white-space:pre` 又不让换行。
    换 `&nbsp;` 两头都占：缩进还在，行太宽照样折行。
    """
    out: list[str] = []
    last = 0
    for match in _TAG.finditer(line):
        out.append(line[last : match.start()].replace(" ", "&nbsp;"))
        out.append(match.group(0))
        last = match.end()
    out.append(line[last:].replace(" ", "&nbsp;"))
    return "".join(out)


def _code_lines(painted: str) -> str:
    """把高亮后的整段拆成一行一段的 `<p>`（见 `_CODE_STYLE` 上面那段说明）。"""
    lines = painted.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines = [""]
    return "".join(
        f"<p style='{_CODE_STYLE}'>{_nbsp(line) if line else '&nbsp;'}</p>"
        for line in lines
    )


def code_text(text: str) -> str:
    """代码块的纯文本（复制用）：`&nbsp;` 还原成空格，行尾空白去掉。"""
    return "\n".join(line.replace("\xa0", " ").rstrip() for line in text.split("\n"))


def _highlight(code: str, lang: str, _attrs: str = "") -> str:
    """代码块上色；认不出语言就原样等宽显示（不猜）。"""
    body = ""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(lang) if lang else None
        if lexer is not None:
            body = highlight(code, lexer, HtmlFormatter(noclasses=True, style="monokai", nowrap=True))
    except Exception:  # noqa: BLE001
        body = ""
    if not body:
        body = html.escape(code)
    return _code_lines(body)


def inline(text: str) -> str:
    """一段普通文字的行内渲染（粗体、行内代码、链接、公式）；不含外层段落标签。"""
    if not text.strip():
        return ""
    protected, holders = _protect_math(text)
    try:
        markup = _parser().renderInline(protected)
    except Exception:  # noqa: BLE001
        markup = html.escape(protected)
    return _restore_math(_style_links(markup), holders)


def _style_tables(markup: str) -> str:
    """给表格补边框与表头底色：Qt 对裸 <table> 用默认细线，暗色主题下几乎看不出来。"""
    if "<table>" not in markup:
        return markup
    markup = markup.replace(
        "<table>", "<table border='1' cellspacing='0' cellpadding='6' style='border-color:#3a3f4b;'>"
    )
    return markup.replace(
        "<th>", "<th style='color:#cfd6e6;background-color:#242733;'>"
    )


QUOTE_BG = "#2a2433"
QUOTE_FG = "#b9c0cf"
QUOTE_INDENT = 12
LINK_COLOR = "#8ab4ff"


def _style_links(markup: str) -> str:
    """链接染成主题浅蓝。

    Qt 富文本画锚点走系统链接色（默认 #0000ff），暗底上糊成一团；`color` 写在 `<a>` 上才会
    落到字符格式里，所以在这一步补进去，两条插入路径（正文、折叠段正文）一次都覆盖到。
    """
    if "<a " not in markup:
        return markup
    return re.sub(r"<a (?![^>]*style=)", f"<a style='color:{LINK_COLOR};' ", markup)


def _style_quotes(markup: str) -> str:
    """引用块：按层缩进 + 淡底色 + 淡化字色。

    桌面端是 CSS 的左边框 + 淡底（浏览器默认样式）；Qt 的富文本没有左边框这回事，
    缩进一层层加深、底色铺在段上，是同一套观感里能落地的那种。缩进和底色都写在 `<p>` 的
    样式上——Qt 的 `insertHtml` 认 `margin-left` 与 `background-color`，会转成段格式。
    """
    if "<blockquote" not in markup:
        return markup
    out: list[str] = []
    depth = 0
    for piece in re.split(r"(</?blockquote[^>]*>)", markup):
        low = piece.lower()
        if low.startswith("<blockquote"):
            depth += 1
            continue
        if low.startswith("</blockquote"):
            depth = max(0, depth - 1)
            continue
        if depth:
            style = (
                f"margin-top:0;margin-bottom:0;margin-left:{QUOTE_INDENT * depth}px;"
                f"background-color:{QUOTE_BG};color:{QUOTE_FG};"
            )
            for plain_p in ("<p>", "<p style='margin:0;'>"):
                piece = piece.replace(plain_p, f"<p style='{style}'>")
        out.append(piece)
    return "".join(out)


def leading_block_style(markup: str) -> tuple[int, str] | None:
    """抠出开头那段 `<p>` 的（左缩进, 底色），给调用方补一次。

    `insertHtml` 有个脾气：光标所在的那一段会**并进**开头这个 `<p>`，可 `<p>` 上的
    `margin-left`/`background-color` 却不跟过去——引用第一层就成了没缩进、没底色的普通段。
    文字色是跟过去的，缺的只是段格式，所以插入之后照这份数据补一张段格式即可。
    """
    match = re.match(r"\s*<p style='([^']*)'>", markup)
    if not match:
        return None
    style = match.group(1)
    left = re.search(r"margin-left:(\d+)px", style)
    bg = re.search(r"background-color:(#[0-9a-fA-F]{3,8})", style)
    if not left or not bg:
        return None
    return int(left.group(1)), bg.group(1)


def split_fences(text: str) -> list[tuple[str, str, str]]:
    """把一段 markdown 按围栏切开：`[("text", "", 原文), ("code", 语言, 代码)]`。

    代码块不走 HTML：Qt 的富文本片段导入对 `<pre>`/`<p>` 的底色处理很不稳（追加时丢、往后渗），
    所以代码块由聊天窗按块自己画（`ChatPanel._insert_code_block`），这里只负责切。
    """
    segments: list[tuple[str, str, str]] = []
    plain: list[str] = []
    code: list[str] = []
    language = ""
    inside = False
    for line in text.split("\n"):
        if is_fence(line):
            if inside:
                segments.append(("code", language, "\n".join(code)))
                code, language, inside = [], "", False
            else:
                if plain:
                    segments.append(("text", "", "\n".join(plain)))
                    plain = []
                language = line.strip()[3:].strip()
                inside = True
            continue
        (code if inside else plain).append(line)
    if plain:
        segments.append(("text", "", "\n".join(plain)))
    if inside and code:
        segments.append(("code", language, "\n".join(code)))
    return [segment for segment in segments if segment[2].strip() or segment[0] == "code"]


_FLUSH_STYLE = "margin:0;"
_LIST_STYLE = "margin:0;-qt-list-indent:1;"


def _zero_block_margins(markup: str) -> str:
    for tag in ("p", "li"):
        markup = markup.replace(f"<{tag}>", f"<{tag} style='{_FLUSH_STYLE}'>")
    for tag in ("ul", "ol"):
        markup = markup.replace(f"<{tag}>", f"<{tag} style='{_LIST_STYLE}'>")
    return markup


def _unwrap_pre(markup: str) -> str:
    """把围栏代码块外面的 `<pre><code>` 拆掉：内容已经是一行一个 `<p>` 了。

    Qt 认 `<pre>` 就不换行——留着这层壳，代码行再宽也不会折，文档被撑得比视口宽，
    聊天窗底下就冒出横向滚动条（这是它唯一还会露头的地方）。
    带语言的围栏会生成 `<pre><code class="language-python">`，所以要按标签整对处理。
    缩进代码块（四空格）不会走高亮，壳里是裸文本，这里顺手按行画一遍，别让它掉回普通正文。
    """

    def _unwrap(match: re.Match[str]) -> str:
        body = match.group(1)
        return body if body.lstrip().startswith("<p style=") else _code_lines(body)

    markup = re.sub(r"<pre>\s*<code[^>]*>(.*?)</code>\s*</pre>", _unwrap, markup, flags=re.S)
    return markup.replace("<pre>", "").replace("</pre>", "")


def block(text: str) -> str:
    """整段结构（列表 / 表格 / 代码块 / 标题 / 公式）的渲染。"""
    if not text.strip():
        return ""
    protected, holders = _protect_math(text)
    try:
        markup = _zero_block_margins(
            _style_links(_style_quotes(_style_tables(_unwrap_pre(_parser().render(protected)))))
        )
    except Exception:  # noqa: BLE001
        markup = _code_lines(html.escape(protected))
    return _restore_math(markup, holders)


# ---- <details> 折叠块 ----
# Qt 的富文本没有 <details> 这种元素，原样喂进去只会被转义成一行字。
# 真折叠落在文档的折叠段上（`disclosure.Folds`）；这里只认边界，把摘要和正文分开。
_OPEN = re.compile(r"^\s*<details\b[^>]*>", re.I)
_ATTRS = re.compile(r"<details\b([^>]*)>", re.I)
_SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)</summary\s*>", re.I | re.S)
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_TAG = re.compile(r"<[^>]+>")
_HEADS = ("<details", "</details", "<summary")


def masked(text: str) -> str:
    """同长度的副本：行内代码和代码块里的字换成空格，扫标签时就不会认错。

    正文里写一句「说明 `<details>` 没有被收起来」，反引号里那个标签是内容不是结构。
    """
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        head = line.strip()
        if head.startswith(("```", "~~~")):
            out.append(" " * len(line))
            fenced = not fenced
        elif fenced:
            out.append(" " * len(line))
        else:
            out.append(_INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def opens_details(line: str) -> bool:
    """这一行是不是折叠块的开头。"""
    return bool(_OPEN.match(masked(line)))


def tag_balance(line: str) -> int:
    """这一行让折叠块层数涨几层：里层套的也算，行内代码里写的不算。"""
    scan = masked(line).lower()
    return scan.count("<details") - scan.count("</details")


def details_default_open(text: str) -> bool:
    """`<details open>` 一开始就是展开的——HTML 本来就是这个语义。"""
    match = _ATTRS.search(masked(text))
    return bool(match and re.search(r"\bopen\b", match.group(1), re.I))


def details_pending(partial: str) -> bool:
    """这半行是不是折叠块的标记：`<det`、`</deta` 这种半截标记当正文画出去就收不回来。"""
    head = partial.strip().lower()
    return bool(head) and any(tag.startswith(head) or head.startswith(tag) for tag in _HEADS)


def plain(markup: str) -> str:
    """去掉标签留文字（折叠块抬头只收纯文本，混着标记会连尖括号一起画出来）。"""
    return html.unescape(_TAG.sub("", markup)).strip()


def split_details(text: str) -> tuple[str, str]:
    """拆成（摘要文字, 里面的 markdown）。

    里面还套着 `<details>` 时按**最外层**收尾标签截断（最后出现的那个），
    截在前一个上会把里层的正文一起吞掉。
    """
    scan = masked(text)
    match = _SUMMARY.search(scan)
    if match:
        summary = text[match.start(1) : match.end(1)]
        body = text[match.end() :]
    else:
        summary = ""
        body = _OPEN.sub("", text, count=1)
    cut = masked(body).lower().rfind("</details")
    if cut >= 0:
        body = body[:cut]
    return summary.strip(), body.strip("\n")


def split_details_stream(text: str) -> list[tuple[str, str]]:
    """按折叠块切一段文字：[("text", 原文) | ("details", 整段 <details>…</details>)]。

    按层数配对，里层套的整段原样交给调用方再拆——不然一层套一层时，第一个收尾标签
    就把外层关掉了，剩下的 `</details>` 会当正文露出来。
    """
    segments: list[tuple[str, str]] = []
    plain_lines: list[str] = []
    block: list[str] = []
    depth = 0
    for line, scan in zip(text.split("\n"), masked(text).split("\n")):
        low = scan.lower()
        opens = low.count("<details")
        closes = low.count("</details")
        if depth:
            block.append(line)
            depth += opens - closes
            if depth <= 0:
                segments.append(("details", "\n".join(block)))
                block, depth = [], 0
            continue
        if opens:
            if plain_lines:
                segments.append(("text", "\n".join(plain_lines)))
                plain_lines = []
            block = [line]
            depth = opens - closes
            if depth <= 0:
                segments.append(("details", "\n".join(block)))
                block, depth = [], 0
            continue
        plain_lines.append(line)
    if plain_lines:
        segments.append(("text", "\n".join(plain_lines)))
    if block:
        segments.append(("details", "\n".join(block)))
    return segments


def is_structural(line: str) -> bool:
    """这一行是不是「得跟后面几行一起画」的结构行。"""
    return bool(_STRUCTURAL.match(line))


def is_structural_prefix(line: str) -> bool:
    """这半行是不是「还没写完的结构标记」（`-`、`1.`、`##`、`|`、`` ` `` 这些）。

    流式按 token 切，`- 项` 会先到 `-`、`## 标题` 会先到 `##`。只判完整结构行的话，
    这些半截标记会被当普通文字画进上一段，剩下的字也再进不了结构段落——列表/标题就没了。
    """
    return bool(_STRUCTURAL_PREFIX.match(line))


def is_fence(line: str) -> bool:
    """围栏代码块的分界行（``` 或 ~~~，两种写法 markdown 都认）。"""
    return line.strip().startswith(("```", "~~~"))


# ---- 公式 ----
# 桌面端正文是 remark-math + KaTeX（`assistant-ui/thread` 的 markdown 管线），$…$ 渲成数学式。
# Qt 这边没有 JS 环境、KaTeX 用不了，用 matplotlib 的 mathtext 渲成图；分界认四种写法——
# `$…$` / `$$…$$`（remark-math 的规矩：开 `$` 后面不能是空格、闭 `$` 前面不能是空格，
# 不然「$5 和 $10」这种价钱会被当公式）和 `\(…\)` / `\[…\]`（模型写 LaTeX 时爱用）。
# mathtext 只会排一行字面量的矩阵，`\begin{matrix}` 这类环境得自己按格子排。
MATH_SRC = "noesia-math://"
MATH_TEXT_COLOR = "#ece6ef"  # 跟 chat_panel STYLE 里 #view 的正文色一致，改一处记得改另一处
MATH_SIZE_PT = 9.0  # 跟 chat_panel 里正文 9pt 一致
_MATH_KEY = "\ue000noesia-math-{}\ue001"


def _code_span_end(text: str, start: int) -> int:
    """反引号代码段的结束位置（开几个反引号就等几个）；没闭合就到结尾。"""
    run = len(text) - len(text[start:].lstrip("`"))
    close = text.find("`" * run, start + run)
    return (close + run) if close >= 0 else len(text)


def _math_spans(text: str) -> tuple[list[tuple[int, int, str, bool]], bool]:
    """扫出数学式的位置：[(起, 止, 公式源码, 是不是块级)]；顺带报「有没有没闭合的公式」。

    `\\$` 转义过的、反引号代码段里的 `$` 都不算；开括号找不到闭括号时不算公式（渲染就按原文
    走），但「还没写完」要记成 True——流式半截公式得压着别画，不然屏幕上就是一行残源码。
    """
    spans: list[tuple[int, int, str, bool]] = []
    pending = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if text.startswith("\\(", i) or text.startswith("\\[", i):
                display = text[i + 1] == "["
                closer = "\\]" if display else "\\)"
                close = text.find(closer, i + 2)
                if close < 0:
                    pending = True
                    i += 2
                    continue
                tex = text[i + 2 : close]
                if tex.strip():
                    spans.append((i, close + 2, tex, display))
                i = close + 2
                continue
            if i + 1 < len(text):
                i += 2
                continue
            i += 1
            continue
        if ch == "`":
            i = _code_span_end(text, i)
            continue
        if ch != "$":
            i += 1
            continue
        if text.startswith("$$", i):
            close = text.find("$$", i + 2)
            if close < 0:
                pending = True
                i += 2
                continue
            tex = text[i + 2 : close]
            if tex.strip():
                spans.append((i, close + 2, tex, True))
            i = close + 2
            continue
        if i + 1 >= len(text):
            pending = True
            i += 1
            continue
        if text[i + 1].isspace() or text[i + 1] == "$":
            i += 1
            continue
        close = i + 1
        while True:
            close = text.find("$", close)
            if close < 0:
                break
            prev = text[close - 1]
            if prev == "\\" or prev.isspace() or prev == "$":
                close += 1
                continue
            break
        if close < 0:
            pending = True
            i += 1
            continue
        spans.append((i, close + 1, text[i + 1 : close], False))
        i = close + 1
    return spans, pending


def math_incomplete(text: str) -> bool:
    """这一段里有没有还没闭合的公式（流式半截公式压着不画，等闭合再整块画）。"""
    return _math_spans(text)[1]


def is_math_fence(line: str) -> bool:
    """`$$` 或 `\\[` 起头、这一行里没闭合的块级公式（跟 ``` 的开合同款，攒到闭合才画）。"""
    stripped = line.strip()
    if stripped.startswith("$$"):
        return "$$" not in stripped[2:]
    return stripped.startswith("\\[") and "\\]" not in stripped[2:]


def is_math_line(line: str) -> bool:
    """一整行就是一个完整块级公式（`$$x$$` 或 `\\[x\\]`）：单独画成一块。"""
    stripped = line.strip()
    if stripped.startswith("$$"):
        return stripped.endswith("$$") and len(stripped) > 4
    return stripped.startswith("\\[") and stripped.endswith("\\]") and len(stripped) > 4


def math_fence_closes(line: str) -> bool:
    """这一行里出现了块级公式的闭合标记（`$$` 或 `\\]`）；公式内容行里不会有裸的。"""
    stripped = line.strip()
    return "$$" in stripped or "\\]" in stripped


def _protect_math(text: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    """把公式抠出来换成占位符，剩下的文字照常过 markdown；返回（替换后的文本, 占位符→(源码, 块级))。

    公式不进 markdown 解析：不然反斜杠被转义吃掉（`\\,` 会变 `,`）、下划线被当斜体切开。
    """
    spans, _pending = _math_spans(text)
    if not spans:
        return text, {}
    holders: dict[str, tuple[str, bool]] = {}
    pieces: list[str] = []
    last = 0
    for start, end, tex, display in spans:
        key = _MATH_KEY.format(len(holders))
        holders[key] = (tex, display)
        pieces.append(text[last:start])
        pieces.append(key)
        last = end
    pieces.append(text[last:])
    return "".join(pieces), holders


_ENV_RE = re.compile(r"^\s*\\begin\{(\w+)\}(.*)\\end\{\1\}\s*$", re.S)
_ENV_SEG_RE = re.compile(r"(\\begin\{\w+\}.*?\\end\{\w+\})", re.S)
_ENV_DELIMS = {
    "matrix": ("", ""),
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "vmatrix": ("|", "|"),
    "cases": (r"\{", ""),
    "aligned": ("", ""),
}


def _math_html(tex: str, display: bool, *, centered: bool) -> str:
    """公式 → HTML。按环境段切开：环境走格子排版，其余走公式图，可以前后带正文混排。"""
    chunks: list[str] = []
    for piece in _ENV_SEG_RE.split(tex.strip()):
        if not piece.strip():
            continue
        env = _ENV_RE.match(piece)
        if env and env.group(1) in _ENV_DELIMS:
            chunks.append(_env_html(env.group(1), env.group(2), centered=False))
            continue
        try:
            src, _data, width, height = _math_png(piece, display)
        except Exception:  # noqa: BLE001
            chunks.append(f"<span style='{_CODE_STYLE}'>{html.escape(piece.strip())}</span>")
            continue
        chunks.append(f'<img src="{src}" width="{width}" height="{height}"/>')
    if not chunks:
        return ""
    body = "".join(chunks)
    return f"<p style='text-align:center;margin:4px 0;'>{body}</p>" if centered else body


def _split_rows(body: str) -> list[str]:
    """按双反斜杠分行；行距写法（双反斜杠加方括号参数，如 [2pt]）的参数吃掉。"""
    rows: list[str] = []
    for part in body.split(chr(92) * 2):
        row = part.lstrip()
        if row.startswith("[") and "]" in row:
            inner, _, rest = row[1:].partition("]")
            if inner and any(ch.isdigit() for ch in inner) and all(ch in "0123456789.+-ptemxmcdbpslu" for ch in inner):
                row = rest.lstrip()
        rows.append(row)
    return rows


def _env_html(name: str, body: str, *, centered: bool) -> str:
    r"""矩阵 / 对齐 / 分段函数：按 `\\` 分行、`&` 分列，每格单独渲成图再拼表格。

    mathtext 不排多行环境（`\begin{matrix}` 只会当一行字面量画），跨行的 `\left(` 也拉不开，
    所以自己排：格子各渲各的图，括号图按整块高度纵向拉伸，摆在表格两侧。
    """
    rows = [[cell.strip() for cell in row.split("&")] for row in _split_rows(body) if row.strip()]
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    cell_rows: list[list[tuple[str, int]]] = []
    for row in rows:
        cells = [_cell_html(tex) for tex in row]
        cells += [("", 0)] * (columns - len(cells))
        cell_rows.append(cells)
    total = sum(max((height for _markup, height in cells), default=0) for cells in cell_rows) + 3 * len(cell_rows)
    left, right = _ENV_DELIMS[name]
    parts = ["<table border='0' cellspacing='0' cellpadding='2'>"]
    for r, cells in enumerate(cell_rows):
        tds = []
        if r == 0 and left:
            tds.append(f"<td rowspan='{len(cell_rows)}' valign='middle'>{_bracket_html(left, total)}</td>")
        for c, (markup, _height) in enumerate(cells):
            tds.append(f"<td style='{_env_align(name, c)}'>{markup}</td>")
        if r == 0 and right:
            tds.append(f"<td rowspan='{len(cell_rows)}' valign='middle'>{_bracket_html(right, total)}</td>")
        parts.append("<tr>" + "".join(tds) + "</tr>")
    parts.append("</table>")
    table = "".join(parts)
    return f"<p style='text-align:center;margin:4px 0;'>{table}</p>" if centered else table


def _env_align(name: str, column: int) -> str:
    """`aligned` 的列右、左交替（公式在 `=` 对齐）；`cases` 整体靠左；矩阵类居中。"""
    if name == "aligned":
        return "text-align:right;" if column % 2 == 0 else "text-align:left;"
    if name == "cases":
        return "text-align:left;"
    return "text-align:center;"


def _cell_html(tex: str) -> tuple[str, int]:
    if not tex:
        return "", 0
    try:
        src, _data, width, height = _math_png(tex, False)
    except Exception:  # noqa: BLE001
        return f"<span style='{_CODE_STYLE}'>{html.escape(tex)}</span>", 0
    return f'<img src="{src}" width="{width}" height="{height}"/>', height


def _bracket_html(delimiter: str, height: int) -> str:
    r"""括号图：mathtext 出一个字符，纵向拉到整块高（跨行的 `\left(` 拉不开，只能拉图）。"""
    src, _data, width, base = _math_png(delimiter, True)
    return f'<img src="{src}" width="{width}" height="{max(height, base)}"/>'


def _restore_math(markup: str, holders: dict[str, tuple[str, bool]]) -> str:
    """占位符换回公式图；独占一段的块级公式连外层段落一起换成居中那版（不然套两层 `<p>` 多一行空白）。"""
    for key, (tex, display) in holders.items():
        solo = f"<p style='{_FLUSH_STYLE}'>{key}</p>"
        if display and solo in markup:
            markup = markup.replace(solo, _math_html(tex, True, centered=True))
            continue
        markup = markup.replace(key, _math_html(tex, display, centered=False))
    return markup


_PNG_BYTES: dict[str, bytes] = {}
_PNG_CACHE_MAX = 512

_MATH_ALIAS = (
    ("\\dfrac", "\\frac"),
    ("\\tfrac", "\\frac"),
    ("\\operatorname", "\\mathrm"),
    ("\\boldsymbol", "\\mathbf"),
    ("\\Bbb", "\\mathbb"),
    ("\\bm", "\\mathbf"),
    ("\\iff", "\\Longleftrightarrow"),
    ("\\implies", "\\Longrightarrow"),
)
_BIG_RE = re.compile(r"\\[bB]igg?[lr]?(?![A-Za-z])")
_UNKNOWN_RE = re.compile(r"Unknown symbol: (\\[A-Za-z]+)")

_CJK_RANGES = ((0x3000, 0x303F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))
_CJK_FONT: Any = None
_MATH_FONTS_PATCHED = False


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


class _CjkGlyphFallback:
    """数学字形照旧，只有中文缺字形时逐字落到雅黑——和 KaTeX 字体回退同一语义。"""

    def __init__(self, base: Any) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        if name == "_base":
            raise AttributeError(name)
        return getattr(self._base, name)

    def _get_glyph(self, fontname: str, font_class: str, sym: str) -> Any:
        global _CJK_FONT
        if len(sym) == 1 and _is_cjk(sym):
            if _CJK_FONT is None:
                from matplotlib import font_manager
                from matplotlib.ft2font import FT2Font

                try:
                    path = font_manager.findfont(
                        font_manager.FontProperties(family="Microsoft YaHei"), fallback_to_default=False
                    )
                    _CJK_FONT = FT2Font(path)
                except Exception:  # noqa: BLE001
                    _CJK_FONT = False
            if _CJK_FONT and _CJK_FONT.get_char_index(ord(sym)):
                return _CJK_FONT, ord(sym), False
        if fontname in ("it", "regular", "normal"):
            fontname = "rm"
        return self._base._get_glyph(fontname, font_class, sym)


def _setup_math_fonts() -> None:
    """给 mathtext 字体链挂中文回退；失败就保持原样（公式照渲，只有中文仍是假字形）。"""
    global _MATH_FONTS_PATCHED
    if _MATH_FONTS_PATCHED:
        return
    _MATH_FONTS_PATCHED = True
    try:
        from matplotlib import _mathtext

        original = _mathtext.DejaVuSansFonts.__init__

        def patched(self: Any, default_font_prop: Any, load_glyph_flags: Any) -> None:
            original(self, default_font_prop, load_glyph_flags)
            self._fallback_font = _CjkGlyphFallback(self._fallback_font)

        _mathtext.DejaVuSansFonts.__init__ = patched
    except Exception:  # noqa: BLE001
        pass


@lru_cache(maxsize=256)
def _render_math_png(tex: str, display: bool) -> tuple[str, bytes, int, int]:
    """公式 →（资源名，PNG 字节，显示宽，显示高）。按 2 倍密度渲、显示尺寸折半，高分屏不糊。

    mathtext 不认 `\\displaystyle`，显示/行内只差字号；不认得的命令按报错剔除重试，
    剔不掉才抛给调用方兜底——绝不把整条公式退回成源码文字。
    """
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import mathtext
    from matplotlib.font_manager import FontProperties

    _setup_math_fonts()

    tex = tex.replace("\n", " ").strip()
    for alias, target in _MATH_ALIAS:
        tex = tex.replace(alias, target)
    tex = _BIG_RE.sub("", tex)
    buffer = io.BytesIO()
    for _attempt in range(6):
        try:
            mathtext.math_to_image(
                f"${tex}$",
                buffer,
                dpi=192,
                prop=FontProperties(size=MATH_SIZE_PT * (1.35 if display else 1.0)),
                color="black",
            )
            break
        except Exception as exc:  # noqa: BLE001
            unknown = _UNKNOWN_RE.search(str(exc))
            if not unknown:
                raise
            tex = tex.replace(unknown.group(1), "")
            buffer = io.BytesIO()
    data = _transparent_png(buffer.getvalue())
    width, height = struct.unpack(">II", data[16:24])
    src = MATH_SRC + hashlib.sha1(f"{display}|{tex}".encode("utf-8")).hexdigest()[:12]
    return src, data, width // 2, height // 2


def _math_png(tex: str, display: bool) -> tuple[str, bytes, int, int]:
    """`_render_math_png` 外面那层：**命中 lru 也要把字节登记回 `_PNG_BYTES`**。

    lru 按**访问顺序**留（回看老公式会续命），`_PNG_BYTES` 按**插入顺序**删；只在函数体里登记一次的话，
    一条反复回看的老公式会「lru 里在、表里没了」，`math_png()` 回 `b""`、`loadFromData` 失败，
    那几张图整块消失（不占位、不报错）。一场超过 512 个不同公式后必然出现。
    """
    src, data, width, height = _render_math_png(tex, display)
    if src not in _PNG_BYTES:
        if len(_PNG_BYTES) >= _PNG_CACHE_MAX:
            for stale in list(_PNG_BYTES)[: _PNG_CACHE_MAX // 4]:
                _PNG_BYTES.pop(stale, None)
        _PNG_BYTES[src] = data
    return src, data, width, height


# 缓存在里层，外面这层照样把 lru 的那套自省挂出来，免得按老名字看命中率的代码全断。
_math_png.cache_info = _render_math_png.cache_info  # type: ignore[attr-defined]
_math_png.cache_clear = _render_math_png.cache_clear  # type: ignore[attr-defined]


def _transparent_png(raw: bytes) -> bytes:
    """mathtext 出的是白底黑字——白底在深色主题上就是一块白板，近白的字在上面根本看不见。
    拿灰度当透明度（白底透明、黑字实心），再统一染正文色，哪种底上都看得见。"""
    from PIL import Image

    gray = Image.open(io.BytesIO(raw)).convert("L")
    tinted = Image.new("RGBA", gray.size, _hex_rgb(MATH_TEXT_COLOR) + (0,))
    tinted.putalpha(gray.point(lambda value: 255 - value))
    out = io.BytesIO()
    tinted.save(out, "PNG")
    return out.getvalue()


def _hex_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def math_png(src: str) -> bytes:
    """公式图的 PNG 字节（chat_panel 注册进 QTextDocument 文档资源用）。"""
    return _PNG_BYTES.get(src, b"")
