"""她的大小/位置、聊天窗的大小/位置存哪。

Qt 的 `QSettings(组织名, 应用名)` 两参写法在 Windows 上默认写**注册表**
`HKCU\\Software\\<组织名>`——项目不碰用户注册表，也不往 C 盘用户目录里塞东西，
所以统一显式指明到项目 `data/settings/window.ini`（INI 格式，随手能看、能删、能备份）。

一处文件、两个分组：`avatar/` 是她，`chat/` 是聊天窗（键名沿用 `window/pos`、
`panel/pos` 那些，老存档语义不变）。

测试把 `NOESIA_WINDOW_STATE` 指到临时文件，免得跑测试污染仓库里的 `data/`。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PySide6 import QtCore

from settings import ROOT, window_state_path

logger = logging.getLogger(__name__)

ENV_WINDOW_STATE = "NOESIA_WINDOW_STATE"


def _target_path() -> Path:
    """存档落哪个文件：环境变量（测试用）只认项目目录内或系统临时目录内的路径。

    别的一律忽略——一个环境变量不该把窗口档写到项目外面去（`../`、任意绝对路径都挡掉）。
    """
    env = os.environ.get(ENV_WINDOW_STATE, "").strip()
    if not env:
        return window_state_path()
    target = Path(env).expanduser().resolve()
    allowed = (ROOT.resolve(), Path(tempfile.gettempdir()).resolve())
    if any(target.is_relative_to(root) for root in allowed):
        return target
    logger.warning("忽略 %s=%s：只允许项目目录或临时目录里的路径", ENV_WINDOW_STATE, target)
    return window_state_path()


def window_settings() -> QtCore.QSettings:
    """窗口存档：显式 INI 路径，绝不落到注册表；目录写不进去也不许把程序带崩。"""
    target = _target_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 装在受保护目录（Program Files / 只读盘）时这里会失败：退回系统临时目录，
        # 位置存不下来总比启动就崩强。
        logger.warning("窗口存档目录建不出来（%s），改落临时目录", target.parent, exc_info=True)
        target = Path(tempfile.gettempdir()) / "noesia-window.ini"
    return QtCore.QSettings(str(target), QtCore.QSettings.IniFormat)
