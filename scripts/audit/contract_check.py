"""engine_wire 的 `_call("方法", {...})` 与引擎契约对账：方法名、参数名、必填项。"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "engine"))

from tui_gateway.contracts import registry as R  # noqa: E402

tree = ast.parse((ROOT / "src" / "app" / "engine_wire.py").read_text(encoding="utf-8"))
calls = []
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_call"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        keys = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
            keys = [k.value for k in node.args[1].keys if isinstance(k, ast.Constant)]
        calls.append((node.args[0].value, keys, node.lineno))

methods = {name for name, _keys, _line in calls}
known = set(R.METHODS)
print(f"CALLS {len(calls)} METHODS {len(methods)} CONTRACTS {len(known)}")
print("UNKNOWN", sorted(methods - known))

bad = []
for name, keys, line in calls:
    if keys is None:
        continue
    contract = R.METHODS.get(name)
    if contract is None:
        continue
    try:
        R.validate_params(contract, {k: None for k in keys})
    except Exception as exc:  # noqa: BLE001
        bad.append((line, name, str(exc)[:140]))
print("PARAM_MISMATCH", len(bad))
for row in bad[:10]:
    print("  ", row)
