"""正文渲染：把引擎推来的 markdown 变成 Qt 能画的 HTML。

**为什么要这一层**：桌面端的正文段就是 markdown 渲染（`assistant-ui/thread` 里的
`TimelineMarkdownText`，代码块交给高亮器）。我们照同一条路——用现成的 markdown 库解析，
再拿 pygments 上色；不自己写「哪些符号算标题、算列表」这种规则。

两种落法，对应流式的两种情形：
- `inline()`：普通一行字。马上画出来（流式输出才跟得上），只处理行内语法（粗体、行内代码、链接）。
- `block()`：列表 / 表格 / 代码块 / 标题这类要连着好几行才算数的结构，整段一次画。
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Any

# 需要「连着几行一起看」的行首标记：列表项、引用、表格、标题、缩进续行
_STRUCTURAL = re.compile(r"^(\s{2,}|[-*+]\s|\d+[.)]\s|>|\||#{1,6}\s)")

# 代码块的底色：悬停复制按钮要靠它认出“这块是代码”（见 `chat_panel._is_code_block`）。
CODE_BACKGROUND = "#15161c"

# 代码块**一行一段**画（`<p>`），不用 `<pre>`：往已有内容的文档里追加 `<pre>` 时，
# Qt 的 HTML 片段导入会丢掉第一段（有时是前几段）的底色，悬停复制的命中判定就认不出代码；
# 而每行一个 `<p>` 时，外围块底色 + 字符底色双保险，追加多少次都不丢。
#
# 这里**不能写 `white-space:pre`**：Qt 一看见它就不给这一行换行，长一点的代码行
# （ASCII 图、带中文注释的一行）直接把文档撑得比视口宽，聊天窗底下就冒出横向滚动条。
# 缩进靠 `_nbsp()` 把文本里的空格换成 `&nbsp;` 保住——既不被 HTML 压成一个空格，
# 也不挡住换行（行太宽时 `WrapAtWordBoundaryOrAnywhere` 允许断在任意处）。
_CODE_STYLE = (
    f"background-color:{CODE_BACKGROUND};color:#d7dae5;font-family:Consolas,'Cascadia Mono',monospace;"
    "font-size:9pt;margin:0 0 0 6px;"
)
_CODE_LINE_SPAN = f"background-color:{CODE_BACKGROUND}"

_TAG = re.compile(r"<[^>]+>")


@lru_cache(maxsize=1)
def _parser() -> Any:
    """markdown 解析器（只建一次）。表格、删除线打开——引擎的说明里常见这两样。"""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    for rule in ("table", "strikethrough"):
        try:
            md.enable(rule)
        except Exception:  # noqa: BLE001 - 老版本没有这条规则就算了
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
        f"<p style='{_CODE_STYLE}'><span style='{_CODE_LINE_SPAN}'>{_nbsp(line) if line else '&nbsp;'}</span></p>"
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
    except Exception:  # noqa: BLE001 - 高亮失败不影响可读性
        body = ""
    if not body:
        body = html.escape(code)
    return _code_lines(body)


def inline(text: str) -> str:
    """一段普通文字的行内渲染（粗体、行内代码、链接）；不含外层段落标签。"""
    if not text.strip():
        return ""
    try:
        return _parser().renderInline(text)
    except Exception:  # noqa: BLE001 - 解析出错就原样显示，不能吞掉用户的内容
        return html.escape(text)


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


# 块级元素一律不带上下边距：Qt 的富文本导入给 `<p>` 默认 12px、`<ul>`/`<ol>` 12px，
# 段间距本来由块格式（`_plain_block_format()` 的 5px）统一给，两边各给一次就是屏幕上那一大截空白。
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
    """整段结构（列表 / 表格 / 代码块 / 标题）的渲染。"""
    if not text.strip():
        return ""
    try:
        return _zero_block_margins(_style_tables(_unwrap_pre(_parser().render(text))))
    except Exception:  # noqa: BLE001
        return _code_lines(html.escape(text))


def is_structural(line: str) -> bool:
    """这一行是不是「得跟后面几行一起画」的结构行。"""
    return bool(_STRUCTURAL.match(line))


def is_fence(line: str) -> bool:
    """围栏代码块的分界行（```）。"""
    return line.strip().startswith("```")
