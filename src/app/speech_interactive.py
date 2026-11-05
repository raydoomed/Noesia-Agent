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

from PySide6 import QtCore, QtMultimedia

from app.speech_prefs import SpeechError
from app.speech_stream import TARGET_RATE, stream_pcm

logger = logging.getLogger(__name__)

FRAME_BYTES = TARGET_RATE * 2 // 50  # 20ms 一帧：960 字节，Vidu 组件版要求的节奏


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
        except Exception as exc:  # noqa: BLE001 — 线程里兜底，别静默死掉
            self._owner._failed.emit(self._seq, f"合成失败：{exc}")
            return
        self._owner._pull_done.emit(self._seq)


class StreamSpeaker(QtCore.QObject):
    """一段话一个流：到了就喂声卡，念完报 finished，被叫停报 stopped。"""

    started = QtCore.Signal(str)  # 开始出声的纯文字
    progress = QtCore.Signal(int, int)  # 已播毫秒 / 已知总毫秒（流式，总长会变）
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
        # limit_fn：本地声音能播到第几个字节的"许可证"。
        # 云端把我们推上去的音频原样回传，回来的那份有毛刺不能听（实测每秒一下断点），
        # 但它告诉我们"云端现在念到哪了"。所以：播我们自己的干净 PCM，
        # 播到"云端也已经回传到这个位置"为止 —— 声音和画面同一条时间轴，且没有毛刺。
        self._limit_fn = limit_fn
        self._limit_deadline = 0.0
        self._limit_off = limit_fn is None
        self._play_local = play_local
        self._quiet_fn = quiet_fn
        # idle_fn：云端多久没回任何一个字节（连静音都不来）。安静 ≠ 断流：
        # 前者是念完了，后者是 RTC 那头没数据了，得当场收尾，别让球卡在「播放中」。
        self._idle_fn = idle_fn
        self._play_start = None
        self._on_chunk = on_chunk  # 给数字人用的转发钩子（主线程调用）
        self._chunk.connect(self._on_chunk_internal)
        self._pull_done.connect(self._on_pull_done)
        self._failed.connect(self._on_failed_internal)

        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._seq = 0
        self._active = False
        self._pulling = False
        self._pending: deque[bytes] = deque()
        self._sink: QtMultimedia.QAudioSink | None = None
        self._io = None
        self._fed_bytes = 0
        self._total_bytes = 0
        self._fmt: QtMultimedia.QAudioFormat | None = None
        self._carry = b""  # 转发给数字人时攒着的半个 20ms 帧
        self._write_stalls = 0  # 声卡连续几次一个字节都不收（写不进 = 设备没了，不能一直等）

        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(20)  # 和 Vidu 的分片节奏一致
        self._ticker.timeout.connect(self._pump)

    # ---- 对外 ----

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
        self._stop(silent=True)  # 换新一句：只是清场，别对外说"念完了"
        self._seq += 1
        seq = self._seq
        if not self._play_local:
            # 声音交给 RTC：远端音频和画面是同一个流、同一个抖动缓冲，同步由 SDK 保证，
            # 我们只负责把 PCM 推上去驱动口型。别的项目（网页 + ali rtc sdk）就是这么做的。
            self._fmt = None
            self._active = True
            self._pulling = True
            self._pending.clear()
            self._fed_bytes = 0
            self._total_bytes = 0
            self._play_start = None
            self.started.emit(text)
            self._ticker.start()
            self._pool.start(_Puller(text, seq, self))
            return True
        self._limit_off = self._limit_fn is None
        self._limit_deadline = time.monotonic() + 3.0  # 云端 3 秒还没回音频，就别等了
        fmt = QtMultimedia.QAudioFormat()
        fmt.setSampleRate(TARGET_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QtMultimedia.QAudioFormat.SampleFormat.Int16)
        device = QtMultimedia.QMediaDevices.defaultAudioOutput()
        sink = QtMultimedia.QAudioSink(device, fmt, self)
        sink.setVolume(1.0)
        sink.setBufferSize(max(fmt.bytesForDuration(100_000), 4800))  # 缓冲越小，声音越贴画面
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
        self.started.emit(text)
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
        self._carry = b""  # 上一句不足 20ms 的残帧不能拼到下一句开头（口型起点会错位）
        if was_active and not silent:
            self.stopped.emit()

    # ---- 内部 ----

    def _on_chunk_internal(self, seq: int, pcm: bytes) -> None:
        if not self.alive(seq) or not pcm:
            return
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
        # `_active` 也要看：按下停止之后，工作线程的完成通知还可能晚一步投递过来。
        # 不看的话会再走一遍收尾，界面先收到 `stopped` 又收到 `finished`
        # （互动档按秒计费，那一下等于把会话又挂住几秒）。
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
            except Exception:  # noqa: BLE001 - 设备已经没了，停失败不影响收尾
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
        got = int(self._limit_fn() or 0)
        if got <= 0:
            if time.monotonic() < self._limit_deadline:
                return 0
            self._limit_off = True
            return None
        return max(0, got - int(TARGET_RATE * 2 * 0.06))

    def _pump(self) -> None:
        if not self._active:
            return  # 已经停止/收尾：晚到的定时器和完成回调都别再往下走（会重复发 finished）
            # 声音由 RTC 出：进度按时间走（远端回传会一路多送静音，不能当进度用），
            # 收尾看"远端还响不响"（她真的念完了才安静）。
            now = time.monotonic()
            dur = self._total_bytes / (TARGET_RATE * 2) if self._total_bytes else 0.0
            got = int(self._limit_fn() or 0) if self._limit_fn is not None else 0
            if self._play_start is None and (got > 0 or not self._pulling):
                self._play_start = now  # 第一份远端音频到了 = 她开口了
            elapsed = (now - (self._play_start or now)) if self._play_start else 0.0
            done = min(int(elapsed * TARGET_RATE * 2), self._total_bytes)
            self._fed_bytes = done
            # 互动档给不出可信的"总时长"（实际念多久由 Vidu 决定，和 TTS 的 PCM 长度不一样），
            # 硬给一个就会出现 0:06/0:04 这种；传 0 = 只显示已播时间 + 不确定进度条。
            self.progress.emit(int(elapsed * 1000), 0)
            quiet = float(self._quiet_fn() or 0) if self._quiet_fn is not None else 0.0
            idle = float(self._idle_fn() or 0) if self._idle_fn is not None else 0.0
            # 收尾：① TTS 合成完（长度才是真的）② 真的听见过她出声
            #       ③ 远端静了 1.5 秒（句中停顿一般不到 1 秒半）或 时间已过句长 +2 秒
            #       ④ 云端连帧都不回了（断流）：安静和断流不是一回事，前者是念完，后者是没数据
            # 每一条都写日志，下次出问题能直接从日志看出是哪条提前收的。
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
                # 声卡缓冲满时会返回 0，下次再来是对的；但设备被拔掉/被独占/切到无效设备
                # 时它会**一直**返回 0 或 -1，那时 `_pending` 永远不空、`played_us` 不前进、
                # `finished` 也不发：语音条永久停在「正在朗读」。连续 3 秒不进去就当场收尾。
                self._write_stalls += 1
                if self._write_stalls >= 150:  # 20ms × 150 = 3 秒
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
        # 数据拉完了、缓冲喂空了、设备也真的播到头，才算念完（不提前掐尾巴）
        if not self._pulling and not self._pending and played_us + 20_000 >= fed_us:
            self._ticker.stop()
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
            self._io = None
            self._active = False
            self.finished.emit()

