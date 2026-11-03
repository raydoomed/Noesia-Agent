"""语音球：按住说话时随声音起伏的那颗球。

四种样子，对应四个状态：

- `idle` 待机：彩色的光在球里慢慢转，下方一行「按住说话」
- `recording` 录音中：球随音量胀缩，光转得快，外圈弧也跟着跑，下方「在听你说…」
- `thinking` 识别中：光慢下来，外圈弧加速绕圈，下方「正在识别…」
- `error` 失败：整颗转红，下方说清哪里不对

球里不放图标：状态全靠颜色、转速和下面那行字交代，中间塞个麦克风线稿反而把
渐变挡住了。音量直接映射到半径，不做「阈值才动」的处理——说话小声也该有反应，
不然用户会以为麦没工作。
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

# 球里那几团光：粉 / 紫 / 蓝 / 橙。Screen 模式叠在一起就是那种中间烧白的花球
_SIRI = (
    QtGui.QColor("#ff6b9d"),
    QtGui.QColor("#b98cff"),
    QtGui.QColor("#5ec8ff"),
    QtGui.QColor("#ffb060"),
)
_DANGER_GLOW = (
    QtGui.QColor("#ff6b6b"),
    QtGui.QColor("#ff9a6b"),
    QtGui.QColor("#ff4d7d"),
    QtGui.QColor("#ffb06b"),
)

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_THINKING = "thinking"
STATE_ERROR = "error"

_LABELS = {
    STATE_IDLE: "按住说话",
    STATE_RECORDING: "在听你说…",
    STATE_THINKING: "正在识别…",
    STATE_ERROR: "没听清，再来一次",
}


class VoiceOrb(QtWidgets.QWidget):
    """一个会随音量起伏的球。按下/松开的判定在这里发信号，外层只管连。"""

    pressed = QtCore.Signal()
    released = QtCore.Signal()

    SIZE = 94       # 画布宽：够放下下面那行字
    ORB = 71        # 球所在方框的高
    LABEL_H = 23    # 下面那行字占的高

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.ORB + self.LABEL_H + 6)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip("按住说话，松手结束")

        self._state = STATE_IDLE
        self._target = 0.0     # 目标音量 0..1
        self._level = 0.0      # 平滑后的显示值，避免每帧跳
        self._phase = 0.0
        self._holding = False

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)  # 60fps，跟采集的取数节奏一致
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---------------------------------------------------------- 外部接口

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        if state != STATE_RECORDING:
            self._target = 0.0
        self.update()

    def state(self) -> str:
        return self._state

    def set_level(self, value: float) -> None:
        """0..1。只在录音中生效。"""
        if self._state == STATE_RECORDING:
            self._target = max(0.0, min(1.0, float(value)))

    # ---------------------------------------------------------- 交互

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == QtCore.Qt.LeftButton:
            self._holding = True
            self.pressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.LeftButton and self._holding:
            self._holding = False
            self.released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------- 动画

    def _tick(self) -> None:
        # 快上慢下：声音一停球立刻缩回去，但不会抖
        rate = 0.35 if self._target > self._level else 0.18
        self._level += (self._target - self._level) * rate
        if self._state == STATE_RECORDING:
            # 说话时转得快，音量越大越快——球「活」起来靠的就是这个
            self._phase = (self._phase + 0.05 + 0.07 * self._level) % math.tau
        elif self._state == STATE_THINKING:
            self._phase = (self._phase + 0.06) % math.tau
        elif self._state == STATE_ERROR:
            self._phase = (self._phase + 0.02) % math.tau
        else:
            self._phase = (self._phase + 0.011) % math.tau
        self.update()

    # ---------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)

        center = QtCore.QPointF(self.SIZE / 2, self.ORB / 2)
        base = self.ORB * 0.38          # 主球半径：留出下面那行字的位置，光晕别压到字上
        level = self._level

        if self._state == STATE_RECORDING:
            radius = base * (1.0 + 0.13 * level)
        elif self._state == STATE_THINKING:
            radius = base * (0.96 + 0.04 * math.sin(self._phase * 0.9))
        elif self._state == STATE_ERROR:
            radius = base * 0.94
        else:
            radius = base * (0.97 + 0.03 * math.sin(self._phase))

        colors = _DANGER_GLOW if self._state == STATE_ERROR else _SIRI

        # 球外那层柔光：光斑溢出来的余晖，让球不像贴图一样浮在底上
        self._paint_halo(painter, center, radius, colors, level)
        # 外圈三段弧：一圈完整圆环太死板，断成三段才像在转
        self._paint_arcs(painter, center, radius)
        self._paint_ball(painter, center, radius, colors, level)
        self._paint_label(painter)

    def _paint_halo(self, painter, center, radius, colors, level) -> None:
        spread = radius * (1.24 + 0.16 * level)
        for index, color in enumerate(colors):
            angle = self._phase + index * math.tau / len(colors)
            offset = radius * 0.30
            spot = QtCore.QPointF(
                center.x() + math.cos(angle) * offset,
                center.y() + math.sin(angle) * offset,
            )
            gradient = QtGui.QRadialGradient(spot, spread)
            gradient.setColorAt(0.0, QtGui.QColor(color.red(), color.green(), color.blue(), 70))
            gradient.setColorAt(0.55, QtGui.QColor(color.red(), color.green(), color.blue(), 26))
            gradient.setColorAt(1.0, QtGui.QColor(color.red(), color.green(), color.blue(), 0))
            painter.setBrush(QtGui.QBrush(gradient))
            painter.drawEllipse(center, spread, spread)
        painter.setPen(QtCore.Qt.NoPen)

    def _paint_arcs(self, painter, center, radius) -> None:
        """三段断续的细弧绕着球跑，各走各的速度。"""
        if self._state == STATE_RECORDING:
            specs = ((1.06, 78, 1.5, 190), (1.14, 52, -1.0, 130), (1.21, 30, 0.6, 82))
        elif self._state == STATE_THINKING:
            specs = ((1.06, 96, 2.4, 190), (1.14, 60, -1.7, 125), (1.21, 34, 1.1, 76))
        elif self._state == STATE_ERROR:
            specs = ((1.06, 60, 0.2, 120), (1.14, 40, -0.15, 76), (1.21, 24, 0.1, 44))
        else:
            specs = ((1.06, 62, 0.30, 132), (1.14, 42, -0.20, 88), (1.21, 26, 0.12, 54))

        painter.setBrush(QtCore.Qt.NoBrush)
        for index, (scale, span, speed, alpha) in enumerate(specs):
            r = radius * scale
            rect = QtCore.QRectF(center.x() - r, center.y() - r, r * 2, r * 2)
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255, alpha))
            pen.setWidthF(1.5)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(pen)
            start = (math.degrees(self._phase) * speed + index * 120) % 360
            painter.drawArc(rect, int(start * 16), int(span * 16))
        painter.setPen(QtCore.Qt.NoPen)

    def _paint_ball(self, painter, center, radius, colors, level) -> None:
        """球体：几团彩色光在圆里互相叠加，中心烧白。"""
        path = QtGui.QPainterPath()
        path.addEllipse(center, radius, radius)
        painter.save()
        painter.setClipPath(path)
        # 底色压得够深：Screen 叠加要有东西可叠，底浅了叠出来发灰、颜色全被冲淡
        painter.fillPath(path, QtGui.QColor(26, 18, 48))
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Screen)

        spin = self._phase * (1.0 + 0.5 * level)
        reach = radius * (0.56 + 0.06 * level)
        for index, color in enumerate(colors):
            angle = spin + index * math.tau / len(colors)
            spot = QtCore.QPointF(
                center.x() + math.cos(angle) * reach,
                center.y() + math.sin(angle) * reach,
            )
            # 光斑别铺满整颗球：半径收到球的直径以内，四团色才分得开、看得出是四个颜色，
            # 铺太开就四色相加糊成一片白
            gradient = QtGui.QRadialGradient(spot, radius * 1.02)
            gradient.setColorAt(0.0, QtGui.QColor(color.red(), color.green(), color.blue(), 195))
            gradient.setColorAt(0.50, QtGui.QColor(color.red(), color.green(), color.blue(), 112))
            gradient.setColorAt(1.0, QtGui.QColor(color.red(), color.green(), color.blue(), 0))
            painter.setBrush(QtGui.QBrush(gradient))
            painter.drawEllipse(center, radius, radius)

        # 中心那点白热：没有它整颗球是「彩」但不「亮」
        hot = QtGui.QRadialGradient(center, radius * 0.9)
        hot.setColorAt(0.0, QtGui.QColor(255, 255, 255, 85))
        hot.setColorAt(0.45, QtGui.QColor(255, 255, 255, 22))
        hot.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        painter.setBrush(QtGui.QBrush(hot))
        painter.drawEllipse(center, radius, radius)
        painter.restore()

    def _paint_label(self, painter) -> None:
        text = _LABELS.get(self._state, "")
        font = QtGui.QFont(self.font())
        font.setPointSizeF(9.5)
        # 画布只有 SIZE 宽，长句子（「没听清，再来一次」）放不下时会被两边各裁掉一点，
        # 看着像糊了个字。按实际宽度把字号收一档，宁可字小一点也不切字。
        need = QtGui.QFontMetricsF(font).horizontalAdvance(text)
        if need > self.SIZE:
            font.setPointSizeF(max(7.0, 9.5 * self.SIZE / need))
        painter.setFont(font)
        if self._state == STATE_ERROR:
            color = QtGui.QColor(255, 150, 150, 215)
        else:
            color = QtGui.QColor(255, 255, 255, 150)
        painter.setPen(QtGui.QPen(color))
        rect = QtCore.QRectF(0, self.ORB + 6, self.SIZE, self.LABEL_H)
        painter.drawText(
            rect,
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop,
            text,
        )
