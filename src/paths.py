"""运行时目录解析：仓库里跑、pip 装完在别的工作目录跑，都能找到 data/。

优先级：
1. 环境变量 `NOESIA_HOME`——明确指定的数据目录（部署时最稳）；
2. 当前工作目录——里面有 `data/` 就用它（`noesia-gui` 在项目目录里跑）；
3. 源码树根（从本文件往上找带 `data/` 的祖先目录）；
4. 都没有就退回当前工作目录，让上层按「缺配置」报错。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "NOESIA_HOME"


def project_root(start: Path | None = None) -> Path:
    """返回项目根（data/、assets/、src/ 的父目录）。"""
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd().resolve()
    if (cwd / "configs").is_dir() or (cwd / "data").is_dir():
        return cwd

    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / "configs").is_dir() or (parent / "data").is_dir():
            return parent
    return cwd


def repo_root() -> Path:
    """代码所在仓库根（**不受** `NOESIA_HOME` 影响）。

    脚本、assets 都跟着代码走，跟「数据放在哪」是两件事：`NOESIA_HOME` 指到
    `data/NoesiaHome` 时 `project_root()` 会跟着指过去，拿它定位代码或素材就会跑到
    引擎家目录里找——那儿本来就没有。
    """
    return Path(__file__).resolve().parents[1]

