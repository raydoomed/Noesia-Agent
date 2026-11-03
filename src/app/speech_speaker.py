"""把回答念出来：文字 → 边合成边播（豆包 / Edge / ElevenLabs 都走同一条流式链路）。

以前是「整段合成完 → 落成 wav/mp3 → 再解码播放」：一句 20 秒的话要等 1.8 秒才开口，
长句子更久。现在直接用 `speech_stream.stream_pcm()`：引擎给一片就喂一片给声卡，
Edge 实测首片 0.48 秒（120 字、20 秒音频），豆包 0.5 秒左右。

播放不用 `QMediaPlayer`：它在「解码到头」时就报结束，声卡缓冲里还压着约 150ms 没播，
听感是最后一个字没读完整。这里把 PCM 直接喂 `QAudioSink`，等设备真排空（processedUSecs
到已喂进去的时长）才算播完。

流式拿不到「总时长」（后面还有多少不知道），所以进度只报已播；界面收到 total=0
就只显示时间、不画比例。
"""

from __future__ import annotations

import re
import threading

from PySide6 import QtCore, QtMultimedia

from app.speech_stream import TARGET_RATE, SpeechError, stream_pcm

# 不该念出来的东西：代码块、行内代码、MEDIA 路径、链接地址、markdown 记号
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_MEDIA_TOKEN = re.compile(r"MEDIA:\S+")  # 行内行首都可能夹着，一律抹掉
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARK = re.compile(r"[*_>#|]+")


def speakable(text: str) -> str:
    """把回答洗成能念的纯文字；洗完没内容就返回空串（调用方直接跳过）。

    不截断：回答有多少字就念多少字。以前这里按 max_chars 截到句末，
    长回答会在中途停住、后面整段没声音；超长文本的分段交给 speech_stream。
    """
    cleaned = _CODE_FENCE.sub(" ", text)
    cleaned = _MEDIA_TOKEN.sub(" ", cleaned)
    cleaned = _INLINE_CODE.sub(" ", cleaned)
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _MARK.sub(" ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s+([，。！？；：、])", r"\1", cleaned)  # 删掉标点前的空格，「重点 ：」→「重点：」
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    return cleaned


class _StreamJob(QtCore.QRunnable):
    """在工作线程里跑 `stream_pcm`，每拿到一片就发信号交给主线程。"""

    def __init__(self, text: str, owner: "Speaker", seq: int, cancel: threading.Event) -> None:
        super().__init__()
        self._text, self._owner, self._seq, self._cancel = text, owner, seq, cancel

    @QtCore.Slot()
    def run(self) -> None:
        # 这里在工作线程，**不许碰任何 Qt 对象**（声卡、定时器都属于主线程）
        try:
            for chunk in stream_pcm(self._text):
                if self._cancel.is_set():
                    return
                if chunk:
                    self._owner._chunk_internal.emit(self._seq, chunk)
        except SpeechError as exc:
            self._owner._failed_internal.emit(self._seq, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — 线程里兜底，不能让它静默死掉
            self._owner._failed_internal.emit(self._seq, f"合成失败：{exc}")
            return
        self._owner._done_internal.emit(self._seq)


class _StreamPlayback(QtCore.QObject):
    """边收边播：收到一片喂一片，设备排空 + 合成结束才算播完。"""

    progress = QtCore.Signal(int, int)  # 已播毫秒 / 总毫秒（流式给不出总长，恒为 0）
    finished = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._sink: QtMultimedia.QAudioSink | None = None
        self._io = None
        self._format = QtMultimedia.QAudioFormat()
        self._format.setSampleRate(TARGET_RATE)
        self._format.setChannelCount(1)
        self._format.setSampleFormat(QtMultimedia.QAudioFormat.SampleFormat.Int16)
        self._pending = bytearray()
        self._fed_bytes = 0  # 一共喂进去多少（算"该播多久"）
        self._synth_done = False
        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(30)
        self._ticker.timeout.connect(self._pump)

    @property
    def playing(self) -> bool:
        return self._sink is not None and self._sink.state() == QtMultimedia.QAudio.State.ActiveState

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self._sink is None and not self._open():
            return
        self._pending += pcm
        self._fed_bytes += len(pcm)
        self._pump()

    def mark_done(self) -> None:
        self._synth_done = True
        self._pump()

    def stop(self) -> None:
        self._ticker.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
        self._io = None
        self._pending.clear()
        self._fed_bytes = 0
        self._synth_done = False

    # ---- 内部 ----

    def _open(self) -> bool:
        device = QtMultimedia.QMediaDevices.defaultAudioOutput()
        sink = QtMultimedia.QAudioSink(device, self._format, self)
        sink.setVolume(1.0)
        # 缓冲 250ms：够顶住引擎的分片间隔（Edge 中位 54ms），又不会让"停止"迟钝
        sink.setBufferSize(max(self._format.bytesForDuration(250_000), 8192))
        io = sink.start()
        if io is None:  # 设备打不开（被别的程序独占之类）
            self.failed.emit("打不开音频输出设备（可能被别的程序占用）")
            return False
        self._sink = sink
        self._io = io
        self._ticker.start()
        return True

    def _pump(self) -> None:
        if self._sink is None or self._io is None:
            return
        if self._pending:
            written = self._io.write(bytes(self._pending))
            if written > 0:
                del self._pending[:written]
            elif written < 0:
                # 声卡报错（-1）：再等也不会自己好，这一句就地收尾并说清楚，不然永远收不了尾
                self.stop()
                self.failed.emit("写不进音频设备，这一句没念出来")
                return
        played_us = int(self._sink.processedUSecs())
        self.progress.emit(int(played_us / 1000), 0)
        if not self._synth_done or self._pending:
            return
        # 合成结束 + 全灌进去了：等设备真播完（留 40ms 余量）才收
        fed_us = int(self._format.durationForBytes(self._fed_bytes))
        if played_us >= fed_us - 40_000:
            self.stop()
            self.finished.emit()


class Speaker(QtCore.QObject):
    """一条回答一条语音，一条接一条念；新的一条来了就打断旧的。"""

    preparing = QtCore.Signal()  # 刚开合成、还没出声（界面显示「正在准备音频」）
    started = QtCore.Signal(str)  # 正在念的纯文字
    progress = QtCore.Signal(int, int)  # 已播毫秒 / 总毫秒（流式为 0）
    stopped = QtCore.Signal()  # 被停下（自己点停止、换会话、发新消息都走这里）
    finished = QtCore.Signal()
    failed = QtCore.Signal(str)

    # 线程池 → 主线程的内部信号（不是给界面用的）
    _chunk_internal = QtCore.Signal(int, object)
    _done_internal = QtCore.Signal(int)
    _failed_internal = QtCore.Signal(int, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._chunk_internal.connect(self._on_chunk)
        self._done_internal.connect(self._on_done)
        self._failed_internal.connect(self._on_failed)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # 串行：避免两句同时合成抢额度
        self._seq = 0
        self._active = False  # 这条（合成中或正在播）还算数吗；停下/换条就失效
        self._cancel = threading.Event()
        self._text = ""
        self._announced = False

        self._playback = _StreamPlayback(self)
        self._playback.progress.connect(self._on_progress)
        self._playback.failed.connect(self._on_playback_failed)
        self._playback.finished.connect(self._on_playback_finished)

    # ---- 对外 ----

    def speak(self, text: str) -> bool:
        """念这段文字。返回 False 表示没有可念的内容（已静默跳过）。"""
        from app.speech_prefs import KEYED_PROVIDERS, missing_key_message, read_api_key
        from app.speech_prefs import current as voice_prefs

        chosen = voice_prefs()
        if not chosen["enabled"]:
            return False
        if chosen["provider"] in KEYED_PROVIDERS and not read_api_key(chosen["provider"]):
            # 没 Key：连语音条都别弹（弹了也是立刻失败、只闪一下），直接把原因交出去
            self.failed.emit(missing_key_message(chosen["provider"]))
            return True
        plain = speakable(text)
        if not plain:
            return False
        self._halt()  # 换一条：旧的那条立刻作废（不算「被停下」，不发 stopped）
        self._active = True
        self._announced = False
        self._text = plain
        # 每条一个取消旗：池子只有一条线程，旧那句还在跑时把它自己的旗 clear 掉，
        # 它就再也看不到「取消」，必须等它整句合成完才轮到新句（表现就是很久没声音）。
        self._cancel = threading.Event()
        self.preparing.emit()
        self._pool.start(_StreamJob(plain, self, self._seq, self._cancel))
        return True

    def stop(self) -> None:
        """停下当前这条（合成中或正在播）；界面靠 `stopped` 收掉那条进度。"""
        self._halt()
        if self._active:
            self._active = False
            self.stopped.emit()

    def _halt(self) -> None:
        """内部刹车：停止拉取、停播、把计数推一格让在跑的合成结果作废。"""
        self._cancel.set()
        self._playback.stop()
        self._seq += 1
        self._announced = False

    @property
    def playing(self) -> bool:
        return self._playback.playing

    # ---- 内部 ----

    def _on_chunk(self, seq: int, pcm: bytes) -> None:
        if seq != self._seq or not self._active:
            return  # 这条已经被掐掉了（停止/换句）：多出来的分片直接丢
        if not self._announced:
            self._announced = True
            self.started.emit(self._text)  # 第一片到了才算真的开口
        self._playback.feed(bytes(pcm))

    def _on_done(self, seq: int) -> None:
        if seq != self._seq or not self._active:
            return
        self._playback.mark_done()

    def _on_failed(self, seq: int, message: str) -> None:
        if seq != self._seq:
            return
        was_announced = self._announced
        self._active = False
        self._playback.stop()
        if was_announced:
            # 已经念了一部分：当作念完收场，别把界面卡在语音条上
            self.finished.emit()
            return
        self.failed.emit(message)

    def _on_progress(self, position: int, duration: int) -> None:
        if self._active:
            self.progress.emit(int(position), int(duration))

    def _on_playback_failed(self, message: str) -> None:
        if not self._active:
            return
        self._active = False
        self._playback.stop()
        self.failed.emit(message)

    def _on_playback_finished(self) -> None:
        if not self._active:
            return
        self._active = False
        self.finished.emit()
