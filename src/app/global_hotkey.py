"""系统级「按住说话」：低层键盘钩子（WH_KEYBOARD_LL）。

为什么不用 RegisterHotKey：它只发 WM_HOTKEY（也就是按下），没有松开这一半，
而按住说话必须知道手什么时候抬起来。低层钩子按下、松开都拿得到。

匹配上就吞掉（返回 1）：Shift+Space 在中文输入法里是全角/半角切换，不吞的话
每说一次话输入法也跟着切一次。独占是按住说话本来该有的代价。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from PySide6 import QtCore

WH_KEYBOARD_LL = 13
HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
_DOWN = (WM_KEYDOWN, WM_SYSKEYDOWN)
_UP = (WM_KEYUP, WM_SYSKEYUP)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# 修饰键的虚拟码 → 修饰位。左右分开的版本也认，否则按左 Shift 会漏
_MOD_BY_VK = {
    0x10: MOD_SHIFT, 0xA0: MOD_SHIFT, 0xA1: MOD_SHIFT,
    0x11: MOD_CONTROL, 0xA2: MOD_CONTROL, 0xA3: MOD_CONTROL,
    0x12: MOD_ALT, 0xA4: MOD_ALT, 0xA5: MOD_ALT,
    0x5B: MOD_WIN, 0x5C: MOD_WIN,
}

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_PROBE_ID = 0x4E53  # 只用来探一下能不能注册，探完立刻注销


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


# 64 位下 wt.WPARAM / wt.LPARAM 还是 32 位的 c_long，回传 lParam 时会溢出并让回调崩掉
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t

_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, _WPARAM, _LPARAM)


def _user32():
    return ctypes.windll.user32


def _kernel32():
    return ctypes.windll.kernel32


def _declare():
    """声明用到的 Win32 函数签名。

    不声明的话 ctypes 按 c_int 传参，函数指针会被截成 32 位，SetWindowsHookExW
    拿着半截地址返回 ERROR_MOD_NOT_FOUND(126)。
    """
    u = _user32()
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wt.HMODULE, wt.DWORD]
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.UnhookWindowsHookEx.restype = wt.BOOL
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, _WPARAM, _LPARAM]
    u.CallNextHookEx.restype = ctypes.c_ssize_t
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    # 句柄一定按 64 位取，默认的 c_int 会把它截断，拿去做模块句柄就是 ERROR_MOD_NOT_FOUND
    _kernel32().GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _kernel32().GetModuleHandleW.restype = wt.HMODULE
    u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    u.TranslateMessage.restype = wt.BOOL
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    u.DispatchMessageW.restype = ctypes.c_ssize_t
    u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.UINT, wt.UINT]
    u.RegisterHotKey.restype = wt.BOOL
    u.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.UnregisterHotKey.restype = wt.BOOL
    u.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, _WPARAM, _LPARAM]
    u.PostThreadMessageW.restype = wt.BOOL


_declare()


def current_modifiers() -> int:
    """此刻按住的是哪些修饰键（钩子里光看 vkCode 不够，修饰键状态要另外问系统）。"""
    u = _user32()
    mods = 0
    if u.GetAsyncKeyState(VK_MENU) & 0x8000:
        mods |= MOD_ALT
    if u.GetAsyncKeyState(VK_CONTROL) & 0x8000:
        mods |= MOD_CONTROL
    if u.GetAsyncKeyState(VK_SHIFT) & 0x8000:
        mods |= MOD_SHIFT
    if (u.GetAsyncKeyState(VK_LWIN) & 0x8000) or (u.GetAsyncKeyState(VK_RWIN) & 0x8000):
        mods |= MOD_WIN
    return mods


def probe(mods: int, vk: int) -> tuple[bool, str]:
    """这个组合能不能拿来用：试着注册一下，能注册说明没被别的程序占着。

    只能查「注册型热键」的占用。输入法那种不是注册来的（比如 Shift+Space 切全角），
    查不出来——那种靠钩子吞键解决，见模块开头的说明。
    """
    if mods == 0:
        return False, "系统级的键至少要带一个修饰键（Ctrl / Alt / Shift / Win）"
    u = _user32()
    if u.RegisterHotKey(None, _PROBE_ID, mods, vk):
        u.UnregisterHotKey(None, _PROBE_ID)
        return True, ""
    err = _kernel32().GetLastError()
    if err == ERROR_HOTKEY_ALREADY_REGISTERED:
        # 1409 是 Windows 自己的说法：HOTKEY_ALREADY_REGISTERED
        return False, "这个组合已经被其他程序占用了"
    return False, f"注册失败（错误码 {err}）"


class GlobalHotkey(QtCore.QObject):
    """全局按住说话。pressed / released 会从钩子线程发出来，Qt 自己排队送到主线程。"""

    pressed = QtCore.Signal()
    released = QtCore.Signal()
    # 捕获模式专用：改键时任何按键都报上来（修饰位, 虚拟键码）
    captured = QtCore.Signal(int, int)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._mods = 0
        self._vk = 0
        self._held_mods = 0  # 此刻按住的修饰键，钩子自己记账
        self._capture = False  # 改键用：什么都报上去，一个都不吞
        self._capture_last = (0, 0, 0.0)  # 上一次报的 (修饰位, 键, 时刻)，用来压住长按自动重复
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._hook = None
        self._proc = None
        self._last_error = 0

    # ---- 配置 ----

    def start_capture(self) -> None:
        """改键用：任何按键都报上来、一个都不吞。

        Win+空格 这种系统保留组合会被系统直接接走，Qt 的 keyPressEvent 根本不来，
        只有低层钩子这一层看得见。所以改键必须靠它，不能只靠窗口事件。
        """
        self._capture = True
        self._capture_last = (0, 0, 0.0)
        self.start()

    def stop_capture(self) -> None:
        self._capture = False
        self.stop()

    def set_sequence(self, mods: int, vk: int) -> None:
        self._mods = mods & (MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_WIN)
        self._vk = vk

    @property
    def sequence(self) -> tuple[int, int]:
        return self._mods, self._vk

    # ---- 启停 ----

    def start(self) -> None:
        """装钩子。

        必须自己起一个线程跑阻塞式 GetMessage：低级钩子的回调靠安装线程的消息循环
        被系统调起来，而 Qt 的 processEvents 泵不到这类消息。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._held_mods = current_modifiers()  # 启动时手上可能已经按着修饰键
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="noesia-global-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._stop.set()
            ident = self._thread.ident
            if ident:
                # 叫醒阻塞在 GetMessage 的线程；ident 为空时传 0 会发给所有线程，不能发
                _user32().PostThreadMessageW(ident, 0, 0, 0)
            self._thread.join(timeout=1.5)
        self._thread = None
        self._hook = None
        self._held_mods = 0

    @property
    def running(self) -> bool:
        return self._hook is not None

    # ---- 钩子线程 ----

    def _run(self) -> None:
        u = _user32()
        self._proc = _HOOKPROC(self._callback)  # 回调要给钩子留引用，否则会被回收
        # 低级钩子的钩子过程在本线程里（ctypes 回调），hMod 必须是 NULL
        self._hook = u.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0) or None
        self._last_error = 0 if self._hook else _kernel32().GetLastError()
        self._ready.set()
        if not self._hook:
            return
        hook = self._hook
        try:
            msg = wt.MSG()
            # 阻塞取消息：Peek + sleep 顶不住，钩子回调要的是这个真循环
            while not self._stop.is_set() and u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        finally:
            # 用自己的局部引用：stop() 可能已经把 self._hook 置空，用 None 卸不掉就是泄漏
            u.UnhookWindowsHookEx(hook)
            self._hook = None

    # ---- 钩子回调 ----
    #
    # 放行一律 CallNextHookEx(None, ...)：第一个参数传自己的钩子句柄表面上也能过，
    # 实测键就丢了（窗口里打不出空格），传 None 才真的往下走。

    def _callback(self, n_code, w_param, l_param):
        if n_code != HC_ACTION:
            return _user32().CallNextHookEx(None, n_code, w_param, l_param)
        info = ctypes.cast(ctypes.c_void_p(l_param & 0xFFFFFFFFFFFFFFFF),
                           ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        vk = info.vkCode

        # 修饰键状态自己记：回调里 GetAsyncKeyState 还没反映这一次按键，
        # 系统要到回调返回之后才更新，问它等于问上一次
        mod_bit = _MOD_BY_VK.get(vk)
        if mod_bit is not None:
            if w_param in _DOWN:
                self._held_mods |= mod_bit
            elif w_param in _UP:
                self._held_mods &= ~mod_bit

        if self._capture:
            # 改键期间：凡是主键都报上去，一个都不吞。
            # 这里不能只认 self._vk——捕获时压根还没有目标键。
            if w_param in _DOWN and vk and mod_bit is None:
                now = time.monotonic()
                last_mods, last_vk, last_at = self._capture_last
                # 按住不放系统会连着发按下，同一个键 0.3 秒内只报一次，
                # 不然下面每报一次都要向系统注册试探一回
                if (self._held_mods, vk) != (last_mods, last_vk) or now - last_at > 0.3:
                    self._capture_last = (self._held_mods, vk, now)
                    self.captured.emit(self._held_mods, vk)
            return _user32().CallNextHookEx(None, n_code, w_param, l_param)

        if vk and vk == self._vk:
            return self._on_key(w_param, l_param)
        return _user32().CallNextHookEx(None, n_code, w_param, l_param)

    def _on_key(self, w_param, l_param) -> int:
        """主键按下/抬起：只负责「提醒 + 必要时吞掉」。

        这里不维护「正在按住」这种状态。它一旦丢了松开事件就会永久卡住、
        之后这个键全被吞掉——这才是之前按住说话时好时坏的根因。
        该不该录由上层读真实键态判断，钩子只做它能确定的事。
        """
        u = _user32()
        if w_param in _DOWN and self._held_mods == self._mods:
            self.pressed.emit()
            return 1  # 命中了才吞：Shift+Space 不给输入法，免得顺手切全角
        if w_param in _UP:
            self.released.emit()
        return u.CallNextHookEx(None, HC_ACTION, w_param, l_param)
        return u.CallNextHookEx(None, HC_ACTION, w_param, l_param)
