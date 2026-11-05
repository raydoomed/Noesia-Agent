"""从引擎的 DEFAULT_CONFIG 生成 docs/engine-config.md（完整键表 + 内置默认值）。

改完引擎或升级引擎后跑一次，保持文档与默认值一致（没有自动校验，得手动跑）：
    python scripts/gen_engine_config_doc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "engine"))

from noesia_cli.config_defaults import DEFAULT_CONFIG  # noqa: E402

OUT = ROOT / "docs" / "engine-config.md"


def leaves(node: object, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, f"{prefix}{key}.")
    else:
        yield prefix.rstrip("."), node


def show(value: object) -> str:
    if value is None:
        return "`null`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, str):
        return f'`"{value}"`' if value else '`""`'
    if isinstance(value, (list, tuple)):
        return "`[]`" if not value else "`" + str(list(value)) + "`"
    return f"`{value}`"


def render() -> str:
    rows = sorted(leaves(DEFAULT_CONFIG))
    lines = [
        "# 引擎配置键表",
        "",
        "这份表由引擎的 `DEFAULT_CONFIG`（`src/engine/noesia_cli/config_defaults.py`）自动汇总，",
        "列出全部配置键和内置默认值。改配置的两种方式：",
        "",
        "1. 改 `<home>/config.yaml`（引擎自己的配置，认这份）——只写你要改的键，其余按本表默认；",
        "2. 新装用户也可以在第一次启动前把值写进去；引擎只在键缺失时才补默认值",
        "",
        "引擎家目录里的 `config.yaml` 就是这份表（每次启动按引擎默认值补齐、你改过的键保留），",
        "所以改配置不需要另找地方——直接改那一份，重开应用生效。",
        "",
        "本表只列默认值，不代表所有键都做过实测——拿不准就保持默认。",
        "",
        f"共 {len(rows)} 个键。重新生成本表：`python scripts/gen_engine_config_doc.py`。",
        "",
        "| 键 | 默认值 |",
        "| --- | --- |",
    ]
    lines += [f"| `{path}` | {show(value)} |" for path, value in rows]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"写出 {OUT.relative_to(ROOT)} — {OUT.stat().st_size} 字节")
