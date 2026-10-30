"""本项目的两个自检子命令：`noesia check-config` / `noesia check-assets`。

用户的命令是 `noesia`（等于引擎 CLI 本身，见 `src/noesia_entry.py`）；这个模块只被它按路径
加载成 `shell` 模块，负责读配置、查素材——不进程、不通信，界面与引擎的通道在 `src/app/engine_wire.py`。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from paths import repo_root

from settings import ConfigError, app_config_path, load_config



def _project_root() -> Path:
    """仓库根：配置模板、脚本都相对它定位（不能走 `project_root()`，它会被 NOESIA_HOME 带走）。"""
    return repo_root()


def _engine_selection(cfg, root: Path) -> tuple[str, str]:
    """引擎配置里现在选的 (model, provider)。

    真源是 `engine.home/config.yaml`，所以先把 `NOESIA_HOME` 钉到项目内的引擎家目录，
    再问引擎（`agent.agent_factory.current_selection`）。引擎不可用就返回空串，不拦启动。
    """
    from backends.catalog import current_option

    try:
        option = current_option(cfg, root=root)
        return option.model, option.backend
    except Exception:  # noqa: BLE001 - 自检/启动提示不该因为引擎还没装就崩
        return "", ""


def cmd_check_config(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else _project_root()
    cfg_path = Path(args.config) if args.config else app_config_path(root)
    try:
        cfg, resolver = load_config(cfg_path, root=root)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[OK] {cfg_path}")
    print(f"     avatar={cfg.avatar.id} dir={cfg.avatar.dir} idle={cfg.avatar.idle_video}")
    model, provider = _engine_selection(cfg, root)
    print(f"     engine={provider or 'auto'}:{model}")
    print(f"     root={resolver.root}")
    return 0


def cmd_check_assets(args: argparse.Namespace) -> int:
    from assets.check import inspect_assets, render_report

    root = Path(args.root).resolve() if args.root else _project_root()
    cfg_path = Path(args.config) if args.config else app_config_path(root)
    try:
        cfg, resolver = load_config(cfg_path, root=root)
    except ConfigError as exc:
        print(f"[FAIL] 配置无法加载：{exc}")
        return 1
    report = inspect_assets(resolver, cfg.avatar)
    print(render_report(report))
    return 0 if not report.has_errors else 1


