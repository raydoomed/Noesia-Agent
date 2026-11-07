"""录音：WASAPI 共享模式直采。

只用 `ctypes` 调系统 DLL，不引入 PortAudio / sounddevice / Qt Multimedia。

**开流不写死采样率**：先按调用方给的格式请求，系统能做转换（`AUTOCONVERTPCM`）就直接用；
系统不肯转，就退到**设备自己的采样率和声道数**（`GetMixFormat`）——不同设备原生是 16k
还是 48k 由它自己说了算。实际拿到多少从 `Capture.rate / Capture.channels` 读，调用方据此去重采样。

**位深永远是 16bit**：混音格式普遍是 32bit float，照抄下来上层按 int16 解析就是一串垃圾。
所以降级时只取设备的采样率和声道数，格式仍按 16bit PCM 要，让系统转位深——转不了就报错，
绝不把不是 int16 的数据交给上层。

**不给音频流设通信类用途**（`AudioCategory_Communications` / `Speech`）：通信类会进入
系统的「通信活动」策略，其他声音可能被压低或被静音，而采集本身并不需要这个身份。

**只留 WASAPI 一条路**：不做 MME（waveIn）兜底、不做蓝牙链路预唤醒。多一条后端就是
多一条能挂住设备句柄的路径，也会让「这次到底走的哪条」变得不可知。共享模式下
WASAPI 自己就能从蓝牙麦克风拿到数据。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_char, c_int, c_ulong, c_ushort, c_void_p

from app.audio_devices import Endpoint, _call, _enumerator, _guid, _release

logger = logging.getLogger(__name__)

IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

_BUFFER_HNS = 2_000_000

WAVE_FORMAT_PCM = 1


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", c_ushort), ("nChannels", c_ushort), ("nSamplesPerSec", c_ulong),
        ("nAvgBytesPerSec", c_ulong), ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class CaptureError(RuntimeError):
    """打不开麦克风。消息可以直接给用户看。"""


def _pcm_format(rate: int, channels: int) -> WAVEFORMATEX:
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_PCM
    fmt.nChannels = channels
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = 16
    fmt.nBlockAlign = channels * 2
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


def _free_mix_format(ptr) -> None:
    """`GetMixFormat` 的返回值是 `CoTaskMemAlloc` 分配的，按文档要还回去。

    `ctypes.windll` 默认把参数当 `c_int`，64 位上会把指针的高 32 位截掉——
    必须显式声明 `c_void_p`，否则这里会踩内存。
    """
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree(ctypes.cast(ptr, c_void_p))


def _peak(pcm: bytes) -> int:
    """这段 PCM 里绝对值最大的采样（int16 域）。全零数据 = 0。"""
    if not pcm:
        return 0
    usable = len(pcm) - (len(pcm) % 2)
    if not usable:
        return 0
    values = memoryview(pcm[:usable]).cast("h")
    return max(max(values), -min(values))


# ================================================================ WASAPI

class WasapiCapture:
    """按端点 GUID 直接采。`rate` / `channels` 是请求值，开流后变成设备实际给的格式。"""

    backend = "wasapi"

    def __init__(self, endpoint: Endpoint, rate: int, channels: int = 1) -> None:
        self.endpoint = endpoint
        self.rate = rate
        self.channels = channels
        self._device = None
        self._client = None
        self._capture = None
        self._format = _pcm_format(rate, channels)
        self._opened = False

    def _initialize(self, client, fmt_ptr, flags: int) -> int:
        """`IAudioClient::Initialize`（共享模式）。`fmt_ptr` 是 WAVEFORMATEX 指针。

        用 `c_void_p` 而不是 `POINTER(WAVEFORMATEX)`：设备报的格式可能是
        WAVEFORMATEXTENSIBLE，比 WAVEFORMATEX 多出扩展部分，按窄类型传会丢掉。
        """
        hr = _call(
            client, 3, ctypes.c_long,
            c_int, c_ulong, ctypes.c_longlong, ctypes.c_longlong, c_void_p, c_void_p,
        )(
            client, AUDCLNT_SHAREMODE_SHARED, flags,
            ctypes.c_longlong(_BUFFER_HNS), ctypes.c_longlong(0), fmt_ptr, None,
        )
        return hr & 0xFFFFFFFF

    def _mix_format(self, client):
        """`IAudioClient::GetMixFormat`：设备自己的混音格式，共享模式下一定接受。"""
        ptr = POINTER(WAVEFORMATEX)()
        hr = _call(client, 8, ctypes.c_long, POINTER(POINTER(WAVEFORMATEX)))(client, byref(ptr))
        if hr != 0 or not ptr:
            return None
        return ptr

    def open(self) -> None:
        enumerator = _enumerator()
        try:
            device = c_void_p()
            hr = _call(enumerator, 5, ctypes.c_long, ctypes.c_wchar_p, POINTER(c_void_p))(
                enumerator, self.endpoint.guid, byref(device)
            )
            if hr != 0 or not device:
                raise CaptureError(f"找不到「{self.endpoint.name}」（hr=0x{hr & 0xFFFFFFFF:08X}）")
        finally:
            _release(enumerator)
        self._device = device

        client = c_void_p()
        hr = _call(device, 3, ctypes.c_long, POINTER(ctypes.c_byte * 16), c_ulong, c_void_p, POINTER(c_void_p))(
            device, ctypes.pointer(_as_bytes(_guid(IID_IAudioClient))), 0x17, None, byref(client)
        )
        if hr != 0 or not client:
            self._release_all()
            raise CaptureError(f"「{self.endpoint.name}」打不开（Activate hr=0x{hr & 0xFFFFFFFF:08X}）")
        self._client = client

        flags = AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        hr = self._initialize(client, ctypes.cast(byref(self._format), c_void_p), flags)
        if hr != 0:
            native = self._mix_format(client)
            if native is None:
                self._release_all()
                raise CaptureError(
                    f"「{self.endpoint.name}」不接受 {self.rate}Hz/{self.channels} 声道，"
                    f"也读不到它的混音格式（hr=0x{hr & 0xFFFFFFFF:08X}）"
                )
            native_rate = native.contents.nSamplesPerSec
            native_channels = native.contents.nChannels
            _free_mix_format(native)

            fmt16 = _pcm_format(native_rate, native_channels)
            hr = self._initialize(client, ctypes.cast(byref(fmt16), c_void_p), flags)
            if hr != 0:
                self._release_all()
                raise CaptureError(
                    f"「{self.endpoint.name}」只接受 {native_rate}Hz/{native_channels} 声道，"
                    f"但转不成 16bit 采样（hr=0x{hr & 0xFFFFFFFF:08X}）"
                )
            logger.info(
                "[录音] %s 不接受 %d Hz / %d 声道，改用设备原生的 %d Hz / %d 声道（16bit）",
                self.endpoint.name, self.rate, self.channels, native_rate, native_channels,
            )
            self._format = fmt16

        self.rate = self._format.nSamplesPerSec
        self.channels = self._format.nChannels

        capture = c_void_p()
        hr = _call(client, 14, ctypes.c_long, POINTER(ctypes.c_byte * 16), POINTER(c_void_p))(
            client, ctypes.pointer(_as_bytes(_guid(IID_IAudioCaptureClient))), byref(capture)
        )
        if hr != 0 or not capture:
            self._release_all()
            raise CaptureError(f"「{self.endpoint.name}」取不到采集接口（hr=0x{hr & 0xFFFFFFFF:08X}）")
        self._capture = capture

        hr = _call(client, 10, ctypes.c_long)(client)  # Start
        if hr != 0:
            self._release_all()
            raise CaptureError(f"「{self.endpoint.name}」启动失败（hr=0x{hr & 0xFFFFFFFF:08X}）")
        self._opened = True

    def read(self) -> bytes:
        """把当前能拿到的数据全取走；没有新数据返回空。"""
        if not self._opened or not self._capture:
            return b""
        chunks: list[bytes] = []
        data = POINTER(c_char)()
        frames = ctypes.c_uint()
        flags = c_ulong()
        position = ctypes.c_ulonglong()
        qpc = ctypes.c_ulonglong()
        while True:
            hr = _call(self._capture, 3, ctypes.c_long, POINTER(POINTER(c_char)), POINTER(ctypes.c_uint),
                       POINTER(c_ulong), POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))(
                self._capture, byref(data), byref(frames), byref(flags), byref(position), byref(qpc)
            )
            if hr < 0:
                self._opened = False
                self._release_all()
                raise CaptureError(
                    f"「{self.endpoint.name}」录音中断开了（hr=0x{hr & 0xFFFFFFFF:08X}）"
                )
            if not frames.value:
                break
            size = frames.value * self._format.nBlockAlign
            if data:
                chunks.append(ctypes.string_at(data, size))
            else:
                chunks.append(b"\x00" * size)
            _call(self._capture, 4, ctypes.c_long, ctypes.c_uint)(self._capture, frames.value)
        return b"".join(chunks)

    def close(self) -> None:
        if self._client and self._opened:
            try:
                _call(self._client, 11, ctypes.c_long)(self._client)  # Stop
            except Exception:  # noqa: BLE001
                logger.debug("停流失败", exc_info=True)
        self._opened = False
        self._release_all()

    def _release_all(self) -> None:
        for attr in ("_capture", "_client", "_device"):
            handle = getattr(self, attr, None)
            if handle:
                _release(handle)
                setattr(self, attr, None)


def _as_bytes(guid) -> ctypes.Array:
    return (ctypes.c_byte * ctypes.sizeof(guid)).from_buffer_copy(guid)
