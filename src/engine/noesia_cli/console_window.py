"""退出对话界面时，把「为 noesia 单开的」那个控制台窗口一起关掉。

为什么需要：在 cmd 里敲 `noesia`，Ctrl+C×2（或 `/exit`）退出后窗口还在——因为那个窗口
不是 noesia 开的，而是 cmd 开的：程序退出只是把提示符还给 cmd。用户看到的现象就是
「TUI 明明结束了，cmd 窗口还杵着」。

关得掉的前提是「这个窗口只挂着 noesia」：那种窗口（双击 exe、快捷方式、`start noesia`）
是 Windows 为这个进程开的，进程一 `FreeConsole()`，conhost 就把窗口收掉。

窗口里还挂着 cmd.exe 的情况分两种：
- 老式控制台（窗口类 `ConsoleWindowClass`）：`WM_CLOSE` 交给 conhost，窗口与里面的 cmd 一起关。
  这是 `always` 模式干的事——代价是那个 cmd 里别的活也会一起断。
- Windows Terminal 之类的伪控制台（窗口类不是 `ConsoleWindowClass`）：窗口归终端程序所有，
  `WM_CLOSE` 会把整个终端（所有标签页）关掉，所以**一律不动**，只让 noesia 退出。

模式从配置 `display.exit_closes_window` 读：`own`（默认，只关独占窗口）/ `always` / `never`。
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

WM_CLOSE = 0x0010
CONSOLE_WINDOW_CLASS = "ConsoleWindowClass"


def _load_user32():
    import ctypes

    return ctypes, ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)


def attached_pids() -> list[int]:
    """当前挂在这个控制台上的进程号（第一个通常是自己）。非 Windows 返回空。"""
    if sys.platform != "win32":
        return []
    try:
        ctypes, _user32, kernel32 = _load_user32()
        buffer = (ctypes.c_uint * 64)()
        count = kernel32.GetConsoleProcessList(buffer, len(buffer))
        return [int(buffer[i]) for i in range(max(0, int(count)))]
    except Exception:
        logger.debug("GetConsoleProcessList 失败", exc_info=True)
        return []


def window_class() -> str:
    """控制台窗口的窗口类名；没有窗口或非 Windows 返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        ctypes, user32, _kernel32 = _load_user32()
        hwnd = user32.GetConsoleWindow()
        if not hwnd:
            return ""
        name = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, name, 256):
            return name.value
        return ""
    except Exception:
        logger.debug("GetClassName 失败", exc_info=True)
        return ""


def decide(attached, mine: int, klass: str, mode: str) -> str:
    """该怎么离开这个控制台：`detach`（自己退出，窗口跟着收）/ `close`（连 cmd 一起关）/ `leave`。

    纯函数，方便钉住规则；真正动手的是 `close_console_window()`。
    """
    mode = (mode or "own").strip().lower()
    if mode == "never":
        return "leave"
    if not attached:
        return "leave"  # 没有控制台（从资源管理器/服务里起），没什么可关
    if attached == [mine]:
        return "detach"  # 这个窗口是给自己开的
    if mode == "always" and klass == CONSOLE_WINDOW_CLASS:
        return "close"  # 老式控制台：关窗口会把里面的 cmd 一起带走，用户要的就是这个
    return "leave"  # 共用窗口（尤其是 Windows Terminal 的标签页）不能替别人关


def close_console_window(mode: str = "own", mine: int | None = None) -> str:
    """按 `decide()` 的结论收掉控制台窗口，返回做了什么（`detach` / `close` / `leave`）。"""
    if sys.platform != "win32":
        return "leave"
    mine = mine if mine is not None else _my_pid()
    action = decide(attached_pids(), mine, window_class(), mode)
    if action == "leave":
        return "leave"

    try:
        ctypes, user32, kernel32 = _load_user32()
        if action == "detach":
            kernel32.FreeConsole()  # 最后一个使用者撒手，conhost 随之收掉窗口
            return "detach"
        hwnd = user32.GetConsoleWindow()
        if not hwnd:
            return "leave"
        kernel32.FreeConsole()
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return "close"
    except Exception:
        logger.debug("关闭控制台窗口失败", exc_info=True)
        return "leave"


def _my_pid() -> int:
    import os

    return os.getpid()
