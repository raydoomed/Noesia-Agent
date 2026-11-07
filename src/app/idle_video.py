"""待机视频：Qt6 播放 + numpy 逐帧绿幕抠像。

`assets/idle_video.mp4` 是 720×960、HEVC、**没有 alpha** 的绿幕（ffprobe 实测 24fps / 241 帧）。
这里用 `QMediaPlayer` + `QVideoSink` 取帧，按 `greenness = G − max(R, B)` 做软过渡抠像并去溢色，
输出带 alpha 的 `QImage` 交给窗口直接画。

为什么运行时抠而不是预压 alpha 序列：实测取帧约 21fps、抠像中位 11ms/帧，24fps 的预算是 41.7ms，
只占 26.5% —— 省掉几十 MB 的预压资源和一整套转码流程。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import numpy as np
import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtMultimedia as QtMultimedia

KEY_LOW = 10.0
KEY_HIGH = 60.0


def key_bgra(buffer: np.ndarray, *, low: float = KEY_LOW, high: float = KEY_HIGH) -> np.ndarray:
    """就地给 BGRA 像素加 alpha 并去溢色，返回同一块内存。

    buffer 形状 (H, W, 4)、dtype uint8，通道顺序 BGRA —— Qt 的 `Format_ARGB32`
    在小端机器上内存布局就是 BGRA。
    """
    blue = buffer[..., 0].astype(np.float32)
    green = buffer[..., 1].astype(np.float32)
    red = buffer[..., 2].astype(np.float32)

    greenness = green - np.maximum(red, blue)
    ramp = (greenness - low) * (255.0 / (high - low))
    buffer[..., 3] = (255.0 - np.clip(ramp, 0.0, 255.0)).astype(np.uint8)

    buffer[..., 1] = np.minimum(green, np.maximum(red, blue)).astype(np.uint8)
    return buffer


def key_image(image: QtGui.QImage, *, low: float = KEY_LOW, high: float = KEY_HIGH) -> QtGui.QImage:
    """给一张 ARGB32 图抠像（就地改），返回同一张图。"""
    if image.format() != QtGui.QImage.Format.Format_ARGB32:
        image = image.convertToFormat(QtGui.QImage.Format.Format_ARGB32)

    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = np.frombuffer(image.bits(), dtype=np.uint8, count=stride * height)
    rows = raw.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
    key_bgra(rows, low=low, high=high)
    return image


class IdleVideoPlayer(QtCore.QObject):
    """循环播放待机视频，逐帧抠像后以 `frame_ready(QImage)` 发出。"""

    frame_ready = QtCore.Signal(QtGui.QImage)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        path: Path | str,
        parent: QtCore.QObject | None = None,
        *,
        low: float = KEY_LOW,
        high: float = KEY_HIGH,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self._low = low
        self._high = high
        self._frames = 0
        self._key_ms: list[float] = []
        self._target: QtCore.QSize | None = None

        self._sink = QtMultimedia.QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)

        self._player = QtMultimedia.QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._player.setLoops(QtMultimedia.QMediaPlayer.Loops.Infinite)
        self._player.setAudioOutput(None)  # pyright: ignore[reportArgumentType]
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_status)


    def set_target_size(self, size: QtCore.QSize) -> None:
        """显示尺寸：帧先缩到这里再抠像，抠像和绘制都按窗口大小算，快得多。"""
        self._target = size if size.isValid() and not size.isEmpty() else None

    def start(self) -> bool:
        if not self.path.is_file():
            self.failed.emit(f"待机视频不存在：{self.path}")
            return False
        self._player.setSource(QtCore.QUrl.fromLocalFile(str(self.path)))
        self._player.play()
        return True

    def stop(self) -> None:
        self._player.stop()

    def pause(self) -> None:
        """互动画面要占这块显示位：暂停解码，别白烧 CPU。"""
        self._player.pause()

    def resume(self) -> None:
        """互动会话结束：待机视频接回来。"""
        self._player.play()

    @property
    def stats(self) -> dict[str, float]:
        """最近若干帧的抠像耗时，用来核对 24fps 预算。"""
        recent = self._key_ms[-120:]
        return {
            "frames": self._frames,
            "median_key_ms": round(statistics.median(recent), 2) if recent else 0.0,
        }


    def _on_error(self, error, message: str) -> None:  # noqa: ANN001
        self.failed.emit(f"播放失败（{error}）：{message}")

    def _on_status(self, status) -> None:  # noqa: ANN001
        if status in (
            QtMultimedia.QMediaPlayer.MediaStatus.InvalidMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.NoMedia,
        ):
            self.failed.emit(f"视频无法播放：{self.path}")

    def _on_frame(self, frame) -> None:  # noqa: ANN001 (QVideoFrame)
        image = frame.toImage()
        if image.isNull():
            return
        if self._target is not None and image.size() != self._target:
            image = image.scaled(self._target, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.FastTransformation)
        started = time.perf_counter()
        keyed = key_image(image, low=self._low, high=self._high)
        self._key_ms.append((time.perf_counter() - started) * 1000.0)
        if len(self._key_ms) > 600:
            del self._key_ms[:-600]
        self._frames += 1
        self.frame_ready.emit(keyed)
