"""「设置模型」对话框：填密钥 + 选模型，保存即生效。

之前那个按钮只是把下拉框弹开，模型没配好时等于什么也没做；真正要用户做的事——把哪家的密钥
填进引擎的 `<home>/.env`——没有地方可填。这个对话框就是那个地方：

- 供应商、要填的变量名、是否已经配过，全从引擎的表里来（不写死任何一家）；
- 密钥只交给引擎的 `save_env_value` 写盘（它负责更新同名行、让缓存失效），我们不自己拼 .env；
- 保存后立即让引擎切到选中的模型，不需要重启程序。

窗口用顶层 + 置顶：主界面是无边框置顶的 Tool 窗口，挂在它下面的模态框会被挡到后面、点不着。
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app.engine_config import workspace_from_config
from app.env_keys import env_file
from backends import catalog
from settings import ROOT, SETTINGS, ConfigError


def _soul_path() -> Path:
    """她的人设文件：和 `.env` 同一个 home（`NOESIA_HOME`），路径只解析这一次。"""
    return env_file().with_name("SOUL.md")


def _read_soul() -> str:
    """磁盘上现在那份人设；读不到就返回空的，别让设置窗打不开。"""
    try:
        return _soul_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def _default_soul() -> str:
    """安装时写进 `SOUL.md` 的那份默认人设。

    用的就是引擎首次启动时写的同一个常量（`src/engine/noesia_cli/default_soul.py`），不在界面里
    抄第二份——抄一份就会和安装时的内容悄悄走偏。界面进程的 sys.path 里没有 `src/engine`（引擎是
    独立进程），所以按文件加载这一个纯常量模块，不去改 sys.path。
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "engine" / "noesia_cli" / "default_soul.py"
    spec = importlib.util.spec_from_file_location("noesia_default_soul", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"读不到默认人设：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DEFAULT_SOUL_MD

STYLE = """
/* 全透明、无彩：只有不同程度的白和黑，不再出现任何蓝色 */
QDialog { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 12px; color: #eef1f6; }
#dialog-card {
    background-color: rgba(18, 18, 22, 236);
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 16px;
}
#titlebar { background: transparent; }
#dialog-title { color: #ffffff; font-size: 14px; font-weight: 600; letter-spacing: 0.3px; }
#dialog-sub { color: rgba(255, 255, 255, 100); font-size: 11px; }
#dialog-hint { color: rgba(255, 255, 255, 100); font-size: 11px; }
#live { color: rgba(255, 255, 255, 158); font-size: 11.5px; }
#block-name { color: rgba(255, 255, 255, 246); font-size: 12.5px; font-weight: 600; }
#closebtn {
    background: transparent;
    border: none;
    border-radius: 8px;
    color: rgba(255, 255, 255, 110);
    font-size: 13px;
}
#closebtn:hover { background: rgba(255, 255, 255, 26); color: #ffffff; }
/* 页签：透明底的分段胶囊，选中的只是白一点、边白一点 */
QTabWidget::pane { border: none; background: transparent; }
/* 窄窗口真挤不下时留两个几乎看不见的翻页箭头，别让最后一个页签点不到 */
QTabBar QToolButton { background: transparent; border: none; width: 9px; }
QTabBar QToolButton:hover { background: rgba(255, 255, 255, 20); border-radius: 5px; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: 9px;
    color: rgba(255, 255, 255, 120);
    padding: 4px 8px;
    margin-right: 4px;
    margin-bottom: 2px;
    font-size: 11.5px;
}
QTabBar::tab:hover { color: #ffffff; border-color: rgba(255, 255, 255, 50); }
QTabBar::tab:selected {
    background: rgba(255, 255, 255, 24);
    border: 1px solid rgba(255, 255, 255, 78);
    color: #ffffff;
    font-weight: 600;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background-color: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 8px;
    color: #ffffff;
    padding: 3px 10px;
    font-size: 12px;
    selection-background-color: rgba(255, 255, 255, 70);
    selection-color: #10121a;
}
QLineEdit:hover, QComboBox:hover, QPlainTextEdit:hover { border-color: rgba(255, 255, 255, 62); }
QLineEdit:focus, QPlainTextEdit:focus { border-color: rgba(255, 255, 255, 130); background-color: rgba(255, 255, 255, 16); }
QLineEdit:disabled, QComboBox:disabled { color: rgba(255, 255, 255, 70); border-color: rgba(255, 255, 255, 16); }
QComboBox::drop-down { border: none; width: 20px; }
QPlainTextEdit { padding: 6px 10px; }
QComboBox QAbstractItemView {
    background-color: rgba(24, 24, 28, 246);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 9px;
    color: #eef1f6;
    padding: 4px;
    outline: none;
    selection-background-color: rgba(255, 255, 255, 40);
    selection-color: #ffffff;
}
QComboBox QAbstractItemView::item { padding: 4px 8px; border-radius: 5px; }
/* 每家一块：透明玻璃，只有发丝边；选中的那块白一点点，左边一道竖光 */
#text-block, #voice-block {
    background-color: rgba(255, 255, 255, 6);
    border: 1px solid rgba(255, 255, 255, 20);
    border-radius: 12px;
}
/* 选择标记是 _SelectDot 自己画的（细环 + 白核 + 一圈柔光），这里只负责文字和留位 */
QRadioButton {
    background: transparent;
    border: none;
    padding: 1px 2px 1px 17px;
    color: rgba(255, 255, 255, 105);
    font-size: 11.5px;
}
QRadioButton:hover { color: rgba(255, 255, 255, 215); }
QRadioButton:checked { color: #ffffff; font-weight: 600; }
QRadioButton::indicator { width: 0; height: 0; border: none; background: transparent; }
QCheckBox { color: rgba(255, 255, 255, 200); font-size: 12px; spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; border-radius: 4px; border: 1px solid rgba(255, 255, 255, 70); background: rgba(255, 255, 255, 8); }
QCheckBox::indicator:checked { background: rgba(255, 255, 255, 220); border: 1px solid rgba(255, 255, 255, 220); }
/* 主按钮是一块压白，次按钮只有一圈发丝边 */
QPushButton {
    background-color: rgba(255, 255, 255, 225);
    border: none;
    border-radius: 9px;
    color: #14161c;
    padding: 7px 16px;
    font-size: 12.5px;
    font-weight: 600;
}
QPushButton:hover { background-color: #ffffff; }
QPushButton:pressed { background-color: rgba(255, 255, 255, 180); }
#secondary {
    background-color: transparent;
    border: 1px solid rgba(255, 255, 255, 34);
    color: rgba(255, 255, 255, 185);
    font-weight: 400;
}
#secondary:hover { background-color: rgba(255, 255, 255, 16); color: #ffffff; border-color: rgba(255, 255, 255, 70); }
/* 页签里的长列表：横向一律不滚，竖向的条细、圆角、白半透明，没有箭头也没有凹槽 */
#dialog-scroll, #dialog-scroll > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0 2px 0; border: none; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 60); border-radius: 3px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 120); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; border: none; background: transparent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; background: none; }
"""


# ElevenLabs 的文本转语音模型（官方模型表 + 计费页）。价格是他家每 1K 字符的价，
# 只写页面上能对上的四个；延迟是官方文档标的中位值。
VOICE_MODELS: tuple[tuple[str, str], ...] = (
    ("eleven_v4", "eleven_v4（情感最强，最接近人类）· $0.08/千字符"),
    ("eleven_v4_turbo", "eleven_v4_turbo（实时，约 100ms）· $0.04/千字符"),
    ("eleven_v3", "eleven_v3（情感饱满的戏剧演绎）· $0.08/千字符"),
    ("eleven_v3_conversational", "eleven_v3 对话版（低延迟，约 280ms）· $0.04/千字符"),
    ("eleven_multilingual_v2", "eleven_multilingual_v2（稳、29 种语言）"),
    ("eleven_flash_v2_5", "eleven_flash_v2_5（最快，约 75ms）"),
)


class _VoiceFetcher(QtCore.QObject):
    """在后台线程里拉音色清单：ElevenLabs 要联网，最慢一秒多，不能卡住对话框。"""

    loaded = QtCore.Signal(list)
    failed = QtCore.Signal(str)

    def fetch(self, api_key: str = "") -> None:
        from app import speech_prefs

        try:
            voices = list(speech_prefs.fetch_voices(api_key))
        except Exception as exc:  # noqa: BLE001 - 网络/Key/额度都可能，原样告诉界面
            self._emit(self.failed, str(exc))
            return
        self._emit(self.loaded, voices)

    @staticmethod
    def _emit(signal, payload) -> None:
        """对话框可能在拉音色途中被关掉：那时信号源已经没了，emit 会抛 RuntimeError。

        这不是错误，是「没人接电话」——咽掉，别在退出时甩一串 traceback。
        """
        try:
            signal.emit(payload)
        except RuntimeError:
            pass


class _FetchJob(QtCore.QRunnable):
    def __init__(self, fetcher: "_VoiceFetcher", api_key: str = "") -> None:
        super().__init__()
        self._fetcher = fetcher
        self._api_key = api_key

    @QtCore.Slot()
    def run(self) -> None:
        self._fetcher.fetch(self._api_key)


def _mask_secret(value: str) -> str:
    """凭证只露末尾 4 位：够认出是哪一把，又不会被截图带走。"""
    text = (value or "").strip()
    return f"已保存 …{text[-4:]}" if len(text) > 8 else "已保存"


def _verify_vidu(token: str) -> str:
    from app import vidu_session

    return vidu_session.verify_token(token)


# 厂商 → 校验函数。以后接别家数字人，实现一个"只读查询"的校验函数挂进来即可。
_TOKEN_VERIFIERS = {"vidu": _verify_vidu}


class _TokenChecker(QtCore.QObject):
    """后台问一次厂商「这把凭证能用吗」——只做最便宜的只读查询，不建会话、不花积分。

    校验函数由 `_TOKEN_VERIFIERS` 按厂商取；没有校验函数的厂商就直接放行（只做格式检查）。
    """

    done = QtCore.Signal(bool, str, str)  # (通过吗, 提示, 校的是哪把凭证)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)

    def start(self, provider: str, token: str) -> None:
        import threading

        verify = _TOKEN_VERIFIERS.get(provider)
        if verify is None:
            self.done.emit(True, "已保存（这家没有在线校验，先只查了格式）", token)
            return

        def work() -> None:
            try:
                self.done.emit(True, verify(token), token)
            except RuntimeError as exc:
                self.done.emit(False, str(exc), token)
            except Exception as exc:  # noqa: BLE001 — 校验线程不能静默死掉
                self.done.emit(False, f"校验没成功：{exc}", token)

        threading.Thread(target=work, daemon=True).start()


def _page_scroller(parent: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """页签里的长列表：只竖着滚，横向一根都不许出现。"""
    area = QtWidgets.QScrollArea(parent)
    area.setObjectName("dialog-scroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
    body = QtWidgets.QWidget(area)
    body.setObjectName("dialog-scroll-body")
    area.setWidget(body)
    return area


def _add_short(combo: QtWidgets.QComboBox, text: str, data: str, limit: int = 0) -> None:
    """下拉里放完整名字（截一半的模型名等于没显示），弹层自己按最长那条撑宽；
    收起状态下控件窄，Qt 会自己打省略号，不会把窗口顶宽。"""
    clean = " ".join(str(text).split())
    combo.addItem(clean if not limit else (clean if len(clean) <= limit else clean[: limit - 1] + "…"), data)
    combo.setItemData(combo.count() - 1, text, QtCore.Qt.ToolTipRole)


def _model_label(provider: str, item: str) -> str:
    """引擎里把 model 填成供应商自己的名字，意思是「不指定，交给它自己挑」——那不是模型，别照抄给用户看。"""
    return "默认（不指定模型，由它自己挑）" if item == provider else item


def _fit_popup(combo: QtWidgets.QComboBox, max_width: int = 520) -> None:
    """弹层至少和最长那条一样宽，不然点开还是「nvidia/nemotron-3-sup…」。"""
    metrics = combo.fontMetrics()
    widest = max((metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    combo.view().setMinimumWidth(min(max(widest + 34, combo.width()), max_width))


def _slim(combo: QtWidgets.QComboBox, length: int = 12) -> QtWidgets.QComboBox:
    """下拉不按最长那条撑宽度：音色名动辄「名字 · 中文 · 女 · accent」，一撑窗口就横着溢出。"""
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(length)
    combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    combo.setFixedHeight(28)  # 跟 Key 框同高，横排过去不参差
    return combo


class _SelectDot(QtWidgets.QRadioButton):
    """「启用」的标记：自己画的一枚小点——未启用是一圈淡白空心环，启用是绿核 + 一圈绿光晕。

    系统那个圆点（或去掉圆点改下划线）都不好看，所以这块不下发 QSS，直接画：
    未选是一圈半透明的空心环；选中是白核外面一圈光晕，方块也跟着亮起来。
    """

    RADIUS = 4.5
    LEFT = 9.0
    GREEN = "#4ade80"  # 启用状态灯的绿

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt 的命名
        hint = super().sizeHint()
        return QtCore.QSize(hint.width() + 16, max(hint.height(), 20))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        super().paintEvent(event)  # 文字交给 QSS 那套（颜色、字重都在样式表里）
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        center = QtCore.QPointF(self.LEFT, self.rect().center().y() + 0.5)
        if self.isChecked():
            # 启用 = 绿灯：实心绿核 + 一圈绿光晕 + 一道细绿环；未启用只有一圈很淡的白环
            green = QtGui.QColor(self.GREEN)
            glow = QtGui.QRadialGradient(center, 9.5)
            glow.setColorAt(0.0, QtGui.QColor(green.red(), green.green(), green.blue(), 130))
            glow.setColorAt(0.55, QtGui.QColor(green.red(), green.green(), green.blue(), 48))
            glow.setColorAt(1.0, QtGui.QColor(green.red(), green.green(), green.blue(), 0))
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, 9.5, 9.5)
            painter.setPen(QtGui.QPen(QtGui.QColor(green.red(), green.green(), green.blue(), 150), 1.0))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawEllipse(center, self.RADIUS + 1.6, self.RADIUS + 1.6)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(green)
            painter.drawEllipse(center, self.RADIUS, self.RADIUS)
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 78), 1.2))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawEllipse(center, self.RADIUS, self.RADIUS)


class _NarrowCombo(QtWidgets.QComboBox):
    """下拉的最小宽度不跟着最长那条走：一条四十字的模型名就够把整页撑出横向滚动条。"""

    NARROW = 96

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt 的命名
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), self.NARROW), hint.height())

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """没点开的时候滚轮不该改选中项。

        用户是在滚这一页找供应商，鼠标划过一个收起状态的下拉就把模型换成了别的，
        而且界面上一声不响——他根本不知道自己「在用」的模型已经被换掉了。
        交给父级滚页面；真要看列表就点开，弹层露出时滚轮照旧可用。
        """
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class _LazyCombo(_NarrowCombo):
    """点开下拉那一刻才去要模型清单：四十多家一次要齐要好几种，谁也不愿意开个设置等半天。"""

    def __init__(self, parent: QtWidgets.QWidget, loader) -> None:
        super().__init__(parent)
        self._loader = loader

    def showPopup(self) -> None:  # noqa: N802 - Qt 的命名
        self._loader(self)
        super().showPopup()


class ModelDialog(QtWidgets.QDialog):  # type: ignore[misc]

    # 出错提示一律红字：灰色小字用户根本看不见（人设页、工作目录页、语音页的说明文字都是 dialog-hint）
    FAIL_STYLE = "color: #ff6b6b; font-size: 11px;"
    # 「现在真在跑的」那几行按档上色，一眼能分出是哪一段；缺配提醒黄字，真报错红字
    # 文本模型（必选、主档）→ 绿；语音引擎 → 紫；语音互动 → 橙；缺配 → 黄；出错 → 红
    LIVE_TEXT_STYLE = "color: #4ade80; font-size: 11.5px;"
    LIVE_VOICE_STYLE = "color: #c084fc; font-size: 11.5px;"
    LIVE_INTERACT_STYLE = "color: #fb923c; font-size: 11.5px;"
    NOTE_STYLE = "color: #ffd166; font-size: 11.5px;"

    TAB_TEXT = 0
    TAB_VOICE = 1
    TAB_AVATAR = 2
    TAB_WORKSPACE = 3
    TAB_SOUL = 4
    """填密钥 / 选模型。数据全部来自引擎的清单（`backends.catalog` 整理过的那份）。"""

    saved = QtCore.Signal(dict)

    def __init__(
        self,
        choices: list[dict],
        parent: QtWidgets.QWidget | None = None,
        current: str = "",
        engine_save=None,
    ) -> None:
        # 无边框 + 半透明：跟聊天窗一样是一块悬在桌面上的玻璃，标题栏自己画
        super().__init__(
            parent,
            QtCore.Qt.Window | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint,
        )
        self.setWindowTitle("设置模型")
        self.setModal(True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        # 关掉这个对话框不等于退出程序：主界面是 `Qt.Tool` 窗，不参与「最后一个窗口关了」的判断，
        # 少了这一行，点保存/取消会把整个 Noesia 一起带走。
        self.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(400)

        # 顺序由调用方（catalog）决定：配好的 → 文档里写了怎么配的 → 其余。这里不再排一遍，
        # 两处各排一次必然打架——之前就是这里把「还没模型可选」的 OAuth 家顶到了第一位。
        self._choices = [row for row in choices if row.get("provider")]
        # 现在生效的是哪一家哪个模型（引擎给的 option id，形如 `deepseek:deepseek-flash`）：
        # 文本页用它决定「在用」圈在谁身上，并且逐家回填上次选过的模型。
        self._current_option = str(current or "")
        # 真正写 .env / 切模型的那一头（main.save_model_key）：点了保存要等它回执才关窗。
        # 没有它（测试、别的调用方）就还是老样子，校验过即关。
        self._engine_save = engine_save
        self._save_waiting = False
        self._save_timer: QtCore.QTimer | None = None
        self._save_notes: list[str] = []

        frame = QtWidgets.QFrame(self)
        frame.setObjectName("dialog-card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)  # 给投影留一圈，不然影子被窗口裁掉
        outer.addWidget(frame)
        shadow = QtWidgets.QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QtGui.QColor(0, 0, 0, 175))
        frame.setGraphicsEffect(shadow)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(20, 15, 20, 16)
        layout.setSpacing(10)

        self._drag_from = None
        titlebar = QtWidgets.QWidget(frame)
        titlebar.setObjectName("titlebar")
        self._titlebar = titlebar
        bar = QtWidgets.QHBoxLayout(titlebar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        title = QtWidgets.QLabel("设置", titlebar)
        title.setObjectName("dialog-title")
        close = QtWidgets.QToolButton(titlebar)
        close.setObjectName("closebtn")
        close.setText("✕")
        close.setToolTip("关掉（Esc 也行）")
        close.setCursor(QtCore.Qt.PointingHandCursor)
        close.clicked.connect(self.reject)
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(close)
        titlebar.installEventFilter(self)  # 按住标题栏可以拖窗口
        layout.addWidget(titlebar)

        hint = QtWidgets.QLabel("每家都能填 Key、选模型；圈了「启用」的那家才真的生效。", frame)
        hint.setObjectName("dialog-sub")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 两个页签：文本模型（原来那套：供应商 + 密钥 + 模型）、语音模型（音色 + 档位 + 开关）
        self._tabs = tabs = QtWidgets.QTabWidget(frame)
        tabs.setObjectName("dialog-tabs")
        tabs.setDocumentMode(True)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setDrawBase(False)
        tabs.setUsesScrollButtons(True)  # 窄窗口真放不下时还能翻到最后一个页签
        text_page = QtWidgets.QWidget(tabs)
        text_layout = QtWidgets.QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 8, 0, 0)
        text_layout.setSpacing(8)
        tabs.addTab(text_page, "文本模型")
        tabs.addTab(self._build_voice_page(tabs), "语音模型")
        tabs.addTab(self._build_avatar_page(tabs), "语音互动")
        tabs.addTab(self._build_workspace_page(tabs), "工作目录")
        tabs.addTab(self._build_soul_page(tabs), "人设")
        layout.addWidget(tabs)

        self._build_text_page(text_layout, frame)

        self._warn = QtWidgets.QLabel("", frame)
        self._warn.setObjectName("dialog-warn")
        self._warn.setWordWrap(True)
        text_layout.addWidget(self._warn)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", frame)
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton("保存并启用", frame)
        save.clicked.connect(self._on_save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        grip = QtWidgets.QSizeGrip(frame)  # 无边框还得能拉大小，右下角留一个抓手
        buttons.addWidget(grip)
        layout.addLayout(buttons)

        self._save_button = save
        if not self._choices:
            # 拿不到模型清单只影响「文本模型」这一页，别把按钮锁死：
            # 锁了就是点了毫无反应，用户看不出是按钮灰着（真踩过）。
            self._warn.setStyleSheet(self.FAIL_STYLE)
            self._warn.setText("还没从引擎拿到模型清单：这一页这次不存，其他几页照常保存。")
        # 一次保存要跑五页：先记住打开设置那一刻的样子，只有真被改过的页才写盘、才可能拦住保存
        self._initial = self._snapshot_pages()

    def _live_label(
        self, parent: QtWidgets.QWidget, text: str, style: str = ""
    ) -> QtWidgets.QLabel:
        """只读一行：现在真正在跑的是谁。

        绿点是你这次想启用谁（意图），这一行是引擎/配置里此刻的事实——两者不混着说，
        才不会出现「界面写着启用、实际跑的是另一家」这种看不懂的状态。
        """
        label = QtWidgets.QLabel(text, parent)
        label.setObjectName("live")
        label.setWordWrap(True)
        if style:
            label.setStyleSheet(style)
        return label

    def _note_label(self, parent: QtWidgets.QWidget, text: str) -> QtWidgets.QLabel:
        """可选功能「还没配好」的提醒：黄字，看得见，但不当成报错。"""
        return self._live_label(parent, text, self.NOTE_STYLE)

    def _live_text_line(self) -> str:
        raw = str(self._current_option or "").strip()
        if not raw:
            return "引擎现在在跑：还没启用任何模型"
        provider, sep, model = raw.partition(":")
        if not sep:  # 引擎给的是裸模型名：按各家清单反查是哪家
            for row in self._choices:
                if raw in (row.get("models") or []):
                    return f"引擎现在在跑：{row.get('label') or row.get('provider')} · {raw}"
            return f"引擎现在在跑：{raw}（不在任何一家的清单里）"
        label = next(
            (str(row.get("label") or row.get("provider")) for row in self._choices
             if row.get("provider") == provider),
            provider,
        )
        return f"引擎现在在跑：{label} · {model}"

    def _build_text_page(self, layout: QtWidgets.QVBoxLayout, frame: QtWidgets.QWidget) -> None:
        """文本模型页：每一家一块，Key 框和模型下拉同时摆在页面上。

        不再「切一家填一家」：想配几家就一次填完，保存时挨个读框——没有草稿表，
        也就没有「哪一环漏了 Key 就丢」的机会。右侧单选决定哪一家真的生效。
        """
        from app import text_prefs

        self._text_blocks: dict[str, dict] = {}
        self._text_saved: dict = {}
        self._text_use = ""
        self._text_group = QtWidgets.QButtonGroup(self)
        self._text_group.setExclusive(True)

        layout.addWidget(self._live_label(frame, self._live_text_line(), self.LIVE_TEXT_STYLE))

        # 四十多家平铺最难找：上面给一个搜索框 + 排序，找谁直接打名字
        tools = QtWidgets.QHBoxLayout()
        tools.setContentsMargins(0, 0, 4, 0)
        tools.setSpacing(6)
        self._text_search = QtWidgets.QLineEdit(frame)
        self._text_search.setObjectName("search")
        self._text_search.setPlaceholderText("搜供应商或模型名…")
        self._text_search.setClearButtonEnabled(True)
        self._text_search.setFixedHeight(28)
        self._text_sort = _NarrowCombo(frame)
        self._text_sort.setObjectName("sort")
        for label, key in (("已配置优先", "smart"), ("名称 A–Z", "az")):
            self._text_sort.addItem(label, key)
        self._text_sort.setFixedHeight(28)
        self._text_sort.setToolTip("配过的排前面，还是按名字排")
        tools.addWidget(self._text_search, 1)
        tools.addWidget(self._text_sort)
        layout.addLayout(tools)
        self._text_search.textChanged.connect(self._text_apply_view)
        self._text_sort.currentIndexChanged.connect(self._text_apply_view)

        scroller = _page_scroller(frame)
        self._text_scroller = scroller
        inner = scroller.widget()
        rows = QtWidgets.QVBoxLayout(inner)
        rows.setContentsMargins(0, 2, 4, 2)
        rows.setSpacing(8)
        layout.addWidget(scroller, 1)

        current_provider, current_model = "", ""
        option = str(self._current_option or "")
        if ":" in option:
            current_provider, current_model = option.split(":", 1)
        elif option:
            current_model = option
        if not current_provider and current_model:
            # 引擎只给了模型名、没带「哪家:」前缀：按各家清单反查，别因为格式就这样一个都不圈
            for row in self._choices:
                if current_model in list(row.get("models") or []):
                    current_provider = str(row.get("provider") or "")
                    break

        # 排一下：配过的、能选模型的排前面，既没配又没清单的沉到底——一屏里先看到跟自己有关的
        ordered = sorted(
            self._choices,
            key=lambda item: (0 if item.get("configured") else 1, 0 if item.get("models") else 1),
        )
        for row in ordered:
            slug = str(row["provider"])
            names = list(row.get("env_vars") or [])  # 只用来写 Key 框的占位提示，不进标题栏
            block = QtWidgets.QFrame(inner)
            block.setObjectName("text-block")
            box = QtWidgets.QVBoxLayout(block)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(7)

            head = QtWidgets.QHBoxLayout()
            # 标题只留名字：括号里的计价说明、变量名都另有出处（变量名就是 Key 框的占位提示），
            # 全塞进标题栏会把每块的最小宽度顶到 500 以上，窄窗口就横着溢出了
            name = QtWidgets.QLabel(str(row.get("label") or slug).split("（")[0], block)
            name.setObjectName("block-name")
            badge = QtWidgets.QLabel("", block)
            badge.setObjectName("dialog-hint")
            env = None  # 变量名就是下面 Key 框的占位提示，标题栏里再挂一遍只会把这一块撑宽
            use = _SelectDot("", block)
            self._text_group.addButton(use)
            head.addWidget(name)
            head.addWidget(use)  # 紧跟着名字：别让它在最右边跟名字隔半个屏幕
            head.addWidget(badge)
            head.addStretch(1)
            box.addLayout(head)

            # 引擎给的全是「名字 + 变量名」：四十多家一次要齐要好几秒，所以清单点开下拉时才取
            model = _slim(_LazyCombo(block, lambda combo, slug=slug: self._ensure_models(slug, combo)), 8)
            if row.get("models"):
                for item in list(row.get("models") or []):
                    _add_short(model, _model_label(slug, item), item)
            else:
                _add_short(model, "点开取模型清单", "")
                model.setToolTip("点开这个下拉就向引擎要这一家的模型清单")
            _fit_popup(model)
            wanted = current_model if slug == current_provider else text_prefs.remembered(slug)
            index = model.findData(wanted) if wanted else -1
            if index >= 0:
                model.setCurrentIndex(index)
            box.addWidget(model)

            key = QtWidgets.QLineEdit(block)
            key.setEchoMode(QtWidgets.QLineEdit.Password)
            key.setPlaceholderText(
                f"粘贴 {names[0]} 的值（已经配过就留空）" if names else "这家不需要密钥（或由引擎的登录态管理）"
            )
            key.setEnabled(bool(names))
            key.setFixedHeight(28)
            box.addWidget(key)

            note = QtWidgets.QLabel("", block)
            note.setObjectName("dialog-hint")
            note.setWordWrap(True)
            box.addWidget(note)

            rows.addWidget(block)
            self._text_blocks[slug] = {
                "row": row,
                "block": block,
                "badge": badge,
                "note": note,
                "use": use,
                "model": model,
                "key": key,
                "configured": bool(row.get("configured")),
                "preferred": wanted,
                "loading": False,
                "error": "",
            }
            key.textChanged.connect(lambda _text, slug=slug: self._text_refresh(slug))
            model.currentIndexChanged.connect(lambda _index, slug=slug: self._text_refresh(slug))
            use.toggled.connect(lambda on, slug=slug: self._text_use_changed(slug, on))

        if current_provider in self._text_blocks:
            self._text_use = current_provider
        else:
            # 引擎还没在用任何一家（一个 Key 都没配过）：一个都不圈。
            # 以前这里回落到「第一家」或者「第一家配置过的」，结果一打开就把 copilot 圈成「在用」，
            # 他没配过 Key，点保存又因为「这页没动过」直接放行——既没圈对也提醒。
            self._text_use = ""
            self._text_group.setExclusive(False)
            for block in self._text_blocks.values():
                block["use"].setChecked(False)  # 互斥组里要先把排他关掉才摘得掉
            self._text_group.setExclusive(True)
        for slug in self._text_blocks:
            self._text_refresh(slug)

        self._text_empty = QtWidgets.QLabel(
            "没有匹配的供应商：换个词试试（也可以搜模型名，比如 gpt）。", inner
        )
        self._text_empty.setObjectName("dialog-hint")
        self._text_empty.setVisible(False)
        rows.addWidget(self._text_empty)
        rows.addStretch(1)  # 内容不够一屏时余量给这条弹簧，块保持自己的高度，不被拉长
        self._text_apply_view()

    def _text_apply_view(self) -> None:
        """按搜索框和排序把这一页重排一遍：找供应商不用一条条往下滚。

        搜索词同时匹配供应商名和它下面的模型名（搜「gpt」能把有 gpt 的家都捞出来），
        隐藏的块只是不显示，填过的 Key/选好的模型都还在。
        """
        if not self._text_blocks:
            return
        query = self._text_search.text().strip().lower()
        mode = str(self._text_sort.currentData() or "smart")

        def label_of(block: dict) -> str:
            return str(block["row"].get("label") or block["row"].get("provider") or "")

        def sort_key(item):
            slug, block = item
            name = label_of(block).lower()
            if mode == "az":
                return (name,)
            return (0 if block["configured"] else 1, 0 if block["row"].get("models") else 1, name)

        ordered = sorted(self._text_blocks.items(), key=sort_key)
        for slug, block in ordered:
            hit = True
            if query:
                parts = [slug.lower(), label_of(block).lower()]
                parts += [str(model).lower() for model in (block["row"].get("models") or [])]
                hit = query in " ".join(parts)
            block["block"].setVisible(hit)
        lay = self._text_scroller.widget().layout()
        for _slug, block in ordered:  # 先全摘下来，再按新顺序放回去
            lay.removeWidget(block["block"])
        for position, (_slug, block) in enumerate(ordered):
            lay.insertWidget(position, block["block"])
        # 用 isHidden 而不是 isVisible：窗口还没显示时 isVisible 一律是 False，会把「没找到」错亮出来
        self._text_empty.setVisible(bool(query) and all(b["block"].isHidden() for _s, b in ordered))

    def _ensure_models(self, slug: str, combo: QtWidgets.QComboBox | None = None) -> None:
        """这一家的模型清单还没取：现在取一次，取回来填进它自己的下拉。"""
        block = self._text_blocks.get(slug)
        if not block:
            return
        row = block["row"]
        if row.get("models"):
            self._fill_block_models(block)
            return
        if block.get("loading"):
            return
        block["loading"] = True
        block["error"] = ""
        try:
            row["models"] = list(catalog.provider_models(slug))
        except catalog.LLMConfigError as exc:
            block["error"] = str(exc)
        finally:
            block["loading"] = False
        if row.get("models"):
            self._fill_block_models(block)
        self._text_refresh(slug)

    def _fill_block_models(self, block: dict) -> None:
        """把清单填进这一块自己的下拉，并停在「上次选的那一个」上。"""
        combo = block["model"]
        picked = str(combo.currentData() or "")  # 可能已经手选过：重填不许把手选的那个冲掉
        combo.blockSignals(True)  # 重填不算用户改选
        combo.clear()
        slug = str(block["row"].get("provider") or "")
        for item in block["row"]["models"]:
            _add_short(combo, _model_label(slug, item), item)
        for wanted in (picked, str(block.get("preferred") or "")):
            index = combo.findData(wanted) if wanted else -1
            if index >= 0:
                combo.setCurrentIndex(index)
                break
        combo.blockSignals(False)
        _fit_popup(combo)

    def _text_refresh(self, slug: str) -> None:
        """一块的样子按「框里有没有新值 / 这家配过没有」重画，别的块不动。"""
        block = self._text_blocks.get(slug)
        if not block:
            return
        row = block["row"]
        typed = block["key"].text().strip()
        using = slug == self._text_use
        block["use"].blockSignals(True)  # 回填不算用户点选
        block["use"].setChecked(using)
        block["use"].blockSignals(False)
        block["use"].setText("启用" if using else "未启用")
        # 选中的那块：白一点点、左边一道竖光当「就是它」的标记；其余只有发丝边
        if using:
            block["block"].setStyleSheet(
                "#text-block { border: 1px solid rgba(255,255,255,66);"
                " border-left: 2px solid rgba(255,255,255,205);"
                " border-radius: 12px; background-color: rgba(255,255,255,14); }"
            )
        else:
            block["block"].setStyleSheet(
                "#text-block { border: 1px solid rgba(255,255,255,20);"
                " border-radius: 12px; background-color: rgba(255,255,255,6); }"
            )
        names = list(row.get("env_vars") or [])
        block["note"].setStyleSheet("")
        block["note"].setVisible(False)  # 没话说就整行收起来，别留一个空行把块撑高
        if not row.get("models"):
            block["badge"].setText("没有模型")
            if block.get("error"):
                block["note"].setStyleSheet(self.FAIL_STYLE)
                block["note"].setText(str(block["error"]))
            else:
                block["note"].setText("")  # 下拉里的占位已经在说「点开取模型清单」
            block["note"].setVisible(bool(block["note"].text().strip()))
            return
        if typed:
            block["badge"].setText("填好了，还没保存")
            block["note"].setText(f"这一串还没保存（{len(typed)} 位）：点「保存并启用」就写进 .env。")
        elif block["configured"]:
            block["badge"].setText("已配置")
            # 占位提示里已经写了「已经配过就留空」，这里不再重复一遍
            block["note"].setText("" if names else "这家用引擎的登录态，没有可填的密钥。")
        else:
            # 没有 Key 可填的家（走引擎登录态）不叫「未配置」，它压根不缺 Key
            block["badge"].setText("用登录态" if not names else "未配置")
            # 占位提示里已经写明「粘贴哪个变量」/「这家用引擎的登录态」，这里不再复述
            block["note"].setText("")
        block["note"].setVisible(bool(block["note"].text().strip()))

    def _text_use_changed(self, slug: str, on: bool) -> None:
        """圈了「未启用」的那家：真正生效的换成它，其余块的单选和状态一起重画。"""
        if not on:
            return
        self._text_use = slug
        self._ensure_models(slug)  # 点了这一家：它还没有模型清单就现在取
        for other in self._text_blocks:
            self._text_refresh(other)

    def _build_voice_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """语音模型页：三家引擎各一块，Key、语言、音色、档位各归各段。

        跟文本页同一个规矩——一次能把几家的 Key 都填上、一次保存全部落进 `.env`；
        右边单选决定现在是谁在说话，其余家只记自己选过的音色。
        开/关不在这里：那个开关在输入框旁边（审批/思考档位那一排），说话前顺手就能关。
        """
        from app import doubao_voices, speech_prefs

        self._voice_picks: dict[str, dict] = {}
        self._voice_blocks: dict[str, dict] = {}
        self._voice_saved: dict = {}
        self._voice_group = QtWidgets.QButtonGroup(self)
        self._voice_group.setExclusive(True)

        page = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        self._voice_prefs = speech_prefs.current()
        self._voices = [item for item in self._voice_prefs["voices"] if isinstance(item, dict)]
        self._loading = False
        self._voice_fetcher = _VoiceFetcher(self)
        self._voice_fetcher.loaded.connect(self._on_voices_loaded)
        self._voice_fetcher.failed.connect(self._on_voices_failed)
        # Key 一填好就自己去拉音色：停手 0.7 秒再发请求，打字和粘贴都不会连发
        self._voice_fetch_timer = QtCore.QTimer(self)
        self._voice_fetch_timer.setSingleShot(True)
        self._voice_fetch_timer.setInterval(700)
        self._voice_fetch_timer.timeout.connect(self._maybe_fetch_voices)

        hint = QtWidgets.QLabel(
            "三家各管各的：Key 和音色各存各家，几家的 Key 可以一次填完、一次保存；"
            "圈了「启用」的那家才会发声。",
            page,
        )
        hint.setObjectName("dialog-hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        now = speech_prefs.current()
        running = str(now.get("provider") or "")
        speaking = {"elevenlabs": "ElevenLabs", "doubao": "豆包", "edge": "Edge"}.get(running, running)
        voice_shown = now.get("voice_name") or now.get("voice_id") or "默认音色"
        has_key, _key_status = speech_prefs.key_status(running)
        if running in speech_prefs.KEYED_PROVIDERS and not has_key:
            # 配置里「启用」的是它，可 `.env` 里没有它的 Key：那就发不出声，不能写「现在发声的是」。
            # 语音本来就是可选的：没配就说没配，不当错误，也不拦保存。
            layout.addWidget(
                self._note_label(
                    page,
                    f"现在启用的是 {speaking}（{voice_shown}），还没填 {speech_prefs.env_key_name(running)}："
                    "这一档暂时发不出声",
                )
            )
        else:
            layout.addWidget(
                self._live_label(page, f"现在发声的是：{speaking} · {voice_shown}", self.LIVE_VOICE_STYLE)
            )

        scroller = _page_scroller(page)
        layout.addWidget(scroller, 1)
        inner = scroller.widget()
        rows = QtWidgets.QVBoxLayout(inner)
        rows.setContentsMargins(0, 2, 8, 2)  # 右边留出滚动条的位置，内容不贴着条
        rows.setSpacing(8)

        for provider, label in speech_prefs.PROVIDERS:
            keyed = provider in speech_prefs.KEYED_PROVIDERS
            has_key, status = speech_prefs.key_status(provider)
            block = QtWidgets.QFrame(inner)
            block.setObjectName("voice-block")
            box = QtWidgets.QVBoxLayout(block)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(7)

            head = QtWidgets.QHBoxLayout()
            name = QtWidgets.QLabel(label.split("（")[0], block)
            name.setObjectName("block-name")
            badge = QtWidgets.QLabel("", block)
            badge.setObjectName("dialog-hint")
            env = None  # 变量名就是 Key 框的占位提示，标题栏里不再挂一遍
            use = _SelectDot("", block)
            self._voice_group.addButton(use)
            head.addWidget(name)
            head.addWidget(use)  # 紧跟着名字：别让它在最右边跟名字隔半个屏幕
            head.addWidget(badge)
            head.addStretch(1)
            box.addLayout(head)

            key = None
            if keyed:
                key = QtWidgets.QLineEdit(block)
                key.setEchoMode(QtWidgets.QLineEdit.Password)
                key.setFixedHeight(28)
                key.setPlaceholderText(
                    "留空就是不改" if has_key else f"粘贴 {speech_prefs.env_key_name(provider)} 的值"
                )
                key.textChanged.connect(lambda _text, p=provider: self._voice_key_changed(p))
                box.addWidget(key)

            lang = None
            if provider == "doubao":  # 247 个音色按语言分组：先选语言，不然一个下拉几百条没法用
                lang = _slim(_NarrowCombo(block), 6)
                lang.addItem("全部语言", "")
                for item in doubao_voices.languages():
                    lang.addItem(item, item)
                lang.setCurrentIndex(max(lang.findData("中文"), 0))
                lang.currentIndexChanged.connect(lambda _index, p=provider: self._fill_voices(p))
                box.addWidget(lang)

            voice_row = QtWidgets.QHBoxLayout()
            voice = _slim(_NarrowCombo(block), 10)
            voice.currentIndexChanged.connect(lambda _index, p=provider: self._voice_pick_changed(p))
            voice_row.addWidget(voice, 1)
            refresh = None
            if provider == "elevenlabs":
                refresh = QtWidgets.QPushButton("刷新", block)
                refresh.setObjectName("secondary")
                refresh.setToolTip("从 ElevenLabs 拉一次音色列表，不消耗字符额度")
                refresh.clicked.connect(self._refresh_voices)
                voice_row.addWidget(refresh)
            box.addLayout(voice_row)

            model = None
            if provider == "elevenlabs":  # 只有这家有合成档位
                model = _slim(_NarrowCombo(block), 8)
                for model_id, model_label in VOICE_MODELS:
                    model.addItem(model_label, model_id)
                index = model.findData(self._voice_prefs.get("model_id"))
                if index >= 0:
                    model.setCurrentIndex(index)
                model.currentIndexChanged.connect(lambda _index, p=provider: self._voice_pick_changed(p))
                box.addWidget(model)

            note = QtWidgets.QLabel(status, block)
            note.setObjectName("dialog-hint")
            note.setWordWrap(True)
            box.addWidget(note)

            rows.addWidget(block)
            self._voice_blocks[provider] = {
                "provider": provider,
                "label": label,
                "keyed": keyed,
                "block": block,
                "badge": badge,
                "note": note,
                "use": use,
                "key": key,
                "voice": voice,
                "model": model,
                "lang": lang,
                "refresh": refresh,
                "configured": has_key,
                "loading": False,
                "error": "",
            }
            use.toggled.connect(lambda on, p=provider: self._voice_use_changed(p, on))
        rows.addStretch(1)

        self._voice_use = str(self._voice_prefs.get("provider") or "edge")
        if self._voice_use not in self._voice_blocks:
            self._voice_use = next(iter(self._voice_blocks), "")
        for provider in self._voice_blocks:
            self._fill_voices(provider)
            self._voice_refresh(provider)
        self._load_voices()  # ElevenLabs 的音色跟着它自己那一把 Key 走
        return page

    # ---- 语音页 ----

    def _voice_block(self, provider: str) -> dict:
        return self._voice_blocks.get(provider) or {}

    @staticmethod
    def _voice_text(item: dict) -> str:
        """下拉里显示「Noesia · 中文 · 女」：账户里 21 个英文预置，只看名字分不出语言。

        音色归属也要说：`professional`/`library` 是付费套餐才给 API 念的，`generated`/`cloned`
        是账号自己的——不标出来，用户选中后听到 402 会一头雾水。
        """
        labels = item.get("labels") or {}
        raw_language = str(labels.get("language") or "")
        raw_gender = str(labels.get("gender") or "")
        bits = [{"zh": "中文", "en": "英语"}.get(raw_language, raw_language),
                {"female": "女", "male": "男"}.get(raw_gender, raw_gender),
                str(labels.get("accent") or "")]
        bits = [bit for bit in bits if bit]
        name = str(item.get("name") or item.get("id") or "")
        if item.get("gender"):  # 豆包清单自带语言/性别，语言已经在上头选过，这里只补性别
            text = f"{name} · {item['gender']}"
        else:
            text = f"{name} · {' · '.join(bits)}" if bits else name
        hint = {
            "professional": "需付费套餐",
            "library": "需付费套餐",
            "generated": "我的音色",
            "cloned": "我的音色",
        }.get(str(item.get("category") or ""), "")
        return f"{text} · {hint}" if hint else text

    def _voice_choices(self, provider: str | None = None) -> list[dict]:
        """这一家能选哪些音色：ElevenLabs 来自账号、豆包按语言筛、Edge 是固定的那几个。"""
        from app import speech_prefs

        provider = provider or self._voice_use
        if provider == "doubao":
            block = self._voice_block(provider)
            lang = str(block["lang"].currentData() or "") if block.get("lang") is not None else ""
            return [voice for voice in speech_prefs.DOUBAO_VOICES if not lang or voice["lang"] == lang]
        if provider == "edge":
            return list(speech_prefs.EDGE_VOICES)
        return list(self._voices)

    def _wanted_voice(self, provider: str) -> str:
        """每个引擎认自己上次选的音色，别把对方的 id 套过来。"""
        from app import speech_prefs

        if provider == str(self._voice_prefs.get("provider") or ""):
            return str(self._voice_prefs.get("voice_id") or "")
        remembered = (self._voice_prefs.get("history") or {}).get(provider) or {}
        if remembered.get("id"):
            return str(remembered["id"])
        if provider == "edge":
            return speech_prefs.EDGE_VOICES[0]["id"]
        if provider == "doubao":
            return speech_prefs.DOUBAO_VOICES[0]["id"]
        return ""

    def _fill_voices(self, provider: str) -> None:
        """把这一家的音色填进它自己的下拉，停在「上次选的那一个」上。"""
        from app import speech_prefs

        block = self._voice_block(provider)
        combo = block.get("voice")
        if combo is None:
            return
        choices = self._voice_choices(provider)
        picks = self._voice_picks.setdefault(provider, {})
        chosen = str(picks.get("voice") or "") or self._wanted_voice(provider)
        combo.blockSignals(True)  # 重填不算用户改选
        combo.clear()
        if not choices:
            _add_short(combo, "填好 Key 就会自动取音色", "")
            combo.blockSignals(False)
            _fit_popup(combo)
            return
        for item in choices:
            _add_short(combo, self._voice_text(item), item["id"])  # 名字给全，宽度交给弹层
        index = combo.findData(chosen)
        if index < 0:
            # 存着的音色不在这份清单里（别人的账号、或账号里删了）：落到「这个账号现在能念」的
            # 第一个音色上。绝不能停在念不出来的 id 上——那会让人以为语音坏了。
            fallback = speech_prefs.usable_default_voice(choices)
            index = combo.findData(fallback)
            if index < 0:
                index = 0
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        picks["voice"] = str(combo.currentData() or "")
        picks["name"] = next((item["name"] for item in choices if item["id"] == picks["voice"]), "")
        _fit_popup(combo)

    def _voice_pick_changed(self, provider: str) -> None:
        """用户自己挑了音色/档位：记在这家引擎名下，清单重填也不许换掉。"""
        block = self._voice_block(provider)
        picks = self._voice_picks.setdefault(provider, {})
        if block.get("voice") is not None:
            picked = str(block["voice"].currentData() or "")
            picks["voice"] = picked
            picks["name"] = next(
                (item["name"] for item in self._voice_choices(provider) if item["id"] == picked), ""
            )
        if block.get("model") is not None:
            picks["model"] = str(block["model"].currentData() or "")

    def _voice_use_changed(self, provider: str, on: bool) -> None:
        """圈了「未启用」的那家：换谁在说话，其余块的单选和光晕一起重画。"""
        if not on:
            return
        self._voice_use = provider
        for other in self._voice_blocks:
            self._voice_refresh(other)

    def _voice_key_changed(self, provider: str) -> None:
        """Key 框里改了字：状态立刻跟着变；ElevenLabs 停手一会儿就自己去拉音色。"""
        block = self._voice_block(provider)
        block["error"] = ""
        self._voice_refresh(provider)
        if provider == "elevenlabs":
            self._voice_fetch_timer.start()

    def _voice_refresh(self, provider: str) -> None:
        """一块的样子按「有没有新 Key / 配过没有 / 谁在用」重画，别的块不动。"""
        from app import speech_prefs

        block = self._voice_block(provider)
        if not block:
            return
        using = provider == self._voice_use
        block["use"].blockSignals(True)  # 回填不算用户点选
        block["use"].setChecked(using)
        block["use"].blockSignals(False)
        block["use"].setText("启用" if using else "未启用")
        if using:
            block["block"].setStyleSheet(
                "#voice-block { border: 1px solid rgba(255,255,255,66);"
                " border-left: 2px solid rgba(255,255,255,205);"
                " border-radius: 12px; background-color: rgba(255,255,255,14); }"
            )
        else:
            block["block"].setStyleSheet(
                "#voice-block { border: 1px solid rgba(255,255,255,20);"
                " border-radius: 12px; background-color: rgba(255,255,255,6); }"
            )
        typed = block["key"].text().strip() if block.get("key") is not None else ""
        if block.get("error"):
            badge = "没取到"
        elif typed:
            badge = "填好了，还没保存"
        elif block["configured"]:
            badge = "已配置"
        elif not block["keyed"]:
            badge = "免费"
        else:
            badge = "未配置"
        block["badge"].setText(badge)
        block["note"].setStyleSheet("")
        if block.get("error"):
            block["note"].setStyleSheet(self.FAIL_STYLE)
            block["note"].setText(str(block["error"]))
        elif typed and (not typed.isascii() or any(ch.isspace() for ch in typed)):
            block["note"].setStyleSheet(self.FAIL_STYLE)
            block["note"].setText("这个 Key 里混了中文或空格，重新粘贴一次。")
        elif typed and len(typed) < 20:
            block["note"].setStyleSheet(self.FAIL_STYLE)
            block["note"].setText(f"这个 Key 不完整（{len(typed)} 位）：一般 40 位上下。")
        elif typed:
            block["note"].setText(f"这一串还没保存（{len(typed)} 位）：点「保存并启用」就写进 .env。")
        elif block.get("loading"):
            block["note"].setText("正在从 ElevenLabs 取音色…")
        else:
            _, status = speech_prefs.key_status(provider)
            block["note"].setText(status)
        block["note"].setVisible(bool(block["note"].text().strip()))

    def _load_voices(self) -> None:
        """去 ElevenLabs 拉一次音色（不消耗字符额度）；拉的过程界面照常能点。"""
        from app import speech_prefs

        block = self._voice_block("elevenlabs")
        if not block or self._loading:
            return
        typed = block["key"].text().strip() if block.get("key") is not None else ""
        if not typed and not speech_prefs.read_api_key("elevenlabs"):
            # 一个 Key 都没有：别空跑一次再把「缺少 API Key」红字甩给用户
            block["note"].setStyleSheet("")
            block["note"].setText("填好 API Key 就会自动取音色。")
            return
        self._loading = True
        block["loading"] = True
        self._voice_refresh("elevenlabs")
        # 输入框里刚填的 Key 优先：还没保存也能先拉清单（首次安装就靠这条才能选音色）
        QtCore.QThreadPool.globalInstance().start(_FetchJob(self._voice_fetcher, typed))

    def _on_voices_loaded(self, voices: list) -> None:
        block = self._voice_block("elevenlabs")
        self._loading = False
        self._voices = [item for item in voices if isinstance(item, dict)]
        if not block:
            return
        before = str((self._voice_picks.get("elevenlabs") or {}).get("voice") or "")
        block["loading"] = False
        block["error"] = ""
        self._fill_voices("elevenlabs")
        self._voice_refresh("elevenlabs")
        if not block.get("error"):
            after = str((self._voice_picks.get("elevenlabs") or {}).get("voice") or "")
            if before and after and before != after:
                # 配置里存的是别人账号的音色（或者账号里删了）：已经换掉，并且说清楚
                block["note"].setText(
                    f"取到 {len(self._voices)} 个音色；原来选的那个不在这个账号里，已经换成当前这个。"
                )
            else:
                block["note"].setText(f"Key 可用，取到 {len(self._voices)} 个音色。")

    def _on_voices_failed(self, message: str) -> None:
        block = self._voice_block("elevenlabs")
        self._loading = False
        if not block:
            return
        block["loading"] = False
        # 红字直说：Key 不对 / 没权限 / 连不上，别让人对着空清单猜
        block["error"] = f"音色没取回来：{message}"
        self._voice_refresh("elevenlabs")

    def _maybe_fetch_voices(self) -> None:
        """输入框里的 Key 变了：像样就自动拉音色，明显不对就当场红字拦下来。"""
        block = self._voice_block("elevenlabs")
        if not block:
            return
        typed = block["key"].text().strip() if block.get("key") is not None else ""
        block["error"] = ""
        if not typed or not typed.isascii() or any(ch.isspace() for ch in typed) or len(typed) < 20:
            self._voice_refresh("elevenlabs")  # 格式都不对就不发请求，红字已经贴上了
            return
        if self._loading:
            return
        self._load_voices()

    def _refresh_voices(self) -> None:
        block = self._voice_block("elevenlabs")
        if block:
            block["error"] = ""
        self._load_voices()

    def voice_selection(self) -> dict:
        """「在用」那家现在选的是什么：音色（ElevenLabs 还带档位），Key 有就一起带上。"""
        if self._voice_saved:
            return dict(self._voice_saved)
        provider = self._voice_use
        block = self._voice_block(provider)
        if not block:
            return {"provider": provider}
        voice_id = str(block["voice"].currentData() or "")
        picked = next((item for item in self._voice_choices(provider) if item["id"] == voice_id), None)
        chosen = {
            "provider": provider,
            "voice_id": voice_id,
            # 存纯名字（下拉里那串「名字 · 中文 · 女」只是给眼睛看的，别写进配置）
            "voice_name": (picked or {}).get("name") or (block["voice"].currentText() if voice_id else ""),
            # 档位只有 ElevenLabs 有；豆包/Edge 没有，别把对方的档位写进配置
            "model_id": str(block["model"].currentData() or "") if block.get("model") is not None else "",
        }
        typed = block["key"].text().strip() if block.get("key") is not None else ""
        if typed:  # 空着不动，免得把已保存的键抹掉
            chosen["api_key"] = typed
        for empty_field in ("voice_id", "voice_name"):
            if not chosen[empty_field]:  # 清单还没拉到时是空的，别拿它覆盖已选的音色
                chosen.pop(empty_field)
        return chosen

    def voice_keys(self) -> dict:
        """每家填过还没保存的 Key：一次配多家时，保存要一起写进 .env。"""
        keys: dict[str, str] = {}
        for provider, block in self._voice_blocks.items():
            if block.get("key") is None:
                continue
            typed = block["key"].text().strip()
            if typed:
                keys[provider] = typed
        return keys

    # ---- 内部 ----



    # 每家一个框、保存挨个读：旧的「切一家填一家 + 草稿表」整套已经删掉

    def selection(self) -> dict:
        """现在界面上这一页的取值：每家填过的 Key + 每家选的模型 + 哪一家「在用」。

        `keys` / `models` 是整张表（一次能配好几家），`key` / `model` 是「在用」那家的，
        留着给只关心一家的调用方用。
        """
        if self._text_saved:
            # 已经交出去过：外面随后再问一次要的还是同一份，别因为界面状态变了给出另一个版本
            return dict(self._text_saved)
        keys: dict[str, str] = {}
        models: dict[str, str] = {}
        for slug, block in self._text_blocks.items():
            typed = block["key"].text().strip()
            if typed:
                keys[slug] = typed
            models[slug] = str(block["model"].currentData() or "")
        using = self._text_use  # 没圈就是没圈：别替他挑一家
        return {
            "provider": using,
            "key": keys.get(using, ""),
            "keys": keys,
            "model": models.get(using, ""),
            "models": models,
        }

    # ---------------- 工作目录页 ----------------

    def _build_avatar_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """语音互动页：选哪家数字人 + 它的凭证。

        **开关不在这里**：真正的开关是输入框那三档（静音 / 纯语音 / 语音互动）。
        这一页只管"用的是哪家、凭证对不对"，别让人以为在这里启用。
        """
        from app import avatar_prefs

        page = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        intro = QtWidgets.QLabel(
            "开关在输入框那个语音按钮上（静音 / 纯语音 / 语音互动），这里只配跟谁连、凭证对不对。",
            page,
        )
        intro.setObjectName("dialog-hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QtWidgets.QLabel("厂商", page))
        self._avatar_provider_box = QtWidgets.QComboBox(page)
        for pid, label, _env in avatar_prefs.PROVIDERS:
            self._avatar_provider_box.addItem(label, pid)
        index = self._avatar_provider_box.findData(avatar_prefs.current()["provider"])
        self._avatar_provider_box.setCurrentIndex(max(index, 0))
        layout.addWidget(self._avatar_provider_box)

        layout.addWidget(QtWidgets.QLabel("数字人模型", page))
        self._avatar_model_box = QtWidgets.QComboBox(page)
        for mid, label in avatar_prefs.MODELS:
            self._avatar_model_box.addItem(label, mid)
        index = self._avatar_model_box.findData(avatar_prefs.current()["model"])
        self._avatar_model_box.setCurrentIndex(max(index, 0))
        layout.addWidget(self._avatar_model_box)

        self._avatar_key_label = QtWidgets.QLabel("凭证", page)
        layout.addWidget(self._avatar_key_label)
        self._avatar_key_edit = QtWidgets.QLineEdit(page)
        self._avatar_key_edit.textChanged.connect(lambda _text: self._on_avatar_key_typed())
        self._avatar_key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        layout.addWidget(self._avatar_key_edit)

        self._avatar_hint = QtWidgets.QLabel("", page)
        self._avatar_hint.setObjectName("dialog-hint")
        self._avatar_hint.setWordWrap(True)
        layout.addWidget(self._avatar_hint)
        try:
            from app import avatar_prefs

            avatar_now = avatar_prefs.current()
            shown_provider = avatar_now.get("label") or avatar_now.get("provider")
            shown_model = avatar_now.get("model")
            if not avatar_prefs.read_token(str(avatar_now.get("provider") or "")):
                # 选了 Vidu 却没 Token：如实说这一档还没配好，不当错误、也不拦保存
                layout.addWidget(
                    self._note_label(
                        page,
                        f"语音互动选的是 {shown_provider}（{shown_model}），还没填 "
                        f"{avatar_now.get('env_key') or 'VIDU_TOKEN'}：这一档暂时开不起来",
                    )
                )
            else:
                layout.addWidget(
                    self._live_label(
                        page,
                        f"语音互动现在用的是：{shown_provider} · {shown_model}",
                        self.LIVE_INTERACT_STYLE,
                    )
                )
        except Exception:  # noqa: BLE001 - 配置读不出来就别显示，不能因此打不开设置
            pass

        self._avatar_note = QtWidgets.QLabel("", page)
        self._avatar_note.setObjectName("dialog-hint")
        self._avatar_note.setWordWrap(True)
        layout.addWidget(self._avatar_note)
        self._avatar_note.setText("")
        layout.addStretch(1)

        self._avatar_pending = ""
        self._avatar_pending_provider = ""
        self._avatar_key_drafts: dict[str, str] = {}
        self._saved_pages: set[str] = set()
        self._avatar_checker = _TokenChecker(self)
        self._avatar_checker.done.connect(self._on_avatar_checked)
        self._avatar_provider_box.currentIndexChanged.connect(self._on_avatar_provider_changed)
        self._on_avatar_provider_changed()  # 初始按当前厂商摆一遍（顺手校一次已存的凭证）
        return page

    def _avatar_provider_ready(self, provider: str) -> bool:
        """这家有没有接好（能校验、能建会话）。接好之前别让人把凭证填进去还以为能用。"""
        return provider in _TOKEN_VERIFIERS

    def _on_avatar_provider_changed(self) -> None:
        from app import avatar_prefs

        provider = self._avatar_provider_box.currentData() or avatar_prefs.PROVIDERS[0][0]
        key_name = avatar_prefs.env_key(provider)
        self._avatar_key_label.setText(f"凭证（写进 .env 的 {key_name}）")
        self._avatar_note.setText(self._avatar_costs(provider))
        stored = avatar_prefs.read_token(provider)
        # 每家一个凭证：切走时把输入框那串收进它自己名下，切回来再填回去
        owner = getattr(self, "_avatar_key_owner", provider)
        if owner != provider:
            self._avatar_key_drafts[owner] = self._avatar_key_edit.text()
        self._avatar_key_owner = provider
        self._avatar_key_edit.blockSignals(True)  # 回填不算用户输入
        self._avatar_key_edit.setText(self._avatar_key_drafts.get(provider, ""))
        self._avatar_key_edit.blockSignals(False)
        self._avatar_key_edit.setPlaceholderText("留空就是不改" if stored else "粘贴控制台里的 Token")
        self._avatar_hint.setStyleSheet("")
        if not self._avatar_provider_ready(provider):
            self._avatar_hint.setText("这家还没接好（没有校验和建会话的实现），先别填。")
            return
        if self._avatar_key_drafts.get(provider, "").strip():
            self._avatar_hint.setText("这一串还没保存，点保存会先校验再写进 .env。")
            return
        if not stored:
            self._avatar_hint.setText("还没填凭证：填了才能用「语音互动」这一档。")
            return
        # 打开页面不联网：跟语音页的 ElevenLabs 一样只看本地存了什么。
        # 以前这里一进来就发起校验，网络慢/没网就永远停在"正在校验…"，看不出到底校没校。
        self._avatar_hint.setText(
            f"{_mask_secret(stored)}（留空就是不改；填新的，保存时会再校验一次）"
        )

    def _avatar_costs(self, provider: str) -> str:
        if provider == "vidu":
            return "Vidu 组件版按 1 积分/秒计费，余额低于 45 积分发不起；画面走它托管的 RTC，不另外收费。"
        return ""

    def _build_workspace_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """工具和 agent 在哪个目录里干活：写进引擎配置的 `terminal.cwd`。"""
        page = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(QtWidgets.QLabel("工作目录", page))
        row = QtWidgets.QHBoxLayout()
        self._workspace_edit = QtWidgets.QLineEdit(page)
        self._workspace_edit.setPlaceholderText("工具和 agent 在这个目录里干活")
        self._workspace_edit.setText(str(self._current_workspace()))
        row.addWidget(self._workspace_edit, 1)
        browse = QtWidgets.QPushButton("浏览…", page)
        browse.clicked.connect(self._browse_workspace)
        row.addWidget(browse)
        layout.addLayout(row)

        self._workspace_hint = QtWidgets.QLabel(
            "写进引擎配置的 terminal.cwd（data/NoesiaHome/config.yaml）；换完从下一个新会话开始生效，"
            "当前这轮不改、也不用重启。",
            page,
        )
        self._workspace_hint.setObjectName("dialog-hint")
        self._workspace_hint.setWordWrap(True)
        layout.addWidget(self._workspace_hint)
        layout.addStretch(1)
        return page

    def _build_soul_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """她「是谁」：`NOESIA_HOME` 下的 SOUL.md，整篇可改，保存后下一句话就生效。"""
        page = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(QtWidgets.QLabel("人设（SOUL.md）", page))
        self._soul_edit = QtWidgets.QPlainTextEdit(page)
        self._soul_edit.setPlainText(_read_soul())
        self._soul_edit.setMinimumHeight(280)
        self._soul_edit.textChanged.connect(self._on_soul_edited)
        layout.addWidget(self._soul_edit, 1)

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        self._soul_reset = QtWidgets.QPushButton("恢复初始人格", page)
        self._soul_reset.setObjectName("secondary")
        self._soul_reset.setToolTip("把编辑框填成全新安装时的那份默认人设；点「保存并启用」才写回文件")
        self._soul_reset.clicked.connect(self._on_soul_reset)
        actions.addWidget(self._soul_reset)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._soul_hint = QtWidgets.QLabel("", page)
        self._soul_hint.setObjectName("dialog-hint")
        self._soul_hint.setWordWrap(True)
        layout.addWidget(self._soul_hint)
        self._on_soul_edited()
        return page

    def _on_soul_reset(self) -> None:
        """恢复初始人格：编辑框换成安装时那份默认人设，写盘仍然只走「保存并启用」这一条路。"""
        try:
            text = _default_soul()
        except Exception as exc:  # noqa: BLE001 - 读不到就说清楚，别静默什么都不做
            self._soul_hint.setText(f"读不到默认人设：{exc}")
            return
        self._soul_edit.setPlainText(text)
        self._soul_hint.setText(
            f"已填回安装时的默认人设（{len(text)} 字）。点「保存并启用」写回文件，关掉窗口则什么都不改。"
        )

    def _on_soul_edited(self) -> None:
        """字数实时跟着走：人设越长，每轮带进上下文的越多，得让人看得见。"""
        count = len(self._soul_edit.toPlainText())
        self._soul_hint.setText(
            f"现在 {count} 字。这里写的是她「是谁」——身份、信念、边界、说话方式；"
            "改完点「保存并启用」，下一句话就按新的来，不用换会话、不用重启。"
        )

    def _save_soul_only(self) -> str:
        """只改人设：整篇写回 `NOESIA_HOME` 下的 SOUL.md，别的设置一个字节都不动。"""
        text = self._soul_edit.toPlainText()
        if text.strip().splitlines() == self._initial["soul"].strip().splitlines():
            # 这页没动过（哪怕本来就是空的/还没有 SOUL.md）：不写、也不拦着别的页保存
            self._soul_changed = False
            self._mark_saved("soul")
            self._soul_hint.setStyleSheet("")  # 没改动也算这一次走通了：把之前的红字撤掉
            return "ok"
        if not text.strip():
            self._soul_hint.setStyleSheet(self.FAIL_STYLE)
            self._soul_hint.setText("人设不能是空的——清空了她就没有「是谁」了。不想改就不动这一页。")
            return "fail"
        # 一个字没改就不写盘：省得 mtime 变了、界面还回一句「下一句话就按新的来」，
        # 让人以为真改过（实际引擎比对内容，不会重拼，也不会有那条提醒）。
        current = _read_soul()
        # 按行比：CRLF 与 LF 不算改动，行尾多空一行也不算（引擎那边同样是先 strip 再比）。
        self._soul_changed = text.strip().splitlines() != current.strip().splitlines()
        if not self._soul_changed:
            self._mark_saved("soul")
            return "ok"
        try:
            target = _soul_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
        except Exception as exc:  # noqa: BLE001 - 写不进去就说清楚，别装作保存了
            self._soul_hint.setStyleSheet(self.FAIL_STYLE)
            self._soul_hint.setText(f"写不进去：{exc}")
            return "fail"
        self._mark_saved("soul")
        self._soul_hint.setStyleSheet("")  # 存好了：把上一次的红字撤掉
        return "ok"

    def soul_changed(self) -> bool:
        """刚才保存的人设是真的改了，还是原样保存（`main.py` 据此用不同措辞）。"""
        return bool(getattr(self, "_soul_changed", False))

    def _current_workspace(self) -> Path:
        """现在生效的工作目录：语义只有一处定义，和启动时用的是同一个函数。"""
        return workspace_from_config(ROOT / SETTINGS.engine.home, ROOT / SETTINGS.engine.workdir)

    def _browse_workspace(self) -> None:
        start = self._workspace_edit.text().strip() or str(ROOT)
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "选工作目录", start)
        if chosen:
            self._workspace_edit.setText(str(Path(chosen)))

    def _save_workspace_only(self) -> str:
        """只改工作目录：写进引擎配置的 terminal.cwd，模型、语音一个字节都不动。"""
        raw = self._workspace_edit.text().strip()
        if raw == self._initial["workspace"]:
            return "ok"  # 没动过（本来空的也算没动）：不写、不提示
        if not raw:
            self._workspace_hint.setStyleSheet(self.FAIL_STYLE)
            self._workspace_hint.setText("填一个目录，或者点「浏览…」选一个。")
            return "fail"
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = ROOT / target
        target = target.resolve()
        if not target.is_dir():
            self._workspace_hint.setStyleSheet(self.FAIL_STYLE)
            self._workspace_hint.setText(f"这个目录不存在：{target}")
            return "fail"

        from app.engine_config import set_engine_config

        try:
            set_engine_config(
                ROOT / SETTINGS.engine.core_path,
                ROOT / SETTINGS.engine.home,
                {"terminal.cwd": str(target)},
            )
        except Exception as exc:  # noqa: BLE001 - 写不进去就说清楚，别装作保存了
            self._workspace_hint.setStyleSheet(self.FAIL_STYLE)
            self._workspace_hint.setText(f"写配置失败：{exc}")
            return "fail"
        self._workspace_edit.setText(str(target))
        self._workspace_hint.setStyleSheet("")  # 存好了：把上一次的红字撤掉
        self._saved_workspace = str(target)
        self._mark_saved("workspace")
        return "ok"

    def saved_workspace(self) -> str:
        """刚保存的工作目录（只有从「工作目录」页签保存过才有）。"""
        return getattr(self, "_saved_workspace", "")

    def saved_tab(self) -> int:
        """刚才是从哪个页签点保存的（`TAB_*` 那几个常量）。"""
        return getattr(self, "_saved_tab", 0)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt 的命名
        """按住标题栏拖窗口：无边框了，系统那套拖动就没了，得自己接一下。"""
        if watched is self._titlebar:
            kind = event.type()
            if kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
                self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if kind == QtCore.QEvent.MouseMove and self._drag_from is not None:
                self.move(event.globalPosition().toPoint() - self._drag_from)
                return True
            if kind == QtCore.QEvent.MouseButtonRelease:
                self._drag_from = None
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        if event.key() == QtCore.Qt.Key_Escape:  # 无边框没有关闭按钮以外的出口，Esc 必须有
            self.reject()
            return
        super().keyPressEvent(event)

    def _snapshot_pages(self) -> dict:
        """打开设置那一刻五页各自的取值：用来判断这次到底改了哪几页。"""
        return {
            "text": self.selection(),
            "voice": self.voice_selection(),
            "avatar": (
                str(self._avatar_provider_box.currentData() or ""),
                str(self._avatar_model_box.currentData() or ""),
            ),
            "workspace": self._workspace_edit.text().strip(),
            "soul": self._soul_edit.toPlainText(),
        }

    def _mark_saved(self, page: str) -> None:
        """记下这一页这次真的落地了（`main.py` 按这个逐项收尾，不再猜是哪个页签）。"""
        self._saved_pages.add(page)

    def save_notes(self) -> list[str]:
        """只有关窗才会丢的信息（比如这一页压根没拿到模型清单）：窗口关掉之后要补说一句。"""
        return list(getattr(self, "_save_notes", []) or [])

    def saved_pages(self) -> set[str]:
        """这次保存真正落地的页：`text` / `soul` / `workspace` / `avatar` / `voice`。"""
        return set(getattr(self, "_saved_pages", set()))

    def _save_text_page(self) -> str:
        """文本模型这一页：挨个读每家的框——填了 Key 的全部交出，只有「在用」那家切模型。

        一页能配好几家：三个 Key 一次填完、一次保存，全落进 `.env`；各家选过的模型也一起记住。
        """
        from app import text_prefs

        if not self._text_blocks:
            # 还没拿到供应商清单：这页没得改，别挡住别的页；但关窗之后要说清这一页没存
            self._save_notes.append("文本模型这一页这次没保存：没从引擎拿到模型清单，重开一次设置再试。")
            return "ok"
        data = self.selection()
        if data == self._initial["text"]:
            return "ok"  # 这页没动过：不写、也不拦着别的页保存
        slug = str(data.get("provider") or "")
        block = self._text_blocks.get(slug) or {}
        row = block.get("row") or {}
        note = block.get("note")

        def fail(message: str) -> str:
            if note is not None:
                note.setStyleSheet(self.FAIL_STYLE)
                note.setText(message)
            self._warn.setStyleSheet(self.FAIL_STYLE)
            self._warn.setText(message)
            return "fail"

        if not slug:
            # 没圈就是没圈：不许替他圈。填了 Key 但没圈「启用」= 这次什么都没生效，
            # 就得明说没保存——不要让人以为填完就生效了。
            return fail("还没圈「启用」：这次什么都没保存，先圈一家再点进来一次。")
        if not data["model"]:
            return fail(f"{row.get('label') or slug} 没有可选模型：换一家当「启用」，或者先给它填好 Key。")
        names = list(row.get("env_vars") or [])
        if names and not data["key"] and not row.get("configured"):
            # 只有「这家本来就要一把 Key」时才要求填。copilot-acp 这种走引擎登录态的没有 Key 可填，
            # 以前这里对着它喊「填上密钥」，而它的输入框是灰的——纯粹是拿它没法满足的条件去卡它。
            return fail(f"「启用」的这家还差 {names[0]}：填上再保存，否则这个模型用不了。")
        typed_key = str(data.get("key") or "")
        if typed_key:  # 当场拦：写进 .env 之后才发现，就只能在运行时收一个 401 了
            bad = ""
            if not typed_key.isascii():
                bad = "这把 Key 里有中文或全角字符——多半是复制时带进来的。"
            elif any(ch.isspace() for ch in typed_key):
                bad = "Key 中间夹了空格或换行：它应该是一串连续字符。"
            elif len(typed_key) < 8:
                bad = f"这把 Key 只有 {len(typed_key)} 位，短得不像一把 Key。"
            if bad:
                return fail(bad)
        text_prefs.remember_many(data.get("models") or {})  # 每家上次选的模型各自记着
        self._text_saved = dict(data)  # 交出去的那份留底
        self.saved.emit(data)
        self._mark_saved("text")
        return "ok"

    def _on_save(self) -> None:
        """点一次保存就把五页一起存：哪一页没过校验就跳到那一页贴原因，窗口不关。"""
        self._saved_tab = self._tabs.currentIndex()
        self._saved_pages = set()
        steps = (
            (self.TAB_TEXT, self._save_text_page),
            (self.TAB_SOUL, self._save_soul_only),
            (self.TAB_WORKSPACE, self._save_workspace_only),
            (self.TAB_AVATAR, self._save_avatar_only),
            (self.TAB_VOICE, self._save_voice_only),
        )
        pending = False
        self._warn.setStyleSheet("")
        try:
            for tab, save in steps:
                outcome = save()
                if outcome == "fail":
                    self._tabs.setCurrentIndex(tab)  # 停在不通过的那一页：原因已经贴在该页上
                    return
                if outcome == "pending":
                    pending = True
                    self._tabs.setCurrentIndex(tab)  # 别让人干等：切到正在等结果的那一页
        except Exception as exc:  # noqa: BLE001 - 保存里崩了也必须说出来，不许没反应
            self._warn.setStyleSheet(self.FAIL_STYLE)
            self._warn.setText(f"保存出错了：{type(exc).__name__}: {exc}")
            return
        if pending:  # 人形那页还在校验 Token：等校验回来再走 _close_or_save()
            return
        self._close_or_save()

    def _close_or_save(self) -> None:
        """该收尾了：文本页这次真要写盘就先等引擎回执，否则直接关窗。

        人形页的 Token 校验是异步的（`_on_avatar_checked` 回来才继续），两条路都从这里收，
        否则「同时改了文本和人形」时，Token 一校验完就直接关窗，文本那页的 Key 谁也没发出去。
        """
        if self._save_waiting:
            return
        if self._engine_save is not None and "text" in (self._saved_pages or set()):
            self._begin_engine_save()
            return
        self.accept()

    def _begin_engine_save(self) -> None:
        """把这一页交出去，然后等着：只有引擎回话说成了，才算成。"""
        self._save_waiting = True
        self._save_button.setEnabled(False)
        self._save_button.setText("正在保存…")
        self._warn.setStyleSheet("")
        self._warn.setText("正在写进 .env 并切换，等引擎回话…")
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._engine_save_timed_out)
        self._save_timer.start(8000)  # 引擎没回话就别一直吊着：8 秒后如实说不知道成没成
        try:
            self._engine_save(self.selection(), self.engine_save_done)
        except Exception as exc:  # noqa: BLE001 - 交不出去也得说清，不能停在「正在保存…」上
            self.engine_save_done(False, [f"没能把这次保存交给引擎：{type(exc).__name__}: {exc}"])

    def engine_save_done(self, ok: bool, lines: list[str] | None = None) -> None:
        """引擎回执到了：成了就关窗，没成就留在窗口里逐条说清谁成了、谁没成。"""
        if not self._save_waiting:
            return
        self._save_waiting = False
        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer = None
        if ok:
            self.accept()
            return
        self._save_button.setEnabled(True)
        self._save_button.setText("保存并启用")
        self._warn.setStyleSheet(self.FAIL_STYLE)
        detail = "\n".join(lines or []) or "引擎没说原因"
        self._warn.setText(f"没保存成功：\n{detail}")

    def _engine_save_timed_out(self) -> None:
        self.engine_save_done(
            False,
            ["引擎 8 秒没回话：不确定成没成。重开一次设置，看副标题那行「引擎现在在跑」对不对。"],
        )

    def _on_avatar_checked(self, ok: bool, message: str, token: str = "") -> None:
        """校验回来了：通过才落地到 .env，不通过就把原因贴出来（不存坏的）。"""
        from app import avatar_prefs

        # 慢的那次校验可能在换了另一把 Token 之后才回来：结果对不上就不许写盘，
        # 否则会被旧结果覆盖成一把你已经不想要的凭证。
        if self._avatar_pending and token and token != self._avatar_pending:
            return
        if not ok:
            self._avatar_hint.setStyleSheet(self.FAIL_STYLE)
            self._avatar_hint.setText(message + "（没有保存）")
            return
        if not self._avatar_pending:  # 校的是存着的那把：只报状态
            self._avatar_hint.setStyleSheet("")
            self._avatar_hint.setText(message)
            return
        avatar_prefs.write_token(self._avatar_pending, self._avatar_pending_provider)
        self._avatar_key_drafts.pop(self._avatar_pending_provider, None)  # 已经进 .env，草稿不用留
        self._avatar_pending = ""
        self._avatar_key_edit.clear()
        self._avatar_hint.setStyleSheet("")
        self._avatar_hint.setText(message + " 已存进 data/NoesiaHome/.env。")
        self._close_or_save()  # 校验过了就收尾；文本页那边要等引擎回执的，走的是同一条路

    def _on_avatar_key_typed(self) -> None:
        """凭证框一敲字就把提示换成对的：别再挂着「还没填凭证」，那会让人以为根本没填上。"""
        typed = self._avatar_key_edit.text().strip()
        if not typed:
            return
        self._avatar_key_drafts[str(self._avatar_provider_box.currentData() or "")] = self._avatar_key_edit.text()
        self._avatar_hint.setStyleSheet("")
        self._avatar_hint.setText(
            f"这一串还没保存（{len(typed)} 位）：点「保存并启用」会先校验，通过才写进 .env。"
        )

    def _save_avatar_only(self) -> str:
        """只写语音互动这一套：选哪家 + 它的凭证。语音引擎、文本模型一个字节都不碰。"""
        from app import avatar_prefs

        provider = self._avatar_provider_box.currentData()
        if not provider:
            self._warn.setText("先选一家语音互动的厂商。")
            return "fail"
        typed = self._avatar_key_edit.text().strip()
        if not typed and (str(provider), str(self._avatar_model_box.currentData() or "")) == self._initial["avatar"]:
            return "ok"  # 这页没动过：不写、也不拦着别的页保存
        avatar_prefs.save(provider, self._avatar_model_box.currentData())  # 用哪家、哪个模型进 avatar.json；Key 只进 .env
        self._mark_saved("avatar")
        if not typed:
            return "ok"  # 只改了厂商，存完就算过
        problem = avatar_prefs.token_issue(typed)
        if problem:
            self._avatar_hint.setStyleSheet(self.FAIL_STYLE)
            self._avatar_hint.setText(problem)
            return "fail"
        if not self._avatar_provider_ready(provider):
            self._avatar_hint.setStyleSheet(self.FAIL_STYLE)
            self._avatar_hint.setText("这家还没接好（没有校验和建会话的实现），先别填。")
            return "fail"
        # 先问云端一次：省得存了一把用不了的 Token，等建会话那一刻才发现
        self._avatar_pending = typed
        self._avatar_pending_provider = provider
        self._avatar_hint.setStyleSheet("")
        self._avatar_hint.setText("正在校验 Token…")
        self._avatar_checker.start(provider, typed)
        return "pending"

    def _save_voice_only(self) -> str:
        """只写语音：音色/档位/Key（Key 进 .env），绝不碰文本模型选的是哪家、哪个模型。

        一页能配好几家：几家的 Key 一次填完就一次全写进去，只有「在用」那家会真的发声。
        """
        from app import speech_prefs

        keys = self.voice_keys()
        if not keys and self.voice_selection() == self._initial["voice"]:
            return "ok"  # 这页没动过：不写、也不拦着别的页保存
        # 先把每家的 Key 过一遍：哪一串明显不像 Key 就停在那一家，坏的绝不写盘
        for provider, typed in keys.items():
            block = self._voice_block(provider)
            if not typed.isascii() or any(ch.isspace() for ch in typed):
                block["note"].setStyleSheet(self.FAIL_STYLE)
                block["note"].setText("这个 Key 里混了中文或空格，重新粘贴一次。")
                return "fail"
            if len(typed) < 20:
                block["note"].setStyleSheet(self.FAIL_STYLE)
                block["note"].setText(f"这个 Key 不完整（{len(typed)} 位）：一般 40 位上下。")
                return "fail"
        # 刚填十一家的 Key、清单还没到：先把 Key 存进 .env，就地拉一次清单，让人选完音色再保存
        # 否则窗口一关 voice_id 就永远是空的（踩过：填了 Key 直接保存，音色没得选）
        eleven = self._voice_block("elevenlabs")
        if keys.get("elevenlabs") and not self._voice_choices("elevenlabs"):
            speech_prefs.write_api_key(keys["elevenlabs"], "elevenlabs")
            eleven["key"].clear()  # 已经进 .env，框里不用再留
            eleven["configured"] = True
            self._load_voices()
            self._voice_refresh("elevenlabs")
            return "fail"  # 停在语音页：还差一个音色没选
        # 别家先把 Key 落盘（每家一个变量、互不影响），「在用」那家走正常保存流程
        for provider, typed in keys.items():
            if provider == self._voice_use:
                continue
            speech_prefs.write_api_key(typed, provider)
            block = self._voice_block(provider)
            block["key"].clear()
            block["configured"] = True
            self._voice_refresh(provider)
        try:
            speech_prefs.save(**self.voice_selection())
        except ConfigError as exc:
            # 手改过 speech.json 的人可能把资源 id 写坏：红字说清能填什么，别让保存流程崩掉
            block = self._voice_block("doubao")
            if block:
                block["note"].setStyleSheet(self.FAIL_STYLE)
                block["note"].setText(str(exc))
            return "fail"
        # 每家选过的音色都记进 voice_history：重开设置时各家的选择都还在
        for provider, pick in self._voice_picks.items():
            if pick.get("voice"):
                speech_prefs.remember_voice(provider, pick["voice"], pick.get("name") or "")
        self._voice_saved = dict(self.voice_selection())
        used = self._voice_block(self._voice_use)
        if used.get("key") is not None:
            used["key"].clear()
            used["configured"] = True
        for provider in self._voice_blocks:
            self._voice_refresh(provider)
        self._mark_saved("voice")
        return "ok"


