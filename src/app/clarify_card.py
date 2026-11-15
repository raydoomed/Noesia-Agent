"""提问卡片：设计图里那块粉底药丸卡（问题 / 选项 / 「✓ 已答：…」三行）。

文档层只能给字符铺底色，圆角、内边距、边框都做不出来，所以这张卡是一块真控件：
正文里留一块等高空位（`Folds.attach_widget`），控件摆在那块空位上，自己画自己的样子。
"""

from __future__ import annotations

import json

from PySide6 import QtCore, QtGui, QtWidgets

PINK = "#ffd7e6"
QUESTION_COLOR = "#cfc5da"
OPTION_COLOR = "#cbbfd8"
DONE_COLOR = "#5f8f6c"
MUTED_COLOR = "#6c6478"

_NON_CHOICE = {"✔ 就这样"}

CARD_FILL_TOP = QtGui.QColor(255, 215, 230, 14)
CARD_FILL_BOTTOM = QtGui.QColor(255, 215, 230, 5)
CARD_BORDER = QtGui.QColor(255, 215, 230, 41)

GHOST_QSS = (
    "QPushButton{padding:4px 10px;border-radius:13px;font-size:11px;"
    f"color:{MUTED_COLOR};background:transparent;border:1px solid rgba(255,255,255,18);}}"
    "QPushButton:hover{color:#cfc5da;border-color:rgba(255,255,255,36);}"
    "QPushButton:disabled{color:rgba(108,100,120,120);}"
)

FIELD_QSS = (
    "QLineEdit{padding:4px 10px;border-radius:13px;font-size:11px;"
    f"color:{QUESTION_COLOR};background:rgba(255,255,255,8);"
    "border:1px solid rgba(255,255,255,36);}"
    "QLineEdit:focus{border-color:rgba(255,215,230,90);background:rgba(255,255,255,14);}"
    "QLineEdit:disabled{color:rgba(108,100,120,120);border-color:rgba(255,255,255,18);}"
)

CONFIRM_QSS = (
    "QPushButton{padding:4px 10px;border-radius:13px;font-size:11px;font-weight:500;"
    f"color:#2b1520;background:{PINK};border:1px solid {PINK};}}"
    "QPushButton:hover{background:#ffe3ee;}"
    "QPushButton:disabled{color:rgba(255,215,230,90);background:rgba(255,215,230,10);"
    "border-color:rgba(255,215,230,26);}"
)

ACTION_QSS = (
    "QPushButton{padding:4px 2px;border:none;background:transparent;font-size:11px;"
    f"font-weight:500;color:{PINK};}}"
    "QPushButton:hover{color:#ffe3ee;text-decoration:underline;}"
    f"QPushButton:disabled{{color:rgba(255,215,230,90);}}"
)

OPTION_QSS = (
    "QLabel{border-radius:13px;"
    f"color:{OPTION_COLOR};background:rgba(255,255,255,8);"
    "border:1px solid rgba(255,255,255,36);}"
    "QLabel:hover{background:rgba(255,255,255,20);border-color:rgba(255,215,230,60);}"
    "QLabel:disabled{color:" + MUTED_COLOR + ";background:transparent;border-color:rgba(255,255,255,18);}"
    'QLabel[hot="true"]{color:#2b1520;background:' + PINK + ";border-color:" + PINK + ";}"
)

TAG_QSS = (
    "QLabel{color:rgba(255,215,230,140);font-size:9px;background:transparent;padding:0 0 0 1px;}"
    "QLabel:disabled{color:rgba(255,215,230,55);}"
)


def _flush_layout() -> None:
    """同步跑掉挂起的布局请求：选项改完宽高后，布局要晚一步才把新尺寸算进 sizeHint。"""
    QtWidgets.QApplication.sendPostedEvents(None, int(QtCore.QEvent.Type.LayoutRequest))


class _Option(QtWidgets.QLabel):
    """选项药丸：宽度够就一行，放不下才从头折行。QPushButton 放不下会把开头顶掉，只剩尾巴。"""

    clicked = QtCore.Signal()

    def __init__(self, text: str) -> None:
        super().__init__(text)
        font = self.font()
        font.setPixelSize(11)
        self.setFont(font)
        self.setWordWrap(True)
        self.setContentsMargins(10, 4, 10, 4)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        """按「不折行」的自然宽度报，别让 QLabel 自己先折成窄条。"""
        metrics = self.fontMetrics()
        margins = self.contentsMargins()
        return QtCore.QSize(metrics.horizontalAdvance(self.text()) + margins.left() + margins.right(),
                            metrics.height() + margins.top() + margins.bottom())

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        metrics = self.fontMetrics()
        margins = self.contentsMargins()
        return QtCore.QSize(metrics.horizontalAdvance("……") + margins.left() + margins.right(),
                            metrics.height() + margins.top() + margins.bottom())

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ClarifyCard(QtWidgets.QWidget):
    """一张提问卡：问题（淡字）/ 药丸选项 / 答完那行绿字。"""

    picked = QtCore.Signal(int, int)
    skipped = QtCore.Signal(int)
    custom = QtCore.Signal(int, str)

    def __init__(self, questions: list[dict], parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ClarifyCard")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self._questions = questions
        self._pills: dict[int, list[QtWidgets.QLabel | QtWidgets.QPushButton]] = {}
        self._positions: dict[int, list[int]] = {}
        self._tags: dict[int, list[QtWidgets.QLabel]] = {}
        self._option_rows: dict[int, list[tuple[_Option, QtWidgets.QLabel | None]]] = {}
        self.wheel_target: QtWidgets.QWidget | None = None
        self._row: dict[int, QtWidgets.QWidget] = {}
        self._field: dict[int, QtWidgets.QLineEdit] = {}
        self._ok: dict[int, QtWidgets.QPushButton] = {}
        self._answered: set[int] = set()
        self._settled = False
        self._done = QtWidgets.QLabel("")
        self._done.setStyleSheet(f"color:{DONE_COLOR};font-size:10.5px;background:transparent;")
        self._done.setMinimumHeight(self._done.fontMetrics().height())
        self._build()

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(0)
        for index, question in enumerate(self._questions):
            if index:
                layout.addSpacing(10)
            layout.addWidget(self._question_label(index, question))
            choices = [str(choice) for choice in (question.get("choices") or []) if str(choice).strip()]
            if choices:
                layout.addSpacing(8)
                layout.addWidget(self._option_column(index, question, choices))
            layout.addSpacing(6)
            layout.addWidget(self._answer_row(index))
        layout.addSpacing(7)
        layout.addWidget(self._done)

    def _answer_row(self, index: int) -> QtWidgets.QWidget:
        """每题一行：自己写的答案 + 确认 + 跳过（选项不合意就走这条）。"""
        row = QtWidgets.QWidget()
        line = QtWidgets.QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        field = QtWidgets.QLineEdit()
        field.setPlaceholderText("✎ 或者自己写一句")
        field.setStyleSheet(FIELD_QSS)
        field.returnPressed.connect(lambda q=index: self._submit_field(q))
        ok = QtWidgets.QPushButton("确认")
        ok.setStyleSheet(CONFIRM_QSS)
        ok.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        ok.setEnabled(False)
        ok.clicked.connect(lambda _=False, q=index: self._submit_field(q))
        field.textChanged.connect(lambda text, button=ok: button.setEnabled(bool(text.strip())))
        skip = QtWidgets.QPushButton("跳过")
        skip.setStyleSheet(GHOST_QSS)
        skip.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        skip.clicked.connect(lambda _=False, q=index: self.skipped.emit(q))
        line.addWidget(field, 1)
        line.addWidget(ok)
        line.addWidget(skip)
        self._row[index] = row
        self._field[index] = field
        self._ok[index] = ok
        return row

    def _submit_field(self, index: int) -> None:
        field = self._field.get(index)
        if field is None or self._settled or index in self._answered:
            return
        text = field.text().strip()
        if not text:
            return
        field.clear()
        self.custom.emit(index, text)

    def _question_label(self, index: int, question: dict) -> QtWidgets.QLabel:
        text = " ".join(str(question.get("question") or "").split())
        if len(self._questions) > 1:
            text = f"{index + 1}. {text}"
        if question.get("choices") and question.get("multi_select"):
            text = f"{text}  可多选"
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color:{QUESTION_COLOR};font-size:9pt;background:transparent;")
        return label

    def _option_column(self, index: int, question: dict, choices: list[str]) -> QtWidgets.QWidget:
        """选项一题一列、一个选项一行（左右不再并排挤成一条）。"""
        column = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        buttons: list[QtWidgets.QLabel | QtWidgets.QPushButton] = []
        positions: list[int] = []
        rows: list[tuple[_Option, QtWidgets.QLabel | None]] = []
        tags: list[QtWidgets.QLabel] = []
        seen: set[str] = set()
        for position, choice in enumerate(choices):
            label = self._bare(choice)
            if label in seen:
                continue
            seen.add(label)
            button = _Option(label)
            button.setStyleSheet(OPTION_QSS)
            button.clicked.connect(lambda _=False, q=index, p=position: self.picked.emit(q, p))
            buttons.append(button)
            positions.append(position)
            line = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.addWidget(button)
            tag_label = QtWidgets.QLabel("推荐") if "(Recommended)" in choice else None
            if tag_label is not None:
                tag_label.setStyleSheet(TAG_QSS)
                row.addWidget(tag_label)
                tags.append(tag_label)
            rows.append((button, tag_label))
            row.addStretch(1)
            box.addWidget(line)
        if question.get("multi_select"):
            done = QtWidgets.QPushButton("✔ 就这样")
            done.setStyleSheet(ACTION_QSS)
            done.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            done.clicked.connect(lambda _=False, q=index: self.picked.emit(q, -1))
            line = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.addWidget(done)
            row.addStretch(1)
            box.addWidget(line)
            buttons.append(done)
            positions.append(-1)
        self._pills[index] = buttons
        self._positions[index] = positions
        self._tags[index] = tags
        self._option_rows[index] = rows
        return column

    @staticmethod
    def _bare(choice: str) -> str:
        """引擎会在推荐项后面加 "(Recommended)"（`mark_recommended`）：卡片上只留选项本身。"""
        suffix = "(Recommended)"
        text = " ".join(choice.split())
        return text[: -len(suffix)].strip() if text.endswith(suffix) else text

    def mark_picks(self, index: int, picks: set[int]) -> None:
        """多选：勾上的那几颗点着，其余灭掉。"""
        buttons = self._pills.get(index, [])
        positions = self._positions.get(index, [])
        for position, button in zip(positions, buttons):
            if button.text() in _NON_CHOICE:
                continue
            self._set_hot(button, position in picks)

    @staticmethod
    def _answer_items(answer: str) -> list[str]:
        """答案回读成「我选了哪几项」：多选是 JSON 数组字面量，单选和手打就是原话本身。

        不能按逗号硬拆——选项自己可能带逗号（「苹果,脆的那种」），拆开就谁也对不上药丸，
        选中态丢了、碎片还会被拼进输入框当凭据。
        """
        text = str(answer or "").strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            if isinstance(parsed, list):
                items = [str(item).strip() for item in parsed]
                return [item for item in items if item]
        return [text]

    def mark_answered(self, index: int, answer: str) -> None:
        """这一题答完了：选中的那颗留粉，其余按下去；那一行原地留着，高度一点不动。

        收起（隐藏行）会让整块正文缩一截，聊天画面跟着跳——所以这里只变灰、不消失。
        手打的那句答案填回输入框里，当「我答的是什么」留个凭据。
        """
        self._answered.add(index)
        shown = self._answer_items(answer)
        chosen = False
        for button in self._pills.get(index, []):
            if button.text() in _NON_CHOICE:
                self._mute(button)
                continue
            hot = button.text() in shown
            chosen = chosen or hot
            self._set_hot(button, hot)
            self._mute(button)
        field = self._field.get(index)
        if field is not None and not chosen:
            typed = "、".join(shown)
            if typed:
                field.setText(typed)
        self._dim_row(index)

    def mark_skipped(self, index: int) -> None:
        """这一题用户按了跳过：选项按下去、那一行留着变灰，卡片高度不动。"""
        self._answered.add(index)
        for button in self._pills.get(index, []):
            self._mute(button)
        self._dim_row(index)

    def show_done(self, label: str) -> None:
        """整张卡答完了：只在卡底那一行写字，高度、行、选项一个都不动。"""
        self._done.setText(label)

    def settle(self, label: str) -> None:
        """这张卡结束了（超时 / 没人答）：整张卡按下去，底下给一句交代，同样不改高度。

        每一题的输入行也要一起停（`_dim_row` 会连输入框、确认、跳过一起禁用）——
        只停药丸的话，卡看着还能答、字打进去却落进一张已经作废的卡里，回车也没人接。
        """
        for index in range(len(self._questions)):
            self._dim_row(index)
        self._settled = True
        for buttons in self._pills.values():
            for button in buttons:
                self._mute(button)
        for tags in self._tags.values():
            for tag in tags:
                tag.setEnabled(False)
        self._done.setText(label)
        self._done.setStyleSheet(f"color:{MUTED_COLOR};font-size:10.5px;background:transparent;")

    def _dim_row(self, index: int) -> None:
        """这一题不动了：那一行留着变灰（收了会改卡片高度，画面跟着跳）。"""
        row = self._row.get(index)
        if row is None:
            return
        row.setEnabled(False)
        for widget in row.findChildren(QtWidgets.QWidget):
            widget.setEnabled(False)
        for button in row.findChildren(QtWidgets.QPushButton):
            button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        for tag in self._tags.get(index, []):
            tag.setEnabled(False)

    @staticmethod
    def _mute(button: QtWidgets.QLabel | QtWidgets.QPushButton) -> None:
        button.setEnabled(False)
        button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)

    @staticmethod
    def _set_hot(button: QtWidgets.QLabel | QtWidgets.QPushButton, hot: bool) -> None:
        if bool(button.property("hot")) == hot:
            return
        button.setProperty("hot", hot)
        button.style().unpolish(button)
        button.style().polish(button)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        """鼠标停在卡上也能滚聊天：按钮不收滚轮，事件冒泡到这儿，转给正文去滚。"""
        target = self.wheel_target
        if target is None:
            super().wheelEvent(event)
            return
        QtWidgets.QApplication.sendEvent(target, event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        """卡片自己画：5px 圆角、5.5% 粉底、1px 粉边（样式表画出来的圆角会漏底，这里手画）。"""
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 5.0, 5.0)
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, CARD_FILL_TOP)
        gradient.setColorAt(1.0, CARD_FILL_BOTTOM)
        painter.fillPath(path, QtGui.QBrush(gradient))
        painter.setPen(QtGui.QPen(CARD_BORDER, 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()

    def fit(self, width: int) -> int:
        """定宽、按内容定高：正文里那块空位要跟卡片一样高（和 ToolScroll.fit 同一份约定）。"""
        width = max(1, int(width))
        self.setFixedWidth(width)
        self._size_options(width)
        layout = self.layout()
        if layout is None:
            return 1
        layout.invalidate()
        _flush_layout()
        layout.activate()
        _flush_layout()
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        return max(1, int(height))

    def _size_options(self, width: int) -> None:
        """选项按卡宽定死：装得下就一行，装不下才折行——宽窄由这里算，不交给布局猜。"""
        inner = max(1, width - 24)
        for rows in self._option_rows.values():
            for option, tag in rows:
                metrics = option.fontMetrics()
                margins = option.contentsMargins()
                natural = metrics.horizontalAdvance(option.text()) + margins.left() + margins.right()
                reserved = tag.sizeHint().width() + 2 if tag is not None else 0
                option.setFixedWidth(min(natural, max(40, inner - reserved)))
                avail = max(1, option.width() - margins.left() - margins.right())
                rect = metrics.boundingRect(
                    QtCore.QRect(0, 0, avail, 0), int(QtCore.Qt.TextFlag.TextWordWrap), option.text()
                )
                option.setFixedHeight(rect.height() + margins.top() + margins.bottom())
