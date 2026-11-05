"""引擎被占着时的接管顺序：先让命令行会话 Ctrl+C 退出 → 再杀掉残留进程 → 然后启动。

为什么需要这一步：命令行里聊过天的 `noesia`，Ctrl+C 只停前台，它拉起的 detached
`gateway run --quiet --idle-exit` 还攥着 `data/NoesiaHome`，界面拉起的引擎于是
`refused: … is owned by a running gateway` 直接 exit 1。放着不管，就得等那条 gateway
空闲超时（默认 600 秒）才能开窗口。

顺序为什么是这个：命令行那台会话只要还活着，它下一句话就会再拉一条 gateway
出来——所以先请它退出（替它按 Ctrl+C，让它自己收尾），再杀掉还残留的进程，最后启动。
``taskkill``/``powershell``/``tasklist`` 一个都不用：桌面端是 GUI 进程，拉它们会各闪一个黑窗口
（`app/winproc.py` 里说明了为什么全部走 Win32 API）。

安全边界：只动**确认属于本项目**的进程（`engine_wire.console_clients()` /
`blocking_owner()` 都验路径与命令行），分不清是什么就一个都不碰；而且先问一句，
因为关窗口会打断命令行里正在进行的对话。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def looks_like_occupied(message: str) -> bool:
    """这条启动失败是不是「数据目录被别的后台进程占着」。

    只看引擎自己吐的那句话（`describe_engine_exit()` 已经把它翻成中文），不去摸进程表——
    弹卡片那一刻不许有任何可能拖住或抛错的东西（2026-10-25 就是这里先摸了进程表，
    卡片干脆没弹出来，用户看到的就是「改废了」）。
    """
    text = message or ""
    return "被另一个 Noesia 后台进程占着" in text or "is owned by a running gateway" in text


def occupied_pid(message: str) -> int | None:
    """引擎报错那句话里写着的那个 pid（`describe_engine_exit()` 是从 `gateway.lock` 读出来的）。

    用它就行，不必再去扫进程表：真机上从 GUI 扫进程表（powershell / 进程快照）会卡住或者抛错，
    卡片点完却什么都没发生——那条路不再走。
    """
    match = re.search(r"pid\s+(\d+)", message or "")
    return int(match.group(1)) if match else None


def release(project_root, pid: int) -> tuple[bool, str]:
    """把挡路的东西请走：先让命令行会话 Ctrl+C 退出，再收起那个后台 gateway。

    阻塞（最多几秒），只在后台线程里调。`project_root` 给 None 就只处理 pid，
    ——认不出项目根时宁可少动，也不去扫全局进程表乱杀。
    """
    from app.engine_wire import close_console_sessions, quit_process  # 模块函数，不是 EngineWire 的方法

    notes: list[str] = []
    if project_root is not None:
        notes.extend(close_console_sessions(project_root))
    ok, note = quit_process(pid)
    notes.append(note)
    logger.info("接管：%s", "；".join(notes))
    return ok, "；".join(text for text in notes if text)
