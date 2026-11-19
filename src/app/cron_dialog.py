"""定时任务：左边任务清单，右边一条任务的详情、运行记录和编辑表单。

数据全在引擎的 cron 库里（`data/cron/jobs.json` + 运行会话），界面只负责画和转发：
清单走引擎现成的那份行结构，编辑表单按记录的原始字段回填，运行记录按执行记录排。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from html import escape

from PySide6 import QtCore, QtGui, QtWidgets

from app.approval_dialog import ConfirmDialog
from app.model_dialog import STYLE, _apply_dark_palette

FIRE_POLL_MS = 1000
RUNS_POLL_MS = 8000
QUEUE_SETTLE_TIMEOUT_S = 90.0
QUEUE_START_SLACK_S = 2.0
# 手动触发要等这一趟真跑完才回包（`cron.trigger` 是 long_lived），跑几分钟很正常，
# 所以这个上限只用来兜「回包永远不来」：明显大于常规运行，超了就把按钮放开、说清结果未知。
FIRE_GIVE_UP_S = 1800.0

EXTRA_STYLE = """
#cron-search, #cron-custom {
    background-color: rgba(255, 255, 255, 8);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 9px;
    color: #ffffff;
    padding: 5px 10px;
    font-size: 12px;
}
#cron-search:focus, #cron-custom:focus { border-color: rgba(240, 178, 210, 150); }
#cron-list, #cron-runs { background: transparent; border: none; outline: none; color: rgba(246, 223, 234, 218); }
#cron-runs::item { color: rgba(246, 223, 234, 218); padding: 2px; }
#cron-run-line { color: rgba(246, 223, 234, 218); font-size: 12px; background: transparent; }
#cron-run-detail { padding: 1px 9px; font-size: 11px; }
#run-transcript {
    background-color: rgba(255, 255, 255, 6);
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: 10px;
    color: #f6dfea;
}
#cron-list::item { border-radius: 10px; margin: 1px 0; }
#cron-list::item:selected { background: rgba(240, 178, 210, 34); }
#cron-list::item:hover { background: rgba(255, 255, 255, 14); }
#cron-pane {
    background-color: rgba(255, 255, 255, 6);
    border: 1px solid rgba(240, 178, 210, 22);
    border-radius: 12px;
}
#cron-name { color: #fbe9f2; font-size: 13.5px; font-weight: 600; }
#cron-meta { color: rgba(255, 255, 255, 120); font-size: 11px; }
#cron-detail-name { color: #fbe9f2; font-size: 16px; font-weight: 600; }
#cron-detail-line { color: rgba(246, 223, 234, 200); font-size: 12px; }
#cron-section { color: rgba(246, 223, 234, 150); font-size: 11px; letter-spacing: 0.4px; }
#cron-empty { color: rgba(255, 255, 255, 105); font-size: 12px; }
#cron-note { color: rgba(246, 223, 234, 205); font-size: 11.5px; }
#danger { background: transparent; border: 1px solid rgba(255, 120, 120, 60); color: #ff9a9a; font-weight: 400; }
#danger:hover { background: rgba(255, 120, 120, 26); border-color: rgba(255, 120, 120, 130); color: #ffbdbd; }
#cron-actions QPushButton { padding: 5px 12px; font-size: 12px; }
QSpinBox, QTimeEdit {
    background-color: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 8px;
    color: #ffffff;
    padding: 3px 8px;
    font-size: 12px;
}
QSpinBox:focus, QTimeEdit:focus { border-color: rgba(240, 178, 210, 150); }
QSpinBox::up-button, QSpinBox::down-button,
QTimeEdit::up-button, QTimeEdit::down-button {
    width: 13px;
    background: transparent;
    border: none;
}
"""

FREQUENCIES = (
    ("daily", "每天"),
    ("weekdays", "工作日"),
    ("weekly", "每周"),
    ("monthly", "每月"),
    ("hourly", "每小时"),
    ("every15", "每 15 分钟"),
    ("custom", "自定义"),
)
WEEKDAYS = (("1", "周一"), ("2", "周二"), ("3", "周三"), ("4", "周四"), ("5", "周五"), ("6", "周六"), ("0", "周日"))
STATE_LABELS = {
    "enabled": "已启用",
    "scheduled": "已排程",
    "running": "运行中",
    "paused": "已暂停",
    "disabled": "已禁用",
    "error": "错误",
    "completed": "已完成",
}

STATE_TINT = {
    "enabled": "#8fd8a8",
    "scheduled": "#8fd8a8",
    "running": "#8ab4ff",
    "error": "#ff8a8a",
    "run": "#8ab4ff",
    "wait": "#8fd8a8",
    "paused": "rgba(255, 255, 255, 110)",
    "disabled": "rgba(255, 255, 255, 90)",
    "completed": "rgba(255, 255, 255, 90)",
    "done": "rgba(255, 255, 255, 90)",
    "fail": "#ff8a8a",
}

# 桌面的 NEXT_RUN_OVERDUE_GRACE_MS：忙碌的一轮 tick 允许迟到几分钟，超过这个宽限就是调度器没跑。
OVERDUE_GRACE_S = 15 * 60


def _parse_iso(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(moment: datetime) -> datetime:
    return moment.astimezone() if moment.tzinfo else moment


def _clock(moment: datetime) -> str:
    return _local(moment).strftime("%H:%M")


def _when(value) -> str:
    """「下次/上次」那一行：先给「还有多久」，再给具体钟点——看相对时间比看 ISO 串快。"""
    moment = _parse_iso(value)
    if moment is None:
        return "—"
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    delta = moment - now
    seconds = delta.total_seconds()
    if abs(seconds) < 60:
        rel = "就在这一刻" if seconds >= 0 else "刚刚"
    elif 0 < seconds < 3600:
        rel = f"{int(seconds // 60)} 分钟后"
    elif 0 < seconds < 86400:
        rel = f"{int(seconds // 3600)} 小时后"
    elif seconds > 0:
        rel = f"{int(seconds // 86400)} 天后"
    else:
        past = -seconds
        rel = f"{int(past // 60)} 分钟前" if past < 3600 else f"{int(past // 3600)} 小时前"
    local = _local(moment)
    today = datetime.now(local.tzinfo).date() if local.tzinfo else datetime.now().date()
    if local.date() == today:
        stamp = f"今天 {local.strftime('%H:%M')}"
    elif local.date() == today + timedelta(days=1):
        stamp = f"明天 {local.strftime('%H:%M')}"
    else:
        stamp = local.strftime("%m-%d %H:%M")
    return f"{stamp}（{rel}）"


def _preview(text, limit: int = 120) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


_WEEKDAY_NAMES = {
    "monday": "周一",
    "tuesday": "周二",
    "wednesday": "周三",
    "thursday": "周四",
    "friday": "周五",
    "saturday": "周六",
    "sunday": "周日",
}
_CRON_DOW = {**{str(n): name for n, name in enumerate(("周日", "周一", "周二", "周三", "周四", "周五", "周六"))}, "7": "周日"}


def _clock_text(raw: str) -> str:
    """引擎排程里的钟点（9am / 9:30pm / 09:00）统一成 24 小时的 HH:MM，认不出来就原样给。"""
    text = raw.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
    if not match:
        return raw.strip()
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _schedule_label(text) -> str:
    """把引擎自己的英文排程描述翻成中文：只翻认得出来的几种，其余原样给。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.fullmatch(r"every\s+(\d+)\s*m", raw)
    if match:
        return f"每 {match.group(1)} 分钟"
    match = re.fullmatch(r"every\s+(\d+)\s*h", raw)
    if match:
        return f"每 {match.group(1)} 小时"
    match = re.fullmatch(r"(?:once\s+)?in\s+(\d+)\s*(m|h)", raw)
    if match:
        unit = "分钟" if match.group(2) == "m" else "小时"
        return f"{match.group(1)} {unit}后一次性"
    match = re.fullmatch(r"once\s+at\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", raw)
    if match:
        return f"一次性 {match.group(1)} {match.group(2)}"
    match = re.fullmatch(r"every\s+day\s+(?:at\s+)?(.+)", raw)
    if match:
        return f"每天 {_clock_text(match.group(1))}"
    match = re.fullmatch(r"(?:every\s+)?weekdays?\s+(?:at\s+)?(.+)", raw)
    if match:
        return f"工作日 {_clock_text(match.group(1))}"
    match = re.fullmatch(r"every\s+([a-z]+)\s+(?:at\s+)?(.+)", raw)
    if match and match.group(1) in _WEEKDAY_NAMES:
        return f"每{_WEEKDAY_NAMES[match.group(1)]} {_clock_text(match.group(2))}"
    parts = raw.split()
    if len(parts) == 5 and all(re.fullmatch(r"[\d*/,-]+", part) for part in parts):
        minute, hour, day, month, weekday = parts
        if minute.isdigit() and hour.isdigit() and day == "*" and month == "*":
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if weekday == "*":
                return f"每天 {clock}"
            if weekday in _CRON_DOW:
                return f"每{_CRON_DOW[weekday]} {clock}"
    return raw


def _schedule_text(job: dict) -> str:
    """清单/详情都从这里取排程那一句：先要引擎写好的，其次排程里的 display/expr/run_at。"""
    raw = job.get("schedule_display")
    if not raw:
        schedule = job.get("schedule")
        if isinstance(schedule, dict):
            raw = schedule.get("display") or schedule.get("expr") or schedule.get("run_at")
        else:
            raw = schedule
    return _schedule_label(raw) or "—"


def _overdue(job: dict) -> bool:
    """下次运行停在过去超过宽限：调度器没跑起来，界面上得看得见（桌面同一条判定）。"""
    moment = _parse_iso(job.get("next_run_at"))
    if moment is None:
        return False
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    return (now - moment).total_seconds() > OVERDUE_GRACE_S


def _row_state(job: dict) -> tuple[str, str]:
    """(状态词, 颜色键)：状态就是引擎的 job.state（照桌面 jobState），没给才按 enabled 推。

    上次成功/失败不进状态词——那是「上次运行」的事，桌面上分两栏放。
    """
    state = str(job.get("state") or "").strip()
    if not state:
        state = "disabled" if job.get("enabled") is False else "scheduled"
    if state == "scheduled" and _overdue(job):
        return "已逾期", "error"
    return STATE_LABELS.get(state, state), state



def _repeat_left(value) -> int | None:
    """记录里的 repeat 有时是整数、有时是 `{times, completed}`：两种都算成「还剩几次」。"""
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, dict):
        times, done = value.get("times"), value.get("completed")
        if isinstance(times, int):
            left = times - (done if isinstance(done, int) else 0)
            return left if left > 0 else None
    return None


class _JobRow(QtWidgets.QWidget):
    """清单里的一行：名字一行、排程和状态一行，左边一颗状态点。"""

    def __init__(self, job: dict, parent: QtWidgets.QWidget | None = None, firing: bool = False) -> None:
        super().__init__(parent)
        state_text, tint_key = _row_state(job)
        if firing:
            state_text, tint_key = "运行中", "run"
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        dot = QtWidgets.QLabel("●", self)
        dot.setStyleSheet(f"color: {STATE_TINT.get(tint_key, '#8fd8a8')}; font-size: 9px;")
        name = QtWidgets.QLabel(str(job.get("name") or job.get("job_id") or "未命名"), self)
        name.setObjectName("cron-name")
        top.addWidget(dot)
        top.addWidget(name, 1)
        meta = QtWidgets.QLabel(
            f"{_schedule_text(job)} · {state_text}", self
        )
        meta.setObjectName("cron-meta")
        layout.addLayout(top)
        layout.addWidget(meta)


_ROLE_NAMES = {"user": "你", "assistant": "Noesia", "tool": "工具", "system": "系统"}

def _deliver_text(record: dict | None) -> str:
    """投递目标照 `jobs.json` 里的原文显示，不翻译——面板上的字必须能和存储逐字核对。

    新建任务时引擎按「带着 origin 建」落值，所以新建页写 `origin`；老记录没有这个字段时，
    投递侧取的是 `local`（`job.get("deliver", "local")`），照它显示。
    """
    if record is None:
        return "origin"
    return str(record.get("deliver") or "").strip() or "local"


def _transcript_html(rows: list) -> str:
    """历史行 → 只读 HTML：谁说的在前，正文保留换行；工具行画名字和参数预览，失败的带 ✗。"""
    parts = [
        "<style>"
        "body { background: transparent; color: #f6dfea; font-size: 12px; }"
        ".who { color: rgba(240, 178, 210, 225); font-size: 11px; margin: 12px 0 2px 0; }"
        ".said { margin: 0; white-space: pre-wrap; }"
        "</style>"
    ]
    said_any = False
    for row in rows:
        role = str(row.get("role") or "")
        if role == "system":
            continue
        if role == "tool":
            who = f"工具 · {row.get('name') or 'tool'}"
            said = str(row.get("context") or "")
            if row.get("ok") is False:
                who += " ✗"
        else:
            who = _ROLE_NAMES.get(role, role or "消息")
            said = str(row.get("text") or "")
        if not said.strip():
            continue
        said_any = True
        parts.append(f'<p class="who">{escape(who)}</p>')
        parts.append(f'<p class="said">{escape(said)}</p>')
    if not said_any:
        parts.append('<p class="said">这场运行没有留下消息</p>')
    return "".join(parts)


class _RunDetailDialog(QtWidgets.QDialog):  # type: ignore[misc]
    """一次运行的完整内容：只读，整份取那场会话的历史，不动当前正在聊的那场。"""

    loaded = QtCore.Signal(list, str)

    def __init__(self, engine, session_id: str, heading: str, parent=None) -> None:
        super().__init__(
            parent,
            QtCore.Qt.WindowType.Window
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setWindowTitle("运行记录")
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setStyleSheet(STYLE + EXTRA_STYLE)
        _apply_dark_palette(self)
        self.resize(720, 560)
        self.session_id = session_id

        frame = QtWidgets.QFrame(self)
        frame.setObjectName("dialog-card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(frame)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(20, 15, 20, 16)
        layout.setSpacing(10)

        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        title = QtWidgets.QLabel(heading or "运行记录", frame)
        title.setObjectName("dialog-title")
        title.setWordWrap(True)
        close = QtWidgets.QToolButton(frame)
        close.setObjectName("closebtn")
        close.setText("✕")
        close.setToolTip("关掉（Esc 也行）")
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.reject)
        bar.addWidget(title, 1)
        bar.addWidget(close)
        layout.addLayout(bar)

        self.transcript = QtWidgets.QTextBrowser(frame)
        self.transcript.setObjectName("run-transcript")
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setPlainText("读取中…")
        layout.addWidget(self.transcript, 1)

        self.loaded.connect(self.paint)
        threading.Thread(target=self._fetch, args=(engine,), name="cron-run-detail", daemon=True).start()

    def _fetch(self, engine) -> None:
        try:
            rows = engine.read_session_messages(self.session_id)
        except Exception as exc:  # noqa: BLE001 — 取不到就把原因写给人看，别让窗口空着
            self.loaded.emit([], f"历史没取到：{exc}")
            return
        self.loaded.emit(list(rows or []), "")

    def paint(self, rows: list, error: str) -> None:
        if error:
            self.transcript.setHtml(f'<p class="said">{escape(error)}</p>')
            return
        self.transcript.setHtml(_transcript_html(list(rows or [])))


def _run_is_running(row: dict) -> bool:
    """运行记录里这一行是不是真的还在跑。引擎的 ``is_active`` 只是 300 秒活动窗口——被前置检查
    拦下的运行会留下一条没关闭的会话，5 分钟里都读成活跃；同一份数据里还有 ``scheduler_owned``，
    说的是调度器是不是还握着这一趟，以它为准（脚本任务的记录没有这个字段，按原来的算）。"""
    if not isinstance(row, dict) or not row.get("is_active"):
        return False
    return row.get("scheduler_owned") is not False


class CronDialog(QtWidgets.QDialog):  # type: ignore[misc]
    """定时任务面板。引擎的 `cron_result` 回包按 tag 分派，`cron_changed` 让清单自己刷新。"""

    NOTE_ERROR = "color: #ff8a8a;"
    NOTE_OK = "color: #8fd8a8;"

    def __init__(self, engine, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(
            parent,
            QtCore.Qt.WindowType.Window
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setWindowTitle("定时任务")
        self.setModal(True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setStyleSheet(STYLE + EXTRA_STYLE)
        _apply_dark_palette(self)
        self.setMinimumWidth(760)
        self.setMinimumHeight(540)

        self._engine = engine
        self._drag_from = None
        self._jobs: list[dict] = []
        self._records: dict[str, dict] = {}
        self._current = ""
        self._editing = ""
        self._firing: dict[str, tuple[float, float]] = {}  # job_id → (点下去的 monotonic, 墙钟)
        self._status_error_text = ""  # 详情区那句「上次失败：…」，好让它该消失时消失
        self._active_job = ""
        self._note_serial = 0
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(RUNS_POLL_MS)
        self._poll.timeout.connect(self._poll_tick)
        self._poll.start()

        frame = QtWidgets.QFrame(self)
        frame.setObjectName("dialog-card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(frame)
        shadow = QtWidgets.QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QtGui.QColor(0, 0, 0, 175))
        frame.setGraphicsEffect(shadow)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(20, 15, 20, 16)
        layout.setSpacing(10)

        titlebar = QtWidgets.QWidget(frame)
        titlebar.setObjectName("titlebar")
        self._titlebar = titlebar
        bar = QtWidgets.QHBoxLayout(titlebar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        title = QtWidgets.QLabel("定时任务", titlebar)
        title.setObjectName("dialog-title")
        close = QtWidgets.QToolButton(titlebar)
        close.setObjectName("closebtn")
        close.setText("✕")
        close.setToolTip("关掉（Esc 也行）")
        close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.reject)
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(close)
        titlebar.installEventFilter(self)
        layout.addWidget(titlebar)

        subtitle = QtWidgets.QLabel(
            "按排程自动跑一段提示词。任务在引擎的 cron 库里，关掉窗口也照样跑。", frame
        )
        subtitle.setObjectName("dialog-sub")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        layout.addLayout(body, 1)

        left = QtWidgets.QFrame(frame)
        left.setObjectName("cron-pane")
        left.setFixedWidth(238)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)
        self._search = QtWidgets.QLineEdit(left)
        self._search.setObjectName("cron-search")
        self._search.setPlaceholderText("搜索任务…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._rebuild_list)
        left_layout.addWidget(self._search)
        self._list = QtWidgets.QListWidget(left)
        self._list.setObjectName("cron-list")
        self._list.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setUniformItemSizes(False)
        self._list.itemSelectionChanged.connect(self._on_selected)
        left_layout.addWidget(self._list, 1)
        self._new_button = QtWidgets.QPushButton("新建定时任务", left)
        self._new_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._new_button.clicked.connect(self._start_create)
        left_layout.addWidget(self._new_button)
        body.addWidget(left)

        self._pages = QtWidgets.QStackedWidget(frame)
        self._pages.addWidget(self._build_detail_page())
        self._pages.addWidget(self._build_editor_page())
        body.addWidget(self._pages, 1)

        self._status = QtWidgets.QLabel("", frame)
        self._status.setObjectName("cron-note")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._engine.cron_result.connect(self._on_result)
        self._engine.cron_changed.connect(self._on_changed)
        self._engine.cron_list()

    # ── 界面骨架 ────────────────────────────────────────────────────────────
    def _build_detail_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self._detail_name = QtWidgets.QLabel("", page)
        self._detail_name.setObjectName("cron-detail-name")
        self._detail_name.setWordWrap(True)
        layout.addWidget(self._detail_name)

        self._detail_state = QtWidgets.QLabel("", page)
        self._detail_state.setObjectName("cron-detail-line")
        self._detail_state.setWordWrap(True)
        layout.addWidget(self._detail_state)

        self._detail_schedule = QtWidgets.QLabel("", page)
        self._detail_schedule.setObjectName("cron-detail-line")
        self._detail_schedule.setWordWrap(True)
        layout.addWidget(self._detail_schedule)

        prompt_label = QtWidgets.QLabel("提示词", page)
        prompt_label.setObjectName("cron-section")
        layout.addWidget(prompt_label)
        self._detail_prompt = QtWidgets.QPlainTextEdit(page)
        self._detail_prompt.setReadOnly(True)
        self._detail_prompt.setFixedHeight(112)
        layout.addWidget(self._detail_prompt)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(8)
        actions.setContentsMargins(0, 0, 0, 0)
        self._pause_button = QtWidgets.QPushButton("暂停", page)
        self._pause_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._pause_button.clicked.connect(self._toggle_paused)
        self._trigger_button = QtWidgets.QPushButton("立即触发", page)
        self._trigger_button.setObjectName("secondary")
        self._trigger_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._trigger_button.setToolTip("不等排程，现在就跑一次")
        self._trigger_button.clicked.connect(self._trigger_now)
        self._edit_button = QtWidgets.QPushButton("编辑", page)
        self._edit_button.setObjectName("secondary")
        self._edit_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._edit_button.clicked.connect(self._start_edit)
        self._delete_button = QtWidgets.QPushButton("删除", page)
        self._delete_button.setObjectName("danger")
        self._delete_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._delete_button.clicked.connect(self._delete_current)
        for button in (self._pause_button, self._trigger_button, self._edit_button, self._delete_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        runs_label = QtWidgets.QLabel("运行记录", page)
        runs_label.setObjectName("cron-section")
        layout.addWidget(runs_label)
        self._runs = QtWidgets.QListWidget(page)
        self._runs.setObjectName("cron-runs")
        self._runs.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._runs.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self._runs.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._runs.setWordWrap(True)
        layout.addWidget(self._runs, 1)
        return page

    def _build_editor_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._editor_title = QtWidgets.QLabel("新建定时任务", page)
        self._editor_title.setObjectName("cron-detail-name")
        layout.addWidget(self._editor_title)
        self._editor_hint = QtWidgets.QLabel(
            "用 cron 语法或「每 15 分钟」这类短语都行；自定义那格原样交给引擎。", page
        )
        self._editor_hint.setObjectName("cron-meta")
        self._editor_hint.setWordWrap(True)
        layout.addWidget(self._editor_hint)

        name_caption = QtWidgets.QLabel("名称", page)
        name_caption.setObjectName("cron-section")
        layout.addWidget(name_caption)
        self._name_edit = QtWidgets.QLineEdit(page)
        self._name_edit.setPlaceholderText("晨间简报")
        layout.addWidget(self._name_edit)

        prompt_caption = QtWidgets.QLabel("提示词", page)
        prompt_caption.setObjectName("cron-section")
        layout.addWidget(prompt_caption)
        self._prompt_edit = QtWidgets.QPlainTextEdit(page)
        self._prompt_edit.setPlaceholderText("总结我未读的话题，并把重点发给我…")
        self._prompt_edit.setMinimumHeight(96)
        layout.addWidget(self._prompt_edit, 1)

        schedule_caption = QtWidgets.QLabel("排程", page)
        schedule_caption.setObjectName("cron-section")
        layout.addWidget(schedule_caption)

        freq_row = QtWidgets.QHBoxLayout()
        freq_row.setSpacing(8)
        self._freq_box = QtWidgets.QComboBox(page)
        for key, label in FREQUENCIES:
            self._freq_box.addItem(label, key)
        self._freq_box.currentIndexChanged.connect(self._sync_schedule_widgets)
        self._freq_box.setFixedWidth(104)
        self._time_edit = QtWidgets.QTimeEdit(page)
        self._time_edit.setDisplayFormat("HH:mm")
        self._time_edit.setTime(QtCore.QTime(9, 0))
        self._time_edit.setFixedWidth(84)
        self._week_box = QtWidgets.QComboBox(page)
        for key, label in WEEKDAYS:
            self._week_box.addItem(label, key)
        self._week_box.setFixedWidth(84)
        self._dom_spin = QtWidgets.QSpinBox(page)
        self._dom_spin.setRange(1, 31)
        self._dom_spin.setValue(1)
        self._dom_spin.setFixedWidth(70)
        for widget in (self._freq_box, self._week_box, self._dom_spin, self._time_edit):
            widget.setFixedHeight(26)
            freq_row.addWidget(widget)
        freq_row.addSpacing(10)
        freq_row.addStretch(1)
        layout.addLayout(freq_row)

        self._custom_edit = QtWidgets.QLineEdit(page)
        self._custom_edit.setObjectName("cron-custom")
        self._custom_edit.setPlaceholderText("0 9 * * * 或 weekdays at 9am")
        layout.addWidget(self._custom_edit)

        tail = QtWidgets.QHBoxLayout()
        tail.setSpacing(8)
        repeat_caption = QtWidgets.QLabel("重复次数", page)
        repeat_caption.setObjectName("cron-section")
        self._repeat_edit = QtWidgets.QLineEdit(page)
        self._repeat_edit.setPlaceholderText("留空 = 一直跑")
        self._repeat_edit.setFixedWidth(120)
        deliver_caption = QtWidgets.QLabel("投递至", page)
        deliver_caption.setObjectName("cron-section")
        deliver_box = QtWidgets.QComboBox(page)
        deliver_box.addItem("origin", "origin")
        deliver_box.addItem("latest", "latest")
        deliver_box.addItem("local", "local")
        deliver_box.setFixedWidth(104)
        deliver_box.setToolTip(
            "origin = 只投回建这条任务的那场对话；\n"
            "latest = 投到运行那一刻最近还在聊的那场；\n"
            "local = 不投到任何对话，结果只留在运行记录里")
        self._deliver_box = deliver_box
        tail.addWidget(repeat_caption)
        tail.addWidget(self._repeat_edit)
        tail.addSpacing(10)
        tail.addWidget(deliver_caption)
        tail.addWidget(deliver_box)
        tail.addStretch(1)
        layout.addLayout(tail)

        self._editor_note = QtWidgets.QLabel("", page)
        self._editor_note.setObjectName("cron-note")
        self._editor_note.setWordWrap(True)
        layout.addWidget(self._editor_note)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", page)
        cancel.setObjectName("secondary")
        cancel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self._cancel_edit)
        self._save_button = QtWidgets.QPushButton("创建定时任务", page)
        self._save_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._save_button.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self._save_button)
        layout.addLayout(buttons)

        self._sync_schedule_widgets()
        return page

    def _sync_schedule_widgets(self) -> None:
        freq = self._freq_box.currentData()
        self._custom_edit.setVisible(freq == "custom")
        self._week_box.setVisible(freq == "weekly")
        self._dom_spin.setVisible(freq == "monthly")
        self._time_edit.setVisible(freq in {"daily", "weekdays", "weekly", "monthly", "hourly"})
        self._time_edit.setDisplayFormat("HH:mm" if freq != "hourly" else ":mm")

    # ── 引擎回包 ────────────────────────────────────────────────────────────
    def _on_result(self, tag: str, payload: dict) -> None:
        ok = bool(payload.get("ok"))
        if tag == "cron.list":
            if not ok:
                self._status.setText(f"加载定时任务失败：{payload.get('message') or ''}")
                self._status.setStyleSheet(self.NOTE_ERROR)
                return
            self._jobs = [row for row in (payload.get("jobs") or []) if isinstance(row, dict)]
            self._rebuild_list()
            return
        if tag == "cron.detail":
            if not ok:
                self._status.setText(str(payload.get("message") or "读不到这条任务"))
                self._status.setStyleSheet(self.NOTE_ERROR)
                return
            record = payload.get("record")
            if isinstance(record, dict):
                self._records[str(record.get("id") or self._current)] = record
                if str(record.get("id") or self._current) == self._current:
                    self._paint_detail(record)
            return
        if tag.startswith("cron.runs"):
            asked = tag.partition(":")[2]
            if asked and asked != self._current:
                # 回的是别的任务的运行记录（切任务时后发先回）：丢掉，别画到当前这条头上。
                return
            rows = (payload.get("runs") or []) if ok else []
            self._paint_runs(rows)
            # 有活着的运行（比如别处刚点过触发、或这条正在跑）就把这条当「运行中」；
            # 关窗再开时看不到在途的回包，只能靠运行记录里这面旗。
            active = self._current if any(_run_is_running(row) for row in rows) else ""
            if active != self._active_job:
                self._active_job = active
                record = self._records.get(self._current)
                if record:
                    self._paint_detail(record)
                self._rebuild_list()
            return
        if tag in {"cron.add", "cron.update"}:
            self._save_button.setEnabled(True)
            self._save_button.setText("保存更改" if self._editing else "创建定时任务")
            if not ok:
                message = str(payload.get("message") or "保存失败")
                self._editor_note.setText(f"保存失败：{message}")
                self._editor_note.setStyleSheet(self.NOTE_ERROR)
                return
            created = payload.get("job_id") or (payload.get("job") or {}).get("job_id")
            self._status.setText("定时任务已创建" if tag == "cron.add" else "定时任务已更新")
            self._status.setStyleSheet(self.NOTE_OK)
            self._pages.setCurrentIndex(0)
            record = payload.get("record") if isinstance(payload.get("record"), dict) else None
            if record and record.get("id"):
                self._current = str(record["id"])
            elif created:
                self._current = str(created)
            self._engine.cron_list()
            self._refresh_detail()
            return
        if tag.startswith("cron.trigger:"):
            # 哪一条跑完就只放开哪一条：别的任务在等回包，不该被这条连累。
            job_id = tag.partition(":")[2]
            self._firing.pop(job_id, None)
            self._set_poll_interval()
            self._apply_busy()
            if job_id == self._current:
                if not ok:
                    self._status.setText(f"触发失败：{payload.get('message') or ''}")
                    self._status.setStyleSheet(self.NOTE_ERROR)
                else:
                    self._status.setText("定时任务已触发")
                    self._status.setStyleSheet(self.NOTE_OK)
            self._engine.cron_list()
            self._refresh_detail()
            return
        if not ok:
            self._status.setText(str(payload.get("message") or "操作失败"))
            self._status.setStyleSheet(self.NOTE_ERROR)
            return
        if tag == "cron.remove":
            self._status.setText("定时任务已删除")
            self._status.setStyleSheet(self.NOTE_OK)
            self._current = ""
            self._pages.setCurrentIndex(0)
            self._clear_detail()
        elif tag in {"cron.pause", "cron.resume"}:
            self._status.setText("定时任务已暂停" if tag == "cron.pause" else "定时任务已恢复")
            self._status.setStyleSheet(self.NOTE_OK)
        self._engine.cron_list()
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        """操作完把当前这条重读一遍：状态词、按钮名（暂停/恢复）和运行记录都得跟着变。"""
        if not self._current:
            return
        self._engine.cron_detail(self._current)
        self._engine.cron_runs(self._current, 12)

    def _on_changed(self) -> None:
        if self.isVisible():
            self._engine.cron_list()
            self._refresh_detail()

    # ── 清单 ────────────────────────────────────────────────────────────────
    def _rebuild_list(self) -> None:
        needle = self._search.text().strip().lower()
        self._list.blockSignals(True)
        self._list.clear()
        shown = 0
        for job in self._jobs:
            name = str(job.get("name") or job.get("job_id") or "")
            text = f"{name} {job.get('prompt_preview') or ''}".lower()
            if needle and needle not in text:
                continue
            item = QtWidgets.QListWidgetItem(self._list)
            job_id = str(job.get("job_id") or job.get("id") or "")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, job_id)
            item.setSizeHint(QtCore.QSize(10, 50))
            self._list.setItemWidget(item, _JobRow(job, self._list, firing=job_id in self._firing or job_id == self._active_job))
            shown += 1
            if job_id == self._current:
                item.setSelected(True)
        self._list.blockSignals(False)
        if self._jobs and not shown and needle:
            self._status.setText("没有匹配的任务")
            self._status.setStyleSheet("")
        elif not self._jobs:
            self._clear_detail()
            self._status.setText("暂无排程任务")
            self._status.setStyleSheet("")
        if not self._current and shown:
            self._list.setCurrentRow(0)
            self._on_selected()

    def _on_selected(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        job_id = str(items[0].data(QtCore.Qt.ItemDataRole.UserRole) or "")
        if not job_id or job_id == self._current:
            return
        self._current = job_id
        self._pages.setCurrentIndex(0)
        record = self._records.get(job_id)
        if record:
            self._paint_detail(record)
        else:
            self._clear_detail()
            self._detail_name.setText(self._row_name(job_id))
        self._engine.cron_detail(job_id)
        self._engine.cron_runs(job_id, 12)

    def _row_name(self, job_id: str) -> str:
        for job in self._jobs:
            if str(job.get("job_id") or job.get("id") or "") == job_id:
                return str(job.get("name") or job_id)
        return job_id

    def _row(self, job_id: str) -> dict:
        for job in self._jobs:
            if str(job.get("job_id") or job.get("id") or "") == job_id:
                return job
        return {}

    # ── 详情 ────────────────────────────────────────────────────────────────
    def _clear_detail(self) -> None:
        self._detail_name.setText("")
        self._detail_state.setText("")
        self._detail_schedule.setText("")
        self._detail_prompt.setPlainText("")
        self._runs.clear()

    def _paint_detail(self, record: dict) -> None:
        self._detail_name.setText(str(record.get("name") or record.get("id") or ""))
        state_text, _ = _row_state({**record, "enabled": record.get("enabled", True)})
        if self._current and (self._current in self._firing or self._active_job == self._current):
            state_text = "运行中"
        bits = [f"状态：{state_text}"]
        bits.append(f"下次：{_when(record.get('next_run_at'))}")
        if record.get("last_run_at"):
            bits.append(f"上次：{_when(record.get('last_run_at'))}")
        self._detail_state.setText("　·　".join(bits))
        schedule = _schedule_text(record)
        left = _repeat_left(record.get("repeat"))
        if left:
            schedule = f"{schedule}（还会跑 {left} 次）" if schedule else f"还会跑 {left} 次"
        self._detail_schedule.setText(f"排程：{schedule or '—'}")
        self._detail_prompt.setPlainText(str(record.get("prompt") or ""))
        paused = record.get("state") == "paused" or record.get("enabled") is False
        self._pause_button.setText("恢复" if paused else "暂停")
        self._apply_busy()
        # 跑完但东西没送到也算失败：last_error 是 agent 报错，投递失败记在 last_delivery_error。
        text = ""
        if record.get("last_error"):
            text = f"上次失败：{record.get('last_error')}"
        elif record.get("last_delivery_error"):
            text = f"上次投递失败：{record.get('last_delivery_error')}"
        if text:
            self._status_error_text = text
            self._status.setText(text)
            self._status.setStyleSheet(self.NOTE_ERROR)
        elif self._status_error_text and self._status.text() == self._status_error_text:
            # 上次失败已经过去（后面成功跑过一趟，两个字段都被清空）：这条红字得跟着消失，
            # 不然它会一直挂到面板重开。只清自己写的那句，别的提示照旧留着。
            self._status.setText("")
            self._status.setStyleSheet("")
            self._status_error_text = ""

    def _paint_runs(self, runs: list) -> None:
        self._runs.clear()
        rows = [row for row in runs if isinstance(row, dict)]
        if not rows and self._current not in self._firing:
            item = QtWidgets.QListWidgetItem("尚无运行")
            item.setForeground(QtGui.QColor(255, 255, 255, 105))
            self._runs.addItem(item)
            return
        for run in rows[:12]:
            stamp = run.get("started_at")
            try:
                when = time.strftime("%m-%d %H:%M", time.localtime(float(stamp))) if stamp else "—"
            except (TypeError, ValueError):
                when = "—"
            title = str(run.get("title") or run.get("session_id") or "一次运行")
            if _run_is_running(run):
                title = "运行中 · " + title
            line = f"{when}　{title}"
            session_id = str(run.get("id") or "") if str(run.get("source") or "") == "cron" else ""
            if session_id:
                item = QtWidgets.QListWidgetItem()
                item.setSizeHint(QtCore.QSize(0, 26))
                self._runs.addItem(item)
                self._runs.setItemWidget(item, self._run_row(line, session_id, title))
                continue
            item = QtWidgets.QListWidgetItem(line)
            item.setForeground(QtGui.QColor(246, 223, 234, 218))
            self._runs.addItem(item)
        # 刚点下「立即触发」那一趟还没落地成运行记录：先替它占一行（照桌面的「排队中的运行」）。
        # 运行记录里出现不早于点击时刻的那一行就算落地；一直不落地，过了 QUEUE_SETTLE_TIMEOUT_S 也撤掉。
        clicked = self._firing.get(self._current)
        if clicked:
            mono, wall = clicked
            settled = any(float(row.get("started_at") or 0) >= wall - QUEUE_START_SLACK_S for row in rows)
            if not settled and time.monotonic() - mono < QUEUE_SETTLE_TIMEOUT_S and not self._runs_has_active():
                self._runs.insertItem(0, self._pending_item())

    def _run_row(self, line: str, session_id: str, title: str) -> QtWidgets.QWidget:
        """运行记录一行：左边时间+标题，右边「详情」——点开看这一趟跑的完整内容。"""
        row = QtWidgets.QWidget(self._runs)
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(8)
        label = QtWidgets.QLabel(line, row)
        label.setObjectName("cron-run-line")
        button = QtWidgets.QPushButton("详情", row)
        button.setObjectName("cron-run-detail")
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setToolTip("看这一趟跑的完整内容")
        button.clicked.connect(lambda _=False, sid=session_id, head=title: self._show_run_detail(sid, head))
        lay.addWidget(label, 1)
        lay.addWidget(button)
        return row

    def _show_run_detail(self, session_id: str, heading: str) -> None:
        """只读详情窗：整份取那场运行的历史，不动当前正在聊的那场。"""
        # 窗是 ApplicationModal，开着的时候点不到列表，不会有第二个——不用自己记着谁开着。
        _RunDetailDialog(self._engine, session_id, heading, self).show()

    # ── 操作 ────────────────────────────────────────────────────────────────
    def _toggle_paused(self) -> None:
        job_id = self._current
        if not job_id:
            return
        record = self._records.get(job_id) or self._row(job_id)
        paused = record.get("state") == "paused" or record.get("enabled") is False
        self._engine.cron_set_paused(job_id, paused=not paused)

    def _pending_item(self) -> QtWidgets.QListWidgetItem:
        """「排队中的运行」占位行：点了触发但引擎还没把这一趟落成运行记录时用。"""
        item = QtWidgets.QListWidgetItem(f"{time.strftime('%m-%d %H:%M')}　排队中的运行")
        item.setForeground(QtGui.QColor(246, 223, 234, 170))
        return item

    def _trigger_now(self) -> None:
        """立即触发：引擎那边要等这一趟跑完才回包，所以点下去先把这条的「运行中 / 排队中」画上。

        等回包是按任务记的（`_firing`）：A 在跑的时候照样能触发 B，两条各跑各的。
        """
        job_id = self._current
        if not job_id or job_id in self._firing:
            return
        self._firing[job_id] = (time.monotonic(), time.time())
        self._status.setText("正在触发定时任务…")
        self._status.setStyleSheet("")
        self._rebuild_list()
        record = self._records.get(job_id)
        if record:
            self._paint_detail(record)
        self._apply_busy()
        for index in range(self._runs.count() - 1, -1, -1):
            existing = self._runs.item(index)
            if existing is not None and existing.text() == "尚无运行":
                self._runs.takeItem(index)
        if not self._runs_has_active():
            self._runs.insertItem(0, self._pending_item())
        self._set_poll_interval()
        self._engine.cron_trigger(job_id)

    def _set_poll_interval(self) -> None:
        """有触发在等回包就 1 秒重读一次，都回来了再退回 8 秒。"""
        interval = FIRE_POLL_MS if self._firing else RUNS_POLL_MS
        if self._poll.interval() != interval:
            self._poll.setInterval(interval)
        if not self._poll.isActive():
            self._poll.start()

    def _poll_tick(self) -> None:
        """平时 8 秒、有触发在等回包时 1 秒重读一次：排程跑和手动触发都能及时落到界面上。

        回包可能永远不来（引擎那一趟被掐掉、连接换了），所以给每次触发一个墙钟上限：
        超了就把这条放开、写「结果未知」——不能让人对着灰键等下去，也不能连累别的任务。
        """
        now = time.monotonic()
        expired = [job_id for job_id, (at, _wall) in self._firing.items() if now - at > FIRE_GIVE_UP_S]
        for job_id in expired:
            self._firing.pop(job_id, None)
            if job_id == self._current:
                self._status.setText("已在跑，结果未知（超过 30 分钟没等到回包）")
                self._status.setStyleSheet(self.NOTE_ERROR)
        if expired:
            self._apply_busy()
            self._rebuild_list()
        self._set_poll_interval()
        self._engine.cron_list()
        self._refresh_detail()

    def _runs_has_active(self) -> bool:
        for index in range(self._runs.count()):
            item = self._runs.item(index)
            if item is not None and "运行中" in item.text():
                return True
        return False

    def _apply_busy(self) -> None:
        """这条在等它自己的触发回包时，四个动作别让人再点第二下；别的任务不受影响。"""
        busy = self._current in self._firing
        has_job = bool(self._current)
        for button in (self._pause_button, self._edit_button, self._delete_button, self._trigger_button):
            button.setEnabled(has_job and not busy)
        self._trigger_button.setText("触发中…" if busy else "立即触发")

    def _delete_current(self) -> None:
        job_id = self._current
        if not job_id:
            return
        name = self._row_name(job_id)
        # 用仓库里的置顶确认框：`QMessageBox` 会被置顶的面板压住，看不见却把面板卡在 exec() 里。
        if not ConfirmDialog.ask(
            self,
            "删除定时任务？",
            f"这会永久移除「{name}」，它会立刻停止触发。",
            "删除",
        ):
            return
        self._engine.cron_delete(job_id)

    def _start_create(self) -> None:
        self._editing = ""
        self._editor_title.setText("新建定时任务")
        self._save_button.setText("创建定时任务")
        self._save_button.setEnabled(True)
        self._name_edit.clear()
        self._prompt_edit.clear()
        self._repeat_edit.clear()
        self._custom_edit.clear()
        self._freq_box.setCurrentIndex(0)
        self._time_edit.setTime(QtCore.QTime(9, 0))
        self._editor_note.clear()
        self._editor_note.setStyleSheet("")
        self._sync_schedule_widgets()
        self._set_deliver(None)
        self._pages.setCurrentIndex(1)
        self._name_edit.setFocus()

    def _start_edit(self) -> None:
        job_id = self._current
        record = self._records.get(job_id) or {}
        if not job_id or not record:
            self._status.setText("这条任务还没读出来，等一下再试")
            self._status.setStyleSheet(self.NOTE_ERROR)
            return
        self._editing = job_id
        self._editor_title.setText("编辑定时任务")
        self._save_button.setText("保存更改")
        self._save_button.setEnabled(True)
        self._name_edit.setText(str(record.get("name") or ""))
        self._prompt_edit.setPlainText(str(record.get("prompt") or ""))
        repeat = _repeat_left(record.get("repeat"))
        self._repeat_edit.setText(str(repeat) if repeat else "")
        self._fill_schedule(record)
        self._editor_note.clear()
        self._editor_note.setStyleSheet("")
        self._set_deliver(record)
        self._pages.setCurrentIndex(1)
        self._prompt_edit.setFocus()

    def _cancel_edit(self) -> None:
        self._editing = ""
        self._save_button.setEnabled(True)
        self._save_button.setText("创建定时任务")
        self._pages.setCurrentIndex(0)

    def _set_deliver(self, record: dict | None) -> None:
        """选中这条记录的投递目标。记录里是 origin/latest 以外的值（local、platform:chat_id…）
        就临时加一项照原文显示——面板上的字必须能和存储逐字核对，不能显示成一个它其实不是的值。"""
        text = _deliver_text(record)
        index = self._deliver_box.findData(text)
        if index < 0:
            self._deliver_box.addItem(text, text)
            index = self._deliver_box.count() - 1
        self._deliver_box.setCurrentIndex(index)

    def _fill_schedule(self, record: dict) -> None:
        raw = record.get("schedule")
        schedule: dict = raw if isinstance(raw, dict) else {}
        kind = str(schedule.get("kind") or "")
        expr = str(schedule.get("expr") or "").strip()
        minutes = schedule.get("minutes")
        self._freq_box.blockSignals(True)
        if kind == "cron" and expr:
            parts = expr.split()
            freq, hour, minute = "custom", 9, 0
            if len(parts) == 5 and parts[0].isdigit() and parts[1:].count("*") == 4:
                freq, minute = "hourly", int(parts[0])
            elif len(parts) == 5 and parts[2:] == ["*", "*", "*"]:
                minute, hour = int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 9
                freq = "daily"
            elif len(parts) == 5 and parts[1:] == ["*", "*", "1-5"]:
                minute, hour, freq = int(parts[0]), int(parts[1]), "weekdays"
            elif len(parts) == 5 and parts[2:4] == ["*", "*"] and parts[4].isdigit():
                minute, hour, freq = int(parts[0]), int(parts[1]), "weekly"
                index = self._week_box.findData(parts[4])
                self._week_box.setCurrentIndex(max(0, index))
            elif len(parts) == 5 and parts[1] == "*" and parts[3] == "*" and parts[4] == "*" and parts[2].isdigit():
                minute, hour, freq = int(parts[0]), int(parts[1]), "monthly"
                self._dom_spin.setValue(int(parts[2]))
            self._freq_box.setCurrentIndex(max(0, self._freq_box.findData(freq)))
            self._time_edit.setTime(QtCore.QTime(hour % 24, minute % 60))
            self._custom_edit.setText("" if freq != "custom" else expr)
        elif kind == "interval" and isinstance(minutes, int):
            freq = "every15" if minutes == 15 else "custom"
            self._freq_box.setCurrentIndex(max(0, self._freq_box.findData(freq)))
            self._custom_edit.setText("" if freq != "custom" else f"{minutes}m")
        else:
            self._freq_box.setCurrentIndex(max(0, self._freq_box.findData("custom")))
            self._custom_edit.setText(str(record.get("schedule_display") or schedule.get("run_at") or ""))
        self._freq_box.blockSignals(False)
        self._sync_schedule_widgets()

    def _schedule_text(self) -> str:
        freq = self._freq_box.currentData()
        if freq == "custom":
            return self._custom_edit.text().strip()
        moment = self._time_edit.time()
        minute, hour = moment.minute(), moment.hour()
        if freq == "daily":
            return f"{minute} {hour} * * *"
        if freq == "weekdays":
            return f"{minute} {hour} * * 1-5"
        if freq == "weekly":
            return f"{minute} {hour} * * {self._week_box.currentData()}"
        if freq == "monthly":
            return f"{minute} {hour} {self._dom_spin.value()} * *"
        if freq == "hourly":
            return f"{minute} * * * *"
        return "*/15 * * * *"

    def _save(self) -> None:
        prompt = self._prompt_edit.toPlainText().strip()
        schedule = self._schedule_text()
        if not prompt:
            self._editor_note.setText("提示词为必填项。")
            self._editor_note.setStyleSheet(self.NOTE_ERROR)
            return
        if not schedule:
            self._editor_note.setText("排程为必填项。")
            self._editor_note.setStyleSheet(self.NOTE_ERROR)
            return
        name = self._name_edit.text().strip()
        repeat_text = self._repeat_edit.text().strip()
        fields: dict = {"name": name or _preview(prompt, 18), "prompt": prompt, "schedule": schedule,
                        "deliver": self._deliver_box.currentData()}
        if repeat_text:
            if not repeat_text.isdigit() or int(repeat_text) <= 0:
                self._editor_note.setText("重复次数要写正整数，或者留空表示一直跑。")
                self._editor_note.setStyleSheet(self.NOTE_ERROR)
                return
            fields["repeat"] = int(repeat_text)
        if not self._editor_note.text().strip():
            self._editor_note.setText("正在保存…")
        self._save_button.setEnabled(False)
        self._save_button.setText("保存中…")
        self._engine.cron_save(self._editing, fields)

    # ── 窗口 ────────────────────────────────────────────────────────────────
    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt
        if watched is self._titlebar:
            kind = event.type()
            if kind == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if kind == QtCore.QEvent.Type.MouseMove and self._drag_from is not None:
                self.move(event.globalPosition().toPoint() - self._drag_from)
                return True
            if kind == QtCore.QEvent.Type.MouseButtonRelease:
                self._drag_from = None
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if self._pages.currentIndex() == 1:
                self._cancel_edit()
                return
            self.reject()
            return
        super().keyPressEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 - Qt
        self._poll.stop()
        for signal, slot in (
            (self._engine.cron_result, self._on_result),
            (self._engine.cron_changed, self._on_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)
