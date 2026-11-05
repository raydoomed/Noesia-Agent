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


def _engine_home() -> Path:
    """引擎的 home（`NOESIA_HOME`）：`config.yaml`、`.env`、`SOUL.md`、`memories/` 都在这一层。"""
    return env_file().parent


def _memory_file(name: str) -> Path:
    """她的记忆文件：`NOESIA_HOME/memories/`，和引擎读写的是同一份。"""
    return env_file().with_name("memories") / name


def _read_memory_file(name: str) -> str:
    """磁盘上那份记忆原文（条目之间单独一行 `§`）；还没写过就是空的。"""
    try:
        return _memory_file(name).read_text(encoding="utf-8-sig")
    except OSError:
        return ""


# 条目分隔符：和引擎 `memory_tool_store.ENTRY_DELIMITER` 一致，改这里等于改记忆文件格式
MEMORY_DELIMITER = "\n§\n"


def _memory_entries(text: str) -> list[str]:
    """按引擎的规则切条目：整行 `§` 才算分隔；条目首尾空白去掉，空条目丢掉。"""
    return [entry for entry in (part.strip() for part in text.split(MEMORY_DELIMITER)) if entry]


def _memory_text(entries: list[str]) -> str:
    """条目拼回文件内容；计数、比较、写盘都用这一份，三处口径不会跑偏。"""
    return MEMORY_DELIMITER.join(entries)


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

def _field_label(text: str, parent: QtWidgets.QWidget) -> QtWidgets.QLabel:
    """页内小标题。颜色写在 `#field-label` 上，不吃系统主题的默认字色。"""
    label = QtWidgets.QLabel(text, parent)
    label.setObjectName("field-label")
    return label


def _fill_editor(editor: QtWidgets.QPlainTextEdit, text: str) -> None:
    """把整篇写进编辑框，并把字色定成浅色。

    `QPlainTextEdit` 的字色走文档格式/调色板，QSS 里的 `color` 不一定吃得住——浅色主题下就
    变成黑字压在深色框上。`setPlainText` 又会把格式清掉，所以每次写内容都跟一遍整篇字色。
    """
    editor.setPlainText(text)
    light = QtGui.QTextCharFormat()
    light.setForeground(QtGui.QColor("#eef1f6"))
    cursor = editor.textCursor()
    cursor.select(QtGui.QTextCursor.SelectionType.Document)
    cursor.mergeCharFormat(light)
    cursor.clearSelection()
    editor.setTextCursor(cursor)
    editor.setCurrentCharFormat(light)  # 接着往下打字也是这个颜色


# 编辑框自带的样式：不指望对话框那张大表一定解析成功，这几个值就是大表里同一套
_EDITOR_QSS = """
QPlainTextEdit {
    background-color: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 8px;
    color: #eef1f6;
    padding: 6px 10px;
    font-size: 12px;
    selection-background-color: rgba(255, 255, 255, 70);
    selection-color: #10121a;
}
QPlainTextEdit:hover { border-color: rgba(255, 255, 255, 62); }
QPlainTextEdit:focus { border-color: rgba(255, 255, 255, 130); background-color: rgba(255, 255, 255, 16); }
"""


def _make_editor(parent: QtWidgets.QWidget, text: str, min_height: int) -> QtWidgets.QPlainTextEdit:
    """一块能编辑整篇文本的框：深色玻璃底 + 浅色字，写在它自己身上，换主题也不会变黑。"""
    editor = QtWidgets.QPlainTextEdit(parent)
    editor.setMinimumHeight(min_height)
    editor.setStyleSheet(_EDITOR_QSS)
    _fill_editor(editor, text)
    return editor


def _apply_dark_palette(widget: QtWidgets.QWidget) -> None:
    """给整个设置窗定死深色底 + 浅色字。

    QSS 覆盖不到的地方（最典型就是 `QPlainTextEdit` 里已经写进去的字——它的颜色走文档/调色板，
    不是每条 `color` 都吃）会退回系统调色板：Windows 浅色主题下就是黑字压在深色框上，等于看不见。
    这里把底色、正文、占位符、选中色都定死，哪个平台什么主题都不影响。
    """
    palette = widget.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(18, 18, 22))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(30, 30, 36))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#eef1f6"))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#eef1f6"))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#eef1f6"))
    palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(255, 255, 255, 96))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(255, 255, 255, 70))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#10121a"))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(24, 24, 28))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor("#eef1f6"))
    widget.setPalette(palette)


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
/* 页内小标题：不靠继承，直接给颜色——浅色主题下 QLabel 会退回系统黑字 */
#field-label { color: rgba(255, 255, 255, 214); font-size: 11.5px; }
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

        # 调用方先用 `_avatar_provider_ready()` 挡过没有校验实现的厂商，这里不会有 KeyError
        verify = _TOKEN_VERIFIERS[provider]

        def work() -> None:
            try:
                self._emit(True, verify(token), token)
            except RuntimeError as exc:
                self._emit(False, str(exc), token)
            except Exception as exc:  # noqa: BLE001 — 校验线程不能静默死掉
                self._emit(False, f"校验没成功：{exc}", token)

        threading.Thread(target=work, daemon=True).start()

    def _emit(self, ok: bool, message: str, token: str) -> None:
        """结果回主线程；窗已经关了对象就没了——那是正常收场，不该在后台线程里炸一次。"""
        try:
            self.done.emit(ok, message, token)
        except RuntimeError:
            return


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
    OK_COLOR = "#4ade80"  # 「成立了」的绿：已配置徽标、正在跑的那行都用它
    LIVE_TEXT_STYLE = f"color: {OK_COLOR}; font-size: 11.5px;"
    LIVE_VOICE_STYLE = "color: #c084fc; font-size: 11.5px;"
    LIVE_INTERACT_STYLE = "color: #fb923c; font-size: 11.5px;"
    NOTE_STYLE = "color: #ffd166; font-size: 11.5px;"

    TAB_KEYS = 0
    TAB_MODELS = 1
    TAB_VOICE = 2
    TAB_AVATAR = 3
    TAB_WORKSPACE = 4
    TAB_SOUL = 5
    TAB_MEMORY = 6
    """密钥设置（填 Key）/ 模型设置（选模型）。数据全部来自引擎的清单，界面自己不留名单。"""



    def __init__(
        self,
        choices: list[dict],
        parent: QtWidgets.QWidget | None = None,
        current: str = "",
        engine_save=None,
        on_key_save=None,
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
        _apply_dark_palette(self)
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
        # 「密钥设置」里那一排「确定」：把这一家的 Key 交给引擎落盘（main.save_provider_key）。
        # 存完引擎会重取清单，模型设置那一页就会出现这家——填 Key 和拿到模型是同一个动作。
        self._on_key_save = on_key_save
        self._save_waiting = False
        self._save_timer: QtCore.QTimer | None = None
        self._save_notes: list[str] = []
        # 普通提醒（「这份记忆被清空了」这种）：走灰字，不冒充错误
        self._save_notices: list[str] = []

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

        hint = QtWidgets.QLabel(
            "先在「密钥设置」里填 Key 并点确定；存好的家才会出现在「模型设置」里。", frame
        )
        hint.setObjectName("dialog-sub")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 页签：密钥设置（填 Key，一家一个「确定」）、模型设置（只列存过 Key 的家，选一个启用）
        self._tabs = tabs = QtWidgets.QTabWidget(frame)
        tabs.setObjectName("dialog-tabs")
        tabs.setDocumentMode(True)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setDrawBase(False)
        tabs.setUsesScrollButtons(True)  # 窄窗口真放不下时还能翻到最后一个页签
        keys_page = QtWidgets.QWidget(tabs)
        keys_layout = QtWidgets.QVBoxLayout(keys_page)
        keys_layout.setContentsMargins(0, 8, 0, 0)
        keys_layout.setSpacing(8)
        models_page = QtWidgets.QWidget(tabs)
        models_layout = QtWidgets.QVBoxLayout(models_page)
        models_layout.setContentsMargins(0, 8, 0, 0)
        models_layout.setSpacing(8)
        tabs.addTab(keys_page, "密钥设置")
        tabs.addTab(models_page, "模型设置")
        tabs.addTab(self._build_voice_page(tabs), "语音模型")
        tabs.addTab(self._build_avatar_page(tabs), "语音互动")
        tabs.addTab(self._build_workspace_page(tabs), "工作目录")
        tabs.addTab(self._build_soul_page(tabs), "人设")
        tabs.addTab(self._build_memory_page(tabs), "记忆")
        layout.addWidget(tabs)
        # 七个页签比 400 宽：再窄最后一个（记忆）就被挤到滚动箭头后面，等于没有这一页
        self.setMinimumWidth(max(400, tabs.tabBar().sizeHint().width() + 72))

        self._build_keys_page(keys_layout, keys_page)
        self._build_models_page(models_layout, models_page)

        self._warn = QtWidgets.QLabel("", frame)
        self._warn.setObjectName("dialog-warn")
        self._warn.setWordWrap(True)
        models_layout.addWidget(self._warn)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("取消", frame)
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton("保存并启用", frame)
        save.setToolTip("把「模型设置」里圈中的那一家换成现在用的（密钥各页各存，不在这里）")
        save.clicked.connect(self._on_save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        grip = QtWidgets.QSizeGrip(frame)  # 无边框还得能拉大小，右下角留一个抓手
        buttons.addWidget(grip)
        layout.addLayout(buttons)

        self._save_button = save
        # 一次保存要跑六页：先记住打开设置那一刻的样子，只有真被改过的页才写盘、才可能拦住保存
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

    # ---------------- 密钥设置 ----------------

    def _build_keys_page(self, layout: QtWidgets.QVBoxLayout, page: QtWidgets.QWidget) -> None:
        """密钥设置：一家一块，只有 Key 输入 + 「确定」。

        存下来的 Key 才会让这家出现在「模型设置」里；所以这一页不参与底部的「保存并启用」，
        每家的「确定」只负责把这一串交给引擎落盘（引擎随即重取清单）。
        """
        self._key_blocks: dict[str, dict] = {}
        # 刚存成功、等引擎把 authenticated 补上的那几家：清单重建后仍要显示「已配置」
        self._key_confirmed: set[str] = set()

        hint = QtWidgets.QLabel(
            "填好 Key 点这一家的「确定」；存好的供应商会出现在「模型设置」里。", page
        )
        hint.setObjectName("dialog-hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        tools = QtWidgets.QHBoxLayout()
        tools.setContentsMargins(0, 0, 4, 0)
        tools.setSpacing(6)
        self._key_search = QtWidgets.QLineEdit(page)
        self._key_search.setObjectName("search")
        self._key_search.setPlaceholderText("搜供应商…")
        self._key_search.setClearButtonEnabled(True)
        self._key_search.setFixedHeight(28)
        self._key_sort = _NarrowCombo(page)
        self._key_sort.setObjectName("sort")
        for label, key in (("已配置优先", "smart"), ("名称 A–Z", "az")):
            self._key_sort.addItem(label, key)
        self._key_sort.setFixedHeight(28)
        tools.addWidget(self._key_search, 1)
        tools.addWidget(self._key_sort)
        layout.addLayout(tools)
        self._key_search.textChanged.connect(self._keys_apply_view)
        self._key_sort.currentIndexChanged.connect(self._keys_apply_view)

        scroller = _page_scroller(page)
        self._keys_scroller = scroller
        inner = scroller.widget()
        rows = QtWidgets.QVBoxLayout(inner)
        rows.setContentsMargins(0, 2, 4, 2)
        rows.setSpacing(8)
        layout.addWidget(scroller, 1)

        for row in self._ordered_rows():
            self._add_key_block(rows, row)

        self._keys_empty = QtWidgets.QLabel("没有匹配的供应商：换个词试试。", inner)
        self._keys_empty.setObjectName("dialog-hint")
        self._keys_empty.setVisible(False)
        rows.addWidget(self._keys_empty)
        rows.addStretch(1)
        self._keys_apply_view()

    def _ordered_rows(self) -> list[dict]:
        """已配置的排前面，其余按名字：一屏里先看到跟自己有关的。"""
        return sorted(
            self._choices,
            key=lambda item: (
                0 if item.get("configured") else 1,
                str(item.get("label") or item.get("provider") or "").lower(),
            ),
        )

    @staticmethod
    def _needs_key(row: dict) -> bool:
        """这家要不要填 Key。

        有变量名要填；引擎没给变量名、但认证类型是 api_key（正常填 Key 的家）也要填——
        配过一次之后，引擎的旧回包里不再带 `key_env`，照变量名判断会把 Key 框和「确定」
        一起灰掉，看着就像密钥再也改不了。
        """
        if row.get("env_vars"):
            return True
        return str(row.get("auth_type") or "api_key") == "api_key"

    def _add_key_block(self, rows: QtWidgets.QVBoxLayout, row: dict) -> None:
        slug = str(row["provider"])
        names = list(row.get("env_vars") or [])
        needs_key = self._needs_key(row)
        block = QtWidgets.QFrame(rows.parentWidget())
        block.setObjectName("text-block")
        box = QtWidgets.QVBoxLayout(block)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(7)

        head = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(str(row.get("label") or slug).split("（")[0], block)
        name.setObjectName("block-name")
        badge = QtWidgets.QLabel("", block)
        badge.setObjectName("dialog-hint")
        head.addWidget(name)
        head.addWidget(badge)
        head.addStretch(1)
        box.addLayout(head)

        key_row = QtWidgets.QHBoxLayout()
        key_row.setSpacing(6)
        key = QtWidgets.QLineEdit(block)
        key.setEchoMode(QtWidgets.QLineEdit.Password)
        key.setPlaceholderText(
            f"粘贴 {names[0]} 的值" if names
            else ("粘贴新的 Key（会覆盖已存的那把）" if needs_key else "这家用引擎的登录态，不需要密钥")
        )
        key.setEnabled(needs_key)
        key.setFixedHeight(28)
        confirm = QtWidgets.QPushButton("确定", block)
        confirm.setObjectName("secondary")
        confirm.setFixedHeight(28)
        confirm.setEnabled(needs_key)
        confirm.setToolTip("把这串 Key 存进 .env（存好就会出现「模型设置」里）")
        key_row.addWidget(key, 1)
        key_row.addWidget(confirm)
        box.addLayout(key_row)

        note = QtWidgets.QLabel("", block)
        note.setObjectName("dialog-hint")
        note.setWordWrap(True)
        box.addWidget(note)

        rows.addWidget(block)
        self._key_blocks[slug] = {
            "row": row,
            "block": block,
            "name": name,
            "badge": badge,
            "note": note,
            "key": key,
            "button": confirm,
            "configured": bool(row.get("configured")),
            "saving": False,
            "error": "",
        }
        confirm.clicked.connect(lambda _checked=False, s=slug: self._on_key_confirm(s))
        key.returnPressed.connect(lambda s=slug: self._on_key_confirm(s))
        self._key_refresh(slug)

    # ---------------- 模型设置 ----------------

    def _build_models_page(self, layout: QtWidgets.QVBoxLayout, page: QtWidgets.QWidget) -> None:
        """模型设置：只列「密钥设置」里存过 Key 的家，圈一家、选个模型、按底部保存。"""
        self._text_blocks: dict[str, dict] = {}
        self._text_saved: dict = {}
        self._text_pending: dict = {}  # 交给引擎、还没等到回执的那一份（失败就不能算已生效）
        self._text_dirty = False  # 用户在这一页真的动过手（圈过、选过模型）：清单后到不算动
        self._text_use = ""
        self._text_group = QtWidgets.QButtonGroup(self)
        self._text_group.setExclusive(True)

        self._live_text = self._live_label(page, self._live_text_line(), self.LIVE_TEXT_STYLE)
        layout.addWidget(self._live_text)

        scroller = _page_scroller(page)
        self._text_scroller = scroller
        inner = scroller.widget()
        rows = QtWidgets.QVBoxLayout(inner)
        rows.setContentsMargins(0, 2, 4, 2)
        rows.setSpacing(8)
        layout.addWidget(scroller, 1)

        self._models_empty = QtWidgets.QLabel(
            "还没有可选的模型：先在「密钥设置」里填好 Key 并点确定。", inner
        )
        self._models_empty.setObjectName("dialog-hint")
        self._models_empty.setWordWrap(True)
        rows.addWidget(self._models_empty)
        rows.addStretch(1)

        self._rebuild_models()

    def show_rows_error(self, message: str) -> None:
        """拿不到供应商清单时，就地在这张表上说清楚。

        设置页是模态的，聊天区被它整个盖住：那边冒一条红字用户根本看不见，
        只会看到「设置页是空的」，然后以为是自己没配对。
        """
        self._models_empty.setText(f"拿不到供应商清单：{message or '引擎没回话'}")
        self._models_empty.setStyleSheet("color: #ff8a8a;")  # 红字：dialog-hint 那种灰字看不见
        self._models_empty.show()

    def _rebuild_models(self) -> None:
        """按现在这份清单重铺「模型设置」：只有配好的家建卡，没配的一家都不列。"""
        picked = {
            slug: str(block["model"].currentData() or "")
            for slug, block in self._text_blocks.items()
        }
        layout = self._text_scroller.widget().layout()
        for slug in list(self._text_blocks):
            block = self._text_blocks.pop(slug)
            block["block"].setParent(None)
            block["block"].deleteLater()

        current_provider, current_model = self._current_parts()
        if not current_provider and current_model:
            for row in self._choices:
                if current_model in list(row.get("models") or []):
                    current_provider = str(row.get("provider") or "")
                    break

        anchor = layout.count() - 2 if layout.count() >= 2 else 0  # 空态那行和弹簧留最后
        for row in self._ordered_rows():
            if not row.get("configured"):
                continue
            self._add_model_block(layout, row, current_provider, current_model, anchor)
            anchor += 1

        if self._text_dirty and self._text_use in self._text_blocks:
            pass  # 用户自己圈的那家：清单后到也不许改他的选择
        else:
            self._text_use = current_provider if current_provider in self._text_blocks else ""
        for slug, block in self._text_blocks.items():
            wanted = picked.get(slug) or block.get("preferred") or ""
            index = block["model"].findData(wanted) if wanted else -1
            if index >= 0:
                block["model"].setCurrentIndex(index)
        if self._text_use:
            dot = self._text_blocks[self._text_use]["use"]
            dot.blockSignals(True)
            dot.setChecked(True)
            dot.blockSignals(False)
        for slug in self._text_blocks:
            self._model_refresh(slug)
        if self._text_blocks:
            # 表里有货了：把上一次「拿不到清单」的红字收回去，别和真清单混在一起看
            self._models_empty.setText("还没有可选的模型：先在「密钥设置」里填好 Key 并点确定。")
            self._models_empty.setStyleSheet("")
        self._models_empty.setVisible(not self._text_blocks)
        if hasattr(self, "_live_text") and self._live_text is not None:
            self._live_text.setText(self._live_text_line())

    def _current_parts(self) -> tuple[str, str]:
        option = str(self._current_option or "")
        if ":" in option:
            provider, model = option.split(":", 1)
            return provider, model
        return "", option

    def _add_model_block(
        self,
        rows: QtWidgets.QVBoxLayout,
        row: dict,
        current_provider: str,
        current_model: str,
        position: int = -1,
    ) -> None:
        """画一家（模型设置里的一张卡）：启用圈 + 模型下拉。"""
        from app import text_prefs

        slug = str(row["provider"])
        block = QtWidgets.QFrame(rows.parentWidget())
        block.setObjectName("text-block")
        box = QtWidgets.QVBoxLayout(block)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(7)

        head = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(str(row.get("label") or slug).split("（")[0], block)
        name.setObjectName("block-name")
        badge = QtWidgets.QLabel("", block)
        badge.setObjectName("dialog-hint")
        use = _SelectDot("", block)
        self._text_group.addButton(use)
        head.addWidget(name)
        head.addWidget(use)
        head.addWidget(badge)
        head.addStretch(1)
        box.addLayout(head)

        model = _slim(_LazyCombo(block, lambda combo, slug=slug: self._ensure_models(slug)), 8)
        if row.get("models"):
            for item in list(row.get("models") or []):
                _add_short(model, _model_label(slug, item), item)
        else:
            _add_short(model, "引擎没取到模型", "")
            model.setToolTip("引擎这次没返回这家的模型清单：换个时间再打开设置")
        _fit_popup(model)
        wanted = current_model if slug == current_provider else text_prefs.remembered(slug)
        index = model.findData(wanted) if wanted else -1
        if index >= 0:
            model.setCurrentIndex(index)
        box.addWidget(model)

        note = QtWidgets.QLabel("", block)
        note.setObjectName("dialog-hint")
        note.setWordWrap(True)
        box.addWidget(note)

        if position >= 0:
            rows.insertWidget(position, block)
        else:
            rows.addWidget(block)
        self._text_blocks[slug] = {
            "row": row,
            "block": block,
            "name": name,
            "badge": badge,
            "note": note,
            "use": use,
            "model": model,
            "configured": bool(row.get("configured")),
            "preferred": wanted,
            "error": "",
        }
        model.currentIndexChanged.connect(lambda _index, slug=slug: self._model_refresh(slug))
        use.toggled.connect(lambda on, slug=slug: self._text_use_changed(slug, on))
        # 选模型就是启用这一家：在下拉里点一个模型等于圈上它。
        # 不然用户「选好了模型」再点「保存并启用」，保存仍是空操作——界面上什么都没发生。
        model.activated.connect(lambda _index, slug=slug: self._enable_block(slug))

    def _set_badge(self, badge: QtWidgets.QLabel, text: str, ok: bool = False) -> None:
        """徽标文字 + 颜色：状态成立了就绿，其余维持原本的淡色。"""
        badge.setText(text)
        badge.setStyleSheet(f"color: {self.OK_COLOR};" if ok else "")

    def _keys_apply_view(self) -> None:
        """密钥设置按搜索词筛一遍（只影响显示，框里填的东西不动）。"""
        if not self._key_blocks:
            return
        query = self._key_search.text().strip().lower()
        mode = str(self._key_sort.currentData() or "smart")

        def sort_key(item):
            _, block = item
            name = str(block["row"].get("label") or block["row"].get("provider") or "").lower()
            if mode == "az":
                return (name,)
            return (0 if block["configured"] else 1, name)

        ordered = sorted(self._key_blocks.items(), key=sort_key)
        for slug, block in ordered:
            hit = True
            if query:
                hit = query in slug.lower() or query in str(
                    block["row"].get("label") or ""
                ).lower()
            block["block"].setVisible(hit)
        layout = self._keys_scroller.widget().layout()
        for _slug, block in ordered:
            layout.removeWidget(block["block"])
        for position, (_slug, block) in enumerate(ordered):
            layout.insertWidget(position, block["block"])
        self._keys_empty.setVisible(bool(query) and all(b["block"].isHidden() for _s, b in ordered))

    def _on_key_confirm(self, slug: str) -> None:
        """这一家的「确定」：把框里的 Key 交给引擎落盘（它随即重取模型清单）。"""
        block = self._key_blocks.get(slug)
        if not block or block.get("saving"):
            return
        text = block["key"].text().strip()
        note = block["note"]
        note.setStyleSheet("")
        if not text:
            note.setStyleSheet(self.FAIL_STYLE)
            note.setText("先把这个供应商的 Key 粘进框里")
            return
        bad = ""
        if not text.isascii():
            bad = "这把 Key 里有中文或全角字符——多半是复制时带进来的。"
        elif any(ch.isspace() for ch in text):
            bad = "Key 中间夹了空格或换行：它应该是一串连续字符。"
        elif len(text) < 8:
            bad = f"这把 Key 只有 {len(text)} 位，短得不像一把 Key。"
        if bad:
            note.setStyleSheet(self.FAIL_STYLE)
            note.setText(bad)
            return
        if not callable(self._on_key_save):
            note.setStyleSheet(self.FAIL_STYLE)
            note.setText("还没连上引擎：这串 Key 没能交出去")
            return

        block["saving"] = True
        block["error"] = ""
        self._key_refresh(slug)

        def done(ok: bool, lines: list[str] | None = None) -> None:
            # 回执可能比引擎推来的新清单晚：那时这块控件已经被 `update_rows` 重建、旧的
            # `deleteLater` 掉了，写上去等于什么都没显示（成功没有「已保存。」、失败连原因都看不见）。
            # 所以一律按 slug 现取当前那块。
            live = self._key_blocks.get(slug) or block
            live["saving"] = False
            if ok:
                self._key_confirmed.add(slug)
                live["configured"] = True
                live["row"]["configured"] = True
                live["key"].clear()
                live["error"] = ""
            else:
                live["error"] = "\n".join(lines or []) or "引擎没说原因"
            # 先让 _key_refresh 把徽标/按钮画对，再写回执那句话——反过来的话它会把提示清掉
            self._key_refresh(slug)
            live["note"].setStyleSheet("" if ok else self.FAIL_STYLE)
            live["note"].setText("已保存。" if ok else live["error"])
            live["note"].setVisible(True)
            if ok:
                # 这家已经能用了：模型设置那边补一张卡（清单由引擎随回包送来时再换一遍）
                self._rebuild_models()

        try:
            self._on_key_save(slug, text, done)
        except Exception as exc:  # noqa: BLE001 - 交不出去也得说清，不能停在「保存中…」
            done(False, [f"没能把 Key 交给引擎：{type(exc).__name__}: {exc}"])
            return
        QtCore.QTimer.singleShot(20000, lambda s=slug, t=text: self._key_give_up(s, t))

    def _key_give_up(self, slug: str, text: str) -> None:
        block = self._key_blocks.get(slug)
        if not block or not block.get("saving") or block["key"].text().strip() != text:
            return
        block["saving"] = False
        block["error"] = "引擎 20 秒没回话：不确定存没存上，重开一次设置看这家是不是「已配置」。"
        block["note"].setStyleSheet(self.FAIL_STYLE)
        block["note"].setText(block["error"])
        self._key_refresh(slug)

    def _key_refresh(self, slug: str) -> None:
        """一家的样子：徽标（已配置/未配置/保存中）+ 按钮可不可按。"""
        block = self._key_blocks.get(slug)
        if not block:
            return
        needs_key = self._needs_key(block["row"])
        if block.get("saving"):
            self._set_badge(block["badge"], "保存中…")
            block["button"].setText("保存中…")
            block["button"].setEnabled(False)
            block["key"].setEnabled(False)
            block["note"].setStyleSheet("")
            block["note"].setText("正在把这串 Key 交给引擎…")
            return
        block["button"].setText("确定")
        block["button"].setEnabled(needs_key)
        block["key"].setEnabled(needs_key)
        if block["configured"]:
            self._set_badge(block["badge"], "已配置", ok=True)
        else:
            self._set_badge(block["badge"], "未配置" if needs_key else "用登录态")
        if not needs_key:
            block["note"].setText("这家用引擎的登录态，没有要填的密钥。")
        elif not block.get("error"):
            block["note"].setText("" if block["configured"] else "")
        block["note"].setVisible(bool(block["note"].text().strip()))

    def update_rows(self, rows: list[dict], current: str | None = None) -> None:
        """引擎把新清单送回来了：两页一起按新数据重铺，框里已经敲的东西、选过的模型都留着。

        `current` 是引擎自己报的「现在在跑哪个模型」——设置页副标题照它说，
        不拿本进程启动时那份（启动时故意不取清单，那份往往还是空的）。
        """
        incoming = [row for row in (rows or []) if row.get("provider")]
        if not incoming:
            return
        if current is not None:
            self._current_option = str(current or "").strip()
        typed = {
            slug: block["key"].text()
            for slug, block in getattr(self, "_key_blocks", {}).items()
            if block["key"].text().strip()
        }
        self._choices = incoming

        if hasattr(self, "_keys_scroller"):
            layout = self._keys_scroller.widget().layout()
            for slug in list(self._key_blocks):
                block = self._key_blocks.pop(slug)
                block["block"].setParent(None)
                block["block"].deleteLater()
            anchor = 0
            for row in self._ordered_rows():
                self._add_key_block(layout, row)  # addWidget → 追加在空态/弹簧后面
                layout.removeWidget(self._key_blocks[str(row["provider"])]["block"])
                layout.insertWidget(anchor, self._key_blocks[str(row["provider"])]["block"])
                anchor += 1
            for slug, text in typed.items():
                block = self._key_blocks.get(slug)
                if block is not None:
                    block["key"].setText(text)
            # 刚存成功、还等引擎把新清单里的 `authenticated` 补上的那几家：重建后照样显示「已配置」。
            # 不这样的话，引擎推清单比回执早一点，徽标就退回「未配置」，用户以为没存上。
            for slug in self._key_confirmed:
                block = self._key_blocks.get(slug)
                if block is not None and not block.get("configured"):
                    block["configured"] = True
                    block["row"]["configured"] = True
            self._keys_apply_view()

        if hasattr(self, "_text_scroller"):
            self._rebuild_models()

    def _text_apply_view(self) -> None:  # 兼容旧调用点：模型设置不做搜索，直接重铺
        self._rebuild_models()

    def _ensure_models(self, slug: str) -> None:
        """这一家的清单还没到手：只按现有数据重画，不去别处找。"""
        block = self._text_blocks.get(slug)
        if not block:
            return
        if block["row"].get("models"):
            self._fill_block_models(block)
        self._model_refresh(slug)

    def _fill_block_models(self, block: dict) -> None:
        """把清单填进这一块自己的下拉，并停在「上次选的那一个」上。"""
        combo = block["model"]
        picked = str(combo.currentData() or "")  # 可能已经手选过：重填不许把手选的那个冲掉
        combo.blockSignals(True)  # 重填不算用户改选
        combo.clear()
        slug = str(block["row"].get("provider") or "")
        # 按名字排：引擎给的顺序是线上清单的原序（OpenRouter 那几百个看着就是乱的）
        for item in sorted(block["row"]["models"], key=lambda it: str(it or "").casefold()):
            _add_short(combo, _model_label(slug, item), item)
        for wanted in (picked, str(block.get("preferred") or "")):
            index = combo.findData(wanted) if wanted else -1
            if index >= 0:
                combo.setCurrentIndex(index)
                break
        combo.blockSignals(False)
        _fit_popup(combo)

    def _model_refresh(self, slug: str) -> None:
        """模型设置里一块的样子：启用圈 + 徽标（已配置/没有模型）。"""
        block = self._text_blocks.get(slug)
        if not block:
            return
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
        block["note"].setStyleSheet("")
        if block["row"].get("models"):
            self._set_badge(block["badge"], "已配置", ok=True)
            block["note"].setText("")
        else:
            self._set_badge(block["badge"], "没有模型")
            block["note"].setText("引擎这次没返回这家的模型清单：换个时间再打开设置。")
        block["note"].setVisible(bool(block["note"].text().strip()))

    def _text_use_changed(self, slug: str, on: bool) -> None:
        """圈了「未启用」的那家：真正生效的换成它，其余块的单选和状态一起重画。"""
        if not on:
            return
        self._text_use = slug
        self._text_dirty = True
        self._ensure_models(slug)  # 点了这一家：它还没有模型清单就按现有数据重画
        for other in self._text_blocks:
            self._model_refresh(other)

    def _enable_block(self, slug: str) -> None:
        """用户在下拉里挑了一个模型 → 这一家就是要启用的那家。

        单选被点掉会走 `_text_use_changed`，`_text_use` 自然跟上；别在这里再写一遍。
        """
        self._text_dirty = True
        block = self._text_blocks.get(slug) or {}
        use = block.get("use")
        if use is not None and not use.isChecked():
            use.setChecked(True)

    def _build_voice_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """语音模型页：三家引擎各一块，Key、语言、音色、档位各归各段。

        跟文本页同一个规矩——一次能把几家的 Key 都填上、一次保存全部落进 `.env`；
        右边单选决定现在是谁在说话，其余家只记自己选过的音色。
        开/关不在这里：那个开关在输入框旁边（审批/思考档位那一排），说话前顺手就能关。
        """
        from app import doubao_voices, speech_prefs

        self._voice_picks: dict[str, dict] = {}
        self._voice_dirty = False   # 用户动过某家的音色（哪怕是没在用的那家）：保存时不能当「没改」跳过
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
            # 变量名就是 Key 框的占位提示，标题栏里不再挂一遍
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
        self._voice_dirty = True
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
        self._set_badge(block["badge"], badge, ok=badge == "已配置")
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
        """「在用」那家现在选的是什么：音色（ElevenLabs 还带档位），Key 有就一起带上。

        缓存只在**界面答不出来**的时候兜底（清单还没拉到、控件被清空）。以前这里无条件
        优先缓存，存过一次之后再改音色/改引擎，第二次保存写回去的还是第一次的快照。
        """
        provider = self._voice_use
        block = self._voice_block(provider)
        saved = dict(self._voice_saved) if self._voice_saved else {}
        if not block:
            return saved or {"provider": provider}
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
                if saved.get(empty_field):
                    chosen[empty_field] = saved[empty_field]  # 界面答不出时才用上次存的那份
                else:
                    chosen.pop(empty_field)
        if "model_id" in chosen and not chosen["model_id"] and saved.get("model_id"):
            # 档位只有 ElevenLabs 有；别家本来就没有这个键，不能凭空补上去
            chosen["model_id"] = saved["model_id"]
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
        """「模型设置」这一页的取值：每家选的模型 + 哪一家「在用」。

        密钥不从这里走：每家的 Key 在「密钥设置」里各自按「确定」存了。
        """
        if self._text_saved:
            # 已经交出去过：外面随后再问一次要的还是同一份，别因为界面状态变了给出另一个版本
            return dict(self._text_saved)
        models: dict[str, str] = {}
        for slug, block in self._text_blocks.items():
            models[slug] = str(block["model"].currentData() or "")
        using = self._text_use  # 没圈就是没圈：别替他挑一家
        return {
            "provider": using,
            "key": "",
            "keys": {},
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
        self._pending_steps: list = []   # 等异步校验时，后面还没验过的保存步骤
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

        layout.addWidget(_field_label("人设（SOUL.md）", page))
        self._soul_edit = _make_editor(page, _read_soul(), 280)
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
        _fill_editor(self._soul_edit, text)
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

    def _build_memory_page(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """她「记得什么」：`NOESIA_HOME/memories/` 下的 USER.md / MEMORY.md，加两个上限。

        条目之间是单独一行 `§`——就是引擎写出来的样子。两个上限对应引擎配置 `memory` 段里的
        `user_char_limit` / `memory_char_limit`，不在这儿另存一份。
        """
        from app.engine_config import memory_limits

        page = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        memory_limit, user_limit = memory_limits(ROOT / SETTINGS.engine.core_path, _engine_home())
        self._limits_at_open = (user_limit, memory_limit)
        limits = QtWidgets.QHBoxLayout()
        limits.setContentsMargins(0, 0, 0, 0)
        limits.setSpacing(6)
        limits.addWidget(_field_label("用户画像上限", page))
        self._user_limit = QtWidgets.QLineEdit(page)
        self._user_limit.setValidator(QtGui.QIntValidator(200, 20_000, page))
        self._user_limit.setFixedWidth(64)
        self._user_limit.setText(str(user_limit) if user_limit else "")
        self._user_limit.textChanged.connect(self._on_memory_edited)
        limits.addWidget(self._user_limit)
        limits.addWidget(_field_label("字符", page))
        limits.addSpacing(16)
        limits.addWidget(_field_label("记忆上限", page))
        self._note_limit = QtWidgets.QLineEdit(page)
        self._note_limit.setValidator(QtGui.QIntValidator(200, 20_000, page))
        self._note_limit.setFixedWidth(64)
        self._note_limit.setText(str(memory_limit) if memory_limit else "")
        self._note_limit.textChanged.connect(self._on_memory_edited)
        limits.addWidget(self._note_limit)
        limits.addWidget(_field_label("字符", page))
        limits.addStretch(1)
        layout.addLayout(limits)

        layout.addWidget(_field_label("用户画像（USER.md）—— 她记住的「你是谁」", page))
        self._user_edit = _make_editor(page, _read_memory_file("USER.md"), 92)
        self._user_edit.textChanged.connect(self._on_memory_edited)
        layout.addWidget(self._user_edit, 1)

        layout.addWidget(_field_label("她的记录（MEMORY.md）—— 她自己的笔记、踩过的坑", page))
        self._note_edit = _make_editor(page, _read_memory_file("MEMORY.md"), 92)
        self._note_edit.textChanged.connect(self._on_memory_edited)
        layout.addWidget(self._note_edit, 1)

        self._memory_hint = QtWidgets.QLabel("", page)
        self._memory_hint.setObjectName("dialog-hint")
        self._memory_hint.setWordWrap(True)
        layout.addWidget(self._memory_hint)
        self._on_memory_edited()
        return page

    def _memory_limit_inputs(self) -> tuple[int, int] | None:
        """(画像上限, 记忆上限)；填的不是 200–20000 的整数就返回 None。"""
        values: list[int] = []
        for box in (self._user_limit, self._note_limit):
            raw = box.text().strip()
            if not raw.isdigit() or not 200 <= int(raw) <= 20_000:
                return None
            values.append(int(raw))
        return values[0], values[1]

    def _memory_snapshot(self) -> tuple[str, str, str, str]:
        """这一页的现值：两份记忆的规范化文本 + 两个上限框。没改动就不写盘。"""
        return (
            _memory_text(_memory_entries(self._user_edit.toPlainText())),
            _memory_text(_memory_entries(self._note_edit.toPlainText())),
            self._user_limit.text().strip(),
            self._note_limit.text().strip(),
        )

    def _on_memory_edited(self) -> None:
        """字数对着上限实时走：超了当场标红，别等点了保存才知道。"""
        limits = self._memory_limit_inputs()
        parts: list[str] = []
        over = False
        for label, editor, index in (("画像", self._user_edit, 0), ("记录", self._note_edit, 1)):
            count = len(_memory_text(_memory_entries(editor.toPlainText())))
            if limits is None:
                parts.append(f"{label} {count:,} 字符")
                continue
            limit = limits[index]
            over = over or count > limit
            pct = int(count * 100 / limit) if limit else 0
            parts.append(f"{label} {count:,}/{limit:,}（{pct}%）")
        self._memory_hint.setStyleSheet(self.FAIL_STYLE if over else "")
        tail = "。超出上限，保存会被拦下。" if over else "。条目之间单独一行 §；保存后新建会话才带进上下文。"
        self._memory_hint.setText("；".join(parts) + tail)

    def _save_memory_only(self) -> str:
        """只改记忆：上限写进引擎配置，两份记忆文件整份写回，别的页一个字节都不动。"""
        from app.engine_config import set_engine_config

        if self._memory_snapshot() == self._initial["memory"]:
            # 没动过（包括本来就没有文件的）：不写、也不拦着别的页保存
            self._memory_changed = False
            self._mark_saved("memory")
            self._on_memory_edited()  # 顺手把上次的红字换回正常说明
            return "ok"

        limits = self._memory_limit_inputs()
        if limits is None:
            self._memory_hint.setStyleSheet(self.FAIL_STYLE)
            self._memory_hint.setText("上限要填 200–20000 之间的整数（字符数）。")
            return "fail"

        user_limit, memory_limit = limits
        plan = (
            ("USER.md", self._user_edit.toPlainText(), user_limit, "用户画像"),
            ("MEMORY.md", self._note_edit.toPlainText(), memory_limit, "记忆"),
        )
        prepared: list[tuple[str, str]] = []
        for name, raw, limit, label in plan:
            text = _memory_text(_memory_entries(raw))
            if len(text) > limit:
                self._memory_hint.setStyleSheet(self.FAIL_STYLE)
                self._memory_hint.setText(
                    f"{label}有 {len(text):,} 字符，超过上限 {limit:,}（多 {len(text) - limit:,}）。"
                    "删掉一些条目，或者把上限调大。"
                )
                return "fail"
            prepared.append((name, text))

        try:
            for name, text in prepared:
                path = _memory_file(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                # 整份替换：先写临时文件再改名，引擎那边永远不会读到写了一半的记忆
                temp = path.with_name(path.name + ".tmp")
                temp.write_text(text, encoding="utf-8", newline="")
                temp.replace(path)
            if (user_limit, memory_limit) != self._limits_at_open:
                set_engine_config(
                    ROOT / SETTINGS.engine.core_path,
                    _engine_home(),
                    {"memory.memory_char_limit": memory_limit, "memory.user_char_limit": user_limit},
                )
        except Exception as exc:  # noqa: BLE001 - 写不进去就说清楚，别装作保存了
            self._memory_hint.setStyleSheet(self.FAIL_STYLE)
            self._memory_hint.setText(f"写不进去：{exc}")
            return "fail"

        # 只在「本来有、这次清空」时说一句；文件本来就不存在（第一个字都没写过）不算清空
        for (name, text), old in zip(prepared, self._initial["memory"][:2]):
            if old and not text:
                self._save_notices.append(f"{name} 已经清空——她这方面现在一条都不记得了。")
        self._memory_changed = True
        self._limits_at_open = (user_limit, memory_limit)
        self._mark_saved("memory")
        self._initial["memory"] = self._memory_snapshot()
        self._on_memory_edited()
        return "ok"

    def memory_changed(self) -> bool:
        """刚才保存的记忆是真的改了，还是原样保存（`main.py` 据此用不同措辞）。"""
        return bool(getattr(self, "_memory_changed", False))

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
        """打开设置那一刻六页各自的取值：用来判断这次到底改了哪几页。"""
        return {
            "text": self.selection(),
            "voice": self.voice_selection(),
            "avatar": (
                str(self._avatar_provider_box.currentData() or ""),
                str(self._avatar_model_box.currentData() or ""),
            ),
            "workspace": self._workspace_edit.text().strip(),
            "soul": self._soul_edit.toPlainText(),
            "memory": self._memory_snapshot(),
        }

    def _mark_saved(self, page: str) -> None:
        """记下这一页这次真的落地了（`main.py` 按这个逐项收尾，不再猜是哪个页签）。"""
        self._saved_pages.add(page)

    def save_notices(self) -> list[str]:
        """跟 `save_notes` 一样只在关窗后说，但这些不是错误：普通一条灰字。"""
        return list(getattr(self, "_save_notices", []) or [])

    def save_notes(self) -> list[str]:
        """只有关窗才会丢的信息（比如这一页压根没拿到模型清单）：窗口关掉之后要补说一句。"""
        return list(getattr(self, "_save_notes", []) or [])

    def saved_pages(self) -> set[str]:
        """这次保存真正落地的页：`text` / `soul` / `workspace` / `avatar` / `voice` / `memory`。"""
        return set(getattr(self, "_saved_pages", set()))

    def _save_text_page(self) -> str:
        """模型设置这一页：只有「圈中的那家 + 它选的模型」要生效；Key 已经在密钥页各存各的。"""
        from app import text_prefs

        if not self._text_blocks:
            # 一家都没配好：这页没得改，别挡住别的页；但关窗之后要说清这一页没存
            self._save_notes.append("模型设置这一页没动：还没存过任何 Key，先在「密钥设置」里配一家。")
            return "ok"
        data = self.selection()
        # 「没动过」只在引擎那边真有一个模型在跑时才算数：一个模型都没启用的时候按保存，
        # 就是要把它用起来——同一份取值也算一次改动，否则点了保存什么都不发生。
        if not self._text_dirty and str(self._current_option or "").strip():
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
            if self._text_dirty:
                # 这一页他确实动过：没圈就不能算存好（不许替他圈一家）
                return fail("还没圈「启用」：模型这一页改了但没选一家，先圈一家再点保存。")
            # 没动过、引擎里也没有在跑的模型（停在「Key 已存、模型未启用」这个中间态）：
            # 这一页本来就没得写，但它没有理由把别的人设/记忆/工作区一起拦下。
            self._save_notes.append("模型设置这一页没动：还没圈「启用」，想换模型在那一页圈一家。")
            return "ok"
        if not data["model"]:
            return fail(f"{row.get('label') or slug} 没有可选模型：换个时间重开设置再取一次。")
        text_prefs.remember_many(data.get("models") or {})  # 每家上次选的模型各自记着
        # 「已生效」等到引擎回执再记：引擎可能失败（Key 过期、写 .env 出错、8 秒不回话）。
        # 先记下来的话，下一轮保存的「没动过」判据会把这个失败当成功放过——窗口照关，
        # 用户以为换好了，其实引擎里还是老模型。
        self._text_pending = {"option": f"{slug}:{data['model']}", "data": dict(data)}
        self._mark_saved("text")
        return "ok"

    def _commit_text_save(self) -> None:
        """引擎回话说成了：这一刻才算这一页真的落地（此后 `selection()` 也照这份答）。"""
        pending, self._text_pending = self._text_pending, {}
        if not pending:
            return
        self._text_dirty = False
        self._current_option = str(pending.get("option") or self._current_option)
        self._text_saved = dict(pending.get("data") or {})

    def _on_save(self) -> None:
        """点一次保存就把六页一起存：哪一页没过校验就跳到那一页贴原因，窗口不关。"""
        self._saved_tab = self._tabs.currentIndex()
        self._saved_pages = set()
        self._text_saved = {}  # 新一轮保存：按界面现值重新取一遍（上次的留底只在同一次保存里复用）
        self._text_pending = {}  # 上一轮的失败没落地：这一轮重新交一次，别拿着旧的那份关窗
        self._pending_steps = []
        steps = (
            (self.TAB_MODELS, self._save_text_page),
            (self.TAB_SOUL, self._save_soul_only),
            (self.TAB_WORKSPACE, self._save_workspace_only),
            (self.TAB_AVATAR, self._save_avatar_only),
            (self.TAB_VOICE, self._save_voice_only),
            (self.TAB_MEMORY, self._save_memory_only),
        )
        self._warn.setStyleSheet("")
        self._run_saves(list(steps))

    def _run_saves(self, steps: list) -> None:
        """按顺序存六页。哪一页在等异步结果（人形页校验 Token），就把剩下的记下来，回来接着走。"""
        try:
            for index, (tab, save) in enumerate(steps):
                outcome = save()
                if outcome == "fail":
                    self._tabs.setCurrentIndex(tab)  # 停在不通过的那一页：原因已经贴在该页上
                    return
                if outcome == "pending":
                    # 后面几页还没验过：等校验回来直接关窗的话，语音页的错会被整页吞掉
                    self._pending_steps = steps[index + 1:]
                    self._tabs.setCurrentIndex(tab)  # 别让人干等：切到正在等结果的那一页
                    return
        except Exception as exc:  # noqa: BLE001 - 保存里崩了也必须说出来，不许没反应
            self._warn.setStyleSheet(self.FAIL_STYLE)
            self._warn.setText(f"保存出错了：{type(exc).__name__}: {exc}")
            return
        self._close_or_save()

    def _resume_saves(self) -> None:
        """异步校验回来了：把没走完的页接着存完，再决定关不关窗。"""
        steps, self._pending_steps = self._pending_steps, []
        if steps:
            self._run_saves(steps)
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
            self._commit_text_save()
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
        # 校验通过了才写：以前先在保存时写 avatar.json，Token 是坏的也把厂商改掉了，
        # 用户看到配置已经变了，以为生效了，其实一次都跑不起来。
        avatar_prefs.write_token(self._avatar_pending, self._avatar_pending_provider)
        avatar_prefs.save(self._avatar_pending_provider, self._avatar_model_box.currentData())
        self._mark_saved("avatar")
        self._avatar_key_drafts.pop(self._avatar_pending_provider, None)  # 已经进 .env，草稿不用留
        self._avatar_pending = ""
        self._avatar_key_edit.clear()
        self._avatar_hint.setStyleSheet("")
        self._avatar_hint.setText(message + " 已存进 data/NoesiaHome/.env。")
        self._resume_saves()  # 校验过了就往下走：后面还没验的页先验完，再收尾

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
        if not typed:
            # 只改了厂商/模型：没有凭证要校验，直接落地
            avatar_prefs.save(provider, self._avatar_model_box.currentData())  # 用哪家、哪个模型进 avatar.json
            self._mark_saved("avatar")
            return "ok"
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
        if not keys and not self._voice_dirty and self.voice_selection() == self._initial["voice"]:
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
            if not speech_prefs.write_api_key(keys["elevenlabs"], "elevenlabs"):
                # 写不进 .env（键里混了中文/被清成空）时不能假装已配置：以前照样清空输入框、
                # 打上「已配置」，再去拉清单——清单拉不回来，用户只看到音色列表是空的。
                if eleven is not None:  # 这张卡没建起来时也要把失败说在能看见的地方
                    eleven["note"].setStyleSheet(self.FAIL_STYLE)
                    eleven["note"].setText(
                        "这把 Key 没存上：里面有中文或别的非英文字符，只粘 sk-… 这一串"
                    )
                return "fail"
            if eleven is not None:
                eleven["key"].clear()  # 已经进 .env，框里不用再留
                eleven["configured"] = True
            self._load_voices()
            self._voice_refresh("elevenlabs")
            return "fail"  # 停在语音页：还差一个音色没选
        # 别家先把 Key 落盘（每家一个变量、互不影响），「在用」那家走正常保存流程
        for provider, typed in keys.items():
            if provider == self._voice_use:
                continue
            if not speech_prefs.write_api_key(typed, provider):
                block = self._voice_block(provider)
                if block:
                    block["note"].setStyleSheet(self.FAIL_STYLE)
                    block["note"].setText(
                        "这把 Key 没存上：里面有中文或别的非英文字符，只粘 sk-… 这一串"
                    )
                return "fail"
            block = self._voice_block(provider)
            block["key"].clear()
            block["configured"] = True
            self._voice_refresh(provider)
        try:
            speech_prefs.save(**self.voice_selection())
        except ConfigError as exc:
            # 错误是跟着当前这块走的：以前一律写在豆包那块，切到 ElevenLabs 时用户看到
            # 「豆包」下面冒一行红字，根本不知道是自己刚填的东西出了问题。
            target = self._voice_block(self._voice_use) or self._voice_block("doubao")
            if target:
                target["note"].setStyleSheet(self.FAIL_STYLE)
                target["note"].setText(str(exc))
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
        self._voice_dirty = False
        self._mark_saved("voice")
        return "ok"


