#!/usr/bin/env python3
"""跑 tests/ 下的回归脚本，外加一条防「测试变成摆设」的护栏。

为什么不用 pytest：`tests/` 下每个文件都是一条独立脚本（自己建 QApplication、
自己 print、末尾用退出码报结果），pytest 收集到 0 项，拿它当门禁等于没门禁。

为什么不用 make：本机（Windows）没装 make，`make test` 跑不起来。
这里一份实现，`make test` 和 `python scripts/run_tests.py` 走的是同一条路。

护栏：直接 import 私有符号（`_开头的名字`）的测试文件数不许超过 4 个。
这类用例钉的是实现细节——函数一改名就红，屏幕上什么都没变，红了只能改测试，
改着改着测试就成了盖章。基线只许减不许增。

`tests/` 不入库，所以别人的 clone 里没有用例：那种情况下只提示、不算失败。

用法：
    python scripts/run_tests.py
    python scripts/run_tests.py -k ptt        # 只跑文件名含 ptt 的
    python scripts/run_tests.py --dir 别的目录   # 换一套用例（自检用）

退出码：0 全过 / 1 有失败。
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TIMEOUT_S = 300  # 单个用例挂住就当场判失败：没有这条，Qt 卡死时 make test 永远不返回

# 直接依赖私有符号的文件数上限。基线（只许减不许增）：
#   test_keymap.py（_to_native/_from_native）、test_markdown_stream.py（_open_link/_photo_name）、
#   test_memory_page.py（_memory_file）、test_ptt_window.py（_key_name/_PttKeyDialog）
PRIVATE_MAX = 4


def _private_in_source(text: str) -> bool:
    """AST 扫一遍：`from a import _x` 算，括号里换行的也算。

    以前是一条正则（`^\\s*from .* import (.*[ ,])?_x`）：`from app.x import (\\n    _a,\\n)` 这种
    多行写法整段漏掉，护栏看着是绿的、实际上新用例一直在直接摸私有符号。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False  # 语法坏掉的用例自己会在跑的时候红，不归护栏管
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name.startswith("_") for alias in node.names):
                return True
    # 只认 import：白盒用例读 `panel._streaming` 这类属性是界面测试的正常手法（16 个文件都这样），
    # 也算进来等于把上限改成没有意义的大数；护栏要守的是「新用例别去 import 私有函数」。
    return False


def private_importers(files: list[Path]) -> list[Path]:
    return [f for f in files if _private_in_source(f.read_text(encoding="utf-8", errors="ignore"))]


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 tests/ 下的回归脚本")
    parser.add_argument("--dir", default=str(PROJECT_ROOT / "tests"), help="用例目录（默认 tests/）")
    parser.add_argument("-k", default="", help="只跑文件名含该子串的")
    args = parser.parse_args()

    test_dir = Path(args.dir)
    files = sorted(test_dir.glob("test_*.py"))
    if args.k:
        matched = [f for f in files if args.k in f.stem]
        if not matched:
            # `-k` 打错一个字，过滤后为空：静默放行等于让守门人以为「跑过了全绿」，
            # 实际一个用例都没跑。这里必须报错退出，并把他打的子串和现有用例都列出来。
            print(f"  -k {args.k!r} 没有匹配的用例。现有：")
            for f in files:
                print(f"    {f.stem}")
            return 2
        files = matched
    if not files:
        # 一个用例都没有不等于「全绿」：以前这里 return 0，别人的 clone（tests/ 不入库）
        # 上 `make test` 也算过关，等于没有任何守护。宁可红。
        print(f"  {test_dir} 里没有 test_*.py：这不是通过，是一个用例都没跑")
        return 2

    failed: list[str] = []
    for f in files:
        try:
            proc = subprocess.run(
                [sys.executable, str(f)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            failed.append(f.name)
            print(f"  FAIL  {f.name}（超过 {TEST_TIMEOUT_S}s 没结束，按挂住处理）")
            tail = "".join(
                part.decode("utf-8", "replace") if isinstance(part, bytes) else part
                for part in (exc.stdout, exc.stderr)
                if part
            )
            for line in tail.strip().splitlines()[-5:]:
                print(f"        {line}")
            continue
        if proc.returncode == 0:
            print(f"  PASS  {f.name}")
            continue
        failed.append(f.name)
        print(f"  FAIL  {f.name}（退出码 {proc.returncode}）")
        for line in (proc.stdout + proc.stderr).strip().splitlines():
            print(f"        {line}")

    priv = private_importers(files)
    if len(priv) > PRIVATE_MAX:
        failed.append("护栏")
        print(f"  FAIL  护栏：{len(priv)} 个测试文件直接依赖私有符号（上限 {PRIVATE_MAX}）")
        for f in priv:
            print(f"        {f.name}")
        print("        新用例改走公开路径；确实要测私有函数，先说明为什么它是契约")
    else:
        print(f"  PASS  护栏：私有符号依赖 {len(priv)}/{PRIVATE_MAX} 个文件，没有新增")

    print(f"\n{len(files)} 个回归脚本，失败 {len(failed)} 个")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
