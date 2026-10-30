"""Windows 进程操作，全程 ctypes —— 一个子进程都不起。

为什么不调 `tasklist` / `taskkill` / `powershell`：桌面端是从资源管理器/快捷方式起的 GUI 程序，
它没有控制台；这些命令行工具被它拉起时会各自**闪一个黑窗口**（控制台子系统程序总要个窗口）。
用户看到的就是「点一下冒出好几个 cmd」，所以判活、认人、结束进程、按 Ctrl+C 全部走 Win32 API。
顺带还避开了另一个坑：`taskkill` 的提示是本地化的，中文系统上按文字判断「进程不存在」必然失手。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TH32CS_SNAPPROCESS = 0x00000002
CREATE_NO_WINDOW = 0x08000000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
KEY_EVENT = 0x0001
LEFT_CTRL_PRESSED = 0x0008
VK_C = 0x43
CTRL_C_SCANCODE = 0x2E


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def run_hidden(args, timeout: float = 10.0):
    """跑一个命令行工具，但**不弹窗口**（桌面端是 GUI 进程，默认会闪黑窗）。

    只给「必须问系统」的场合用（比如要命令行才知道哪个进程是 TUI 会话）；
    能用 Win32 API 拿到的东西一律不用子进程。
    """
    import subprocess

    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    return subprocess.run(
        args, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        startupinfo=startup if sys.platform == "win32" else None,
    )


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _KeyEvent(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.c_int),
        ("wRepeatCount", ctypes.c_ushort),
        ("wVirtualKeyCode", ctypes.c_ushort),
        ("wVirtualScanCode", ctypes.c_ushort),
        ("uChar", ctypes.c_wchar),
        ("dwControlKeyState", ctypes.c_ulong),
    ]


class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEvent)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _Event)]


def process_alive(pid: int) -> bool:
    """这个 pid 还活着吗？（OpenProcess 拿不句柄 = 已退出或没权限）"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def image_path(pid: int) -> str:
    """可执行文件全路径；拿不到返回空串。"""
    if sys.platform != "win32" or pid <= 0:
        return ""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def parent_map() -> dict[int, int]:
    """pid → 父 pid（一次 toolhelp 快照，比逐进程查 WMI 快得多，也不起子进程）。"""
    if sys.platform != "win32":
        return {}
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, -1):
        return {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    parents: dict[int, int] = {}
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def ancestors(pid: int, depth: int = 4) -> list[int]:
    """pid 的父、祖父……（最多 depth 个，遇到断链就停）。"""
    parents = parent_map()
    chain: list[int] = []
    current = parents.get(pid, 0)
    while current and len(chain) < depth:
        chain.append(current)
        current = parents.get(current, 0)
    return chain


def descendants(pid: int) -> list[int]:
    """pid 的所有后代（venv 的 python 垫片会把真正的解释器起成子进程，只杀垫片会留个活的）。"""
    kids: dict[int, list[int]] = {}
    for child, parent in parent_map().items():
        kids.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = list(kids.get(pid, []))
    while queue and len(found) < 64:
        current = queue.pop()
        if current in found or current == os.getpid():
            continue
        found.append(current)
        queue.extend(kids.get(current, []))
    return found


def terminate(pid: int, wait: float = 5.0) -> bool:
    """硬结束一个进程并等它真的走掉（不要 `os.kill`，它在 Windows 上就是 TerminateProcess，还没有等待）。"""
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform != "win32":
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return not process_alive(pid)
        return True
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return not process_alive(pid)  # 已经没了也算成功
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)


def press_ctrl_c(pid: int, presses: int = 2, gap: float = 0.4) -> bool:
    """替用户在 *pid* 所在的那个控制台里按 Ctrl+C（像人手按一样，走它的按键处理）。

    真正的按键（写进控制台输入缓冲）而不是控制台事件：TUI 用 prompt_toolkit 把终端设成原始模式，
    Ctrl+C 在它那里是**一个按键**（先中断当前这一轮，两秒内再按一次才退出）。发 `CTRL_C_EVENT`
    信号会绕过它自己的按键逻辑，可能在写到一半时把界面掀掉。

    前提：本进程没有控制台（桌面端就是 GUI 进程），否则 `AttachConsole` 会失败——那时返回 False，
    由调用方退回「直接结束进程」。
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    kernel32 = _kernel32()
    if kernel32.GetConsoleWindow():
        logger.debug("本进程自己有控制台，不接管别人的按键输入")
        return False
    if not kernel32.AttachConsole(pid):
        logger.debug("AttachConsole(%s) 失败：%s", pid, ctypes.get_last_error())
        return False
    try:
        records = (_InputRecord * 2)()
        records[0].EventType = KEY_EVENT
        records[0].Event.KeyEvent.bKeyDown = 1
        records[0].Event.KeyEvent.wRepeatCount = 1
        records[0].Event.KeyEvent.wVirtualKeyCode = VK_C
        records[0].Event.KeyEvent.wVirtualScanCode = CTRL_C_SCANCODE
        records[0].Event.KeyEvent.uChar = "\x03"
        records[0].Event.KeyEvent.dwControlKeyState = LEFT_CTRL_PRESSED
        records[1].EventType = KEY_EVENT
        records[1].Event.KeyEvent.bKeyDown = 0
        records[1].Event.KeyEvent.wRepeatCount = 1
        records[1].Event.KeyEvent.wVirtualKeyCode = VK_C
        records[1].Event.KeyEvent.wVirtualScanCode = CTRL_C_SCANCODE
        records[1].Event.KeyEvent.uChar = "\x03"
        records[1].Event.KeyEvent.dwControlKeyState = LEFT_CTRL_PRESSED

        sent = 0
        for _ in range(max(1, presses)):
            written = ctypes.c_ulong(0)
            if kernel32.WriteConsoleInputW(kernel32.GetStdHandle(-10), records, 2, ctypes.byref(written)):
                sent += 1
            else:
                logger.debug("WriteConsoleInputW 失败：%s", ctypes.get_last_error())
                break
            time.sleep(gap)
        return sent > 0
    finally:
        kernel32.FreeConsole()


def console_sessions(project_root) -> list[int]:
    """命令行里正开着的 noesia 会话：只看**可执行文件路径**，不问命令行。

    认法：映像路径正好是项目虚拟环境里的 `noesia.exe`。这一条足够准——pytest、脚本、引擎
    跑的都是 `python.exe`；而且只看路径就不用 `powershell` 去取命令行（那条路在真机上会卡）。
    自己和自己往上那一串（GUI 本身可能也是用 `noesia.exe` 起的）一律排除。
    """
    import os
    from pathlib import Path

    target = str(Path(project_root) / ".venv" / "Scripts" / "noesia.exe").lower()
    skip = set(ancestors(os.getpid())) | {os.getpid()}
    found: list[int] = []
    for pid in parent_map():
        if pid in skip:
            continue
        if image_path(pid).lower() == target:
            found.append(pid)
    return found


def _press_within(pid: int, budget: float) -> bool:
    """在预算内尝试按 Ctrl+C；按键这条路是「能按就按」，绝不允许它把流程卡住。

    为什么要单开线程：真实控制台上 `AttachConsole` + `WriteConsoleInputW` 偶尔不回来
    （2026-10-25 日志里就是这样：接管开着头一行，然后一句「已结束」都没有，界面干等）。
    用户要的是「先 Ctrl+C，再杀掉进程」，所以按键有预算，超了就往下走，杀进程那步才是保底。
    """
    import threading

    outcome: list[bool] = []

    def work() -> None:
        try:
            outcome.append(press_ctrl_c(pid))
        except Exception:  # noqa: BLE001 - 按键失败不该影响后面的杀进程
            logger.exception("按 Ctrl+C 时出错（pid %s）", pid)

    worker = threading.Thread(target=work, name="ctrl-c", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        logger.warning("按 Ctrl+C 超过 %.1f 秒没回来（pid %s），不等它了", budget, pid)
        return False
    return bool(outcome and outcome[0])


def quiet_exit(pid: int, timeout: float = 2.5, press_budget: float = 1.5) -> tuple[bool, str]:
    """先请它自己退（Ctrl+C），等不到再连它带它的子进程一起硬结束。

    返回 (现在是不是没了, 给人看的一句话)。按键有预算、结束有轮询，整条路不会卡死。
    """
    if not process_alive(pid):
        return True, f"进程 {pid} 已经不在了"
    logger.info("请 pid %s 退出：先按 Ctrl+C（最多 %.1f 秒）", pid, press_budget)
    if _press_within(pid, press_budget):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not process_alive(pid):
                return True, f"已让 pid {pid} 按 Ctrl+C 退出"
            time.sleep(0.1)
    family = descendants(pid) + [pid]
    survivors = [item for item in family if process_alive(item)]
    logger.info("硬结束：pid %s（连同 %s 个子进程）", pid, len(survivors) - 1)
    for item in survivors:
        terminate(item)
    left = [item for item in survivors if process_alive(item)]
    if not left:
        return True, f"已结束 pid {pid}"
    return False, f"结束 pid {pid} 失败（还剩 {'、'.join(map(str, left))}）"


def project_ancestor(pid: int, project_root: "Path | str", depth: int = 4) -> int | None:
    """从 *pid* 往上找第一个跑在这个项目目录里的祖先进程（= 那条命令行会话）。

    之所以要找祖先：`gateway.lock` 里只有后台进程自己，没有把它拉起来的那个 TUI；
    但「谁拉起来的」就记在进程树的父链上（实测：gateway ← CLI 客户端 ← noesia.exe）。
    """
    root = os.path.normcase(str(Path(project_root).resolve()))
    for ancestor in ancestors(pid, depth=depth):
        image = image_path(ancestor)
        if not image:
            continue
        if os.path.normcase(os.path.abspath(image)).startswith(root) and ancestor != os.getpid():
            return ancestor
    return None
