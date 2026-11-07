"""启动时的开场动画。

引擎是独立进程，冷启动实测 5–7 秒。以前的做法是先把人物窗口亮出来，等用户点开聊天才
发现「还在加载」——看着像卡住。现在改成：先只显示这一幕，引擎连上、模型清单到了
才露出人物窗口；那之后再打开聊天窗就是即时的。

三秒多的登场，全是她自己：暖墨底 → 一束斜光扫过 → 她从暗里亮起来 → 名字和那句
「她住在你的桌面上。」落定 → 左下角换成引擎的真实进度。播完就静止在那儿等，不循环、
不拦着人：点击或按 Esc 直接露窗口。
"""

from __future__ import annotations

import time
from pathlib import Path

import PySide6.QtCore as QtCore
import PySide6.QtGui as QtGui
import PySide6.QtWidgets as QtWidgets

TITLE = "Noesia"
KICKER = "AI Companion"
LINE = "她住在你的桌面上。"
SUB = "会聊天，会记事，也会替你动手干活。"
SKIP_HINT = "点击或按 Esc 跳过"

INK_TOP = QtGui.QColor(42, 32, 40)
INK_MID = QtGui.QColor(26, 20, 27)
INK_EDGE = QtGui.QColor(11, 8, 13)
CREAM = QtGui.QColor(244, 232, 226)
ROSE = QtGui.QColor(217, 167, 155)
FAIL = QtGui.QColor(255, 120, 120)

T_BASE = (0.00, 1.00)
T_SWEEP = (0.60, 3.20)
T_FACE = (1.20, 4.20)
T_WORD = (2.60, 4.00)
T_RULE = (3.90, 4.70)
T_LINE = (4.30, 5.20)
T_SUB = (4.80, 5.70)
T_STATUS = (5.40, 6.40)
T_END = 7.20


def _ease(now: float, span: tuple[float, float]) -> float:
    """一段区间的 eased 进度：区间外是 0 / 1，区间内三次方缓出。"""
    start, end = span
    if now <= start:
        return 0.0
    if now >= end:
        return 1.0
    t = (now - start) / (end - start)
    return 1.0 - (1.0 - t) ** 3


class BootSplash(QtWidgets.QWidget):
    """开场一幕：屏幕正中一块面板，五秒演完自己停住。

    只属于启动阶段：`finish()` 之后就销毁，不参与后面的任何逻辑。
    """

    MIN_VISIBLE_MS = int(T_END * 1000)

    skipped = QtCore.Signal()

    def __init__(
        self,
        avatar_path: Path | str | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._step = "正在准备界面…"
        self._failed = False
        self._skipped = False
        self._started = time.monotonic()
        self._dismissing = False
        self._fade_progress = 1.0
        self._fade: QtCore.QVariantAnimation | None = None
        self._avatar = Path(avatar_path) if avatar_path else None
        self._portrait: QtGui.QPixmap | None = None
        self._portrait_size = (0, 0)
        self._grain = self._grain_tile()

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._place_window()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_step(self, text: str) -> None:
        """换一句「现在在做什么」。界面上只留最新一句，不堆历史。"""
        self._step = text
        self.update()

    @property
    def step(self) -> str:
        return self._step

    def mark_failed(self, text: str) -> None:
        """引擎没起来：动画停住，说明原因，让用户知道窗口马上还是会出来。"""
        self._failed = True
        self._step = text
        self._timer.stop()
        self.update()

    def finish(self, then) -> None:
        """收起动画，然后执行 `then`（露出人物窗口）。

        收的时候先把自己淡掉，不是啪一下消失——硬切过去像画面被撕了一下。
        用户按过 Esc 的则不再等最短时长：他已经在催了，再多留一秒都是添堵。
        """
        waited = (time.monotonic() - self._started) * 1000
        delay = 0 if self._skipped else max(0, int(self.MIN_VISIBLE_MS - waited))
        if delay <= 0:
            self._dismiss(then)
            return
        QtCore.QTimer.singleShot(delay, lambda: self._dismiss(then))

    def _dismiss(self, then) -> None:
        """淡出三百来毫秒再交窗口：两个画面之间要有个交代。"""
        if self._dismissing:
            return
        self._dismissing = True
        self._timer.stop()
        fade = QtCore.QVariantAnimation(self)
        fade.setDuration(340)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QtCore.QEasingCurve.Type.InOutSine)
        fade.valueChanged.connect(self._on_fade)
        fade.finished.connect(then)
        self._fade = fade
        fade.start()

    def _on_fade(self, value) -> None:
        self._fade_progress = float(value)
        self.update()

    def skip(self) -> None:
        """点击 / Esc：不再演，立刻交出窗口（引擎还在连就靠聊天窗里的提示兜着）。"""
        if self._skipped:
            return
        self._skipped = True
        self._timer.stop()
        self.skipped.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt
        if event.key() in (
            QtCore.Qt.Key.Key_Escape,
            QtCore.Qt.Key.Key_Space,
            QtCore.Qt.Key.Key_Return,
            QtCore.Qt.Key.Key_Enter,
        ):
            self.skip()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.skip()
        event.accept()

    def _place_window(self) -> None:
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            self.setFixedSize(960, 540)
            return
        area = screen.availableGeometry()
        width = int(min(max(area.width() * 0.38, 760), 1120))
        height = int(round(width * 9 / 16))
        self.setGeometry(
            area.x() + (area.width() - width) // 2,
            area.y() + (area.height() - height) // 2,
            width,
            height,
        )

    def _tick(self) -> None:
        if self._now() >= T_END and self._timer.interval() < 100:
            self._timer.setInterval(120)
        self.update()

    def _now(self) -> float:
        return time.monotonic() - self._started

    @staticmethod
    def _grain_tile() -> QtGui.QPixmap:
        """2×2 的细点：大屏纯渐变会有色带，撒一层极淡的噪点就看不出来了。"""
        tile = QtGui.QPixmap(2, 2)
        tile.fill(QtGui.QColor(0, 0, 0, 0))
        painter = QtGui.QPainter(tile)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(255, 255, 255, 18))
        painter.drawRect(0, 0, 1, 1)
        painter.end()
        return tile

    def _portrait_pixmap(self) -> QtGui.QPixmap | None:
        """把立绘压成「右边和底下自然淡出」的一张图，尺寸变了才重做。"""
        width, height = self.width(), self.height()
        if not width or not height:
            return None
        if self._portrait is not None and self._portrait_size == (width, height):
            return self._portrait
        self._portrait_size = (width, height)
        if self._avatar is None or not self._avatar.is_file():
            self._portrait = None
            return None
        source = QtGui.QPixmap(str(self._avatar))
        if source.isNull():
            self._portrait = None
            return None

        target_w = max(1, int(width * 0.52))
        scaled = source.scaledToWidth(target_w, QtCore.Qt.TransformationMode.SmoothTransformation)
        crop_h = min(scaled.height(), max(1, int(height * 1.06)))
        canvas = QtGui.QPixmap(target_w, crop_h)
        canvas.fill(QtGui.QColor(0, 0, 0, 0))
        painter = QtGui.QPainter(canvas)
        painter.drawPixmap(0, 0, scaled, 0, 0, target_w, crop_h)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationIn)
        right = QtGui.QLinearGradient(0, 0, canvas.width(), 0)
        right.setColorAt(0.00, QtGui.QColor(0, 0, 0, 255))
        right.setColorAt(0.38, QtGui.QColor(0, 0, 0, 255))
        right.setColorAt(0.62, QtGui.QColor(0, 0, 0, 150))
        right.setColorAt(0.92, QtGui.QColor(0, 0, 0, 0))
        painter.fillRect(canvas.rect(), right)
        fade = height * 0.30 + max(0, canvas.height() - height)
        bottom = QtGui.QLinearGradient(0, canvas.height(), 0, max(0.0, canvas.height() - fade))
        bottom.setColorAt(0.00, QtGui.QColor(0, 0, 0, 0))
        bottom.setColorAt(0.30, QtGui.QColor(0, 0, 0, 60))
        bottom.setColorAt(1.00, QtGui.QColor(0, 0, 0, 255))
        painter.fillRect(canvas.rect(), bottom)
        painter.end()
        self._portrait = canvas
        return canvas

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt
        if self._fade_progress >= 1.0:
            painter = QtGui.QPainter(self)
            self._render(painter)
            painter.end()
            return
        shot = QtGui.QPixmap(self.size())
        shot.fill(QtCore.Qt.GlobalColor.transparent)
        inner = QtGui.QPainter(shot)
        self._render(inner)
        inner.end()
        painter = QtGui.QPainter(self)
        painter.setOpacity(max(0.0, self._fade_progress))
        painter.drawPixmap(0, 0, shot)
        painter.end()

    def _render(self, painter: QtGui.QPainter) -> None:
        now = self._now()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        width, height = self.width(), self.height()
        u = width / 960.0
        radius = 12 * u
        frame = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(frame, radius, radius)
        painter.setClipPath(path)

        self._paint_backdrop(painter, width, height, _ease(now, T_BASE))
        self._paint_portrait(painter, now, width, height)
        self._paint_sweep(painter, now, width, height)
        self._paint_copy(painter, now, width, height)
        self._paint_status(painter, now, width, height)

        painter.setClipping(False)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 26), max(1.0, u)))
        painter.drawRoundedRect(frame, radius, radius)

    def _paint_backdrop(self, painter: QtGui.QPainter, width: int, height: int, progress: float) -> None:
        painter.fillRect(self.rect(), INK_EDGE)
        if progress <= 0:
            return
        painter.save()
        painter.setOpacity(progress)
        glow = QtGui.QRadialGradient(width * 0.28, height * 0.30, max(width, height) * 0.85)
        glow.setColorAt(0.00, INK_TOP)
        glow.setColorAt(0.42, INK_MID)
        glow.setColorAt(0.78, INK_EDGE)
        painter.fillRect(self.rect(), glow)
        painter.setOpacity(progress * 0.55)
        painter.fillRect(self.rect(), self._grain)
        painter.restore()

    def _paint_portrait(self, painter: QtGui.QPainter, now: float, width: int, height: int) -> None:
        visible = _ease(now, T_FACE)
        if visible <= 0:
            return
        portrait = self._portrait_pixmap()
        if portrait is None:
            return
        x = int(-width * 0.03)
        y = int(height * 0.02)
        painter.save()
        painter.setOpacity(visible)
        painter.drawPixmap(x, y, portrait)
        shade = int(255 * 0.72 * (1.0 - visible))
        if shade > 0:
            painter.fillRect(
                QtCore.QRect(x, y, portrait.width(), portrait.height()),
                QtGui.QColor(10, 8, 12, shade),
            )
        painter.setOpacity(1.0)
        fade_h = height * 0.34
        settle = QtGui.QLinearGradient(0, height, 0, height - fade_h)
        settle.setColorAt(0.00, QtGui.QColor(11, 8, 13, 255))
        settle.setColorAt(0.45, QtGui.QColor(11, 8, 13, 200))
        settle.setColorAt(1.00, QtGui.QColor(11, 8, 13, 0))
        painter.fillRect(
            QtCore.QRectF(0, height - fade_h, width, fade_h),
            settle,
        )
        painter.restore()

    def _paint_sweep(self, painter: QtGui.QPainter, now: float, width: int, height: int) -> None:
        progress = _ease(now, T_SWEEP)
        if progress <= 0 or progress >= 1:
            return
        painter.save()
        painter.setClipRect(self.rect())
        band_w = width * 0.26
        x = -band_w * 1.4 + progress * (width * 1.8)
        gradient = QtGui.QLinearGradient(x, 0, x + band_w, 0)
        gradient.setColorAt(0.00, QtGui.QColor(255, 240, 235, 0))
        gradient.setColorAt(0.45, QtGui.QColor(255, 240, 235, 30))
        gradient.setColorAt(0.62, QtGui.QColor(255, 240, 235, 8))
        gradient.setColorAt(1.00, QtGui.QColor(255, 240, 235, 0))
        painter.translate(x, 0)
        painter.rotate(8)
        painter.translate(-x, 0)
        painter.fillRect(QtCore.QRectF(x, -height * 0.4, band_w, height * 1.8), gradient)
        painter.restore()

    def _paint_copy(self, painter: QtGui.QPainter, now: float, width: int, height: int) -> None:
        u = width / 960.0
        left = int(width * 0.55)
        box_w = int(width * 0.40)
        title_px = max(30.0, 60.0 * u)
        kicker_px = max(10.0, 11.5 * u)
        line_px = max(14.0, 15.0 * u)
        sub_px = max(11.0, 12.0 * u)
        gap_kicker = 14 * u
        gap_rule = 18 * u
        gap_line = 8 * u
        rule_len = 46 * u

        kicker_font = QtGui.QFont("Microsoft YaHei UI")
        kicker_font.setPixelSize(int(kicker_px))
        kicker_font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, kicker_px * 0.62)

        title_font = QtGui.QFont("Georgia")
        title_font.setItalic(True)
        title_font.setPixelSize(int(title_px))

        tag_font = QtGui.QFont("Microsoft YaHei UI")
        tag_font.setPixelSize(int(kicker_px * 1.05))
        tag_font.setBold(True)
        tag_font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, kicker_px * 0.4)

        line_font = QtGui.QFont("Microsoft YaHei UI")
        line_font.setPixelSize(int(line_px))

        sub_font = QtGui.QFont("Microsoft YaHei UI")
        sub_font.setPixelSize(int(sub_px))

        title_h = QtGui.QFontMetricsF(title_font).height()
        kicker_h = QtGui.QFontMetricsF(kicker_font).height()
        line_h = QtGui.QFontMetricsF(line_font).height()
        sub_h = QtGui.QFontMetricsF(sub_font).height()
        block_h = kicker_h + gap_kicker + title_h + gap_rule + line_h + gap_line + sub_h
        y = (height - block_h) / 2.0

        kicker = _ease(now, (T_WORD[0] - 0.20, T_WORD[0] + 0.55))
        if kicker > 0:
            painter.save()
            painter.setOpacity(kicker)
            painter.setFont(kicker_font)
            painter.setPen(ROSE)
            painter.drawText(
                QtCore.QRectF(left, y + (1 - kicker) * 8 * u, box_w, kicker_h),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                KICKER.upper(),
            )
            painter.restore()
        y += kicker_h + gap_kicker

        word = _ease(now, T_WORD)
        if word > 0:
            painter.save()
            painter.setOpacity(word)
            painter.setFont(title_font)
            painter.setPen(CREAM)
            painter.drawText(
                QtCore.QRectF(left, y + (1 - word) * 10 * u, box_w, title_h),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                TITLE,
            )
            word_w = QtGui.QFontMetricsF(title_font).horizontalAdvance(TITLE)
            painter.setFont(tag_font)
            painter.setPen(ROSE)
            painter.drawText(
                QtCore.QPointF(
                    left + word_w + 12 * u,
                    y + (1 - word) * 10 * u + title_h * 0.74,
                ),
                "AGENT",
            )
            painter.restore()
        y += title_h + gap_rule

        rule = _ease(now, T_RULE)
        if rule > 0:
            painter.save()
            painter.setPen(QtGui.QPen(ROSE, max(1.0, u)))
            painter.drawLine(
                QtCore.QPointF(left, y),
                QtCore.QPointF(left + rule_len * rule, y),
            )
            painter.restore()
        y += gap_rule

        line = _ease(now, T_LINE)
        if line > 0:
            painter.save()
            painter.setOpacity(line)
            painter.setFont(line_font)
            painter.setPen(QtGui.QColor(239, 225, 220))
            painter.drawText(
                QtCore.QRectF(left, y + (1 - line) * 6 * u, box_w, line_h),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                LINE,
            )
            painter.restore()
        y += line_h + gap_line

        sub = _ease(now, T_SUB)
        if sub > 0:
            painter.save()
            painter.setOpacity(sub)
            painter.setFont(sub_font)
            painter.setPen(QtGui.QColor(178, 165, 168))
            painter.drawText(
                QtCore.QRectF(left, y + (1 - sub) * 6 * u, box_w, sub_h),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                SUB,
            )
            painter.restore()

    def _paint_status(self, painter: QtGui.QPainter, now: float, width: int, height: int) -> None:
        appear = 1.0 if self._failed else _ease(now, T_STATUS)
        if appear <= 0:
            return
        u = width / 960.0
        font = QtGui.QFont("Microsoft YaHei UI")
        font.setPixelSize(max(13, int(12.0 * u)))
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.6 * u)
        metrics = QtGui.QFontMetricsF(font)
        x = width * 0.032
        y = height - 18 * u - metrics.height()
        dot_r = 2.2 * u
        dot_gap = 13 * u

        painter.save()
        if self._failed:
            dot_alpha = 230
        else:
            phase = (now % 1.6) / 1.6
            dot_alpha = int(90 + 130 * (1.0 - abs(phase * 2 - 1)))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(FAIL if self._failed else ROSE)
        painter.setOpacity(appear * dot_alpha / 255)
        painter.drawEllipse(QtCore.QPointF(x + dot_r, y + metrics.height() / 2), dot_r, dot_r)

        painter.setOpacity(appear)
        painter.setFont(font)
        painter.setPen(FAIL if self._failed else QtGui.QColor(244, 232, 226, 150))
        painter.drawText(QtCore.QPointF(x + dot_gap, y + metrics.ascent()), self._step)

        if not self._failed and not self._skipped:
            hint = _ease(now, (3.00, 4.00))
            if hint > 0:
                hint_font = QtGui.QFont("Microsoft YaHei UI")
                hint_font.setPixelSize(max(10, int(10.5 * u)))
                hint_metrics = QtGui.QFontMetricsF(hint_font)
                painter.setFont(hint_font)
                painter.setPen(QtGui.QColor(244, 232, 226, int(80 * hint)))
                painter.drawText(
                    QtCore.QPointF(
                        width - width * 0.032 - hint_metrics.horizontalAdvance(SKIP_HINT),
                        height - 18 * u - hint_metrics.descent(),
                    ),
                    SKIP_HINT,
                )
        painter.restore()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)
