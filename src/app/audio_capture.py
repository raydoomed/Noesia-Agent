"""录音：WASAPI 共享模式直采。

只用 `ctypes` 调系统 DLL，不引入 PortAudio / sounddevice / Qt Multimedia。
格式由系统重采样器（`AUTOCONVERTPCM`）负责，所以固定要 16k 单声道即可。

**绝不给音频流设通信类用途**（`AudioCategory_Communications` / `Speech`）：实测设上去
之后，同一只蓝牙耳机的 A2DP 放音端点会立刻变成 `UNPLUGGED`，而且不会自己恢复，
用户得断开重连才能听回声音。放音被掐的代价远大于回声那点收益。

**也不做 MME（waveIn）兜底、不做蓝牙链路预唤醒**：实测这两种做法都会把蓝牙耳机从
A2DP 拽进 HFP（SCO），一开麦电脑里别的声音就全没了。直接开 WASAPI 共享流能正常
收到麦克风数据，又不碰放音那条链路——网页里的在线麦克风测试就是这么做的。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_char, c_int, c_ulong, c_ushort, c_void_p

from app.audio_devices import Endpoint, _call, _enumerator, _guid, _release

logger = logging.getLogger(__name__)

# ---- WASAPI 常量
IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

# 缓冲区 200ms：够顶住界面 16ms 的取数节奏，又不至于让「按下到有数据」等太久
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
    """按端点 GUID 直接采。"""

    backend = "wasapi"

    def __init__(self, endpoint: Endpoint, rate: int = 16000, channels: int = 1) -> None:
        self.endpoint = endpoint
        self.rate = rate
        self.channels = channels
        self._device = None
        self._client = None
        self._capture = None
        self._format = _pcm_format(rate, channels)
        self._opened = False

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
        hr = _call(client, 3, ctypes.c_long, c_int, c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                   POINTER(WAVEFORMATEX), c_void_p)(
            client, AUDCLNT_SHAREMODE_SHARED, flags,
            ctypes.c_longlong(_BUFFER_HNS), ctypes.c_longlong(0), byref(self._format), None,
        )
        if hr != 0:
            self._release_all()
            raise CaptureError(f"「{self.endpoint.name}」不接受 {self.rate}Hz/单声道（hr=0x{hr & 0xFFFFFFFF:08X}）")

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
            if hr != 0 or not frames.value:
                break
            size = frames.value * self._format.nBlockAlign
            if data:
                chunks.append(ctypes.string_at(data, size))
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
        # 顺序固定：采集 → 客户端 → 设备。反了会留下半开着的 endpoint
        for attr in ("_capture", "_client", "_device"):
            handle = getattr(self, attr, None)
            if handle:
                _release(handle)
                setattr(self, attr, None)


def _as_bytes(guid) -> ctypes.Array:
    return (ctypes.c_byte * ctypes.sizeof(guid)).from_buffer_copy(guid)


# ================================================================ 门面

class Capture:
    """对上层只暴露 open / read / close。只有 WASAPI 一条路。"""

    def __init__(self, endpoint: Endpoint, rate: int = 16000, channels: int = 1) -> None:
        self.endpoint = endpoint
        self.rate = rate
        self.channels = channels
        self.backend = ""
        self._impl: WasapiCapture | None = None
        self._opened = False

    def open(self) -> None:
        wasapi = WasapiCapture(self.endpoint, self.rate, self.channels)
        wasapi.open()
        self._impl = wasapi
        self.backend = "wasapi"
        self._opened = True
        logger.info("[录音] %s 走 WASAPI", self.endpoint.name)

    def read(self) -> bytes:
        return self._impl.read() if self._impl else b""

    def close(self) -> None:
        if self._impl:
            self._impl.close()
        self._impl = None
        self._opened = False

    @property
    def opened(self) -> bool:
        return self._opened
