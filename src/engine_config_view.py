"""读引擎的 `DEFAULT_CONFIG`（外壳要展示"有哪些参数可调"时用）。

引擎是以 `sys.path` 方式导入的（和 `pipeline.agent_host` 同一套），所以这个动作放在
`pipeline/` 里做，`app/` 下不出现 `sys.path` 操作——打包护栏（`tests/test_packaging.py`）
要求前端内部互相导入走包名。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


def engine_defaults(core_path: Path | str) -> dict[str, Any]:
    """引擎全部配置键与默认值的一份拷贝。

    引擎目录不存在或导入失败时抛异常——调用方自己决定要不要把它当致命错误
    （外壳要能在没有引擎时退回纯聊天）。
    """
    core = Path(core_path).resolve()
    if not core.is_dir():
        raise RuntimeError(f"引擎目录不存在：{core}")
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))

    from noesia_cli.config_defaults import DEFAULT_CONFIG  # noqa: PLC0415 - 依赖 sys.path，必须延迟导入

    return copy.deepcopy(DEFAULT_CONFIG)  # 深拷贝：调用方改自己的副本，不会污染引擎默认表


def translated_comments() -> dict[str, str]:
    """引擎注释的中文译文（`config_comments_zh.json`，键路径用点号连）。

    译文是发布时一次性翻好的；引擎升级新增的键没有译文时由调用方回退英文原话。
    """
    source = Path(__file__).resolve().with_name("config_comments_zh.json")
    if not source.exists():
        return {}
    import json  # noqa: PLC0415 - 只有生成配置时才需要

    return json.loads(source.read_text(encoding="utf-8"))


def comment_texts(core_path: Path | str) -> dict[tuple[str, ...], str]:
    """键路径 → 说明文字：优先中文译文，缺了就回退引擎英文原话。"""
    original = engine_comments(core_path)
    translated = translated_comments()
    return {
        path: translated.get(".".join(path), note) if translated else note
        for path, note in original.items()
    }


def engine_comments(core_path: Path | str) -> dict[tuple[str, ...], str]:
    """键路径 → 引擎源码里紧贴该键上方的注释（引擎原话，不翻译、不改写）。

    生成的配置文件要给用户看"这个值是干嘛的"，唯一可信的来源就是引擎自己的注释——
    自己另写一份说明迟早和引擎行为对不上。没有注释的键就不写注释。
    """
    import ast  # noqa: PLC0415 - 只在生成说明时用
    import io  # noqa: PLC0415
    import tokenize  # noqa: PLC0415

    source = Path(core_path).resolve() / "noesia_cli" / "config_defaults.py"
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)

    # 行尾注释（`"nudge_interval": 10,  # 每轮…`）也算说明，一起收进来
    trailing: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            body = token.string.lstrip("#").strip()
            if body:
                trailing[token.start[0]] = body

    literal: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG"
            for target in node.targets
        ):
            literal = node.value
            break
    if literal is None:
        raise RuntimeError(f"{source} 里没找到 DEFAULT_CONFIG")

    def note_above(lineno: int) -> str:
        parts: list[str] = []
        index = lineno - 2
        while index >= 0:
            stripped = lines[index].strip()
            if not stripped.startswith("#"):
                break
            parts.append(stripped.lstrip("#").strip())
            index -= 1
        note = " ".join(part for part in reversed(parts) if part)
        tail = trailing.get(lineno, "")
        if tail:
            note = f"{note} {tail}".strip()
        return note

    found: dict[tuple[str, ...], str] = {}

    def walk(node: ast.AST, path: tuple[str, ...]) -> None:
        if not isinstance(node, ast.Dict):
            return
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            child = path + (key.value,)
            note = note_above(key.lineno)
            if note:
                found[child] = note
            walk(value, child)

    walk(literal, ())
    return found
