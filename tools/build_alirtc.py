"""编 src\\app\\_alirtc.pyd（阿里云 ARTC 的 Python 封装）。

只有改了 _alirtc.cxx 才需要跑这个；编好的 .pyd 随仓库分发，用的人不用编译器。
用法： .venv\\Scripts\\python.exe tools\\build_alirtc.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import sys
import sysconfig
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
VCVARS_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
]


def fail(msg: str) -> "NoReturn":
    print(f"[失败] {msg}")
    raise SystemExit(1)


OUT = "src\\app\\_alirtc.pyd"


def main() -> int:
    global OUT
    args = sys.argv[1:]
    if "--out" in args:
        index = args.index("--out")
        if index + 1 >= len(args):
            fail("--out 后面要跟输出路径，例如 --out src\\app\\_alirtc.pyd")
        OUT = args[index + 1]
    print("输出:", OUT)
    include = sysconfig.get_paths()["include"]
    libdir = sysconfig.get_config_var("LIBDIR")
    libname = "python%d%d.lib" % sys.version_info[:2]
    if not (pathlib.Path(include) / "Python.h").exists():
        fail(f"找不到 Python 头文件：{include}")
    if not (pathlib.Path(libdir) / libname).exists():
        fail(f"找不到 {libname}：{libdir}")
    if not (ROOT / "vendor/alirtc/AliRTCSdk.lib").exists():
        fail("vendor/alirtc/AliRTCSdk.lib 不见了，先把 ARTC Windows SDK 解压到 vendor/alirtc")
    vcvars = next((p for p in VCVARS_CANDIDATES if pathlib.Path(p).exists()), None)
    if vcvars is None:
        fail("找不到 vcvars64.bat，需要 Visual Studio 生成工具（C++ 桌面开发）")

    lines = [
        "@echo off",
        f'call "{vcvars}" >nul',
        f'cd /d "{ROOT}"',
        f'cl /nologo /O2 /EHsc /std:c++17 /MT /utf-8 /LD '
        f'/I"{include}" /I"vendor\\alirtc\\include" '
        f'src\\app\\_alirtc.cxx '
        f'/link "{libdir}\\{libname}" "vendor\\alirtc\\AliRTCSdk.lib" '
        f'/OUT:{OUT}',
        "exit /b %errorlevel%",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        bat = pathlib.Path(tmp) / "build.bat"
        bat.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
        proc = subprocess.run(["cmd", "/c", str(bat)], capture_output=True)
    out = (proc.stdout or b"").decode("gbk", "replace") + (proc.stderr or b"").decode("gbk", "replace")
    print(out.strip()[-4000:])
    if proc.returncode != 0:
        fail("编译没过")
    for junk in ("*.obj", "*.exp", "*.lib"):
        for path in ROOT.glob(junk):
            if path.name != "AliRTCSdk.lib":
                path.unlink()
    print(f"[完成] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
