"""引擎家目录的定位：把 `NOESIA_HOME` 钉到项目里那份配置上。

供应商表和模型清单不在这里了——只有一个来源：引擎的 `model.options`（线上现取）。
这一层只剩一件必须先做的事：问引擎之前，保证它读的是这个项目自己的家目录。
"""

from __future__ import annotations

import os
from pathlib import Path


def engine_core(cfg, root: Path | str | None = None) -> Path:
    """引擎代码目录：`engine.core_path`（相对路径按项目根解析，不按进程 cwd）。

    cwd 是不可靠的——装成包之后用户在任意目录启动，按 cwd 找 `src/engine` 必然找不到。
    """
    core = Path(cfg.engine.core_path)
    if core.is_absolute() or root is None:
        return core.resolve()
    return (Path(root) / core).resolve()


def pin_engine_home(cfg, root: Path | str | None) -> Path:
    """把 `NOESIA_HOME` 钉在项目内的引擎家目录上，返回它。

    引擎按 `NOESIA_HOME` 找自己的配置；不钉的话，用户机器上若另有 `~/.noesia`（跑过引擎 CLI），
    我们会读到那份配置——这个程序就变成了「跟着别的程序走」。相对路径按项目根解析。
    """
    home = Path(cfg.engine.home)
    if not home.is_absolute():
        if root is None:
            home = home.resolve()
        else:
            home = (Path(root) / home).resolve()
    else:
        home = home.resolve()
    os.environ["NOESIA_HOME"] = str(home)
    return home
