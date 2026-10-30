"""输入框正上方那一叠状态：引擎提示、还在跑的后台进程、正在生成的工具调用。

照桌面端 composer 的 status-stack：没有内容时整块藏着、不占高度；提示按 key 管，来新的换字
不叠加，`notification.clear` 能撤掉对应那条；后台进程收成一张可展开的卡，标题带个数。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

# `notification.show` 的 level → 用哪种字色（样式表里已经定好这几种）
_LEVELS = {
    "success": "statusdone",
    "done": "statusdone",
    "ok": "statusdone",
    "error": "statusfailed",
    "failed": "statusfailed",
    "warning": "statusfailed",
    "warn": "statusfailed",
    # 「这一档暂时用不了」这类中性提醒：黄字，不是红字。缺语音 Key 走这一档——
    # 语音不是必需的，没 Key 也能纯文字聊，报红等于告诉用户「你装坏了」。
    "note": "statuswarn",
    "busy": "statusbusy",
    "running": "statusbusy",
}

# 一次性确认（「已保存」「已改动」这种说一句就完的事）统一活 6 秒：
# 同一类事情只该有一个数，别再一个 4 秒、一个 6 秒、一个 8 秒各写各的。
# 状态类提示（缺 Key / 还没配模型 / 掉落纯语音 / 正在重连）**不带 ttl**，靠条件变化撤；
# 真报错也不带 ttl——它没解决就该一直看得见。
CONFIRM_TTL_MS = 6000

_LINGER_OK_MS = 4000  # 跑完的留 4 秒：够看清「退出 0」
_LINGER_BAD_MS = 12000  # 失败的多留一会儿：退出码得看得见


class StatusStack(QtWidgets.QFrame):
    """提示 / 正在调用 / 后台进程，三样都挂在输入框正上方这一条上。"""

    kill_requested = QtCore.Signal(str)  # 后台进程那一行点了停止 → 把 session_id 交出去

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusrow")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        # 提示写长了、窗口又矮：这一条最多占窗口四成高，多出来的部分自己滚，
        # 不再靠「压扁」来塞进去（压扁就是把字切掉半截）。
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._note = QtWidgets.QScrollArea(self)
        self._note.setWidgetResizable(True)
        self._note.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._note.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._note.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 滚动条：6px 细条、圆角半透明，没有箭头没有凹槽，跟这层灰底融在一起
        self._note.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 6px; background: transparent; margin: 4px 1px 4px 0; }"
            "QScrollBar::handle:vertical { background: rgba(150,162,186,0.35); min-height: 18px; border-radius: 3px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(150,162,186,0.60); }"
            "QScrollBar:horizontal { height: 6px; background: transparent; margin: 0 4px 1px 4px; }"
            "QScrollBar::handle:horizontal { background: rgba(150,162,186,0.35); min-width: 18px; border-radius: 3px; }"
            "QScrollBar::handle:horizontal:hover { background: rgba(150,162,186,0.60); }"
            "QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; background: none; border: none; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }"
            "QScrollBar::up-arrow, QScrollBar::down-arrow,"
            "QScrollBar::left-arrow, QScrollBar::right-arrow { width: 0; height: 0; background: none; }"
        )
        inner = QtWidgets.QWidget(self._note)
        inner.setStyleSheet("background: transparent;")
        self._note.setWidget(inner)
        outer.addWidget(self._note)
        self._box = QtWidgets.QVBoxLayout(inner)
        self._box.setContentsMargins(8, 4, 8, 4)
        self._box.setSpacing(2)

        self._rows: dict[str, QtWidgets.QWidget] = {}
        self._timers: dict[str, QtCore.QTimer] = {}
        self._activity: QtWidgets.QWidget | None = None
        self._bg: QtWidgets.QWidget | None = None
        self._bg_head: QtWidgets.QToolButton | None = None
        self._bg_body: QtWidgets.QWidget | None = None
        self._bg_box: QtWidgets.QVBoxLayout | None = None
        self._bg_rows: dict[str, QtWidgets.QWidget] = {}
        self._bg_linger: dict[str, QtCore.QTimer] = {}
        self._bg_done: set[str] = set()
        self.hide()

    # ---- 提示（notification.show / notification.clear） ----

    def show_notice(self, key: str, text: str, *, ttl_ms: int = 0, level: str = "") -> None:
        text = (text or "").strip()
        if not text:
            self.clear_notice(key)
            return
        row = self._rows.get(key)
        if row is None:
            row = QtWidgets.QWidget(self)
            line = QtWidgets.QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(0)
            label = QtWidgets.QLabel(row)
            label.setWordWrap(True)
            line.addWidget(label, 1)
            self._rows[key] = row
            self._box.addWidget(row)
        label = row.findChild(QtWidgets.QLabel)
        label.setObjectName(_LEVELS.get((level or "").strip().lower(), "statusnote"))
        label.setText(text)
        # 换了 objectName 得让样式表重算一遍，否则字色还是上一档的
        label.style().unpolish(label)
        label.style().polish(label)
        row.show()
        timer = self._timers.get(key)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda k=key: self.clear_notice(k))
            self._timers[key] = timer
        timer.stop()
        if ttl_ms and ttl_ms > 0:
            timer.start(int(ttl_ms))
        self._sync()

    def clear_notice(self, key: str) -> None:
        row = self._rows.pop(key, None)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if row is not None:
            self._box.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._sync()

    # ---- 正在调用（tool.generating / tool.progress 的前奏） ----

    def set_activity(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            if self._activity is not None:
                self._box.removeWidget(self._activity)
                self._activity.hide()
                self._activity.deleteLater()
                self._activity = None
            self._sync()
            return
        if self._activity is None:
            self._activity = QtWidgets.QWidget(self)
            line = QtWidgets.QHBoxLayout(self._activity)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(0)
            label = QtWidgets.QLabel(self._activity)
            label.setObjectName("statusbusy")
            label.setWordWrap(True)
            line.addWidget(label, 1)
            self._box.addWidget(self._activity)
        self._activity.findChild(QtWidgets.QLabel).setText(text)
        self._activity.show()
        self._sync()

    # ---- 后台进程（process.list） ----

    def set_processes(self, rows: list) -> None:
        rows = [
            row for row in rows or []
            if isinstance(row, dict) and str(row.get("session_id") or "") not in self._bg_done
        ]
        if not rows:
            self._drop_bg()
            self._sync()
            return
        if self._bg is None:
            self._build_bg()
        while self._bg_box.count():
            item = self._bg_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._bg_rows.clear()
        for row in rows:
            session_id = str(row.get("session_id") or "")
            widget = self._bg_row(row)
            self._bg_box.addWidget(widget)
            self._bg_rows[session_id] = widget
        for key in [key for key in self._bg_linger if key not in self._bg_rows]:
            self._bg_linger.pop(key).deleteLater()
        self._bg_head.setText(
            "%s %d 个后台任务" % ("▾" if self._bg_head.isChecked() else "▸", len(rows))
        )
        self._bg.show()
        self._sync()

    def _build_bg(self) -> None:
        self._bg = QtWidgets.QFrame(self)
        self._bg.setObjectName("statusgroup")
        card = QtWidgets.QVBoxLayout(self._bg)
        card.setContentsMargins(4, 2, 4, 2)
        card.setSpacing(1)
        self._bg_head = QtWidgets.QToolButton(self._bg)
        self._bg_head.setCheckable(True)
        self._bg_head.setChecked(False)  # 默认收起：只显「N 个后台任务」，不占屏幕
        self._bg_head.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._bg_head.toggled.connect(self._toggle_bg)
        card.addWidget(self._bg_head, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self._bg_body = QtWidgets.QWidget(self._bg)
        self._bg_box = QtWidgets.QVBoxLayout(self._bg_body)
        self._bg_box.setContentsMargins(0, 0, 0, 0)
        self._bg_box.setSpacing(1)
        self._bg_body.hide()
        card.addWidget(self._bg_body)
        self._box.addWidget(self._bg)

    def _bg_row(self, row: dict) -> QtWidgets.QWidget:
        session_id = str(row.get("session_id") or "")
        running = str(row.get("status") or "") != "exited"
        exit_code = row.get("exit_code")
        holder = QtWidgets.QWidget(self._bg_body)
        holder.setProperty("proc_status", "running" if running else "exited")
        line = QtWidgets.QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(4)
        glyph = QtWidgets.QLabel("◐" if running else "●", holder)
        if running:
            glyph.setObjectName("statusbusy")
        else:
            glyph.setObjectName("statusdone" if exit_code in (0, None) else "statusfailed")
        line.addWidget(glyph)
        label = QtWidgets.QLabel(holder)
        label.setObjectName("statusnote")
        text = self._bg_text(row)
        label.setText(label.fontMetrics().elidedText(text, QtCore.Qt.TextElideMode.ElideRight, 520))
        label.setToolTip(text)
        line.addWidget(label, 1)
        button = QtWidgets.QToolButton(holder)
        button.setText("停止" if running else "✕")
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False, sid=session_id: self._dismiss(sid))
        line.addWidget(button)
        if not running:
            self._arm_linger(session_id, exit_code)
        return holder

    @staticmethod
    def _bg_text(row: dict) -> str:
        command = " ".join(str(row.get("command") or "").split())
        if str(row.get("status") or "") != "exited":
            return command
        code = row.get("exit_code")
        text = "%s · 退出 %s" % (command, "?" if code is None else code)
        tail = [part.strip() for part in str(row.get("output_preview") or "").splitlines() if part.strip()]
        return text + (" · " + tail[-1] if tail else "")

    def _arm_linger(self, session_id: str, exit_code) -> None:
        if session_id in self._bg_linger or session_id in self._bg_done:
            return
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda sid=session_id: self._dismiss(sid))
        timer.start(_LINGER_OK_MS if exit_code in (0, None) else _LINGER_BAD_MS)
        self._bg_linger[session_id] = timer

    def _dismiss(self, session_id: str) -> None:
        """这一行不要了：跑着的通知引擎停掉，跑完的（和点过的）记下来，别让轮询画回来。"""
        row = self._bg_rows.get(session_id)
        if row is None:
            return
        if str(row.property("proc_status") or "") != "exited":
            self.kill_requested.emit(session_id)
            return
        self._bg_done.add(session_id)
        timer = self._bg_linger.pop(session_id, None)
        if timer is not None:
            timer.deleteLater()
        self._remove_bg_row(session_id)

    def _remove_bg_row(self, session_id: str) -> None:
        row = self._bg_rows.pop(session_id, None)
        if row is not None:
            self._bg_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        if not self._bg_rows:
            self._drop_bg()
        else:
            self._bg_head.setText(
                "%s %d 个后台任务" % ("▾" if self._bg_head.isChecked() else "▸", len(self._bg_rows))
            )
        self._sync()

    def _drop_bg(self) -> None:
        for timer in self._bg_linger.values():
            timer.deleteLater()
        self._bg_linger.clear()
        self._bg_rows.clear()
        if self._bg is not None:
            self._box.removeWidget(self._bg)
            self._bg.hide()
            self._bg.deleteLater()
        self._bg = None
        self._bg_head = None
        self._bg_body = None
        self._bg_box = None

    def _toggle_bg(self, opened: bool) -> None:
        if self._bg_body is not None:
            self._bg_body.setVisible(opened)
        text = self._bg_head.text()
        if text.startswith(("▸", "▾")):
            self._bg_head.setText(("▾" if opened else "▸") + text[1:])

    # ---- 收摊 ----

    def clear_all(self) -> None:
        """换会话：提示和进程行都属于上一场，不能带过去。"""
        for key in list(self._rows):
            self.clear_notice(key)
        self.set_activity("")
        self._bg_done.clear()
        self._drop_bg()
        self._sync()

    def _sync(self) -> None:
        shown = bool(self._rows) or self._activity is not None or self._bg is not None
        self.setVisible(shown)
        if not shown:
            return
        # 高度照旧由提示自己说了算（原来布局就是拿 sizeHint 定这个高度），
        # 里面文字换行后的真实高度更大时，多出来的用竖向滚动条看。
        inner = self._note.widget()
        if inner is None:
            return
        # 高度就是「文字换行后要多少就是多少」——和原来一样，不多给也不少给；
        # 只有窗口矮到放不下这么多行时才收一点，收掉的部分用竖向滚动条看。
        width = max(60, (self.width() or 260) - 16)
        need = 8
        for row in self._rows.values():
            label = row.findChild(QtWidgets.QLabel)
            if label is None:
                continue
            need += max(label.heightForWidth(width), label.fontMetrics().height()) + 2
        cap = int(self.window().height() * 0.45) if self.window() else 400
        height = max(24, min(need, cap))
        inner.setMinimumHeight(need)
        self._note.setFixedHeight(height)

