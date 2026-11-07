"""Windows 音频端点：枚举、属性、热插拔通知。

这里只做「有哪几只设备、叫什么、还连着没有、什么时候变的」，不碰音频流本身
（录音在 `audio_capture.py`）。

为什么不用 Qt 的 `QMediaDevices`：它的 `id()` 虽然也是端点 GUID，但 PySide6 没有
导出 `IAudioClient` 那一层，拿不到「这个端点是蓝牙还是内置」这种设备属性，也没法
在打开失败时区分「设备没了」和「格式不支持」。整条链路只用一套 API，排查时才对得上。

为什么不用 PortAudio / sounddevice：设备编号会随蓝牙连断整表重排，存下来的编号
下一次开机就指到别的设备上；而且它拿不到端点状态，判断不了「还连着没有」。

全部走 `ctypes` 直调系统 DLL（`ole32` / `mmdevapi` / `propsys`），不引入第三方包。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import POINTER, byref, c_int, c_short, c_ulong, c_ushort, c_void_p, c_wchar_p
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_ole32 = ctypes.oledll.ole32

_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = 0x80010106
_coinit_ex = ctypes.windll.ole32.CoInitializeEx
_coinit_ex.restype = ctypes.c_long
_coinit_ex.argtypes = [ctypes.c_void_p, ctypes.c_ulong]


CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IMMNotificationClient = "{7991EEC9-7E89-4D85-8390-6C703CEC60C0}"
FMTID_DEVICE = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
PKEY_Device_FriendlyName = 14
PKEY_Device_EnumeratorName = 24

DEVICE_STATE_ACTIVE = 0x1

E_RENDER = 0
E_CAPTURE = 1

CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0

VT_LPWSTR = 31
STGM_READ = 0

_BLUETOOTH_ENUMERATORS = {"BTHENUM", "BTHLE", "BTHHFENUM"}



class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", ctypes.c_byte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong), ("_pad", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    """只够读字符串用；其余类型不解释（`vt` 不匹配就当没有）。"""
    _fields_ = [
        ("vt", c_short), ("r1", c_short), ("r2", c_short), ("r3", c_short),
        ("p", c_void_p), ("tail", ctypes.c_ulonglong),
    ]


def _guid(text: str) -> GUID:
    value = GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(value))
    return value


def _call(interface, slot: int, restype, *argtypes):
    """按 vtable 槽位取方法（0=QueryInterface 1=AddRef 2=Release，业务方法从 3 起）。"""
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(vtable[slot])


def _release(interface) -> None:
    if not interface:
        return
    try:
        _call(interface, 2, c_ulong)(interface)
    except Exception:  # noqa: BLE001
        logger.debug("Release 失败", exc_info=True)



@dataclass(frozen=True)
class Endpoint:
    """一个音频端点（输入或输出）。"""

    guid: str
    name: str
    state: int           # DEVICE_STATE_*
    bluetooth: bool

    @property
    def active(self) -> bool:
        return bool(self.state & DEVICE_STATE_ACTIVE)



def _com_ready() -> None:
    """当前线程进 COM。已经按别的套间进过就不算失败。

    Qt 在 Windows 上把主线程初始化成 STA（单线程套间），这时再要 MTA 会拿到
    `RPC_E_CHANGED_MODE`——它只说明「这个线程已经是别的套间了」，COM 照样能用，
    在同一个线程里调接口也没问题。把它当错误抛出去，就会变成按语音球毫无反应。
    """
    hr = _coinit_ex(None, COINIT_MULTITHREADED) & 0xFFFFFFFF
    if hr in (_S_OK, _S_FALSE, _RPC_E_CHANGED_MODE):
        return
    raise OSError(f"COM 初始化失败：0x{hr:08X}")


def _enumerator():
    """新建一个 IMMDeviceEnumerator。调用者负责释放。"""
    _com_ready()
    enumerator = c_void_p()
    _ole32.CoCreateInstance(
        byref(_guid(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
        byref(_guid(IID_IMMDeviceEnumerator)), byref(enumerator),
    )
    return enumerator


def _read_string(store, pid: int) -> str:
    key = PROPERTYKEY()
    key.fmtid = _guid(FMTID_DEVICE)
    key.pid = pid
    value = PROPVARIANT()
    _call(store, 5, ctypes.c_long, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
        store, byref(key), byref(value)
    )
    try:
        if value.vt == VT_LPWSTR and value.p:
            return ctypes.wstring_at(value.p)
        return ""
    finally:
        _ole32.PropVariantClear(byref(value))


def _endpoints(flow: int, *, states: int = DEVICE_STATE_ACTIVE) -> list[Endpoint]:
    """列出某一方向（录制/播放）的端点。

    `states` 默认只要 ACTIVE 的——界面清单就是这个；传 `0xF` 能把「拔了但系统还记着」
    的设备也列出来，用来解释「上次选的那只为什么不在了」。
    """
    enumerator = _enumerator()
    found: list[Endpoint] = []
    try:
        collection = c_void_p()
        hr = _call(enumerator, 3, ctypes.c_long, c_int, c_ulong, POINTER(c_void_p))(
            enumerator, flow, states, byref(collection)
        )
        if hr != 0 or not collection:
            logger.warning("枚举音频端点失败 hr=0x%08X", hr & 0xFFFFFFFF)
            return []
        try:
            count = ctypes.c_uint()
            _call(collection, 3, ctypes.c_long, POINTER(ctypes.c_uint))(collection, byref(count))
            for index in range(count.value):
                device = c_void_p()
                if _call(collection, 4, ctypes.c_long, ctypes.c_uint, POINTER(c_void_p))(
                    collection, index, byref(device)
                ) != 0:
                    continue
                try:
                    found.append(_describe(device))
                finally:
                    _release(device)
        finally:
            _release(collection)
    finally:
        _release(enumerator)
    return found


def _describe(device) -> Endpoint:
    device_id = c_wchar_p()
    if _call(device, 5, ctypes.c_long, POINTER(c_wchar_p))(device, byref(device_id)) != 0:
        device_id = c_wchar_p("")
    state = ctypes.c_uint()
    _call(device, 6, ctypes.c_long, POINTER(ctypes.c_uint))(device, byref(state))

    name = ""
    enumerator = ""
    store = c_void_p()
    if _call(device, 4, ctypes.c_long, c_ulong, POINTER(c_void_p))(device, STGM_READ, byref(store)) == 0 and store:
        try:
            name = _read_string(store, PKEY_Device_FriendlyName)
            enumerator = _read_string(store, PKEY_Device_EnumeratorName)
        finally:
            _release(store)

    return Endpoint(
        guid=device_id.value or "",
        name=name or "未命名设备",
        state=state.value,
        bluetooth=enumerator.upper() in _BLUETOOTH_ENUMERATORS,
    )


def inputs(*, include_inactive: bool = False) -> list[Endpoint]:
    """能录的设备。"""
    return _endpoints(E_CAPTURE, states=0xF if include_inactive else DEVICE_STATE_ACTIVE)


def outputs(*, include_inactive: bool = False) -> list[Endpoint]:
    """能放音的设备。"""
    return _endpoints(E_RENDER, states=0xF if include_inactive else DEVICE_STATE_ACTIVE)


def find(guid: str) -> Endpoint | None:
    """按 GUID 找回端点：不管它现在什么状态，找不到返回 None。"""
    for endpoint in _endpoints(E_CAPTURE, states=0xF) + _endpoints(E_RENDER, states=0xF):
        if endpoint.guid.lower() == (guid or "").lower():
            return endpoint
    return None


def default_input() -> Endpoint | None:
    """系统默认录音设备；已经不在场的当没有。

    共享模式下 `GetDefaultAudioEndpoint` 会把状态已经变成 NOTPRESENT 的端点照样返回，
    拿它去开流必然失败，失败后的热插拔事件又被当成「设备变了」再试一次——在这里挡住。
    """
    enumerator = _enumerator()
    try:
        device = c_void_p()
        hr = _call(enumerator, 4, ctypes.c_long, c_int, c_int, POINTER(c_void_p))(
            enumerator, E_CAPTURE, 0, byref(device)
        )
        if hr != 0 or not device:
            return None
        try:
            endpoint = _describe(device)
        finally:
            _release(device)
        return endpoint if endpoint.active else None
    finally:
        _release(enumerator)



class _NotificationVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))),
        ("AddRef", ctypes.WINFUNCTYPE(c_ulong, c_void_p)),
        ("Release", ctypes.WINFUNCTYPE(c_ulong, c_void_p)),
        ("OnDeviceStateChanged", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_wchar_p, c_ulong)),
        ("OnDeviceAdded", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_wchar_p)),
        ("OnDeviceRemoved", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_wchar_p)),
        ("OnDefaultDeviceChanged", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_int, c_int, c_wchar_p)),
        ("OnPropertyValueChanged", ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_wchar_p, POINTER(PROPERTYKEY))),
    ]


class _Notification(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_NotificationVtbl))]


_CALLBACK_NAMES = (
    "_query_interface",
    "_add_ref",
    "_release",
    "_on_state",
    "_on_added",
    "_on_removed",
    "_on_default",
    "_on_property",
)


class EndpointWatcher:
    """监听设备增删、状态变化、默认设备切换。

    **回调在 Core Audio 自己的线程上跑**，绝不能在里面碰 Qt 控件。用法是给一个普通的
    `callback(reason, flow, guid)`，由上层自己决定怎么投递回主线程（GUI 那边用一个
    QTimer 从队列里取，见 `voice_input.py`）。

    回调对象必须一直活着：交给系统注册后自己也要留一份引用，否则被 GC 掉，
    系统再回调就是访问已释放内存。
    """

    def __init__(self, callback) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._refs = 0
        self._thunks = [
            field[1](getattr(self, name))
            for name, field in zip(_CALLBACK_NAMES, _NotificationVtbl._fields_)
        ]
        self._vtable = _NotificationVtbl(*self._thunks)
        self._object = _Notification(POINTER(_NotificationVtbl)(self._vtable))
        self._enumerator = None
        self._registered = False

    # ---- IUnknown
    def _query_interface(self, this, iid, out):
        if not out:
            return -2147467261  # E_POINTER
        target = _guid(IID_IMMNotificationClient)
        wanted = iid.contents if iid else None
        same = wanted is not None and all(
            getattr(wanted, f) == getattr(target, f) for f in ("Data1", "Data2", "Data3")
        )
        if not same:
            out[0] = None
            return -2147467262  # E_NOINTERFACE
        out[0] = ctypes.cast(this, c_void_p)
        self._add_ref(this)
        return 0

    def _add_ref(self, this):
        with self._lock:
            self._refs += 1
            return self._refs

    def _release(self, this):
        with self._lock:
            self._refs -= 1
            return self._refs

    def _fire(self, reason: str, flow: int | None = None, guid: str = "") -> int:
        try:
            self._callback(reason, flow, guid)
        except Exception:  # noqa: BLE001
            logger.warning("端点通知回调失败", exc_info=True)
        return 0

    def _on_state(self, this, device_id, new_state):
        return self._fire("state", None, device_id or "")

    def _on_added(self, this, device_id):
        return self._fire("added", None, device_id or "")

    def _on_removed(self, this, device_id):
        return self._fire("removed", None, device_id or "")

    def _on_default(self, this, flow, role, device_id):
        return self._fire("default", int(flow), device_id or "")

    def _on_property(self, this, device_id, key):
        return self._fire("property", None, device_id or "")

    def start(self) -> bool:
        if self._registered:
            return True
        try:
            self._enumerator = _enumerator()
            hr = _call(
                self._enumerator, 6, ctypes.c_long, POINTER(_Notification), c_void_p,
            )(self._enumerator, byref(self._object), None)
        except Exception:  # noqa: BLE001
            logger.warning("注册端点通知失败", exc_info=True)
            self._release_enumerator()
            return False
        if hr != 0:
            logger.warning("注册端点通知失败 hr=0x%08X", hr & 0xFFFFFFFF)
            self._release_enumerator()
            return False
        self._registered = True
        return True

    def stop(self) -> None:
        if self._registered and self._enumerator:
            try:
                _call(self._enumerator, 7, ctypes.c_long, POINTER(_Notification))(self._enumerator, byref(self._object))
            except Exception:  # noqa: BLE001
                logger.debug("注销端点通知失败", exc_info=True)
        self._registered = False
        self._release_enumerator()

    def _release_enumerator(self) -> None:
        if self._enumerator:
            _release(self._enumerator)
            self._enumerator = None
