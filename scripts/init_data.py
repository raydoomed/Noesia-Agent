#!/usr/bin/env python3
"""初始化运行时目录（`data/` 整目录不入库，clone 下来是空的；删掉以后也能再跑一次）。

实际逻辑在 `src/settings.py` 的 `ensure_runtime()`：引擎进程启动时会自己调同一份实现，所以这个脚本只是
「不想先开程序、想先看一眼建了什么」时的手动入口。人设与记忆不在这里预置——`SOUL.md` 要等
首次见面答完名字才写，提前放空文件会让「还是新用户」的判定失灵。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from settings import APP_TEMPLATE, app_config_path, ensure_runtime, load_config  # noqa: E402


def _seed_shell_config(project_root: Path) -> None:
    """第一次跑：把仓库里的模板复制成这台机器的外壳配置（`data/` 不入库，clone 下来是空的）。"""
    target = app_config_path(project_root)
    if target.exists():
        return
    template = PROJECT_ROOT / APP_TEMPLATE
    if not template.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[new ] {target.relative_to(project_root)}")


def main(root: Path | None = None) -> int:
    project_root = Path(root).resolve() if root else PROJECT_ROOT
    _seed_shell_config(project_root)
    cfg, resolver = load_config(root=project_root)
    created = ensure_runtime(
        root=resolver.root,
        home=resolver.root / cfg.engine.home,
        workdir=resolver.root / cfg.engine.workdir,
    )
    for path in created:
        print(f"[new ] {path.relative_to(resolver.root)}")
    if not created:
        print("[keep] 目录与引擎配置都在，未改动")
    print("完成：目录已就绪；人设、记忆与会话库由引擎在首次使用时创建")
    return 0


if __name__ == "__main__":
    sys.exit(main())
