"""互动数字人：阿里云 ARTC 原生 SDK 直接在本进程里拿画面。

链路：
    ViduSession 建会话（云端渲染 + 托管 RTC）→ `_alirtc` 用下发的 token 入会 →
    SDK 回调把 I420 裸帧交给我们 → 抠绿 → `frame_ready(QImage)` 交给主窗口画。

为什么不用浏览器：QtWebEngine 不带 H.264，带 H.264 的 WebView2 只能靠截屏取帧
（37ms/帧、上限 27fps，还多一个进程和窗口）。原生 SDK 是 3～5ms 拿裸帧、单进程单窗口，
「切到互动档那一下不弹窗、不卡」也就成立。

计费：引擎预加载（`preload`）0 积分；建会话才按秒计费（1 积分/秒），
所以只在「用户快说话」时才 `warm()`，说完闲置由外壳挂断。
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time
from collections import deque

import numpy as np
from PySide6 import QtCore, QtGui

from app.idle_video import key_image
from app.vidu_session import FRAME_BYTES, SAMPLE_RATE, ViduSession, read_token

logger = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RTC_DIR = REPO_ROOT / "vendor" / "alirtc"
RTC_AUDIO_RATE = 48000  # 云端回来的音频固定 48k 单声道 s16（实测 858 帧 / 8.6 秒）
DEFAULT_IMAGE = REPO_ROOT / "assets" / "greenscreen" / "noesia_greenscreen.png"


def _i420_to_rgb(y: bytes, u: bytes, v: bytes, width: int, height: int,
                 stride_y: int, stride_u: int, stride_v: int, step: int = 1) -> np.ndarray:
    """I420 → RGB。stride 可能比宽度大，先整块取再裁掉右边缘。

    `step` 是跳采样步长：显示尺寸比源小很多时（888×1184 画到 360×480），
    先把像素跳掉再转换——全幅转一次要 30ms，跳 2 就只要 8ms，而肉眼看不出差别。
    """
    ya = np.frombuffer(y, np.uint8).reshape(height, stride_y)[0::step, 0::step]
    ya = ya[: (height + step - 1) // step, : (width + step - 1) // step].astype(np.float32)
    out_h, out_w = ya.shape
    ch = (height + 1) // 2
    ua = np.frombuffer(u, np.uint8).reshape(ch, stride_u)[0::step, 0::step]
    va = np.frombuffer(v, np.uint8).reshape(ch, stride_v)[0::step, 0::step]
    ua = ua[: (out_h + 1) // 2, : (out_w + 1) // 2].astype(np.float32)
    va = va[: (out_h + 1) // 2, : (out_w + 1) // 2].astype(np.float32)
    ua = np.repeat(np.repeat(ua, 2, axis=0), 2, axis=1)[:out_h, :out_w]
    va = np.repeat(np.repeat(va, 2, axis=0), 2, axis=1)[:out_h, :out_w]
    yf = (ya - 16.0) * 1.164
    r = yf + 1.596 * (va - 128.0)
    g = yf - 0.813 * (va - 128.0) - 0.391 * (ua - 128.0)
    b = yf + 2.018 * (ua - 128.0)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


SHARPEN = 0.9  # 轻微锐化：Vidu 云端渲染本身偏软，不锐化的话观感明显比待机视频糊


def _rgb_array(image: QtGui.QImage) -> np.ndarray:
    """QImage（任意格式）→ (h, w, 3) 的 RGB numpy（拷出来，别指向 QImage 的缓冲）。"""
    img = image.convertToFormat(QtGui.QImage.Format_RGB888)
    h, w, stride = img.height(), img.width(), img.bytesPerLine()
    rows = np.frombuffer(img.constBits(), np.uint8, stride * h).reshape(h, stride)
    return rows[:, : w * 3].reshape(h, w, 3).copy()


def _sharpen(rgb: np.ndarray, amount: float = SHARPEN) -> np.ndarray:
    """3×3 均值模糊取差分做锐化（相当于轻量 unsharp mask），比 PIL 省一次格式转换。"""
    src = rgb.astype(np.int16)
    blur = (
        src
        + np.roll(src, 1, axis=0)
        + np.roll(src, -1, axis=0)
        + np.roll(src, 1, axis=1)
        + np.roll(src, -1, axis=1)
    ) // 5
    return np.clip(src + (src - blur) * amount, 0, 255).astype(np.uint8)



class AvatarRtc(QtCore.QObject):
    """互动画面来源。生命周期三层：preload（0 积分）→ warm（按秒计费）→ hangup。"""

    frame_ready = QtCore.Signal(QtGui.QImage)
    failed = QtCore.Signal(str)
    ready = QtCore.Signal()   # 会话就绪，可以推音频了
    ended = QtCore.Signal()   # 已挂断，外壳该把待机视频接回来
    notice = QtCore.Signal(str)  # 服务端回执里的问题（比如复位不被支持），界面上说一句

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
        *,
        image_path: pathlib.Path | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.image_path = pathlib.Path(image_path or DEFAULT_IMAGE)
        self.model = model            # 空 = 用 avatar.json 里的 model（唯一来源，不在代码里兜）
        self._module = None            # _alirtc 扩展模块
        self._session: ViduSession | None = None
        self._engine_created = False  # create() 是否真的调过：回调失败重试时不许再造一个实例
        self._dll_dir = None  # add_dll_directory 的句柄必须留着，否则目录会从 DLL 搜索路径里掉出去
        self._target: QtCore.QSize | None = None
        self._lock = threading.Lock()
        self._state = "idle"           # idle / warming / ready
        self._warm_seq = 0             # 建会话的代次：挂断/失败会把它推上去，晚回来的那条 worker 自己收摊
        # 不设上限：TTS 合成得比实时快好几倍，设 50 帧（1 秒）会把前面的话直接丢掉，
        # 结果就是"声音念完了、嘴只做了开头"。100 秒（5000 帧）当保险丝，正常永远碰不到。
        self._queue: deque[bytes] = deque()
        self._queue_max = 5000
        self._silence = b"\x00" * FRAME_BYTES          # 没话说时推静音：保持通话热度
        self._sender: threading.Thread | None = None
        self._send_run = threading.Event()
        self._raw: tuple | None = None          # 最新一帧原始 I420，等着转
        self._raw_lock = threading.Lock()
        self._raw_ready = threading.Event()
        self._convert_run = threading.Event()
        self._converter: threading.Thread | None = None
        self._frames = 0
        self._frames_since_speak = 0  # 这一句开始后画面出了多少帧（短句别在还没出画时就收回）
        self._audio_frames = 0
        # 云端回来的音频：SDK 只把裸数据丢给我们，不会自己出声，所以自己播。
        # 和画面同一条流、只多一个 100ms 缓冲，比"猜个几百毫秒去补"靠谱得多。
        self._last_audio_at = 0.0  # 最后一次收到远端音频的时刻
        self._last_loud_at = 0.0   # 最后一次听到"有声音"的远端音频（判断她念完了没）
        self._speak_mark = 0.0     # 这一句开始的时刻（quiet 最早只从这个时刻算）
        self._recv_samples = 0   # 云端已经回传多少采样（48k 单声道；换算成 24k s16 就是字节数）
        self._rate_warned = False
        self._last_error = ""
        self._reset_off = False       # 复位（type 15）被服务端拒过：别再每句白发一遍

    # ---- 第一层：预加载（0 积分） ----

    def _configured_model(self) -> str:
        """数字人模型只从 `data/settings/avatar.json` 取；缺这个键就报错，不在代码里兜一个默认值。"""
        from app import avatar_prefs

        return avatar_prefs.load()["model"]

    def preload(self) -> str:
        """把 dll 载进来并建好引擎。不建会话、不计费。返回空串表示成功。"""
        if self._module is not None:
            return ""
        try:
            import os

            if not self._engine_created:
                # 句柄要留着：一回收，这个目录就从 DLL 搜索路径里消失了。
                # 顺序不能反：_alirtc.pyd 依赖 rtc/ 里的库，目录不在搜索路径上 import 就失败。
                self._dll_dir = os.add_dll_directory(str(RTC_DIR))

            from app import _alirtc

            if not self._engine_created:
                if not _alirtc.create(""):
                    return "引擎创建失败"
                # 建过就记下：下面回调设置失败时，重试只补回调，不会再 create 出第二个实例
                self._engine_created = True
            _alirtc.set_video_callback(self._on_yuv)
            _alirtc.set_audio_callback(self._on_remote_audio)
            self._module = _alirtc
            self._convert_run.set()
            self._converter = threading.Thread(
                target=self._convert_loop, daemon=True, name="avatar-convert"
            )
            self._converter.start()
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"{exc}"

    @property
    def loaded(self) -> bool:
        return self._module is not None

    # ---- 第二层：建会话（按秒计费） ----

    def warm(self) -> None:
        """后台建会话：建会话 → 入会 → conn_init → 推 1 秒静音把画面叫起来。"""
        with self._lock:
            if self._state != "idle":
                return
            self._state = "warming"
            self._warm_seq += 1
            seq = self._warm_seq

        threading.Thread(
            target=self._warm_worker, args=(seq,), daemon=True, name="avatar-warm"
        ).start()

    def _warm_current(self, seq: int) -> bool:
        """这次建会话还算数吗（挂断/失败会把代次推上去，晚回来的那条就作废）。"""
        with self._lock:
            return seq == self._warm_seq

    def _discard_warm(self, session: "ViduSession") -> None:
        """建会话途中被挂断：刚建出来的这一台已经在计费，当场挂掉，绝不挂到身上。"""
        logger.info("[数字人] 建会话途中被挂断：这一台直接挂掉，不挂到身上")
        if self._session is session:
            self._session = None
        if self._module is not None:
            try:
                self._module.leave()
            except Exception:  # noqa: BLE001 - 收摊失败不该盖住别的
                logger.debug("[数字人] leave 出错", exc_info=True)
        threading.Thread(target=session.hangup, daemon=True, name="avatar-discard").start()

    def _warm_worker(self, seq: int) -> None:
        """后台线程：建会话 → 入会 → 开流 → 推 1 秒静音把画面叫起来。

        `seq` 是发起时的代次。这条链每一步都要一两秒，中间用户随时可能挂断（闲置超时、
        关窗口、自己点停）。每段等待回来都核对一次代次：过期就把这一台挂掉走人——
        不核对的话，`hangup()` 之后这条线程仍会 `self._session = session`、把状态置成
        `ready` 并启动保活播放，凭空复活一场按秒计费的会话，界面上还看不出来。
        """
        session: ViduSession | None = None
        try:
            if self._module is None:  # 预加载没跑或失败过：这里补一次，别拿 None 去判断属性
                why = self.preload()
                if why:
                    raise RuntimeError(f"引擎没载起来：{why}")
            session = ViduSession(read_token(), self.image_path, self.model or self._configured_model())
            session.on_event = lambda msg: logger.info("[数字人] %s", msg)
            session.on_error = self._on_server_error  # 103 错误回执：翻成人话交出去
            # 这一行出现在日志里 = 从这一刻开始按秒计费。只在"按了发送、真要念这句"时才该看到它。
            logger.info("[数字人] 建会话（开始按秒计费）")
            session.create()
            if not self._warm_current(seq):
                self._discard_warm(session)
                return
            # create 成功就已经在计费：先挂上，之后任何一步失败 `_fail` 才能把它挂断
            self._session = session
            rtc = session.rtc
            if not hasattr(self._module, "subscribe_audio"):
                raise RuntimeError(
                    "`_alirtc.pyd` 是旧版本（缺 subscribe_audio），跑 tools/build_alirtc.py 重编"
                )
            self._module.subscribe_audio(True)  # 要云端把音频回传回来，当时间轴用
            code = self._module.join(
                rtc["token"], str(rtc["channel_id"]), str(rtc["user_id"]), "noesia"
            )
            if code != 0:
                raise RuntimeError(f"入会失败，返回 {code}")
            if not self._warm_current(seq):
                self._discard_warm(session)
                return
            session.open_stream()
            session.push_pcm(b"\x00\x00" * (SAMPLE_RATE // 2))  # 1 秒静音：她被叫醒但不说话
            if not self._warm_current(seq):
                self._discard_warm(session)
                return
            with self._lock:
                self._state = "ready"
            self._send_run.set()
            self._sender = threading.Thread(target=self._send_loop, daemon=True, name="avatar-send")
            self._sender.start()
            self.ready.emit()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "429" in msg or "concurrency" in msg:
                # 上一次会话（进程被杀/没挂断）还占着并发的坑，服务端要过一会儿才回收
                msg = "并发已满：上一次会话还没完全关掉，等十几秒再发一次"
            self._fail(f"建会话失败：{msg}")

    def _fail(self, message: str) -> None:
        """出错了：回到 idle，把原因交给上层（上层会退回纯语音）。"""
        logger.warning("[数字人] %s", message)
        session, self._session = self._session, None
        if session is not None:
            # 会话一建出来就按秒计费：失败路径原来只把状态置回 idle，
            # 云端那一场一直挂着（还占并发名额，下一次就是 429）。
            try:
                session.hangup()
            except Exception:  # noqa: BLE001 — 收摊失败不能盖住真正的原因
                logger.warning("[数字人] 挂断失败的会话时出错", exc_info=True)
        with self._lock:
            self._warm_seq += 1  # 正在建的那一台也作废（它回来会自己挂掉）
            self._state = "idle"
        self._last_error = message
        self._send_run.clear()
        self.failed.emit(message)

    @property
    def state(self) -> str:
        """idle / warming / ready。"""
        return self._state

    @property
    def audio_frames(self) -> int:
        """这一句期间收到多少云端音频帧（10ms 一帧）。"""
        return self._audio_frames

    @property
    def frames_since_speak(self) -> int:
        """这一句开始后收到并画出来多少帧。0 = 画面还没出过（短句容易在这里被收早）。"""
        return self._frames_since_speak

    @property
    def quiet_seconds(self) -> float:
        """远端音频安静多久了（0=她这会儿还在出声，或者还没开始出声）。"""
        if not self._last_loud_at:
            return 0.0
        return time.monotonic() - max(self._last_loud_at, self._speak_mark)

    @property
    def audio_idle_seconds(self) -> float:
        """云端有多久没再回音频了（0=刚刚还在回）。

        还没收到过这一句的第一帧时，从「这句开始」算起，不能报 999——收尾判据看见
        「几百秒没音频」会立刻把这句判成断流（日志里「云端断流 999.0s、已过 0.5s、
        回传 0 帧」就是这么来的：长句首帧本来就慢，0.5 秒就被掐掉，一个字都没念出来）。
        久到 5 秒还没首帧，才真的是断流。
        """
        mark = max(self._last_audio_at, self._speak_mark)
        if not mark:
            return 0.0
        return time.monotonic() - mark

    def reset_audio_frames(self) -> None:
        """新一句开始：云端时间轴和"她有没有在出声"全部归零。

        不归零的话上一句的 `_last_loud_at` 会留到这一句，安静时间一上来就是几十秒，
        这一句刚开始就被判定"念完了"——连续对话里第二句起全错就是它。
        """
        self._audio_frames = 0
        self._recv_samples = 0
        self._last_audio_at = 0.0
        self._last_loud_at = 0.0
        self._speak_mark = time.monotonic()

    @property
    def audio_timeline_bytes(self) -> int:
        """云端音频走到哪了（按 24k s16 字节算）。

        48k 的"采样数"刚好等于 24k s16 的"字节数"，所以直接拿来当字节数用。
        本地播自己的干净 PCM，播到这个位置为止，声音就和画面同一条时间轴。
        """
        return self._recv_samples

    @property
    def audio_seconds(self) -> float:
        """云端回传的音频有多长。"""
        return self._recv_samples / RTC_AUDIO_RATE  # 48k 采样数 ÷ 48000 = 秒

    def _on_remote_audio(self, uid, rate, channels, bps, samples, data, begin, end) -> None:
        """SDK 线程回调：只记账。云端这份音频有毛刺不播，只当时间轴。"""
        self._audio_frames += 1
        self._recv_samples += int(samples)
        now = time.monotonic()
        self._last_audio_at = now
        # 远端音频是"观众听到的那份"：它有声音 = 她还在说；它安静下来 = 这句真的念完了。
        # 比"按字数估时长"准，因为 Vidu 的语速和她自己排的停顿只有它知道。
        try:
            if data:
                pcm = np.frombuffer(bytes(data), dtype=np.int16)
                if pcm.size and float(np.abs(pcm).mean()) > 260:
                    self._last_loud_at = now
        except Exception:  # noqa: BLE001
            pass
        if rate != RTC_AUDIO_RATE and not self._rate_warned:
            self._rate_warned = True
            logger.warning("[数字人] 远端音频采样率是 %s，不是 %d", rate, RTC_AUDIO_RATE)

    # ---- 音频：一份 PCM 推给云端驱动口型 ----

    def begin_speak(self) -> bool:
        """开口前调用：把攒的帧清掉，启动定速发送线程。没就绪返回 False。"""
        if self._state != "ready" or self._session is None:
            return False
        self._queue.clear()
        self._frames_since_speak = 0
        if self._sender is None or not self._sender.is_alive():
            self._send_run.set()
            self._sender = threading.Thread(target=self._send_loop, daemon=True, name="avatar-send")
            self._sender.start()
        return True

    def feed_pcm(self, pcm: bytes) -> None:
        """收 20ms 一帧的 PCM（`StreamSpeaker.on_chunk` 就是这么给的）。"""
        if self._state != "ready":
            return
        for offset in range(0, len(pcm), FRAME_BYTES):
            if len(self._queue) >= self._queue_max:
                self._queue.popleft()  # 保险丝：真到了这一步说明云端已经跟不上了
            self._queue.append(pcm[offset : offset + FRAME_BYTES])

    def _send_loop(self) -> None:
        """20ms 一帧定速推：推快了口型会跑。"""
        next_at = time.perf_counter()
        while self._send_run.is_set():
            session = self._session
            if session is None:
                break
            # 没话说就推静音：Vidu 长时间收不到音频会「睡着」，再开口要 1.6 秒才醒
            frame = self._queue.popleft() if self._queue else self._silence
            try:
                session.push_frame(frame)
            except Exception as exc:  # noqa: BLE001
                self._fail(f"推音频失败：{exc}")
                break
            next_at += FRAME_BYTES / (SAMPLE_RATE * 2)
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.2:  # 落后太多就别追了，重新对齐节奏
                next_at = time.perf_counter()

    def say_text(self, text: str) -> None:
        """把这一句要说的文本告诉 Vidu（type=10）：口型还是听 PCM，文本只当语义语境。"""
        session = self._session
        if session is None:
            return
        try:
            session.send_text(text)
        except Exception as exc:  # noqa: BLE001
            logger.info("[数字人] 回复文本没发出去（不影响这一句的出声和口型）：%s", exc)

    def say_action(self, text: str) -> None:
        """把「用户刚说的话」交给 Vidu（type=9）：动作匹配拿它去动作库里找相近的动作。"""
        session = self._session
        if session is None:
            return
        try:
            session.send_action(text)
        except Exception as exc:  # noqa: BLE001
            logger.info("[数字人] 动作文本没发出去（不影响这一句的出声和口型）：%s", exc)

    def baseline_reset(self) -> None:
        """说完/被打断：把画面复位到闭口静息态（type=15）。被服务端拒过一次就不再发。"""
        session = self._session
        if session is None or self._reset_off:
            return
        try:
            session.baseline_reset()
        except Exception as exc:  # noqa: BLE001
            logger.info("[数字人] 复位画面信令没发出去：%s", exc)

    def _on_server_error(self, source: str, code: str, text: str) -> None:  # noqa: ARG002
        """服务端 103 错误回执：复位被拒就停发，并把原因交给界面说一句（不静默）。"""
        logger.warning("[数字人] %s", text)
        if source == "baseline_reset":
            self._reset_off = True
        self.notice.emit(text)

    def interrupt(self) -> None:
        """打断：清掉还没推的音频，并让云端立刻闭嘴。"""
        self._queue.clear()
        if self._session is not None:
            self._session.interrupt()

    # ---- 第三层：挂断（停计费） ----

    def hangup(self) -> None:
        self._send_run.clear()
        self._queue.clear()
        with self._lock:
            self._warm_seq += 1  # 正在进行的那次建会话作废：它回来会自己把这台挂掉
        session = self._session
        self._session = None
        with self._lock:
            self._state = "idle"
        if session is not None:
            logger.info("[数字人] 挂断会话（停计费）")
            threading.Thread(target=session.hangup, daemon=True, name="avatar-hangup").start()
        if self._module is not None:
            try:
                self._module.leave()
            except Exception:  # noqa: BLE001
                pass
        self.ended.emit()

    def shutdown(self) -> None:
        """退出时用：挂断停账，引擎不拆（Destroy 要 1.8 秒，进程退出时系统回收）。"""
        self._convert_run.clear()
        self._send_run.clear()
        with self._lock:
            self._warm_seq += 1  # 退出时正在建的那一台也得作废，不然它会在关窗后又冒出来计费
        session = self._session
        self._session = None
        if session is not None:
            logger.info("[数字人] 退出时挂断会话（停计费）")
            session.hangup()

    # ---- 画面 ----

    def set_target_size(self, size: QtCore.QSize) -> None:
        """显示尺寸：帧先缩到这里再抠像（跟待机视频同一条路，省算力）。"""
        self._target = size if size.isValid() and not size.isEmpty() else None

    @property
    def stats(self) -> dict[str, float]:
        return {"frames": self._frames}

    def _on_yuv(self, _uid, width, height, y, u, v, stride_y, stride_u, stride_v) -> None:  # noqa: PLR0913
        """SDK 线程里被调：只把裸帧拷出来就走（转换交给 `_convert_loop`）。

        在回调里直接转 RGB 要 30ms，把 SDK 的送帧线程卡住，帧率掉到 8fps；
        这里只做一次 memcpy（约 0.3ms），转换在另一条线程里慢慢做，丢帧就丢旧的。
        """
        with self._raw_lock:
            self._raw = (
                width, height,
                bytes(y), bytes(u), bytes(v),
                stride_y, stride_u, stride_v,
            )
        self._raw_ready.set()

    def _convert_loop(self) -> None:
        """把最新一帧转成画面：I420 → RGB → 缩到显示尺寸 → 轻微锐化 → 抠绿 → 交给主线程。"""
        while self._convert_run.is_set():
            if not self._raw_ready.wait(0.5):
                continue
            with self._raw_lock:
                item, self._raw = self._raw, None
                self._raw_ready.clear()
            if item is None:
                continue
            width, height, y, u, v, stride_y, stride_u, stride_v = item
            target = self._target
            step = 1
            if target is not None and target.width() > 0:
                # 显示宽不到源的四分之一才跳采样（跳了会糊，宁可贵一点）
                step = max(1, width // (target.width() * 4))
            rgb = _i420_to_rgb(y, u, v, width, height, stride_y, stride_u, stride_v, step)
            rgb_h, rgb_w = rgb.shape[:2]
            image = QtGui.QImage(
                rgb.data, rgb_w, rgb_h, rgb_w * 3, QtGui.QImage.Format_RGB888
            ).copy()
            if target is not None and image.size() != target:
                # 最近邻，跟待机视频那条路完全一致（平滑缩放实测明显更软，观感就是"糊"）
                image = image.scaled(
                    target, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.FastTransformation
                )
            pixels = _sharpen(_rgb_array(image))
            image = QtGui.QImage(
                pixels.data, image.width(), image.height(), image.width() * 3,
                QtGui.QImage.Format_RGB888,
            ).copy()
            self._frames += 1
            self._frames_since_speak += 1
            try:
                self.frame_ready.emit(key_image(image))
            except RuntimeError:
                return  # 窗口已经销毁（退出中），转换线程跟着收工
