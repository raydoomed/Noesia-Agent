"""语音输入：流管理、设备选择、电平、录音暂存。

分工：

- `audio_devices` 负责「有哪些设备、什么时候变了」；
- `audio_capture` 负责「按某只设备把 PCM 收上来」；
- 这里负责「什么时候开流、什么时候攒数据、设备没了怎么办」。

**流是常开的**：进语音模式就把流打开，一直挂着；`start()/stop()` 只是决定这中间收到
的 PCM 要不要留下。切回文字模式才真正关流。

这样做是因为反复开关蓝牙耳机的采集流代价很高（每次都要建链路，几百毫秒）。
常开之后按下就能录，没有启动延迟。

采集一律按 48k 开流：向蓝牙耳机要 16k 会被系统当成通话而掐掉放音，48k 不会。录完由
`stop()` 降到 16k，调用方拿到的永远是 16k 单声道 s16le。

录音不在主线程做打开动作：`Capture.open()` 要枚举端点、激活音频客户端，合计几十到
几百毫秒。放进工作线程，界面不会僵。
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from array import array
from pathlib import Path

from PySide6 import QtCore

from app import audio_capture, audio_devices
from app.audio_devices import Endpoint

logger = logging.getLogger(__name__)

# 攒数据的上下限
MIN_SECONDS = 0.25          # 短于这个就当没录（按下就松手）
MAX_SECONDS = 60.0          # 一直按着也不会无限涨
# 判「没人说话」的门槛：int16 域，约 -76 dBFS。数字静音才触发，
# 正常底噪够不到，低灵敏度麦也不会误伤
SILENT_PEAK = 5

STATE_IDLE = "idle"
STATE_RECORDING = "recording"


class VoiceRecorder(QtCore.QObject):
    """一只麦克风、一条常开流。"""

    level = QtCore.Signal(float)          # 0..1，给语音球画起伏
    state_changed = QtCore.Signal(str)    # idle / recording
    failed = QtCore.Signal(str)           # 人类可读的失败原因
    devices_changed = QtCore.Signal(list)  # [(guid, name, active), ...]
    stream_ready = QtCore.Signal(str)     # 流开好了，带后端名（wasapi / mme）
    device_lost = QtCore.Signal(str)      # 正在用的设备没了，参数是新设备名

    # 工作线程 → 主线程
    _opened = QtCore.Signal(object)
    _open_failed = QtCore.Signal(str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        # 采集固定 48k：向蓝牙耳机要 16k 会让系统把它切进通话模式、把放音掐掉，
        # 要 48k 则不会。录完在 stop() 里降到 16k，外面看到的仍是 16k 单声道。
        self._rate = 48000
        self._channels = 1
        self._capture: audio_capture.Capture | None = None
        self._endpoint: Endpoint | None = None
        self._chosen_guid = ""            # 用户选的；空 = 跟随系统默认
        self._pcm = bytearray()
        self._collecting = False
        self._state = STATE_IDLE
        self._opening = False
        self._lock = threading.Lock()

        # 设备变更：Core Audio 的回调在它自己的线程，只能投进队列，主线程定时取
        self._events: list[tuple[str, object, str]] = []
        self._events_lock = threading.Lock()
        self._watcher = audio_devices.EndpointWatcher(self._on_os_event)
        self._watcher.start()

        self._pump = QtCore.QTimer(self)
        self._pump.setInterval(16)        # ~60fps，跟语音球动画同步
        self._pump.timeout.connect(self._drain)

        self._events_timer = QtCore.QTimer(self)
        self._events_timer.setInterval(150)
        self._events_timer.timeout.connect(self._drain_events)
        self._events_timer.start()

        self._opened.connect(self._on_opened)
        self._open_failed.connect(self._on_open_failed)

    # ------------------------------------------------------------ 设备

    def devices(self, *, include_inactive: bool = False) -> list[Endpoint]:
        return audio_devices.inputs(include_inactive=include_inactive)

    def outputs(self) -> list[Endpoint]:
        return audio_devices.outputs()

    def current(self) -> Endpoint | None:
        """当前该用的输入设备：用户选的还在就用它，否则跟随系统默认。"""
        if self._chosen_guid:
            endpoint = audio_devices.find(self._chosen_guid)
            if endpoint and endpoint.active:
                return endpoint
        return audio_devices.default_input()

    def preload(self, guid: str) -> None:
        """记住上次选的设备，但**不**立刻开流（窗口刚建的时候别去碰音频设备）。"""
        self._chosen_guid = (guid or "").strip()

    def chosen_guid(self) -> str:
        """当前选的设备 GUID；空串 = 跟随系统默认。"""
        return self._chosen_guid

    def endpoint_name(self) -> str:
        """正在用的麦克风名字（提示语里要用到；还没开流就报正在用的那只）。"""
        if self._capture is not None:
            return self._capture.endpoint.name
        if self._endpoint is not None:
            return self._endpoint.name
        return "麦克风"

    def choose(self, guid: str) -> None:
        """切设备。传空串 = 跟随系统默认。

        只记下选择；真正的换流在 `reopen()` 里做——调用方应当在切完之后立刻收一次
        `stream_ready`/`failed`，界面据此更新。
        """
        self._chosen_guid = (guid or "").strip()
        self.reopen()

    # ------------------------------------------------------------ 流

    def open_stream(self) -> None:
        """进语音模式时调一次。已经在开/已开好就什么都不做。"""
        if self._capture is not None or self._opening:
            return
        self._spawn_open()

    def close_stream(self) -> None:
        """切回文字模式时调。关掉流、丢掉半截数据。"""
        self._collecting = False
        self._state = STATE_IDLE
        self._pump.stop()
        capture, self._capture = self._capture, None
        if capture is not None:
            threading.Thread(target=capture.close, name="noesia-mic-close", daemon=True).start()
        with self._lock:
            self._pcm.clear()

    def reopen(self) -> None:
        """换设备：先关旧的再开新的。"""
        self.close_stream()
        self._opening = False
        self.open_stream()

    def shutdown(self) -> None:
        """窗口在关：把流关掉、热插拔监听取掉。

        常开的流不主动关，进程退出前设备一直被占着——蓝牙耳机那头会一直显示
        「正在使用」，用户换个程序录音就没声。
        """
        self._pump.stop()
        self._events_timer.stop()
        self.close_stream()
        try:
            self._watcher.stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住程序退出
            logger.debug("[录音] 注销热插拔监听失败", exc_info=True)

    def _spawn_open(self) -> None:
        endpoint = self.current()
        if endpoint is None:
            self.failed.emit("没找到可用的麦克风")
            return
        self._opening = True
        self._endpoint = endpoint
        threading.Thread(target=self._open_worker, args=(endpoint,), name="noesia-mic-open", daemon=True).start()

    def _open_worker(self, endpoint: Endpoint) -> None:
        capture = audio_capture.Capture(endpoint, self._rate, self._channels)
        try:
            capture.open()
        except audio_capture.CaptureError as exc:
            self._open_failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任何意外都要变成界面上的一句话，不能静默
            logger.warning("打开麦克风时崩溃", exc_info=True)
            self._open_failed.emit(f"「{endpoint.name}」打不开：{exc}")
            return
        self._opened.emit(capture)

    @QtCore.Slot(object)
    def _on_opened(self, capture) -> None:
        self._opening = False
        self._capture = capture
        self._pump.start()
        logger.info("[录音] %s 就绪（%s）", capture.endpoint.name, capture.backend)
        self.stream_ready.emit(capture.backend)

    @QtCore.Slot(str)
    def _on_open_failed(self, message: str) -> None:
        self._opening = False
        self._capture = None
        logger.warning("[录音] %s", message)
        self.failed.emit(message)

    # ------------------------------------------------------------ 采集

    def start(self) -> bool:
        """开始攒数据。流没开好的话顺手开一次。"""
        if self._capture is None:
            self.open_stream()
            return False
        with self._lock:
            self._pcm.clear()
        self._collecting = True
        self._set_state(STATE_RECORDING)
        return True

    def stop(self) -> bytes | None:
        """停止攒数据，返回这次录到的 PCM（16k 单声道 s16le）。"""
        self._collecting = False
        self._set_state(STATE_IDLE)
        with self._lock:
            data = bytes(self._pcm)
            self._pcm.clear()
        seconds = len(data) / max(1, self._rate * self._channels * 2)
        if seconds < MIN_SECONDS:
            logger.info("[录音] 太短：%.2f 秒", seconds)
            return None
        return _to_16k(data)

    def cancel(self) -> None:
        self._collecting = False
        self._set_state(STATE_IDLE)
        with self._lock:
            self._pcm.clear()

    @QtCore.Slot()
    def _drain(self) -> None:
        capture = self._capture
        if capture is None:
            return
        try:
            chunk = capture.read()
        except Exception:  # noqa: BLE001 - 读取失败不再让它每 16ms 崩一次
            logger.warning("读麦克风失败", exc_info=True)
            self.close_stream()
            self.failed.emit("麦克风读不出来了，已断开")
            return
        if not chunk:
            return
        if self._collecting:
            with self._lock:
                self._pcm.extend(chunk)
                overflow = len(self._pcm) > int(MAX_SECONDS * self._rate * self._channels * 2)
            if overflow:
                self._collecting = False
                logger.info("[录音] 到 %.0f 秒上限，自动停", MAX_SECONDS)
        self.level.emit(_level(chunk))

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    # ------------------------------------------------------------ 设备变更

    def _on_os_event(self, reason: str, flow, guid: str) -> None:
        """**在 Core Audio 自己的线程上跑**：只入队，绝不碰 Qt 对象。"""
        with self._events_lock:
            self._events.append((reason, flow, guid))

    @QtCore.Slot()
    def _drain_events(self) -> None:
        with self._events_lock:
            events, self._events = self._events, []
        if not events:
            return

        # 只关心录制方向；flow 为 None 的是状态/增删事件，两个方向都可能
        relevant = [e for e in events if e[1] in (None, audio_devices.E_CAPTURE)]
        if not relevant:
            return

        self.devices_changed.emit([(e.guid, e.name, e.active) for e in self.devices()])

        endpoints = self.devices()
        alive = {e.guid.lower() for e in endpoints}
        if self._endpoint is not None and self._endpoint.guid.lower() not in alive:
            if self._collecting:
                self.cancel()
            logger.info("[录音] 设备没了：%s", self._endpoint.name)
            self.close_stream()
            replacement = self.current()
            self.device_lost.emit(replacement.name if replacement else "")
            self.open_stream()


def _level(pcm: bytes) -> float:
    """这段 PCM 的响度，0..1，给语音球用。

    用峰值不用 RMS：峰值跟「有没有在说话」更贴，底噪再低也不会把球撑起来。
    除以 8000 是凭经验——正常说话离麦十几厘米，峰值大致在这个量级附近。
    """
    peak = audio_capture._peak(pcm)
    return min(1.0, peak / 8000.0)


def peak_of(pcm: bytes) -> int:
    """这段 PCM 的峰值（int16 域）。判定「麦克风到底有没有收到声音」用它。"""
    return audio_capture._peak(pcm)


def _to_16k(pcm: bytes) -> bytes:
    """48k 单声道 s16le 降到 16k：三个采样取平均。

    顺手当低通用——直接丢弃后两个采样会混叠，语音识别对混叠很敏感。
    60 秒录音走这条路径约 0.3 秒，可以接受。
    """
    if not pcm:
        return b""
    usable = len(pcm) - (len(pcm) % 6)   # 三个采样一组的整倍数
    if not usable:
        return b""
    src = array("h")
    src.frombytes(pcm[:usable])
    out = array("h")
    for i in range(0, len(src), 3):
        out.append((src[i] + src[i + 1] + src[i + 2]) // 3)
    return out.tobytes()


def write_wav(pcm: bytes, path: Path, rate: int = 16000, channels: int = 1) -> Path:
    """把 PCM 包成 WAV 写到 `path`（识别要走文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


def wav_bytes(pcm: bytes, rate: int = 16000, channels: int = 1) -> bytes:
    """内存里包一个 WAV（不落盘时用，比如日志留样）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()
