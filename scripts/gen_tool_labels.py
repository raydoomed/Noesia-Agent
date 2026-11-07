"""把 TS 文案里的 `assistant.tool` 这棵子树原样搬到 Python。

为什么要生成而不是手抄：这张表有二十多个工具、每个三种时态，还带标题模板函数；手抄必错，
错一个字就是界面上的一句怪话。源目录不入库，重跑时用参数指过去：
    python scripts/gen_tool_labels.py --i18n-dir <含 zh.ts / en.ts 的目录>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "src" / "app" / "tool_view" / "labels.py"

KEY_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(?=\s)")
INLINE_KEY_RE = re.compile(r"([{,]+\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(?=\s)")
TERNARY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*===\s*(\d+)\s*\?\s*('(?:[^']*)')\s*:\s*('(?:[^']*)')")
TEMPLATE_RE = re.compile(r"`([^`]*)`")
ANNOT_RE = re.compile(r":\s*(number|string|boolean|unknown)\b")

HEADER = '''"""工具行的中文/英文文案：由 `scripts/gen_tool_labels.py` 生成，别手改。

`ZH` / `EN` 保留原始嵌套结构，`LABELS` 是拍平成点号键之后的结果（供 `i18n.tr` 查）。
"""

from __future__ import annotations

from typing import Any, Callable
'''

FOOTER = '''

def _flatten(node: dict[str, Any], prefix: str = "assistant.tool") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _with_common(flat: dict[str, Any], common: dict[str, str]) -> dict[str, Any]:
    return {**flat, **{f"common.{key}": value for key, value in common.items()}}


LABELS: dict[str, dict[str, Any]] = {
    "zh": _with_common(_flatten(ZH), COMMON_ZH),
    "en": _with_common(_flatten(EN), COMMON_EN),
}
'''


def block(text: str, key: str = "tool", indent: str = "    ", anchor: str | None = None) -> str:
    """取出 `    tool: {` 这棵子树。

    不能靠「第一个 `    tool: {`」：别的子树里也有同名的 `tool`（报错文案那块）。
    认锚点看第一个键（`copyCode`），再找回缩到 4 空格的那一行 `}` 收尾——比数花括号稳，
    字符串和模板串里的括号不会捣乱。
    """
    needle = f"{indent}{key}: {{\n{indent}  {anchor}:" if anchor else f"{indent}{key}: {{"
    start = text.index(needle) + len(f"{indent}{key}: ")
    out: list[str] = []
    for line in text[start:].splitlines():
        out.append(line)
        if line == f"{indent}}}":
            return "\n".join(out)
    raise ValueError(f"{key} 块没闭合")


def _template(src: str) -> str:
    """模板串 → f-string：`` `x${a}` `` → ``f"x{a}"``。

    英文文案里有 JS 三元（`${count === 1 ? '' : 's'}`），Python 的 f-string 不认，
    转成 `(... if ... else ...)`：这一层只出现「某值 === 数字 ? '字面量' : '字面量'」这一种。
    """

    def one(match: re.Match) -> str:
        inner = TERNARY_RE.sub(
            lambda t: f"({t.group(3)} if {t.group(1)} == {t.group(2)} else {t.group(4)})", match.group(1)
        )
        inner = inner.replace(".toLowerCase()", ".lower()").replace(".toUpperCase()", ".upper()")
        return 'f"' + inner.replace("${", "{").replace('"', '\\"') + '"'

    return TEMPLATE_RE.sub(one, src)


def to_python(src: str) -> str:
    """TS 对象字面量 → Python dict 字面量。

    顺序不能乱：先把箭头函数收成 `lambda 参数: 表达式`（参数里的 `: number` 一并去掉），
    再给键加引号——否则 `lambda prefix, action:` 里的 `action:` 会被当成对象键。
    """
    out: list[str] = []
    for line in src.splitlines():
        if "=>" in line:
            head, _, tail = line.partition("=>")
            head = ANNOT_RE.sub("", head)
            indent, _sep, key_part = head.rpartition(":")
            params = key_part.strip().strip("()").strip()
            key = indent.strip()
            pad = indent[: len(indent) - len(indent.lstrip())]
            out.append(f'{pad}"{key}": lambda {params}: {_template(tail.strip())}')
            continue
        line = INLINE_KEY_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}":', _template(line))
        out.append(KEY_LINE_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}":', line))
    return "\n".join(out)


def flatten(node: dict, prefix: str = "assistant.tool") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, path))
        else:
            flat[path] = value
    return flat


COMMON_LINE_RE = re.compile(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*):\s*'((?:[^'\\]|\\.)*)',?\s*$")


COMMON_KEYS = ("copy", "copied")


def common_strings(text: str) -> dict[str, str]:
    """顶层 `common` 里**平铺的字符串键**（工具块用到的「复制/已复制」都在这一层）。

    不把整棵 common 转成 Python：那棵树里有嵌套对象和函数，转过来要处理一堆与工具行无关的东西。
    这里只要单引号字符串，值原样照抄。
    """
    out: dict[str, str] = {}
    for line in block(text, "common", "  ").splitlines():
        match = COMMON_LINE_RE.match(line)
        if match and match.group(1) in COMMON_KEYS:
            out[match.group(1)] = match.group(2).replace("\\'", "'")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 src/app/tool_view/labels.py")
    parser.add_argument("--i18n-dir", required=True, help="含 zh.ts / en.ts 的目录")
    source = Path(parser.parse_args().i18n_dir)
    for name in ("zh.ts", "en.ts"):
        if not (source / name).is_file():
            raise SystemExit(f"{source} 里没有 {name}：--i18n-dir 指的是含 zh.ts / en.ts 的那个目录")
    zh_text = (source / "zh.ts").read_text(encoding="utf-8")
    en_text = (source / "en.ts").read_text(encoding="utf-8")
    zh_src = to_python(block(zh_text, "tool", "    ", "copyCode"))
    en_src = to_python(block(en_text, "tool", "    ", "copyCode"))
    common_zh = common_strings(zh_text)
    common_en = common_strings(en_text)
    zh = flatten(eval(compile(zh_src, "zh", "eval"), {}))  # noqa: S307
    en = flatten(eval(compile(en_src, "en", "eval"), {}))  # noqa: S307
    missing = sorted(set(zh) ^ set(en))
    print(f"zh {len(zh)} 键 / en {len(en)} 键 / 差集 {len(missing)}")
    for key in missing[:10]:
        print("  差:", key)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = (
        HEADER
        + "\nZH: dict[str, Any] = "
        + zh_src
        + "\n\nEN: dict[str, Any] = "
        + en_src
        + "\n\nCOMMON_ZH: dict[str, str] = "
        + repr(dict(sorted(common_zh.items())))
        + "\n\nCOMMON_EN: dict[str, str] = "
        + repr(dict(sorted(common_en.items())))
        + FOOTER
    )
    OUT.write_text(body, encoding="utf-8")
    print("写出", OUT, OUT.stat().st_size, "字节")


if __name__ == "__main__":
    main()
