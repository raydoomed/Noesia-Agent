"""语音输入：流管理、设备选择、电平、录音暂存。

分工：

- `audio_devices` 负责「有哪些设备、什么时候变了」；
- `audio_capture` 负责「按某只设备把 PCM 收上来」；
- 这里负责「什么时候开流、什么时候攒数据、设备没了怎么办」。

**流是常开的**：进语音模式就把流打开，一直挂着；`start()/stop()` 只是决定这中间收到
的 PCM 要不要留下。切回文字模式才真正关流。

这样做是因为反复开关蓝牙耳机的采集流代价很高（每次都要建链路，几百毫秒）。
常开之后按下就能录，没有启动延迟。

开流优先要 48k（向蓝牙耳机要 16k 有可能被系统当成通话需求），但**不写死**：设备不肯转
就用它自己的混音格式，实际采样率和声道数从 `Capture` 读回来。录完由 `stop()` 重采样到
16k，调用方拿到的永远是 16k 单声道 s16le。

录音不在主线程做打开动作：`Capture.open()` 要枚举端点、激活音频客户端，合计几十到
几百毫秒。放进工作线程，界面不会僵。
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np
from PySide6 import QtCore

from app import audio_capture, audio_devices
from app.audio_devices import Endpoint
from app.speech_stream import Resampler

logger = logging.getLogger(__name__)

# 攒数据的上下限
MIN_SECONDS = 0.25          # 短于这个就当没录（按下就松手）
MAX_SECONDS = 60.0          # 一直按着也不会无限涨
# 判「没人说话」的门槛：int16 域，约 -76 dBFS。数字静音才触发，
# 正常底噪够不到，低灵敏度麦也不会误伤
SILENT_PEAK = 5

# 一次设备切换（拔插、蓝牙重连、切换默认设备）会连着吐几十条事件。
# 等它们停够这么久，才当成「稳定了」统一处理一次，不逐条响应。
EVENT_QUIET_SECONDS = 0.6
# 但事件也可能一直不停（设备反复重连）：等够这么久就强制处理一次，不能干等着。
EVENT_MAX_WAIT_SECONDS = 2.0
# 自动重开的次数限制：窗口内超过上限就认输报错，不再无限重试。
REOPEN_WINDOW_SECONDS = 10.0
MAX_REOPEN_IN_WINDOW = 3
# 流开起来之后要稳住这么久，断开记录才算翻篇。
# 不能用「开成功了就清零」——蓝牙切换模式是拆了重建、每次都能开成功，
# 那样计数永远归零，上限等于没有。
OPEN_STABLE_SECONDS = 5.0


class VoiceRecorder(QtCore.QObject):
    """一只麦克风、一条常开流。"""

    level = QtCore.Signal(float)          # 0..1，给语音球画起伏
    failed = QtCore.Signal(str)           # 人类可读的失败原因
    devices_changed = QtCore.Signal(list)  # [(guid, name, active), ...]
    stream_ready = QtCore.Signal(str)     # 流开好了，带后端名（wasapi / mme）
    device_lost = QtCore.Signal(str)      # 正在用的设备没了，参数是新设备名

    # 工作线程 → 主线程
    _opened = QtCore.Signal(object, int)     # (capture, 代次)
    _open_failed = QtCore.Signal(int, str)   # (代次, 错误话)
    _reopened = QtCore.Signal()       # 换设备：旧流关干净了，该开新的了

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        # 开流优先要 48k：向蓝牙耳机要 16k 有可能让系统把它切进通话模式、把放音掐掉。
        # 但这不是硬性要求——设备不肯转就退回它自己的混音格式，实际拿到多少在
        # _on_opened 里从 Capture 读回来，重采样按那个比例算。外面看到的永远是 16k。
        self._request_rate = 48000
        self._rate = self._request_rate      # 开流后会被设备实际值覆盖
        self._channels = 1
        self._capture: audio_capture.WasapiCapture | None = None
        self._endpoint: Endpoint | None = None
        self._chosen_guid = ""            # 用户选的；空 = 跟随系统默认
        self._pcm = bytearray()
        self._collecting = False
        self._opening = False
        self._lock = threading.Lock()

        # 设备变更：Core Audio 的回调在它自己的线程，只能投进队列，主线程定时取
        self._events: list[tuple[str, object, str]] = []
        self._events_lock = threading.Lock()
        self._events_seen_at = 0.0        # 最后一条事件的时刻：用来判断"事件停了没有"
        self._events_first_at = 0.0       # 第一条待处理事件的时刻：事件不停时靠它兜底
        self._reopen_times: list[float] = []   # 最近几次自动重开的时刻
        self._gave_up = False             # 已经报过「反复断开」：同一次别连报好几遍
        self._last_failure = ""           # 上次报给用户的失败话：同一句连续失败只说一遍
        self._wanted = False              # 用户想要麦克风开着；切回文字模式才变 False
        self._open_seq = 0                # 开流代次：晚到的旧结果按它丢掉
        self._reopening = False           # 正在换流：这期间的热插拔事件是它自己弄出来的
        self._closing = False             # 窗口在关：换流线程回来也别再开流了
        self._watcher = audio_devices.EndpointWatcher(self._on_os_event)
        self._watcher.start()

        self._pump = QtCore.QTimer(self)
        self._pump.setInterval(16)        # ~60fps，跟语音球动画同步
        self._pump.timeout.connect(self._drain)

        self._events_timer = QtCore.QTimer(self)
        self._events_timer.setInterval(150)
        self._events_timer.timeout.connect(self._drain_events)
        self._events_timer.start()

        # 流稳住了才算好：蓝牙切换模式每次都能开成功，不能一成功就把断开记录清零
        self._stable_timer = QtCore.QTimer(self)
        self._stable_timer.setSingleShot(True)
        self._stable_timer.timeout.connect(self._mark_stable)

        self._opened.connect(self._on_opened)
        self._open_failed.connect(self._on_open_failed)
        self._reopened.connect(self._on_reopened)

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
        """进语音模式时调一次。已经在开/已开好/正在换流就什么都不做。"""
        self._wanted = True
        if self._capture is not None or self._opening or self._reopening:
            return
        self._spawn_open()

    def close_stream(self, *, keep_open_intent: bool = False) -> None:
        """切回文字模式时调。关掉流、丢掉半截数据。

        `keep_open_intent=True` 是内部用：只是这条流暂时用不了，用户还想要麦克风，
        设备回来要自动接上——比如读失败、重开超限这两种。
        """
        if not keep_open_intent:
            self._wanted = False
        self._collecting = False
        self._pump.stop()
        self._stable_timer.stop()
        capture, self._capture = self._capture, None
        self._open_seq += 1                  # 还在飞的开流结果一律作废
        self._opening = False
        if capture is not None:
            threading.Thread(target=capture.close, name="noesia-mic-close", daemon=True).start()
        with self._lock:
            self._pcm.clear()

    def reopen(self) -> None:
        """换设备：等旧流真的关掉，再开新的。

        旧流是丢在后台线程里关的（`close()` 可能几十毫秒）。不等着就开新流，
        两个句柄会短暂并存于同一个端点，系统把这当成「设备变了」再抛一轮事件，
        事件又触发一次重开——日志里 5 秒刷 13 次「设备没了」就是这么来的。
        """
        self._collecting = False
        self._pump.stop()
        self._stable_timer.stop()
        capture, self._capture = self._capture, None
        self._open_seq += 1          # 在飞的开流结果作废，否则它晚到会盖掉新的那一路
        self._opening = False
        with self._lock:
            self._pcm.clear()

        if capture is None:
            self.open_stream()      # 本来就没开着：直接开
            return
        self._reopening = True      # 关→开这段时间的热插拔事件一律别处理
        threading.Thread(
            target=self._close_then_reopen,
            args=(capture,),
            name="noesia-mic-reopen",
            daemon=True,
        ).start()

    def _close_then_reopen(self, capture) -> None:
        """**在后台线程上跑**：旧流关干净了再回主线程开新的。"""
        try:
            capture.close()
        except Exception:  # noqa: BLE001 - 关不掉也得往下走，否则流再也开不回来
            logger.debug("[录音] 关旧流失败", exc_info=True)
        self._reopened.emit()

    @QtCore.Slot()
    def _on_reopened(self) -> None:
        self._reopening = False
        if self._closing or not self._wanted:
            return   # 换流期间用户切回文字模式了：别再开回来
        # 关流期间攒下的事件是旧流自己弄出来的，不算设备真的变了
        with self._events_lock:
            self._events.clear()
            self._events_first_at = 0.0
        self._events_seen_at = 0.0
        self.open_stream()

    def shutdown(self) -> None:
        """窗口在关：把流关掉、热插拔监听取掉。

        常开的流不主动关，进程退出前设备一直被占着——蓝牙耳机那头会一直显示
        「正在使用」，用户换个程序录音就没声。
        """
        self._pump.stop()
        self._events_timer.stop()
        self._closing = True
        self._reopening = False
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
        self._open_seq += 1          # 每次开流一个新代次：晚到的旧结果会被认出来
        seq = self._open_seq
        threading.Thread(
            target=self._open_worker, args=(endpoint, seq), name="noesia-mic-open", daemon=True
        ).start()

    def _open_worker(self, endpoint: Endpoint, seq: int) -> None:
        capture = audio_capture.WasapiCapture(endpoint, self._request_rate, self._channels)
        try:
            capture.open()
        except audio_capture.CaptureError as exc:
            self._open_failed.emit(seq, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任何意外都要变成界面上的一句话，不能静默
            logger.warning("打开麦克风时崩溃", exc_info=True)
            self._open_failed.emit(seq, f"「{endpoint.name}」打不开：{exc}")
            return
        self._opened.emit(capture, seq)

    @QtCore.Slot(object, int)
    def _on_opened(self, capture, seq: int) -> None:
        if seq != self._open_seq:
            # 又开了一次流，这一路是过期的：关掉，别让它占着设备还没人管
            logger.debug("[录音] 过时的开流结果（代次 %d，当前 %d），关掉", seq, self._open_seq)
            threading.Thread(target=capture.close, name="noesia-mic-close", daemon=True).start()
            return
        self._opening = False
        self._capture = capture
        self._last_failure = ""      # 开起来了：同一句错误下次还能再报
        # 断开记录要等流稳住了才清（蓝牙切换模式每次都能开成功，一成功就清零等于没上限）
        self._stable_timer.start(int(OPEN_STABLE_SECONDS * 1000))
        # 设备实际给了什么格式就按什么算，不假设它一定是 48k
        self._rate = capture.rate or self._rate
        self._channels = capture.channels or 1
        self._pump.start()
        logger.info(
            "[录音] %s 就绪（%s · %d Hz / %d 声道）",
            capture.endpoint.name, capture.backend, self._rate, self._channels,
        )
        self.stream_ready.emit(capture.backend)

    @QtCore.Slot(int, str)
    def _on_open_failed(self, seq: int, message: str) -> None:
        if seq != self._open_seq:
            return   # 过时的失败，别再拿它吓用户一遍
        self._opening = False
        self._capture = None
        logger.warning("[录音] %s", message)
        if self._gave_up or message == self._last_failure:
            return   # 已经认输、或者同一句话刚说过：别再刷一遍
        self._last_failure = message
        self.failed.emit(message)

    @QtCore.Slot()
    def _mark_stable(self) -> None:
        """流稳住了这么久：攒着的断开记录才算真的翻篇。"""
        self._reopen_times.clear()
        self._gave_up = False

    # ------------------------------------------------------------ 采集

    def start(self) -> bool:
        """开始攒数据。流没开好的话顺手开一次。"""
        if self._capture is None:
            self.open_stream()
            return False
        with self._lock:
            self._pcm.clear()
        self._collecting = True
        return True

    def stop(self) -> bytes | None:
        """停止攒数据，返回这次录到的 PCM（16k 单声道 s16le）。"""
        self._collecting = False
        with self._lock:
            data = bytes(self._pcm)
            self._pcm.clear()
        seconds = len(data) / max(1, self._rate * self._channels * 2)
        if seconds < MIN_SECONDS:
            logger.info("[录音] 太短：%.2f 秒", seconds)
            return None
        return _resample(data, self._rate, self._channels)

    def cancel(self) -> None:
        self._collecting = False
        with self._lock:
            self._pcm.clear()

    @QtCore.Slot()
    def _drain(self) -> None:
        capture = self._capture
        if capture is None:
            return
        try:
            chunk = capture.read()
        except audio_capture.CaptureError as exc:
            # 设备中途失效：原因原样说给界面听。以前这条路一律说「说话太短了」，
            # 用户明明说了一整句，得到的是「按住多说一会儿」——误导得去怀疑自己。
            if self._capture is None:
                return  # 我们自己关流关掉的
            logger.warning("读麦克风失败：%s", exc)
            self.close_stream(keep_open_intent=True)  # 用户还想要麦，设备回来要自动接上
            self.failed.emit(str(exc))
            return
        except Exception:  # noqa: BLE001 - 读取失败不再让它每 16ms 崩一次
            if self._capture is None:
                # 是我们自己关流关掉的（close_stream 把引用置空了），不是设备出问题
                return
            logger.warning("读麦克风失败", exc_info=True)
            self.close_stream(keep_open_intent=True)  # 用户还想要麦，设备回来要自动接上
            self.failed.emit("麦克风读不出来了，已断开")
            return
        if not chunk:
            return
        if self._collecting:
            with self._lock:
                self._pcm.extend(chunk)
                overflow = len(self._pcm) > int(MAX_SECONDS * self._rate * self._channels * 2)
            if overflow:
                # 到上限就不再收了，但**必须说出来**：球还在随音量起伏，说的话却全丢，
                # 用户以为还在录、松手只发出去前 60 秒，没人知道后半句去哪了。
                self._collecting = False
                logger.info("[录音] 到 %.0f 秒上限，自动停", MAX_SECONDS)
                self.failed.emit(f"说到 {int(MAX_SECONDS)} 秒上限了，后面的没录上；松手发送或再按住说一段")
        self.level.emit(_level(chunk))


    # ------------------------------------------------------------ 设备变更

    def _on_os_event(self, reason: str, flow, guid: str) -> None:
        """**在 Core Audio 自己的线程上跑**：只入队，绝不碰 Qt 对象。"""
        now = time.monotonic()
        with self._events_lock:
            if not self._events:
                self._events_first_at = now   # 这批事件从什么时候开始攒的
            self._events.append((reason, flow, guid))
        # 记在锁外：_drain_events 靠这个时间戳判断事件停没停
        self._events_seen_at = now

    def _may_retry(self, now: float, message: str) -> bool:
        """还能不能再自动试一次。

        两条重开路径（设备没了、设备在但开流失败）共用这一个窗口：
        任一原因的反复重试都要算进同一个计数，不然从另一条路绕进去就绕过了上限。
        认输时同一类话只报一遍，别让用户每秒看见一次同样的错。
        """
        self._reopen_times = [t for t in self._reopen_times if now - t < REOPEN_WINDOW_SECONDS]
        if len(self._reopen_times) >= MAX_REOPEN_IN_WINDOW:
            if not self._gave_up:
                self._gave_up = True
                logger.warning(
                    "[录音] %.0f 秒内已经重试 %d 次，停止自动重开",
                    REOPEN_WINDOW_SECONDS,
                    len(self._reopen_times),
                )
                self.failed.emit(message)
            return False
        self._reopen_times.append(now)
        self._gave_up = False
        return True

    @QtCore.Slot()
    def _drain_events(self) -> None:
        # 正在换流：这批事件是它自己弄出来的（关旧流、开新流都会触发），别处理
        if self._reopening:
            return
        now = time.monotonic()
        with self._events_lock:
            if not self._events:
                return
            # 一次切换（拔插、蓝牙重连、切换默认设备）会连着吐几十条事件。
            # 等它们停够 EVENT_QUIET_SECONDS 再统一处理一次，别逐条响应——
            # 逐条响应就是每 150ms 关一次流、开一次流，自己跟自己打架。
            # 但事件也可能一直不停（设备反复重连），所以还得有个总上限，不能干等。
            if (
                now - self._events_seen_at < EVENT_QUIET_SECONDS
                and now - self._events_first_at < EVENT_MAX_WAIT_SECONDS
            ):
                return
            events, self._events = self._events, []
            self._events_first_at = 0.0

        # 只关心录制方向；flow 为 None 的是状态/增删事件，两个方向都可能
        relevant = [e for e in events if e[1] in (None, audio_devices.E_CAPTURE)]
        if not relevant:
            return

        endpoints = self.devices()
        self.devices_changed.emit([(e.guid, e.name, e.active) for e in endpoints])
        alive = {e.guid.lower() for e in endpoints}

        if self._endpoint is not None and self._endpoint.guid.lower() not in alive:
            lost_name = self._endpoint.name
            if self._collecting:
                # 这里不能 `cancel()`：它把已经录到的 PCM 清空，用户明明说了一整句，
                # 松手却得到「说话太短了，按住多说一会儿」。保住已录的部分（只停止追加），
                # 松手照样把前半句发出去。
                self._collecting = False
                self.failed.emit(f"「{lost_name}」断开了；已经录到的会照样发出去")
            logger.info("[录音] 设备没了：%s", self._endpoint.name)

            # 反复断开时别再自动重开：越重开越乱，还可能把设备拖挂
            if not self._may_retry(
                now, "麦克风反复断开，先停一下；插好或重新连上之后再点一下麦克风"
            ):
                self.close_stream(keep_open_intent=True)  # 用户还想要麦，设备回来要自动接上
                return

            replacement = self.current()
            if not self._wanted:
                # 已经切回文字模式：拔耳机、切默认设备这类事件照样会来，
                # 这时的正确动作是「记下来、什么都不开」——别把麦克风重新打开
                # （否则占着 Windows 的麦克风、蓝牙耳机被钉在采集态、拾音灯常亮），
                # 也别弹「麦克风断开了」，用户根本没在用麦。
                self._endpoint = None
                return
            self.device_lost.emit(replacement.name if replacement else "")
            self.reopen()  # 走串行化那条路：旧流关干净了才开新的
            return

        # 设备都在，手里却没有流：上次没开成、或者被丢掉了，接上。
        # 只在用户还想要麦克风时才做——切回文字模式后不能在后台偷偷开流。
        # 这条也要计入同一个重试窗口：麦克风被别的程序独占时开流会一直失败，
        # 不计的话就成了每秒重试一次、每秒报一条错。
        if self._wanted and self._capture is None and not self._opening and endpoints:
            if not self._may_retry(
                now, "麦克风一直打不开，先停一下；关掉占用它的程序或重新连接之后再点一下麦克风"
            ):
                return
            logger.info("[录音] 设备在、手里没有流，重新开起来")
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


def _resample(pcm: bytes, src_rate: int, src_channels: int = 1, dst_rate: int = 16000) -> bytes:
    """把设备原生格式的 PCM 换成 16k 单声道 s16le。

    比例按实测算，不写死：设备的原生采样率可能是 44.1k、48k、16k 任何一种，声道可能是
    1 或 2。多声道先平均成单声道，再交给 `speech_stream.Resampler` 变速——整数倍取平均、
    非整数倍线性插值，那套逻辑项目里已经有了，这里不再写一份。
    """
    if not pcm or src_rate <= 0:
        return b""
    channels = max(1, src_channels)
    if channels > 1:
        block = 2 * channels
        usable = len(pcm) - (len(pcm) % block)
        if not usable:
            return b""
        raw = np.frombuffer(pcm[:usable], dtype="<i2").reshape(-1, channels)
        pcm = np.round(raw.mean(axis=1)).astype("<i2").tobytes()
    else:
        usable = len(pcm) - (len(pcm) % 2)
        if not usable:
            return b""
        pcm = pcm[:usable]
    if src_rate == dst_rate:
        return pcm
    if src_rate % dst_rate == 0:
        # 整数倍（48k→16k）用切片平均一次算完，比逐点插值快得多，顺手当低通
        step = src_rate // dst_rate
        data = np.frombuffer(pcm, dtype="<i2")
        n = (data.size // step) * step
        if not n:
            return b""
        return np.round(data[:n].reshape(-1, step).mean(axis=1)).astype("<i2").tobytes()
    return Resampler(src_rate, dst_rate).push(pcm)


def write_wav(pcm: bytes, path: Path, rate: int = 16000, channels: int = 1) -> Path:
    """把 PCM 包成 WAV 写到 `path`（识别要走文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


