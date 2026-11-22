"""Win32 window enumeration behind ``window.read``.

The engine asks which OS window sits directly underneath the Noesia window; the
answer comes from walking the Z-order from our own HWND downwards. Metadata only,
never pixels.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger(__name__)

GW_HWNDNEXT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_WALK = 200


def _user32():
    return ctypes.windll.user32


def _kernel32():
    return ctypes.windll.kernel32


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _exe_of(pid: int) -> str:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _title_of(hwnd: int) -> str:
    user32 = _user32()
    length = int(user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _bounds_of(hwnd: int) -> dict:
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return {}
    return {"x": int(rect.left), "y": int(rect.top),
            "width": int(rect.right - rect.left), "height": int(rect.bottom - rect.top)}


def _skip(hwnd: int, own_pid: int, shell: int) -> bool:
    user32 = _user32()
    if hwnd == shell:
        return True
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return True
    if int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & WS_EX_TOOLWINDOW:
        return True
    if _pid_of(hwnd) == own_pid:
        return True
    return not _title_of(hwnd)


def read_window_below(hwnd: int) -> dict:
    try:
        user32 = _user32()
        if not hwnd:
            return {"error": "Noesia has no window handle to start from.", "platform": "win32"}
        own_pid = _pid_of(hwnd)
        shell = int(user32.GetShellWindow() or 0)
        current = int(user32.GetWindow(hwnd, GW_HWNDNEXT) or 0)
        walked = 0
        while current and walked < _MAX_WALK:
            walked += 1
            if not _skip(current, own_pid, shell):
                pid = _pid_of(current)
                exe = _exe_of(pid)
                return {
                    "window": {
                        "app": os.path.basename(exe) if exe else "",
                        "title": _title_of(current),
                        "bounds": _bounds_of(current),
                        "id": current,
                    },
                    "frontmost": int(user32.GetForegroundWindow() or 0) == current,
                    "platform": "win32",
                }
            current = int(user32.GetWindow(current, GW_HWNDNEXT) or 0)
        return {"error": "No other application window sits below Noesia.", "platform": "win32"}
    except Exception as exc:
        logger.warning("window enumeration failed: %s", exc)
        return {"error": f"Window enumeration is unavailable: {exc}", "platform": "win32"}
