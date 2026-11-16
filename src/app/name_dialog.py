"""起名卡片：引导里唯一的一张卡，摆在消息流里问一句「我该怎么称呼你」。

跟着 `clarify` 那张卡的做法走（`app/clarify_card.py`）：卡片是流里的一块，不是弹出窗，
所以首次运行的消息流里看得见它，答完那行绿字也留在原处。

回包契约在引擎 `tools/setup_beat_tool.py::_reply_event`：
- 正常回答 → `{"answer": {"name": "阿岚"}}`（`flow.Answered`）
- 关掉/不答 → `{}`（`flow.TimedOut`，跟「跳过」一样让它收尾）
帧 id 用 server→client 请求的 `srq-...`，跟审批同一套。
"""

from __future__ import annotations

import PySide6.QtCore as QtCore
import PySide6.QtWidgets as QtWidgets

from app.clarify_card import CONFIRM_QSS, DONE_COLOR, FIELD_QSS, GHOST_QSS, MUTED_COLOR, _flush_layout

TITLE_QSS = "QLabel{color:#f2f4fb;font-size:12.5px;background:transparent;}"
HINT_QSS = f"QLabel{{color:{MUTED_COLOR};font-size:10.5px;background:transparent;}}"


class SetupNameCard(QtWidgets.QWidget):
    """名字卡：问题 + 输入框 + 确认。答完收起输入、把那行绿字留下（历史里也照这个画）。"""

    named = QtCore.Signal(str)

    def __init__(self, question: str, suggested: str = "", answer: str = "",
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SetupNameCard")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.wheel_target: QtWidgets.QWidget | None = None
        self._settled = bool(answer)
        self._question = QtWidgets.QLabel(" ".join(str(question or "").split()))
        self._question.setStyleSheet(TITLE_QSS)
        self._question.setWordWrap(True)
        self._done = QtWidgets.QLabel("")
        self._done.setStyleSheet(f"color:{DONE_COLOR};font-size:10.5px;background:transparent;")
        self._row = QtWidgets.QWidget()
        self._suggested = str(suggested or "").strip()
        self._edit = QtWidgets.QLineEdit()
        self._edit.setPlaceholderText("你的名字")
        self._edit.setStyleSheet(FIELD_QSS)
        self._edit.setText(self._suggested)
        self._edit.returnPressed.connect(self._submit)
        self._hint = QtWidgets.QLabel("")
        self._hint.setStyleSheet(HINT_QSS)
        self._ok = QtWidgets.QPushButton("就叫这个")
        self._ok.setStyleSheet(CONFIRM_QSS)
        self._ok.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._ok.clicked.connect(self._submit)
        self._build()
        if answer:
            self.settle(answer)

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(0)
        layout.addWidget(self._question)
        layout.addSpacing(8)
        row = QtWidgets.QHBoxLayout(self._row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._edit, 1)
        row.addWidget(self._ok)
        skip = QtWidgets.QPushButton("以后再说")
        skip.setStyleSheet(GHOST_QSS)
        skip.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        skip.clicked.connect(lambda: self.named.emit(""))
        row.addWidget(skip)
        layout.addWidget(self._row)
        layout.addSpacing(6)
        layout.addWidget(self._hint)
        layout.addSpacing(2)
        layout.addWidget(self._done)
        self._sync_hint()

    def _sync_hint(self) -> None:
        self._hint.setText(
            f"默认是你系统账户名（{self._suggested}）：点「就叫这个」，或者改成别的再回车。"
            if self._suggested else "输入你的名字，回车就行。"
        )

    def fit(self, width: int) -> int:
        """定宽、按内容定高：正文里那块空位要跟卡片一样高（和 `ClarifyCard.fit` 同一份约定）。"""
        width = max(1, int(width))
        self.setFixedWidth(width)
        layout = self.layout()
        if layout is None:
            return 1
        layout.invalidate()
        _flush_layout()
        layout.activate()
        _flush_layout()
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        return max(1, int(height))

    def _submit(self) -> None:
        """「就叫这个」/ 回车：把框里的名字交回去（留着默认的系统账户名也算）。"""
        name = self._edit.text().strip()
        if not name or self._settled:
            return
        self.named.emit(name)

    def settle(self, name: str) -> None:
        """答完了：收起输入，问题底下留一行绿字；`name` 为空表示这次跳过。"""
        self._settled = True
        self._row.setVisible(False)
        self._hint.setVisible(False)
        self._done.setText(f"✓ 叫我 {name}" if name else "以后再说")
        self.updateGeometry()

    def focus_field(self) -> None:
        """把光标放进名字那一行：卡片一出现就能直接打字。"""
        if self._settled:
            return
        self._edit.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        self._edit.selectAll()


def receipt_item(question: str, name: str, parent: QtWidgets.QWidget | None = None) -> SetupNameCard:
    """历史里那张卡：只有问题和答过的名字，没有输入框。"""
    return SetupNameCard(question, answer=name, parent=parent)
