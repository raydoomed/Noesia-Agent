"""`noesia` 命令入口 = 引擎 CLI 本身（`noesia_cli.main`）。

为什么这么薄：能力全在引擎里。用户敲的 `noesia` 和引擎自己用的必须是同一个命令行——
引擎有什么子命令（chat / config / sessions / skills / tools / gateway / serve …几十个），
这里就有哪些。外壳另立一套只有三个子命令的入口，等于让用户拿到残缺的一半。

这个文件只做两件引擎不需要知道的事：
1. 把 `src/engine` 放进 import 路径最前，并把 `NOESIA_HOME` 默认指向仓库内的引擎家目录；
2. 往引擎的命令树里追加本项目自己的两个自检子命令（实现在 `src/cli.py`，那里也负责
   前端拉起的引擎进程）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _find_root(start: Path) -> Path:
    """向上找「哪一层下面有 `src/engine`」。

    源码树里就是仓库根；`pip install`（非 -e）之后整棵树在 site-packages 里，
    引擎根本不随包发布——那时找不到，返回默认值，由 `main()` 说清楚而不是 IndexError/ImportError。
    """
    for candidate in start.parents[1:]:
        if (candidate / "src" / "engine").is_dir():
            return candidate
    return start.parents[1]


ROOT = _find_root(Path(__file__).resolve())
ENGINE = ROOT / "src" / "engine"

for path in (ENGINE, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    else:
        sys.path.remove(str(path))
        sys.path.insert(0, str(path))

import os  # noqa: E402

os.environ.setdefault("NOESIA_HOME", str(ROOT / "data" / "NoesiaHome"))


def _engine_process_module():
    """按路径加载 `src/cli.py`（引擎进程 + 两个自检），避免和引擎的 `cli` 撞名。"""
    path = ROOT / "src" / "cli.py"
    spec = importlib.util.spec_from_file_location("noesia_engine_process", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _add_local_commands(subparsers) -> None:
    """把本项目自己的两个自检挂进引擎的命令树。"""
    shell = _engine_process_module()

    check_cfg = subparsers.add_parser("check-config", help="校验外壳配置与模型（app.json：形象、素材、模型）")
    check_cfg.add_argument("--root", help="项目根目录（默认仓库根）")
    check_cfg.add_argument("--config", help="外壳配置路径（默认 data/settings/app.json）")
    check_cfg.set_defaults(func=shell.cmd_check_config)

    check_assets = subparsers.add_parser("check-assets", help="体检 assets/ 素材（尺寸、alpha、缺文件）")
    check_assets.add_argument("--root", help="项目根目录（默认仓库根）")
    check_assets.add_argument("--config", help="外壳配置路径（默认 data/settings/app.json）")
    check_assets.set_defaults(func=shell.cmd_check_assets)


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        sys.argv = [sys.argv[0], *argv]

    if not ENGINE.is_dir():
        print(
            "noesia 命令行要在源码树里跑：引擎（src/engine）不随 wheel 发布。\n"
            "请 `pip install -e .` 后在本仓库目录执行，或直接用仓库里的脚本。",
            file=sys.stderr,
        )
        return 2

    import noesia_cli.main as engine_cli

    original = engine_cli._build_cli_parser

    def build_with_local_commands():
        parser, subparsers = original()
        _add_local_commands(subparsers)
        return parser, subparsers

    engine_cli._build_cli_parser = build_with_local_commands
    try:
        return engine_cli.main() or 0
    finally:
        engine_cli._build_cli_parser = original


if __name__ == "__main__":
    sys.exit(main())
