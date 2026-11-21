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

import array
import re
import threading
import time

import PySide6.QtCore as QtCore
import PySide6.QtMultimedia as QtMultimedia

from app.speech_stream import TARGET_RATE, SpeechError, stream_pcm

_CODE_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_VERIFIER_FOOTER = re.compile(
    r"^[ \t]*\u26a0\ufe0f?\s*(?:文件修改校验器|File-mutation verifier)[:：][^\n]*"
    r"(?:\n[ \t]*\u2022[^\n]*)*",
    re.M,
)
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MEDIA_LINE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?`?MEDIA:[^\n]*$", re.M)
_MEDIA_TOKEN = re.compile(r"MEDIA:[^\s，。！？；：、]+")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:[^()]|\([^()]*\))*\)")
_URL = re.compile(
    r"https?://(?:www\.)?([0-9A-Za-z.-]+)"
    r"((?::\d+)?(?:[/?#][^\s，。！？；：、（）【】「」()\[\]{}<>\"']*)?)",
    re.IGNORECASE,
)
_IP_HOST = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|localhost", re.IGNORECASE)
_STRIKE = re.compile(r"~~(.+?)~~")
_EMPH = re.compile(r"(?<![0-9A-Za-z])(\*{1,3}|_{1,3})(?=[^\s*_])(.+?)(?<=[^\s*_])\1(?![0-9A-Za-z])")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)
_QUOTE = re.compile(r"^[ \t]*>+[ \t]?", re.M)
_TASK_BOX = re.compile(r"^[ \t]*\[[ xX]\][ \t]+", re.M)
_LIST_MARK = re.compile(r"^[ \t]*(?:[-*+]|(\d{1,2})[.)])[ \t]+", re.M)
_RULE_LINE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_PIPE = re.compile(r"\|")


def _url_to_speech(m: re.Match[str]) -> str:
    host, tail = m.group(1), m.group(2) or ""
    return host + tail if _IP_HOST.fullmatch(host) else host


def _link_text(m: re.Match[str]) -> str:
    return re.sub(r"[\[\]]", "", m.group(1))


def _media_to_speech(m: re.Match[str]) -> str:
    token = m.group(0)
    trail = ""
    while token and token[-1] in ".,;:!?)]":
        trail = token[-1] + trail
        token = token[:-1]
    return " " + trail

_PREROLL_MS = 250  # 蓝牙（A2DP）和刚醒的声卡会吞掉最先写进去的 150–300ms，先拿静音喂饱它
_TAIL_MS = 250  # 声卡排空不代表耳机播完，立刻停会把最后一个字切掉
_BUFFER_MS = 600  # 声卡缓冲：界面偶尔卡一下（重绘、动画）也不能让声卡饿着断出一截空白
_SILENCE_CAP_MS = 5000  # 等合成时喂静音的封顶，接口挂了不至于一直往设备里灌
_FADE_IN_MS = 20  # 从静音直接跳进语音会有“啪”的爆音，前 20ms 拉个斜坡压掉


def _fade_in(pcm: bytes, ms: int = _FADE_IN_MS) -> bytes:
    """给一段 PCM 加线性淡入：静音→语音那个硬跳变就是爆音的来源。只改开头那几个采样，其余原样。"""
    edge = min(len(pcm) // 2, max(1, int(TARGET_RATE * ms / 1000)))
    head = array.array("h", pcm[: edge * 2])
    for i in range(edge):
        head[i] = int(head[i] * i / edge)
    return head.tobytes() + pcm[edge * 2 :]


def speakable(text: str) -> str:
    """把回答洗成能念的纯文字；洗完没内容就返回空串（调用方直接跳过）。

    不截断：回答有多少字就念多少字。以前这里按 max_chars 截到句末，
    长回答会在中途停住、后面整段没声音；超长文本的分段交给 speech_stream。
    """
    cleaned = _CODE_FENCE.sub(" ", text)
    cleaned = _VERIFIER_FOOTER.sub("", cleaned)
    cleaned = _RULE_LINE.sub("", cleaned)
    cleaned = _AUTOLINK.sub(r"\1", cleaned)
    cleaned = _HTML_COMMENT.sub(" ", cleaned)
    cleaned = _HTML_TAG.sub(" ", cleaned)
    cleaned = _MEDIA_LINE.sub("", cleaned)
    cleaned = _MEDIA_TOKEN.sub(_media_to_speech, cleaned)
    cleaned = _IMAGE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _LINK.sub(_link_text, cleaned)
    cleaned = _STRIKE.sub(r"\1", cleaned)
    cleaned = _EMPH.sub(r"\2", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _QUOTE.sub("", cleaned)
    cleaned = _LIST_MARK.sub(r"\1 ", cleaned)
    cleaned = _TASK_BOX.sub("", cleaned)
    cleaned = _PIPE.sub(" ", cleaned)
    cleaned = _URL.sub(_url_to_speech, cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"^[ \t]+", "", cleaned, flags=re.M)
    cleaned = re.sub(r"\s+([，。！？；：、])", r"\1", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    return cleaned


class _StreamJob(QtCore.QRunnable):
    """在工作线程里跑 `stream_pcm`，每拿到一片就发信号交给主线程。"""

    def __init__(self, text: str, owner: "Speaker", seq: int, cancel: threading.Event) -> None:
        super().__init__()
        self._text, self._owner, self._seq, self._cancel = text, owner, seq, cancel

    @QtCore.Slot()
    def run(self) -> None:
        try:
            for chunk in stream_pcm(self._text):
                if self._cancel.is_set():
                    return
                if chunk:
                    self._owner._chunk_internal.emit(self._seq, chunk)
        except SpeechError as exc:
            self._owner._failed_internal.emit(self._seq, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._owner._failed_internal.emit(self._seq, f"合成失败：{exc}")
            return
        self._owner._done_internal.emit(self._seq)


class _StreamPlayback(QtCore.QObject):
    """边收边播：收到一片喂一片，设备排空 + 合成结束才算播完。"""

    progress = QtCore.Signal(int, int)
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
        self._fed_bytes = 0
        self._preroll_bytes = 0
        self._drained_at = 0.0
        self._silent_head = True
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
        if self._silent_head:
            pcm = _fade_in(pcm)
            self._silent_head = False
        self._pending += pcm
        self._fed_bytes += len(pcm)
        self._pump()

    def arm(self) -> None:
        """合成还要等半秒：先把声卡开起来让设备醒着，蓝牙起振吃掉的是静音，不是第一句。"""
        if self._sink is None:
            self._open(announce=False)

    def mark_done(self) -> None:
        self._synth_done = True
        if self._sink is None:
            self.stop()
            self.finished.emit()
            return
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
        self._preroll_bytes = 0
        self._drained_at = 0.0
        self._silent_head = True
        self._synth_done = False


    def _open(self, announce: bool = True) -> bool:
        device = QtMultimedia.QMediaDevices.defaultAudioOutput()
        sink = QtMultimedia.QAudioSink(device, self._format, self)
        sink.setVolume(1.0)
        sink.setBufferSize(max(self._format.bytesForDuration(_BUFFER_MS * 1000), 8192))
        io = sink.start()
        if io is None:
            # 打不开就把这个 sink 收掉：留着它每片都会再建一个（`feed` 每次重试 `_open`），越堆越多。
            sink.deleteLater()
            if announce:
                self.failed.emit("打不开音频输出设备（可能被别的程序占用）")
            return False
        self._sink = sink
        self._io = io
        silence = bytes(self._format.bytesForDuration(_PREROLL_MS * 1000))
        self._pending += silence
        self._preroll_bytes = len(silence)
        self._fed_bytes += len(silence)
        self._ticker.start()
        self._pump()
        return True

    def _pump(self) -> None:
        if self._sink is None or self._io is None:
            return
        if not self._pending and not self._synth_done and self._silent_head:
            cap = self._format.bytesForDuration(_SILENCE_CAP_MS * 1000)
            if self._preroll_bytes < cap:
                silence = bytes(self._format.bytesForDuration(30_000))
                self._pending += silence
                self._preroll_bytes += len(silence)
                self._fed_bytes += len(silence)
        if self._pending:
            written = self._io.write(bytes(self._pending))
            if written > 0:
                del self._pending[:written]
            elif written < 0:
                self.stop()
                self.failed.emit("写不进音频设备，这一句没念出来")
                return
        played_us = int(self._sink.processedUSecs())
        preroll_us = int(self._format.durationForBytes(self._preroll_bytes))
        self.progress.emit(max(0, int((played_us - preroll_us) / 1000)), 0)
        if not self._synth_done or self._pending:
            return
        fed_us = int(self._format.durationForBytes(self._fed_bytes))
        if played_us < fed_us - 20_000:
            self._drained_at = 0.0
        elif not self._drained_at:
            self._drained_at = time.monotonic()
        elif (time.monotonic() - self._drained_at) * 1000.0 >= _TAIL_MS:
            self.stop()
            self.finished.emit()


class Speaker(QtCore.QObject):
    """一条回答一条语音，一条接一条念；新的一条来了就打断旧的。"""

    preparing = QtCore.Signal()
    started = QtCore.Signal(str)
    progress = QtCore.Signal(int, int)
    stopped = QtCore.Signal()
    finished = QtCore.Signal()
    failed = QtCore.Signal(str)

    _chunk_internal = QtCore.Signal(int, object)
    _done_internal = QtCore.Signal(int)
    _failed_internal = QtCore.Signal(int, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._chunk_internal.connect(self._on_chunk)
        self._done_internal.connect(self._on_done)
        self._failed_internal.connect(self._on_failed)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._seq = 0
        self._active = False
        self._cancel = threading.Event()
        self._text = ""
        self._announced = False

        self._playback = _StreamPlayback(self)
        self._playback.progress.connect(self._on_progress)
        self._playback.failed.connect(self._on_playback_failed)
        self._playback.finished.connect(self._on_playback_finished)


    def speak(self, text: str) -> bool:
        """念这段文字。返回 False 表示没有可念的内容（已静默跳过）。"""
        from app.speech_prefs import KEYED_PROVIDERS, missing_key_message, read_api_key
        from app.speech_prefs import current as voice_prefs

        chosen = voice_prefs()
        if not chosen["enabled"]:
            return False
        if chosen["provider"] in KEYED_PROVIDERS and not read_api_key(chosen["provider"]):
            self.failed.emit(missing_key_message(chosen["provider"]))
            return True
        plain = speakable(text)
        if not plain:
            return False
        self._halt()
        self._active = True
        self._announced = False
        self._text = plain
        self._cancel = threading.Event()
        self.preparing.emit()
        self._playback.arm()
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


    def _on_chunk(self, seq: int, pcm: bytes) -> None:
        if seq != self._seq or not self._active:
            return
        if not self._announced:
            self._announced = True
            self.started.emit(self._text)
        self._playback.feed(bytes(pcm))

    def _on_done(self, seq: int) -> None:
        if seq != self._seq or not self._active:
            return
        self._playback.mark_done()

    def _on_failed(self, seq: int, message: str) -> None:
        if seq != self._seq:
            return
        self._active = False
        self._playback.stop()
        self.stopped.emit()
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
