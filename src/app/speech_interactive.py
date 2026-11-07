"""语音互动：一边合成一边出声，同一份 PCM 还能同时喂给数字人。

和纯语音（`speech_speaker`）的唯一区别是等不等：那边要等整段 wav 合成完才开声，
这边第一片 PCM 到手就喂声卡，所以首字延迟约等于 TTS 首包（豆包实测 0.5s）。

线程划分和纯语音一致：合成在工作线程（不许碰 Qt 对象），声卡和定时器在主线程，
跨线程只用信号。`on_chunk` 回调也在主线程被调，拿到的就是喂给声卡的那份字节，
数字人直接转发同一份，不需要再合成第二次。
"""

from __future__ import annotations

import logging
import time
from collections import deque

import PySide6.QtCore as QtCore
import PySide6.QtMultimedia as QtMultimedia

from app.speech_prefs import SpeechError
from app.speech_stream import TARGET_RATE, stream_pcm

logger = logging.getLogger(__name__)

FRAME_BYTES = TARGET_RATE * 2 // 50


class _Puller(QtCore.QRunnable):
    """工作线程：把 `stream_pcm` 的分片一片片丢回主线程，随时可被打断。"""

    def __init__(self, text: str, seq: int, owner: "StreamSpeaker") -> None:
        super().__init__()
        self._text, self._seq, self._owner = text, seq, owner

    @QtCore.Slot()
    def run(self) -> None:
        try:
            for chunk in stream_pcm(self._text):
                if not self._owner.alive(self._seq):
                    return
                self._owner._chunk.emit(self._seq, chunk)
        except SpeechError as exc:
            self._owner._failed.emit(self._seq, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._owner._failed.emit(self._seq, f"合成失败：{exc}")
            return
        self._owner._pull_done.emit(self._seq)


class StreamSpeaker(QtCore.QObject):
    """一段话一个流：到了就喂声卡，念完报 finished，被叫停报 stopped。"""

    started = QtCore.Signal(str)
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal()
    stopped = QtCore.Signal()
    failed = QtCore.Signal(str)

    _chunk = QtCore.Signal(int, bytes)
    _pull_done = QtCore.Signal(int)
    _failed = QtCore.Signal(int, str)

    def __init__(
        self,
        on_chunk=None,
        parent: QtCore.QObject | None = None,
        limit_fn=None,
        quiet_fn=None,
        idle_fn=None,
        play_local: bool = True,
    ) -> None:
        super().__init__(parent)
        self._limit_fn = limit_fn
        self._limit_deadline = 0.0
        self._limit_off = limit_fn is None
        self._play_local = play_local
        self._quiet_fn = quiet_fn
        self._idle_fn = idle_fn
        self._play_start = None
        self._on_chunk = on_chunk
        self._chunk.connect(self._on_chunk_internal)
        self._pull_done.connect(self._on_pull_done)
        self._failed.connect(self._on_failed_internal)

        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(2)

        self._seq = 0
        self._active = False
        self._pulling = False
        self._announced = False
        self._text = ""
        self._pending: deque[bytes] = deque()
        self._sink: QtMultimedia.QAudioSink | None = None
        self._io = None
        self._fed_bytes = 0
        self._total_bytes = 0
        self._fmt: QtMultimedia.QAudioFormat | None = None
        self._carry = b""
        self._write_stalls = 0

        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(20)
        self._ticker.timeout.connect(self._pump)


    @property
    def playing(self) -> bool:
        return self._active

    def alive(self, seq: int) -> bool:
        """工作线程用：这条流还算数吗（被打断/换条就失效）。"""
        return self._active and seq == self._seq

    def speak(self, text: str) -> bool:
        """开始念；返回 False 表示没问题但没内容。"""
        if not text or not text.strip():
            return False
        self._stop(silent=True)
        self._seq += 1
        seq = self._seq
        if not self._play_local:
            self._fmt = None
            self._active = True
            self._pulling = True
            self._pending.clear()
            self._fed_bytes = 0
            self._total_bytes = 0
            self._play_start = None
            self._announced = False
            self._text = text
            self._ticker.start()
            self._pool.start(_Puller(text, seq, self))
            return True
        self._limit_off = self._limit_fn is None
        self._limit_deadline = time.monotonic() + 3.0
        fmt = QtMultimedia.QAudioFormat()
        fmt.setSampleRate(TARGET_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QtMultimedia.QAudioFormat.SampleFormat.Int16)
        device = QtMultimedia.QMediaDevices.defaultAudioOutput()
        sink = QtMultimedia.QAudioSink(device, fmt, self)
        sink.setVolume(1.0)
        sink.setBufferSize(max(fmt.bytesForDuration(100_000), 4800))
        io = sink.start()
        if io is None:
            sink.deleteLater()
            self.failed.emit("打不开音频输出设备（可能被别的程序占用）")
            return False
        self._fmt, self._sink, self._io = fmt, sink, io
        self._active = True
        self._pulling = True
        self._pending.clear()
        self._fed_bytes = 0
        self._total_bytes = 0
        self._announced = False
        self._text = text
        self._ticker.start()
        self._pool.start(_Puller(text, seq, self))
        return True

    def stop(self) -> None:
        """立刻停：不再拉合成、清空没播的分片、关声卡。"""
        self._stop(silent=False)

    def _stop(self, silent: bool) -> None:
        was_active = self._active
        self._active = False
        self._pulling = False
        self._ticker.stop()
        self._pending.clear()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = None
        self._io = None
        self._fed_bytes = 0
        self._announced = False
        self._carry = b""
        if was_active and not silent:
            self.stopped.emit()


    def _on_chunk_internal(self, seq: int, pcm: bytes) -> None:
        if not self.alive(seq) or not pcm:
            return
        if not self._announced:
            self._announced = True
            self.started.emit(self._text)
        self._pending.append(pcm)
        self._total_bytes += len(pcm)
        if self._on_chunk is not None:
            self._forward(pcm)
        self._pump()

    def _forward(self, pcm: bytes) -> None:
        """给数字人的字节要凑成整 20ms：分片大小不固定，这里攒齐再发。"""
        if self._on_chunk is None:
            return
        self._carry += pcm
        while len(self._carry) >= FRAME_BYTES:
            self._on_chunk(self._carry[:FRAME_BYTES])
            self._carry = self._carry[FRAME_BYTES:]

    def _on_pull_done(self, seq: int) -> None:
        if seq == self._seq and self._active:
            self._pulling = False
            self._pump()

    def _fail_local(self, message: str) -> None:
        """写完不进去这类本地故障：收干净资源，把原因说出去（不能让调用方一直等）。"""
        self._ticker.stop()
        sink, self._sink = self._sink, None
        self._io = None
        self._active = False
        self._pending.clear()
        if sink is not None:
            try:
                sink.stop()
            except Exception:  # noqa: BLE001
                logger.debug("[语音] 关声卡失败", exc_info=True)
            sink.deleteLater()
        self.failed.emit(message)

    def _on_failed_internal(self, seq: int, message: str) -> None:
        if seq != self._seq:
            return
        self.stop()
        self.failed.emit(message)

    def _allowed_bytes(self) -> int | None:
        """本地能播到哪：云端回传的进度减去 60ms 缓冲（防它一阵一阵地来）。"""
        if self._limit_off:
            return None
        limit_fn = self._limit_fn
        got = int((limit_fn() if limit_fn is not None else 0) or 0)
        if got <= 0:
            if time.monotonic() < self._limit_deadline:
                return 0
            self._limit_off = True
            return None
        return max(0, got - int(TARGET_RATE * 2 * 0.06))

    def _pump(self) -> None:
        if not self._active:
            return
        if not self._play_local:
            now = time.monotonic()
            dur = self._total_bytes / (TARGET_RATE * 2) if self._total_bytes else 0.0
            got = int(self._limit_fn() or 0) if self._limit_fn is not None else 0
            if self._play_start is None and (got > 0 or not self._pulling):
                self._play_start = now
            elapsed = (now - (self._play_start or now)) if self._play_start else 0.0
            done = min(int(elapsed * TARGET_RATE * 2), self._total_bytes)
            self._fed_bytes = done
            self.progress.emit(int(elapsed * 1000), 0)
            quiet = float(self._quiet_fn() or 0) if self._quiet_fn is not None else 0.0
            idle = float(self._idle_fn() or 0) if self._idle_fn is not None else 0.0
            if self._play_start is not None and not self._pulling:
                why = ""
                if dur == 0:
                    why = "空句子"
                elif quiet >= 1.5 and elapsed >= 0.2:
                    why = "远端静了 %.1fs" % quiet
                elif dur and elapsed >= dur + 2.0:
                    why = "时间到句长 +2s"
                elif idle >= 5.0 and elapsed >= 0.5:
                    why = "云端断流 %.1fs" % idle
                if not why and elapsed > 180:
                    why = "合成卡住兜底"
                if why:
                    logger.info(
                        "[语音] 收尾（%s）：已知句长 %.1fs，已过 %.1fs，远端静 %.1fs，断流 %.1fs，合成完=%s",
                        why, dur, elapsed, quiet, idle, not self._pulling,
                    )
                    self._ticker.stop()
                    self._active = False
                    self.finished.emit()
            return
        if self._sink is None or self._io is None or self._fmt is None:
            return
        allowed = self._allowed_bytes()
        while self._pending:
            if allowed is not None and self._fed_bytes >= allowed:
                break
            written = self._io.write(self._pending[0])
            if written <= 0:
                self._write_stalls += 1
                if self._write_stalls >= 150:
                    logger.warning("[语音] 声卡写不进去（%s 字节），收尾", written)
                    self._fail_local("声音送不到扬声器：设备可能被拔掉或被别的程序占了")
                    return
                break
            self._write_stalls = 0
            self._pending[0] = self._pending[0][written:]
            self._fed_bytes += written
            if not self._pending[0]:
                self._pending.popleft()
        fed_us = int(self._fmt.durationForBytes(self._fed_bytes))
        played_us = int(self._sink.processedUSecs())
        self.progress.emit(int(played_us / 1000), int(fed_us / 1000))
        if not self._pulling and not self._pending and played_us + 20_000 >= fed_us:
            self._ticker.stop()
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
            self._io = None
            self._active = False
            self.finished.emit()

