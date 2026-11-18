"""engine_wire 的 `_call("方法", {...})` 与引擎契约对账：方法名、参数名、必填项。

查表口径跟 `/api/ws` 分发器一致（`gateway/session_controls.py`）：canonical 优先，没有
canonical 契约才退到 TUI 面的 METHODS。参数值按契约注解填占位值，只让键名（多余/缺失）
暴露出来——类型错归 handler 的域错误码，探针不越权改判。

`**` 展开与运行时才成形的参数静态求不出来，逐条按 SKIP 打出来；方法名本身是变量的
调用（`_call(method, ...)`）计 DYNAMIC，不进 CALLS。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types
import typing

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "engine"))

from pydantic import BaseModel  # noqa: E402

from tui_gateway.contracts import registry as R  # noqa: E402

_UNION = (typing.Union, types.UnionType)


def placeholder(annotation, depth: int = 0):
    """A value that satisfies `annotation`, so only key names can fail validation."""
    if depth > 4:
        return None
    origin = typing.get_origin(annotation)
    if origin in _UNION:
        inner = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return placeholder(inner[0], depth + 1) if inner else None
    if origin is typing.Literal:
        return typing.get_args(annotation)[0]
    if origin in (list, set, tuple):
        return []
    if origin is dict:
        return {}
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is str:
        return "x"
    if annotation is bool:
        return False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        # Required fields only: a None for an optional-but-non-Optional field
        # (`is_bot: bool = False`) is a type error, not a key problem.
        return {
            name: placeholder(field.annotation, depth + 1)
            for name, field in annotation.model_fields.items()
            if field.is_required()
        }
    return None


def fill(contract, keys: list[str]) -> dict:
    fields = contract.params.model_fields
    return {key: placeholder(fields[key].annotation) if key in fields else None for key in keys}


def scan(tree: ast.AST) -> tuple[list[tuple[str, list[str] | None, int, str | None]], int]:
    calls = []
    dynamic = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call"
            and node.args
        ):
            continue
        head = node.args[0]
        if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
            dynamic += 1
            continue
        name = head.value
        if len(node.args) < 2:
            calls.append((name, None, node.lineno, "no-args"))
        elif isinstance(node.args[1], ast.Dict):
            keys = [key.value for key in node.args[1].keys if isinstance(key, ast.Constant)]
            if len(keys) != len(node.args[1].keys):
                calls.append((name, None, node.lineno, "**unpack"))
            else:
                calls.append((name, keys, node.lineno, None))
        else:
            calls.append((name, None, node.lineno, "non-dict"))
    return calls, dynamic


def main() -> int:
    tree = ast.parse((ROOT / "src" / "app" / "engine_wire.py").read_text(encoding="utf-8"))
    calls, dynamic = scan(tree)
    names = {name for name, _keys, _line, _skip in calls}
    known = set(R.METHODS) | set(R.CANONICAL_METHODS)
    print(
        f"CALLS {len(calls)}  METHODS {len(names)}  CONTRACTS {len(known)}  "
        f"UNKNOWN {sorted(names - known)}"
    )

    checked, skipped, bad = 0, [], []
    for name, keys, line, skip in calls:
        if skip is not None or keys is None:
            skipped.append((line, name, skip or "no-keys"))
            continue
        contract = R.CANONICAL_METHODS.get(name)
        if contract is not None:
            problems = R.canonical_param_problems(contract, fill(contract, keys))
        else:
            contract = R.METHODS.get(name)
            if contract is None:
                skipped.append((line, name, "unknown"))
                continue
            problems = R.validate_params(contract, fill(contract, keys))[1]
        checked += 1
        if problems:
            bad.append((line, name, problems if isinstance(problems, str) else ", ".join(problems)))

    print(f"CHECKED {checked}  SKIP {len(skipped)}  DYNAMIC {dynamic}  PARAM_MISMATCH {len(bad)}")
    for line, name, reason in sorted(skipped):
        print(f"SKIP ({line}, '{name}', '{reason}')")
    for line, name, problems in bad:
        print(f"MISMATCH ({line}, '{name}', {problems})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
