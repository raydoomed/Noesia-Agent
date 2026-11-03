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
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 直接依赖私有符号的文件数上限。基线（只许减不许增）：
#   test_keymap.py（_to_native/_from_native）、test_markdown_stream.py（_open_link/_photo_name）、
#   test_memory_page.py（_memory_file）、test_ptt_window.py（_key_name/_PttKeyDialog）
PRIVATE_MAX = 4

# `from app.x import _y` / `from app.x import A, _y` 都算；`import x as y` 不算。
_PRIVATE_IMPORT = re.compile(r"^\s*from\s+[a-z_.]+\s+import\s+(.*[ ,])?_[A-Za-z]", re.MULTILINE)


def private_importers(files: list[Path]) -> list[Path]:
    return [f for f in files if _PRIVATE_IMPORT.search(f.read_text(encoding="utf-8", errors="ignore"))]


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 tests/ 下的回归脚本")
    parser.add_argument("--dir", default=str(PROJECT_ROOT / "tests"), help="用例目录（默认 tests/）")
    parser.add_argument("-k", default="", help="只跑文件名含该子串的")
    args = parser.parse_args()

    test_dir = Path(args.dir)
    files = sorted(test_dir.glob("test_*.py"))
    if args.k:
        files = [f for f in files if args.k in f.stem]
    if not files:
        print(f"  {test_dir} 里没有 test_*.py（tests/ 不入库，别人的 clone 里本来就没有）")
        return 0

    failed: list[str] = []
    for f in files:
        proc = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace")
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
