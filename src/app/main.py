"""Qt 前端骨架（悬浮窗）。

阶段 4 才做精致界面，这里只保证三件事是真的：
1. Qt 绑定缺失时给出可执行的提示，而不是抛一堆 ImportError；
2. 悬浮窗按方案 8C 的窗口标志创建（无边框 / 置顶 / 透明 / 可拖拽）；
3. 形象与界面参数来自 data/settings/app.json（模板 app.example.json），代码里不留常量兜底。

用法：noesia-gui 或 python -m app（在项目目录里跑，或先设 NOESIA_HOME）
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
import math
import sys
from pathlib import Path

from paths import project_root, repo_root

from .status_stack import CONFIRM_TTL_MS

logger = logging.getLogger(__name__)

_CRASH_LOGGING_INSTALLED = False

_MENU_QSS = """
QMenu {
    background-color: #241f2e;
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 10px;
    padding: 6px;
    color: #ece6ef;
    font-size: 12px;
}
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 6px; }
QMenu::item:selected { background-color: rgba(240, 178, 210, 52); color: #ffffff; }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 20); margin: 5px 10px; }
"""

_TIP_QSS = """
QToolTip {
    background-color: #241f2e;
    color: #f2e6ee;
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 6px;
    padding: 3px 7px;
    font-size: 11px;
}
"""

def say(message: str) -> None:
    """启动期的一句话，写进日志，控制台在时也照旧打印。

    双击启动走的是 pythonw.exe，那里没有 stdout——直接 print 的话整行凭空消失，
    看日志时会把「没有输出」当成「没走到这里」。
    """
    logger.info(message)
    if sys.stdout is not None:
        print(message, flush=True)

PROJECT_ROOT = project_root()
REPO_ROOT = repo_root()


def should_clear_on_session_ready(
    result: dict, *, requested: bool, has_transcript: bool
) -> bool:
    """`session.create` 回来时该不该清空聊天窗。

    - 用户点了「新会话」：清（`requested`），屏幕里有什么都不影响；
    - 引擎自己回来的（开窗、重连）：只在屏幕还空着时清——那时工具行已经画在窗口里了，
      清一次等于把刚显示的东西抹掉（`messages` 有内容说明要铺历史，更不能清）。
    """
    if requested:
        return True
    return not result.get("messages") and not has_transcript

from .tool_view.i18n import set_runtime_i18n_locale  # noqa: E402

set_runtime_i18n_locale("zh")

QT_BINDING = ""
try:
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets  # type: ignore

    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtNetwork, QtWidgets  # type: ignore

        QT_BINDING = "PySide2"
    except ImportError:
        QtCore = QtGui = QtNetwork = QtWidgets = None  # type: ignore

INSTANCE_NAME = "NoesiaAvatar"


def _flag(value, *, default: bool = True) -> bool:
    """配置里的开关：只有 `false` / `"false"` / `0` 算关（桌面端 `setShowReasoningFromConfig` 同一条判据）。"""
    if value is None:
        return default
    return not (value is False or value == "false" or value == 0)





def build_model_choices(rows: list[dict]) -> list[dict]:
    """「设置模型」对话框的数据：引擎 `model.options` 的全量供应商行（含没配 Key 的家）。

    `rows` 是引擎回的 `providers`（`explicit_only=False` + `include_unconfigured=True`）：
    已配的家带模型清单，没配的家是骨架行（`authenticated=false`、`key_env` 说明该填哪个变量）。
    这里只做形状转换，不补默认值、不猜清单——能不能用、有哪些模型全是引擎说的。
    """
    choices: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        key_env = str(row.get("key_env") or "").strip()
        choices.append(
            {
                "provider": slug,
                "label": str(row.get("name") or slug),
                "env_vars": [key_env] if key_env else [],
                "configured": row.get("authenticated") is not False,
                "models": [m for m in (row.get("models") or []) if isinstance(m, str) and m],
                "auth_type": str(row.get("auth_type") or ""),
                "warning": str(row.get("warning") or ""),
            }
        )
    return choices


def media_roots() -> list[Path]:
    """模型写的相对路径（`MEDIA:data/workspace/x.png`）该从哪儿找：工作目录 + 引擎家 + 仓库根。"""
    roots: list[Path] = []
    try:
        cfg, resolver = _load_avatar_config()
    except SystemExit:
        cfg = resolver = None
    base = Path(getattr(resolver, "root", None) or PROJECT_ROOT)
    engine_cfg = getattr(cfg, "engine", None)
    for key in ("workdir", "home"):
        raw = getattr(engine_cfg, key, None) if engine_cfg is not None else None
        if raw:
            candidate = Path(str(raw))
            roots.append(candidate if candidate.is_absolute() else (base / candidate))
    roots.append(base / "data" / "workspace")
    roots.append(base)
    return roots


def _load_avatar_config():
    """读 `data/settings/app.json`，返回 (RootConfig, resolver)。"""
    from settings import load_settings

    return load_settings()


if QtWidgets is None:  # pragma: no cover
    say("未检测到 Qt 绑定（PySide6 / PySide2）。安装其一后重试：")
    say('  python -m pip install -e ".[qt]"')
    say("本文件只负责界面骨架，引擎可用 `noesia check-config` 单独验证。")
    raise SystemExit(0)


from backends import catalog
from app.chat_panel import ChatPanel

HEARTS = ((0.40, 13.0, 0.0), (0.57, 16.0, 0.12), (0.48, 11.0, 0.30), (0.66, 12.0, 0.42), (0.35, 14.0, 0.58))
from app.speech_speaker import Speaker
from app.engine_config import sync_engine_config, workspace_from_config
from settings import ensure_runtime
from app.engine_wire import EngineWire
from app.idle_video import IdleVideoPlayer
from app.user_state import window_settings

from app.window_geometry import (
    DEFAULT_WIDTH,
    MIN_WIDTH,
    cursor_for,
    edges_at,
    fit_width,
    hit_region,
    size_from_drag,
)


WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1


def same_workspace(current: object, target: str) -> bool:
    """两个目录是不是同一个（Windows 大小写/斜杠/相对写法都算同一个）。"""
    if current is None or not str(current).strip():
        return False
    try:
        return Path(str(current)).resolve() == Path(str(target)).resolve()
    except OSError:
        return str(current).strip().lower() == str(target).strip().lower()


class _WinMsg(ctypes.Structure):
    """Win32 `MSG` 的前两个字段：只为了认出这条消息是不是 `WM_NCHITTEST`。

    坐标不从 lParam 里拆：那是**物理像素**，而 `mapFromGlobal()` 要**逻辑像素**。屏缩放不是 100% 时
    两者差一个比例，判「在不在她身上」会整体错位——窗口挪到那块屏上就点不动、抓不到边。
    """

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
    ]


class FloatingAvatar(QtWidgets.QWidget):  # type: ignore[misc]
    """无边框、置顶、透明背景、可拖拽的形象窗口。"""

    def __init__(self, image_path: Path, floating_cfg, idle_video: Path | None = None) -> None:
        flags = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint
        if floating_cfg.always_on_top:
            flags |= QtCore.Qt.WindowStaysOnTopHint
        super().__init__(None, flags)
        if floating_cfg.transparent:
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Noesia Agent")

        self._pixmap = QtGui.QPixmap(str(image_path)) if image_path.is_file() else QtGui.QPixmap()
        if self._pixmap.isNull():
            self._pixmap = QtGui.QPixmap(320, 480)
            self._pixmap.fill(QtCore.Qt.transparent)
        self._scale = 0.5
        self._aspect = (self._pixmap.height() / self._pixmap.width()) if self._pixmap.width() else 1.333
        self._default_size = fit_width(DEFAULT_WIDTH, self._aspect)
        self._max_size = fit_width(self._pixmap.width() * self._scale, self._aspect)
        self.setMinimumSize(MIN_WIDTH, round(MIN_WIDTH * self._aspect))
        self.setMaximumSize(self._max_size)
        self.resize(self._default_size)
        self._settings = window_settings()
        self._resize_edge: QtCore.Qt.Edges | None = None
        self._resize_start_size: QtCore.QSize | None = None
        self._resize_start_global: QtCore.QPoint | None = None
        self._drag_offset: QtCore.QPoint | None = None
        self._hit_region: QtGui.QRegion | None = None
        self._hit_size: tuple[int, int] | None = None
        self._hit_timer = QtCore.QTimer(self)
        self._hit_timer.setSingleShot(True)
        self._hit_timer.timeout.connect(self._rebuild_hit_region)
        self._tray: QtWidgets.QSystemTrayIcon | None = None
        self._toggle_action: QtGui.QAction | None = None
        self.setToolTip("拖动移动 · 拖边缘缩放 · 双击聊天 · Esc 隐藏 · 右键菜单")
        self.setMouseTracking(True)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.chat_panel: ChatPanel | None = None
        self._pending_reviews: list[str] = []
        self._pending_notices: list[tuple[str, str]] = []
        self._greeted = False
        self._approval_dialog = None
        self._approval_dialogs: list = []
        self._name_dialog = None
        self.client = None
        self._new_session_pending = False
        self._new_session_sent = False
        self._new_session_queued = 0
        self._new_session_timer = QtCore.QTimer(self)
        self._new_session_timer.setSingleShot(True)
        self._new_session_timer.timeout.connect(self._new_session_timeout)
        self._rewind_pending: tuple[dict, str] | None = None
        self._models = None
        self._provider_rows: list[dict] = []
        self._model_dialog = None
        self._models_refresh_token = 0
        self._reasoning_current: str | None = None
        self._reasoning_pending = False
        self._reasoning_fresh = False
        self._reasoning_wire = ""
        self._boot_done = False
        self.boot_splash = None
        self._history_asked = False
        self._restore_geometry()

        self._frame: QtGui.QImage | None = None
        self._painted_frame: QtGui.QImage | None = None
        self._hearts_at = -99.0
        self._hearts_timer: QtCore.QTimer | None = None
        self.idle_player: IdleVideoPlayer | None = None
        self.speaker = Speaker(self)
        self._avatar = None
        self._streamer = None
        self._pending_speech = ""
        self._speech_seq = 0
        self._last_user_text = ""
        self._interactive_shown = False
        self._hidden = False
        self._avatar_idle = QtCore.QTimer(self)
        self._avatar_idle.setSingleShot(True)
        self._avatar_idle.setInterval(5_000)
        self._avatar_idle.timeout.connect(self._end_avatar_session)
        self._warm_idle = QtCore.QTimer(self)
        self._warm_idle.setSingleShot(True)
        self._warm_idle.setInterval(20_000)
        self._warm_idle.timeout.connect(self._end_avatar_session)
        self._finish_waits = 0
        self._reset_sent = True
        self._avatar_block_until = 0.0
        self._avatar_block_hint = "Vidu 积分不够，互动数字人暂时用不了：这句用纯语音念，音色不变。"
        self._proc_timer: QtCore.QTimer | None = None
        self._voice_failure_shown = ""
        self.speaker.failed.connect(self._note_voice_failure)
        self.speaker.started.connect(lambda _text: self._voice_ok())
        self._reply_text = ""
        self._vocab_learning = False
        self._turn_stopped = False
        self._voice_model_downloading = False
        self._voice_model_tried = False
        self._sending_attachments: list = []
        if idle_video is not None:
            self.idle_player = IdleVideoPlayer(idle_video, self)
            self.idle_player.set_target_size(self.size())
            self.idle_player.frame_ready.connect(self._on_video_frame)
            timer = QtCore.QTimer(self)
            timer.setInterval(33)
            timer.timeout.connect(self._paint_latest_frame)
            timer.start()
            self._paint_timer = timer
            self.idle_player.failed.connect(lambda msg: say(f"[待机视频] {msg}"))


    def _screen_limit(self) -> QtCore.QRect:
        """当前这块屏的可用区域（居中、限高用）。"""
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1920, 1080)

    @staticmethod
    def _on_some_screen(rect) -> bool:
        """这块区域跟**任意一块**屏相交就算在屏幕内。

        只比「当前屏」不行：窗口还没 show 时 `self.screen()` 就是主屏，放在副屏上的位置
        必然不与主屏相交，于是每次启动都被判成「屏幕外」拉回主屏——顺手再把主屏坐标
        写回存档，副屏位置永久丢掉。"""
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return True
        return any(screen.availableGeometry().intersects(rect) for screen in screens)

    def _max_height(self) -> int:
        """高度上限＝能拖到的最大尺寸的高度（窗口宽度最大就是这个尺寸，按比例算下来也一样）。"""
        return self._max_size.height()

    def _sync_chat_anchor(self) -> None:
        """把「她在屏幕上的位置」告诉聊天窗：拖到下方时靠它对齐中心线。"""
        if self.chat_panel is not None:
            self.chat_panel.set_anchor(self.frameGeometry())

    def _center_on_screen(self) -> None:
        area = self._screen_limit()
        self.move(
            area.x() + (area.width() - self.width()) // 2,
            area.y() + (area.height() - self.height()) // 2,
        )

    def reset_size(self) -> None:
        """恢复默认大小并回到屏幕中央（菜单里用）。"""
        self.resize(self._default_size)
        self._center_on_screen()
        self._save_geometry()

    def _restore_geometry(self) -> None:
        """恢复上次的大小与位置；屏幕变了（换显示器/分辨率）就回到默认，别把窗口丢到屏幕外。"""
        saved_size = self._settings.value("window/size")
        if isinstance(saved_size, QtCore.QSize):
            self.resize(
                fit_width(
                    saved_size.width(),
                    self._aspect,
                    min_width=MIN_WIDTH,
                    max_width=self._max_size.width(),
                    max_height=self._max_height(),
                )
            )
        saved_pos = self._settings.value("window/pos")
        if isinstance(saved_pos, QtCore.QPoint) and self._on_some_screen(
            QtCore.QRect(saved_pos, self.size())
        ):
            self.move(saved_pos)
        else:
            self._center_on_screen()

    def _save_geometry(self) -> None:
        self._settings.setValue("window/size", self.size())
        self._settings.setValue("window/pos", self.pos())
        self._settings.sync()


    def setup_tray(self) -> bool:
        """建托盘图标。返回是否建成（没托盘时 Esc 只能退回「退出」）。"""
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return False
        icon = QtGui.QIcon(
            self._pixmap.scaled(64, 64, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        )
        tray = QtWidgets.QSystemTrayIcon(icon, self)
        tray.setToolTip("Noesia Agent：左键显示/隐藏，右键菜单退出")

        menu = QtWidgets.QMenu()
        menu.setStyleSheet(_MENU_QSS)
        self._toggle_action = menu.addAction("隐藏")
        self._toggle_action.triggered.connect(self.toggle_visible)
        menu.addAction("退出 Noesia Agent", QtWidgets.QApplication.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()

        self._tray = tray
        return True

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()
        self._sync_toggle_text()

    def _sync_toggle_text(self) -> None:
        if self._toggle_action is not None:
            self._toggle_action.setText("隐藏" if self.isVisible() else "显示")

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_visible()

    def build_menu(self) -> QtWidgets.QMenu:
        """右键菜单（单独成方法，便于测试而不真的弹窗）。"""
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_MENU_QSS)
        menu.addAction("聊天", self.open_chat)
        menu.addAction("隐藏" if self.isVisible() else "显示", self.toggle_visible)
        menu.addAction("恢复默认大小", self.reset_size)
        menu.addSeparator()
        menu.addAction("重新认识一次（跑一遍起名）", self.restart_onboarding)
        menu.addAction("退出 Noesia Agent", self.quit_app)
        return menu

    def choose_model(self, option_id: str) -> None:
        """点菜单里的模型：交给引擎的 config.set，结果由引擎广播回来。"""
        if self.client is None:
            say("[模型] 还没连上引擎，切换不了")
            if self.chat_panel is not None:
                self.chat_panel.show_error("还没连上引擎，切换不了模型")
            return
        say(f"[模型] 请求切换到 {option_id}")
        self.client.set_model(option_id)

    def refresh_models(self) -> None:
        """聊天窗菜单里的「刷新模型」：让引擎绕过目录缓存重问一遍上游。

        上游上新/下架是绕着程序发生的，清单不能只靠开窗那一次。刷新途中那条菜单项写
        「刷新中…」；清单一回来（`apply_models`）就自动收回，引擎没回音也别一直卡着。
        """
        if self.client is None:
            say("[模型] 还没连上引擎，刷新不了")
            if self.chat_panel is not None:
                self.chat_panel.show_error("还没连上引擎，刷新不了模型清单")
            return
        if self.chat_panel is not None:
            self.chat_panel.set_models_refreshing(True)
        say("[模型] 请求刷新模型清单")
        self._models_refresh_token += 1
        token = self._models_refresh_token
        self.client.request_models(refresh=True)
        QtCore.QTimer.singleShot(30_000, lambda: self._models_refresh_timeout(token))

    def _models_refresh_timeout(self, token: int) -> None:
        """30 秒还没回音：把「刷新中…」收回，并说一句——不要把菜单钉在刷新状态。"""
        if token != self._models_refresh_token:
            return
        if self.chat_panel is not None:
            self.chat_panel.set_models_refreshing(False)
        say("[模型] 刷新没等到引擎回话（30 秒）")

    def request_new_session(self) -> None:
        """聊天窗里的「新会话」：让引擎重建 agent（重读 SOUL.md / 记忆），换个新会话。

        顺序不能换：先把排队的条数记下来 → 真在跑就掐断这一轮并等它真的停下 →
        再建新会话 → 回包后一次清屏（清屏顺带清空队列）。反过来做会留下一轮
        「看不见、停不了、还在计费」的影子轮次，排队的那几句也会无声消失。
        """
        if self.client is None:
            say("[会话] 还没连上引擎，换不了新会话")
            if self.chat_panel is not None:
                self.chat_panel.show_error("还没连上引擎，换不了新会话")
            return
        self._stop_voice()
        self._stop_process_watch()
        panel = self.chat_panel
        self._new_session_queued = panel.queued_count() if panel is not None else 0
        say("[会话] 请求开新会话")
        self._new_session_pending = True
        self._new_session_sent = False
        if panel is not None and panel.is_busy():
            panel.set_activity("正在结束这一轮…")
            self.client.interrupt()
            self._new_session_timer.start(3000)
            return
        self._send_new_session()

    def _new_session_timeout(self) -> None:
        """等不到结果就收回「新会话」这件事，别把界面锁在等里。

        两种情况：
        - 掐断这一轮 3 秒还没等到 `turn.done`：宁可不开，也别留一条停不了的影子轮次
          （引擎那边这一轮还在跑，这会儿建新会话，那一轮就成了「看不见、停不了、还在计费」的东西，
          比「再点一次」糟得多）。
        - `session.create` 发出去 8 秒没有回包（引擎回了 error，或者干脆没回）：**必须**把
          `_new_session_pending` 放掉——挂着它的话之后每一帧 `turn.done` 都会在
          `on_turn_settled` 里被当成「新会话的那一帧」吞掉，排队的话永远发不出去，
          界面也再不会收尾。
        """
        panel = self.chat_panel
        sent = self._new_session_sent
        self._new_session_pending = False
        self._new_session_queued = 0
        if panel is not None:
            panel.set_activity("")
            if sent:
                self._new_session_sent = False
                panel.show_notice("session", "新会话没建起来，还在原来那一场", level="warn")
                panel.settle_turn()
                say("[会话] 建会话没有回包，这次换会话作废")
            else:
                panel.show_notice("session", "这一轮还没停下来，再点一次「新会话」", level="warn")
                say("[会话] 中断没等到 turn.done，这次换会话作废")

    def _send_new_session(self) -> None:
        """真正去建新会话（这一轮已经停了，或者压根没在跑）。"""
        if self._new_session_sent:
            return
        self._new_session_sent = True
        self._new_session_timer.stop()
        panel = self.chat_panel
        if panel is not None:
            panel.set_activity("")
            panel.set_busy(False)
            self._rewind_pending = None
            panel.clear_status_notice("resubmit")
            panel.cancel_rewind()
        if self.client is not None:
            self.client.create_session("")
            self._new_session_timer.start(8000)

    def open_model_settings(self) -> None:
        """「设置」：打开真正的设置窗口（填密钥 + 选模型），不是把下拉框弹一下。

        不管清单有没有可用项都打开——清单没加载完、一家都没配都能填。两个坑都在这几行里：
        窗口要用模态（`exec`），否则主界面那个无边框置顶窗会把它压在后面，用户看到的是
        「点了没反应」；位置要钉在屏幕中央，不能跟父窗口走（聊天窗贴着头像，可能一半在屏幕外）。
        清单在 exec 之前就发出去：窗口开着的时候 Qt 的事件循环照常在跑，回包能落进这个模态窗口
        （`apply_provider_rows` → `dialog.update_rows`）；等到关窗再发就晚了——用户看到的是一张空表。
        """
        panel = self.chat_panel
        if panel is None:
            return
        from app.model_dialog import ModelDialog

        if self.client is not None:
            self.client.request_provider_rows(refresh=True)
        try:
            rows = self.model_choices()
        except Exception as exc:  # noqa: BLE001
            logger.exception("打不开「设置模型」：%s", exc)
            panel.show_error(f"打不开设置窗口：{exc}")
            return

        try:
            dialog = ModelDialog(
                rows,
                parent=None,
                current=str((self._models or {}).get("current") or ""),
                engine_save=self.save_model_key,
                on_key_save=self.save_provider_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("设置窗口建不起来：%s", exc)
            panel.show_error(f"设置窗口打不开：{exc}")
            return
        self._model_dialog = dialog
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = dialog.frameGeometry()
            geometry.moveCenter(screen.availableGeometry().center())
            dialog.move(geometry.topLeft())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            pages = dialog.saved_pages()
            for note in dialog.save_notes():
                if self.chat_panel is not None:
                    self.chat_panel.show_error(note)
            for notice in dialog.save_notices():
                if self.chat_panel is not None:
                    self.chat_panel.show_info(notice)
            if "text" in pages:
                self._greet_if_needed(force=True)
            if "avatar" in pages:
                self._avatar_block_until = 0.0
                self._avatar_block_hint = "Vidu 积分不够，互动数字人暂时用不了：这句用纯语音念，音色不变。"
                self._avatar_ok()
            if "voice" in pages:
                self._voice_ok()
            if "soul" in pages and self.chat_panel is not None:
                if dialog.soul_changed():
                    self.chat_panel.show_notice("soul", "人设已保存——下一句话就按新的来。", ttl_ms=CONFIRM_TTL_MS)
                elif dialog.saved_tab() == ModelDialog.TAB_SOUL:
                    self.chat_panel.show_notice("soul", "人设没有改动，还是原来那份。", ttl_ms=CONFIRM_TTL_MS)
            if "memory" in pages and self.chat_panel is not None and dialog.memory_changed():
                self.chat_panel.show_notice("memory", "记忆已保存——新建会话才带进上下文。", ttl_ms=CONFIRM_TTL_MS)
            if "workspace" in pages and self.client is not None:
                target = dialog.saved_workspace()
                if target.strip() and not same_workspace(getattr(self.client, "workspace", None), target):
                    self.client.set_workspace(target)
                    if self.chat_panel is not None:
                        self.chat_panel.show_notice(
                            "workdir", f"工作目录已改为 {target}，新建会话生效。", ttl_ms=CONFIRM_TTL_MS
                        )
            self._sync_voice_button()
        self._model_dialog = None

    def _end_avatar_session(self) -> None:
        """闲置挂断：会话不再计费。引擎留着（预加载过就不必再载 dll）。"""
        if self._avatar is None:
            return
        if self._streamer is not None and self._streamer.playing:
            self._avatar_idle.start()
            return
        if self._pending_speech:
            self._avatar_idle.start()
            return
        self._avatar.hangup()

    def _stop_voice(self) -> None:
        """停下所有发声路径：纯语音的声卡、互动的流播放，以及数字人那边正在渲染的嘴。"""
        self.speaker.stop()
        if self._streamer is not None:
            self._streamer.stop()
        self._pending_speech = ""
        if self._avatar is not None:
            self._avatar.interrupt()
        self._show_avatar(False)
        if self.chat_panel is not None:
            self.chat_panel.speech_done()

    def _voice_stopped(self) -> None:
        """被 stop() 打断（换句/新消息/中断）：这条路径不该在"开始念"时触发，出问题看这行日志。"""
        logger.info("[语音] stopped 触发收回画面")
        self._voice_finished()

    def _voice_finished(self) -> None:
        """一句读完：交还待机画面，会话再留 5 秒防备下一句。

        短句（几个字）常常是这句话已经念完、画面还没送来第一帧——这时候切回待机会
        "没有画面"，所以先等它真的出过几帧，最多等 2 秒。
        """
        avatar = self._avatar
        if (self._streamer is not None and self._streamer.playing) or self._pending_speech:
            self._finish_waits = 0
            return
        if avatar is not None and avatar.frames_since_speak < 3 and self._finish_waits < 10:
            self._finish_waits += 1
            QtCore.QTimer.singleShot(200, self._voice_finished)
            return
        self._finish_waits = 0
        if avatar is not None:
            logger.info("[数字人] 云端回传 视频 %d 帧 / 音频 %d 帧（%.1f 秒，含静音段），这一句实际结束时长为 %.1f 秒",
                        avatar.frames_since_speak, avatar.audio_frames,
                        avatar.audio_frames * 0.01, avatar.audio_seconds)
            if not self._reset_sent:
                self._reset_sent = True
                avatar.baseline_reset()
        self._show_avatar(False)
        self._avatar_idle.start()

    def _ensure_avatar(self):
        """拿到互动数字人对象（第一次调用才建）。信号一次性接好。"""
        if self._avatar is not None:
            return self._avatar
        from app.avatar_rtc import AvatarRtc

        avatar = AvatarRtc(self)
        avatar.frame_ready.connect(self._on_avatar_frame)
        avatar.ended.connect(self._on_avatar_ended)
        avatar.failed.connect(self._on_avatar_failed)
        avatar.ready.connect(self._flush_pending_speech)
        avatar.notice.connect(self._on_avatar_notice)
        avatar.ready.connect(self._avatar_ok)
        self._avatar = avatar
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(avatar.shutdown)
        return avatar

    def show_notice_later(self, key: str, text: str) -> None:
        """现在就要说的提示，但聊天窗可能还没开：那就攒着，开窗时补发。

        `preload_avatar` 在 `open_chat` 之前跑，直接用 `chat_panel.show_notice` 会静默丢掉，
        用户要等到第一次说话没声才发现互动档根本没载起来。
        """
        if self.chat_panel is not None:
            self.chat_panel.show_notice(key, text, level="note")
        else:
            self._pending_notices.append((key, text))

    def preload_avatar(self) -> None:
        """语音互动档的预加载：载 dll + 建引擎，约 105ms、0 积分，不建会话。"""
        avatar = self._ensure_avatar()
        avatar.set_target_size(self.size())
        why = avatar.preload()
        if why:
            self.show_notice_later("avatar", f"互动数字人引擎没载起来：{why}")

    def _on_avatar_frame(self, image: QtGui.QImage) -> None:
        """互动画面：接在待机视频同一个显示位上；不是在说话的时候，这些帧直接丢掉。"""
        if not self._interactive_shown:
            return
        self._on_video_frame(image)

    def _show_avatar(self, on: bool) -> None:
        """把显示位交给互动画面（说话时），或者交还待机视频（说完/被打断）。"""
        if on == self._interactive_shown:
            return
        self._interactive_shown = on
        if self.idle_player is not None:
            if on or self._hidden:
                self.idle_player.pause()
            else:
                self.idle_player.resume()

    def _on_avatar_notice(self, text: str) -> None:
        """服务端信令失败（比如复位不被支持）：日志里记一笔，界面上说一句，别静默。

        这一条是**红字**：它是真的报错了（会话已经没法按预期走），和「这一档暂时开不了」
        （黄字，走 `note`）分开放，别混成一种颜色。
        """
        logger.warning("[数字人] %s", text)
        if self.chat_panel is not None:
            self.chat_panel.show_notice("avatar", f"[数字人] {text}", level="warn")

    def _on_avatar_ended(self) -> None:
        """会话挂断：待机视频接回来。"""
        self._show_avatar(False)

    def _avatar_ok(self) -> None:
        """会话建好了：那条"没连上/缺 Token"的提示当场撤掉。"""
        self._warm_idle.stop()
        if self.chat_panel is not None:
            self.chat_panel.clear_status_notice("avatar")
        if not self._pending_speech and not (self._streamer is not None and self._streamer.playing):
            self._avatar_idle.start()

    def _avatar_blocked(self) -> bool:
        """互动档刚因为积分不足失败过：别每句话都再去建一次会话（白等、还刷提示）。"""
        return time.monotonic() < self._avatar_block_until

    def _on_avatar_failed(self, message: str) -> None:
        """互动那边断了：只报一句，不动本地这句朗读——声音还在播，进度条别跟着消失。

        已经切回纯语音/静音了就别再弹（不然界面上留一条已经没意义的旧错误）。
        """
        from app.chat_panel import _voice_mode

        say(f"[数字人] {message}")
        if self.chat_panel is None or _voice_mode() != "interactive":
            return
        low = message.lower()
        config_problem = any(word in message for word in ("Token", "凭证", "未配置", "未填", "401", "403"))
        if config_problem:
            hint = f"互动数字人还没配好：{message.strip().splitlines()[0][:70]}（这句用纯语音念；填好 Token 不用重启）"
            self._avatar_block_hint = hint
            self._avatar_block_until = time.monotonic() + 3600
            pending, self._pending_speech = self._pending_speech, ""
            if pending:
                self.speaker.speak(pending)
        elif "insufficient" in low or "creditsinsufficient" in low:
            hint = "Vidu 积分不够了，互动数字人建不了会话。充值后重试，或把语音切到「纯语音」。"
            self._avatar_block_until = time.monotonic() + 300
            pending, self._pending_speech = self._pending_speech, ""
            if pending:
                self.speaker.speak(pending)
        elif "并发" in message:
            hint = "互动数字人并发已满：上一次会话还没关掉，等十几秒再发一次。"
        else:
            hint = "互动数字人没连上：" + message.strip().splitlines()[0][:80]
        self._warm_idle.stop()
        self.chat_panel.show_notice("avatar", hint, level="note")

    def _flush_pending_speech(self) -> None:
        """会话刚就绪：把等着的那句话接上念。"""
        if not self._pending_speech:
            return
        text, self._pending_speech = self._pending_speech, ""
        if self.chat_panel is not None:
            self.chat_panel.clear_status_notice("avatar")
        self._avatar_idle.stop()
        self._start_interactive(text)

    def _speak_reply(self, text: str) -> None:
        """按输入框那个三档开关决定怎么念；互动档失败就退回纯语音，别这轮没声音。"""
        from app.chat_panel import _voice_mode
        from app.speech_speaker import speakable

        clean = speakable(text)
        if not clean or not any(ch.isalnum() for ch in clean):
            logger.info("[语音] 这句没有能念的内容（只有表情/符号/附件/代码），跳过朗读")
            return

        mode = _voice_mode()
        if mode != "interactive" or self._avatar_blocked():
            logger.info("[语音] 走纯语音链路（档位 %s，互动档冷却=%s）", mode, self._avatar_blocked())
            if mode == "interactive" and self.chat_panel is not None:
                self.chat_panel.show_notice("avatar", self._avatar_block_hint, level="note")
            self.speaker.speak(clean)
            return
        avatar = self._ensure_avatar()
        panel = self.chat_panel
        if avatar.state != "ready":
            if panel is not None:
                panel.speech_preparing()
            if self._pending_speech:
                self.speaker.speak(self._pending_speech)
            self._pending_speech = clean
            self._speech_seq += 1
            seq = self._speech_seq
            avatar.warm()
            self._warm_idle.start()
            QtCore.QTimer.singleShot(15_000, lambda s=seq: self._interactive_timeout(s))
            return
        self._avatar_idle.stop()
        self._start_interactive(clean)

    def _interactive_timeout(self, seq: int = -1) -> None:
        """15 秒还没连上：这句退回纯语音，别让她一直没声音。"""
        if seq >= 0 and seq != self._speech_seq:
            return
        if not self._pending_speech:
            return
        text, self._pending_speech = self._pending_speech, ""
        if self.chat_panel is not None:
            self.chat_panel.clear_status_notice("avatar")
        self._warm_idle.stop()
        if self.chat_panel is not None:
            self.chat_panel.show_notice("avatar", "互动数字人没连上，这句先用纯语音念。", level="note")
        self._show_avatar(False)
        self.speaker.speak(text)

    def _start_interactive(self, text: str) -> None:
        """会话已就绪：启动定速推帧 + 边合成边播。"""
        from app.speech_interactive import StreamSpeaker

        avatar = self._ensure_avatar()
        if not avatar.begin_speak():
            self._avatar_idle.start()
            self.speaker.speak(text)
            return
        avatar.reset_audio_frames()
        self._reset_sent = False
        self._warm_idle.stop()
        self._show_avatar(True)
        panel = self.chat_panel
        if self._streamer is None:
            self._streamer = StreamSpeaker(
                on_chunk=avatar.feed_pcm,
                limit_fn=lambda: avatar.audio_timeline_bytes,
                quiet_fn=lambda: avatar.quiet_seconds,
                idle_fn=lambda: avatar.audio_idle_seconds,
                play_local=False,
                parent=self,
            )
            if panel is not None:
                self._streamer.started.connect(lambda _text: panel.speech_playing())
                self._streamer.started.connect(lambda _text: self._voice_ok())
                self._streamer.progress.connect(panel.speech_progress)
                self._streamer.finished.connect(panel.speech_done)
                self._streamer.stopped.connect(panel.speech_done)
            self._streamer.finished.connect(self._voice_finished)
            self._streamer.stopped.connect(self._voice_stopped)
            self._streamer.failed.connect(self._note_voice_failure)
            self._streamer.failed.connect(
                lambda _msg: panel.speech_done() if panel is not None else None
            )
            self._streamer.failed.connect(lambda _msg: self._avatar_idle.start())
            self._streamer.failed.connect(lambda _msg: self._show_avatar(False))
        from app.speech_speaker import speakable

        clean = speakable(text)
        avatar.say_action(self._last_user_text)
        avatar.say_text(clean)
        logger.info("[语音] 走互动链路：%d 字（预计 %d 秒 ≈ %d 积分）",
                    len(clean), max(1, len(clean) // 6), max(1, len(clean) // 6))
        if not self._streamer.speak(clean):
            logger.warning("[语音] 互动档这句洗完是空的，立刻挂断会话")
            self._show_avatar(False)
            avatar.hangup()

    def _sync_voice_button(self) -> None:
        """设置里可能改过语音，回到聊天窗把输入框旁那个开关同步过来。"""
        if self.chat_panel is None:
            return

        self.chat_panel.refresh_voice_state()

    def model_choices(self) -> list[dict]:
        """设置窗口要的形状：引擎的供应商全表 → 每家一张卡（变量名、配没配、有哪些模型）。"""
        return build_model_choices(self._provider_rows)

    def apply_provider_rows(self, rows: list, current: str = "") -> None:
        """引擎回的供应商全表：存下来给设置页用；设置窗口正开着就原地换内容。

        设置页开的那一刻就开始取清单，回包常常比人手快——不在原窗口换，用户得关掉重开才看得到。
        """
        self._provider_rows = [dict(row) for row in (rows or []) if isinstance(row, dict)]
        dialog = self._model_dialog
        if dialog is not None and hasattr(dialog, "update_rows"):
            try:
                dialog.update_rows(build_model_choices(self._provider_rows), current=current)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[模型] 设置页原地更新失败：%s", exc)

    def on_provider_rows_failed(self, message: str) -> None:
        """引擎没给出供应商清单：把原因写进设置页那张表里（窗口开着），关着就落一句通知。"""
        dialog = self._model_dialog
        if dialog is not None and hasattr(dialog, "show_rows_error"):
            try:
                dialog.show_rows_error(message)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("[模型] 设置页错误提示失败：%s", exc)
        self.show_notice("model-options", f"拿不到供应商清单：{message}", level="warn")

    def save_provider_key(self, slug: str, api_key: str, done=None) -> None:
        """密钥设置里这一家按了「确定」：只写这一家的 Key，引擎落盘后自己把清单重取一遍送回来。

        不走 `save_model_key`：那条是「保存并启用」整页提交的链路，一次只能有一份在飞；
        这里一家一份，`done(ok, lines)` 在引擎回执到达时（`on_save_result`）才被调。
        """
        if self.client is None:
            raise RuntimeError("还没连上引擎")
        if done is not None:
            if not hasattr(self, "_key_wait"):
                self._key_wait = {}
            self._key_wait[str(slug)] = done
        self.client.save_key(slug, api_key)

    def save_model_key(self, selection: dict, done=None) -> None:
        """对话框点了「保存并启用」：交给引擎逐家写 `.env`，再切「启用」那家的模型（立即生效）。

        一页能配好几家：`keys` 里有几家填了 Key 就写几条；只有「启用」那家额外切模型。
        每条请求都有回执：全部回来之后才调 `done(ok, lines)`，设置窗口拿它决定关不关窗。
        ——「发出去了」和「真生效了」是两件事，不能拿前者冒充后者。
        """
        provider = str(selection.get("provider") or "")
        model = str(selection.get("model") or "")
        option_id = f"{provider}:{model}" if provider and model else ""
        if self.client is None:
            say("[模型] 还没连上引擎，密钥没保存")
            if self.chat_panel is not None:
                self.chat_panel.show_error("还没连上引擎，密钥没保存")
            if done is not None:
                done(False, ["还没连上引擎：这次什么都没保存。"])
            return
        keys = {
            str(slug): str(text).strip()
            for slug, text in (selection.get("keys") or {}).items()
            if str(text).strip()
        }
        if not keys and str(selection.get("key") or "").strip():
            keys[provider] = str(selection["key"]).strip()
        typed = keys.get(provider, "")
        if not keys and not option_id:
            say("[模型] 没填密钥也没选模型，什么都没改")
            if done is not None:
                done(True, [])
            return
        self._save_round = int(getattr(self, "_save_round", 0)) + 1
        self._save_ctx = {
            "done": done,
            "round": self._save_round,
            "waiting": 0,
            "lines": [],
            "all_ok": True,
            "provider": provider,
            "slugs": set(keys),
            "switch_pending": bool(option_id),
            "stamp": time.monotonic(),
        }
        ctx = self._save_ctx
        for slug, text in keys.items():
            if slug == provider:
                continue
            ctx["waiting"] += 1
            say(f"[模型] 请求保存 {slug} 的密钥")
            self.client.save_key(slug, text, token=str(ctx["round"]))
        if typed:
            ctx["waiting"] += 1
            say(f"[模型] 请求保存 {provider} 的密钥并启用 {option_id}")
            self.client.save_key(provider, typed, option_id, token=str(ctx["round"]))
        elif option_id:
            say(f"[模型] 请求启用 {option_id}（密钥不动）")
            self.client.set_model(option_id, token=str(ctx["round"]))
        if ctx["waiting"] == 0 and not ctx["switch_pending"]:
            self._finish_save(True)

    def on_save_result(self, tag: str, payload: dict) -> None:
        """引擎回执：设置窗口点了「保存并启用」之后在等这两条（写 Key / 切模型）。"""
        ctx = getattr(self, "_save_ctx", None)
        data = dict(payload or {})
        ok = bool(data.get("ok"))
        if tag.startswith("model.save_key"):
            slug = tag.split(":", 1)[1] if ":" in tag else str(data.get("slug") or "")
            waiting = getattr(self, "_key_wait", None)
            callback = waiting.pop(slug, None) if isinstance(waiting, dict) else None
            if callback is not None:
                lines = [] if ok else [f"{slug}：没存成——{data.get('message') or '引擎没说原因'}"]
                callback(ok, lines)
        if not ctx:
            return
        if time.monotonic() - float(ctx.get("stamp") or 0.0) > 9.0:
            return
        if tag.startswith("model.save_key"):
            slug = tag.split(":", 1)[1] if ":" in tag else str(data.get("slug") or "")
            if slug not in ctx.get("slugs", ()):
                return
            ctx["slugs"].discard(slug)
            ctx["waiting"] = max(0, int(ctx["waiting"]) - 1)
            if ok:
                ctx["lines"].append(f"{slug}：已写进 .env")
            else:
                ctx["all_ok"] = False
                ctx["lines"].append(f"{slug}：没存成——{data.get('message') or '引擎没说原因'}")
                if slug == ctx["provider"]:
                    ctx["switch_pending"] = False
        elif tag.startswith("config.set:model"):
            token = tag.rsplit(":", 1)[-1] if tag.count(":") > 1 else ""
            if token and token != str(ctx.get("round") or ""):
                return
            ctx["switch_pending"] = False
            if not ok:
                ctx["all_ok"] = False
                ctx["lines"].append(f"模型没切换（{data.get('message') or '引擎没说原因'}）：引擎还在跑原来的")
        self._maybe_finish_save()

    def _maybe_finish_save(self) -> None:
        ctx = getattr(self, "_save_ctx", None)
        if not ctx or ctx["waiting"] or ctx["switch_pending"]:
            return
        self._finish_save(bool(ctx["all_ok"]))

    def _finish_save(self, ok: bool, extra: list[str] | None = None) -> None:
        """这一轮保存结束：把逐家结果交给设置窗口，让它决定关窗还是把原因贴在脸上。"""
        ctx = self._save_ctx
        self._save_ctx = None
        lines = list(ctx.get("lines") or []) + list(extra or [])
        for line in lines:
            say(f"[模型] {line}")
        done = ctx.get("done")
        if done is not None:
            done(bool(ok), lines)

    def apply_models(self, items: list[dict], current: str, *, switching: bool = False) -> None:
        """引擎推回来的模型清单：刷新菜单缓存、聊天窗标题和托盘提示。

        清单原样是引擎 `model.options` 的字段，界面不猜、不补、不改名。
        """
        rows = [dict(item) for item in (items or []) if isinstance(item, dict)]
        self._models = {"current": current, "items": rows, "switching": switching}
        chosen = next((item for item in rows if str(item.get("id")) == current), None)
        label = str((chosen or {}).get("label") or current)
        self._models_refresh_token += 1
        if self.chat_panel is not None:
            self.chat_panel.set_models(rows, current, switching=switching)
            if current and (chosen is None or chosen.get("available")):
                self.chat_panel.clear_status_notice("setup")
        if self._tray is not None:
            self._tray.setToolTip(f"Noesia Agent · {label}")
        if not switching:
            self.request_reasoning()
        finish = getattr(self, "finish_boot", None)
        if callable(finish):
            finish()
    def quit_app(self) -> None:
        """退出：先打一行日志再走 Qt 的优雅退出（引擎由 aboutToQuit 关掉）。"""
        say("[退出] 正在关闭窗口、IPC 与引擎…")
        QtWidgets.QApplication.quit()

    def _show_menu(self, pos: QtCore.QPoint) -> None:
        menu = self.build_menu()
        try:
            menu.exec(self.mapToGlobal(pos))
        finally:
            menu.deleteLater()


    def on_review_summary(self, text: str) -> None:
        """引擎说「后台回顾改完了（记忆/技能）」：留一条灰字在对话里。

        面板还没建（从没开过聊天窗）就先攒着，打开时补进去——不能因为没开窗就丢掉。
        """
        text = (text or "").strip()
        if not text:
            return
        if self.chat_panel is not None:
            self.chat_panel.append_review_summary(text)
        else:
            self._pending_reviews.append(text)
            del self._pending_reviews[:-20]

    def _flush_pending_reviews(self) -> None:
        if self.chat_panel is None or not self._pending_reviews:
            return
        for text in self._pending_reviews:
            self.chat_panel.append_review_summary(text)
        self._pending_reviews.clear()

    def _flush_pending_notices(self) -> None:
        """开窗前攒下的提示补发（同一 key 只留最后一条：旧的那条已经被新情况取代）。"""
        if self.chat_panel is None or not self._pending_notices:
            return
        pending, self._pending_notices = self._pending_notices, []
        for key, text in pending:
            if key == "boot":
                self.chat_panel.show_error(text)
                continue
            self.chat_panel.show_notice(key, text, level="note")

    def open_chat(self) -> None:
        """打开（或聚焦）聊天窗；第一次调用才建界面。"""
        if self.chat_panel is None:
            self.chat_panel = ChatPanel(None)
            self.chat_panel.set_media_roots(media_roots())
            self.chat_panel.submitted.connect(self._send_user_text)
            self.chat_panel.model_chosen.connect(self.choose_model)
            self.chat_panel.models_refresh_requested.connect(self.refresh_models)
            self.chat_panel.new_session_requested.connect(self.request_new_session)
            self.chat_panel.resubmit_requested.connect(self._resubmit_turn)
            self.chat_panel.model_settings_requested.connect(self.open_model_settings)
            self.chat_panel.approval_requested.connect(self.request_approval_mode)
            self.chat_panel.stop_requested.connect(self.stop_turn)
            self.chat_panel.attachment_picked.connect(self._attach_picked)
            self.chat_panel.clarify_answered.connect(self._answer_clarify)
            self.chat_panel.clarify_skipped.connect(self._skip_clarify)
            self.chat_panel.process_kill_requested.connect(self._kill_process)
            self.chat_panel.attachment_failed.connect(self._attachment_problem)
            self.chat_panel.speech_stop_requested.connect(self._stop_voice)
            self.chat_panel.voice_mode_changed.connect(lambda _mode: self._voice_ok())
            self.speaker.preparing.connect(self.chat_panel.speech_preparing)
            self.speaker.started.connect(lambda _text: self.chat_panel.speech_playing())
            self.speaker.progress.connect(self.chat_panel.speech_progress)
            self.speaker.finished.connect(self.chat_panel.speech_done)
            self.speaker.stopped.connect(self.chat_panel.speech_done)
            self.speaker.failed.connect(lambda _msg: self.chat_panel.speech_done())
            self.chat_panel.reasoning_requested.connect(self.request_reasoning)
            self.chat_panel.voice_transcribe_requested.connect(self._transcribe_voice)
            self.chat_panel.voice_record_started.connect(self._stop_voice)
            self.chat_panel.voice_input_opened.connect(self._ensure_voice_model)
            if self._models:
                self.chat_panel.set_models(
                    list(self._models.get("items") or []), str(self._models.get("current") or "")
                )
        self._flush_pending_reviews()
        self._flush_pending_notices()
        self.request_history()
        self.chat_panel.show_near(self.frameGeometry())
        self._greet_if_needed()
        self.request_approval_mode()
        self.request_reasoning()

    def _ensure_voice_model(self) -> None:
        """进语音模式先问一句模型在不在。引擎顺手在后台预热，缺文件就当场开始下。"""
        wire = getattr(self, "client", None)
        if wire is not None:
            wire.check_voice_model()

    def _on_voice_model(self, info: dict) -> None:
        """识别模型的状态：齐了就报一声，缺了就下，下不成把原因摆出来。"""
        panel = self.chat_panel
        if panel is None:
            return
        if info.get("ready"):
            self._voice_model_tried = True
            if self._voice_model_downloading:
                self._voice_model_downloading = False
                panel.show_notice("voice-model", "语音识别准备好了，按住麦克风说话就行", ttl_ms=5000)
            return
        if info.get("failed"):
            self._voice_model_downloading = False
            self._voice_model_tried = True
            panel.show_notice("voice-model", f"识别模型没下成：{info['failed']}", ttl_ms=8000, level="error")
            return
        if self._voice_model_downloading:
            self._voice_model_downloading = False
            self._voice_model_tried = True
            panel.show_notice(
                "voice-model",
                "识别模型下完了但不完整，语音输入暂时用不了（重启程序会再试一次）",
                ttl_ms=8000,
                level="error",
            )
            return
        if self._voice_model_tried:
            panel.show_notice(
                "voice-model",
                "识别模型还没装好，语音输入暂时用不了（重启程序会再试一次）",
                ttl_ms=6000,
                level="error",
            )
            return
        self._voice_model_downloading = True
        total = int(info.get("total") or 0)
        size = f"{total / 1048576:.0f} MB" if total else "两百多兆"
        panel.show_notice(
            "voice-model",
            f"第一次用要先下语音识别模型（{size}），下好会告诉你",
            ttl_ms=0,
        )
        wire = getattr(self, "client", None)
        if wire is not None:
            wire.download_voice_model()

    def _transcribe_voice(self, path: str) -> None:
        """录好的一段交给引擎识别。引擎还没起好就当场说清，别让球卡在「识别中」。"""
        wire = getattr(self, "client", None)
        panel = self.chat_panel
        if wire is None or not wire.is_running():
            if panel is not None:
                panel.voice_transcribe_failed("识别引擎还没起好，稍等一下再试", path)
            return
        wire.transcribe_audio(path)

    def stop_turn(self) -> None:
        """叫住这一轮：真停由引擎的 interrupt 做，界面只发一条 stop。"""
        self._turn_stopped = True
        self._stop_voice()
        if self.chat_panel is not None:
            self.chat_panel.stop_streaming()
            self.chat_panel.set_busy(False)
        self._refresh_processes()
        if self.client is None:
            return
        self.client.interrupt()

    def _start_process_watch(self) -> None:
        """会话一连上就一直盯着后台进程，每 5 秒问一次（桌面端也是这样：窗口活着就问）。

        只在一轮里问是不够的——`terminal(background=true)` 的进程往往在你没说话的时候还在跑，
        那一轮一结束就看不见了，跑完了也没人知道。
        """
        if self.client is None:
            return
        if self._proc_timer is None:
            self._proc_timer = QtCore.QTimer(self)
            self._proc_timer.setInterval(5000)
            self._proc_timer.timeout.connect(self._refresh_processes)
        self._refresh_processes()
        self._proc_timer.start()

    def _stop_process_watch(self) -> None:
        """换会话 / 关窗口：不再问这一场了（行由 `clear_all()` 一起撤掉）。"""
        if self._proc_timer is not None:
            self._proc_timer.stop()
        if self.chat_panel is not None:
            self.chat_panel.set_processes([])

    def _refresh_processes(self) -> None:
        if self.client is not None:
            self.client.list_processes()

    def request_approval_mode(self, mode: str = "") -> None:
        """查（不传 mode）或改审批档：manual / smart / off。

        改完的回包带引擎真正落下的值（`{key, value}`），界面照它勾；这里不自己补一次查询。
        """
        if self.client is None:
            return
        if mode:
            self.client.set_approval_mode(mode)
        else:
            self.client.config_get("approvals.mode")

    def request_reasoning(self, effort: str = "") -> None:
        """查（不传 effort）或改推理强度。

        档位清单是固定的（照桌面端 `REASONING_EFFORTS`），这里只要有「现在是哪一档」：
        查用 `config.get reasoning`，改的回包自己带 value，回来照它勾。
        """
        if self.client is None:
            return
        if effort:
            self._reasoning_pending = False
            self._reasoning_fresh = False
            self.client.set_reasoning(effort)
        else:
            self.client.config_get("reasoning")

    def boot_step(self, text: str) -> None:
        """启动动画上的那句话：说出来就好，没动画（比如已经在跑）也不报错。"""
        splash = getattr(self, "boot_splash", None)
        if splash is not None:
            splash.set_step(text)
        say(f"[启动] {text}")

    def boot_failed(self, text: str) -> None:
        """引擎没起来 / 起得太慢：写清原因，然后照常把窗口放出来（别把人卡在动画里）。

        动画只闪一下，用户根本读不到那句话——所以原因同时写进聊天窗（红色一行），
        开聊天就能看见；日志里也留下 WARNING，之后排查有据可查。
        """
        if getattr(self, "_boot_done", True):
            return
        if getattr(self, "_boot_failure", ""):
            return
        self._boot_failure = text
        splash = getattr(self, "boot_splash", None)
        if splash is not None:
            splash.mark_failed(text)
        say(f"[启动] {text}")
        logger.warning("启动失败：%s", text)
        panel = getattr(self, "chat_panel", None)
        if panel is not None:
            panel.show_error(text)
        else:
            self._pending_notices.append(("boot", text))
        self.finish_boot()

    def finish_boot(self) -> None:
        """加载完成：收起动画、露出人物窗口——之后点开聊天窗就是即时的。

        只做一次；`boot_splash` 为空的窗口（测试替身、无动画场景）直接显形。
        """
        if getattr(self, "_boot_done", True):
            return
        self._boot_done = True
        splash = getattr(self, "boot_splash", None)
        self.boot_splash = None
        if splash is None:
            self.show()
            return

        def reveal() -> None:
            splash.close()
            splash.deleteLater()
            self.show()
            if getattr(self, "_boot_failure", ""):
                say("[启动] 人物窗口出现（引擎没起来，原因见上）")
            else:
                say("[启动] 已就绪，人物窗口出现")

        splash.finish(reveal)

    def request_history(self) -> None:
        """把上一段对话要回来铺进聊天窗——用户什么都不用输入。

        开窗那一下引擎常常还没连上（`client.send()` 没连上时返回 False），所以不能只问一次：
        开窗、连上都补一遍，问过就记下，别重复。
        """
        if self._history_asked or self.client is None or self.chat_panel is None:
            return
        self._history_asked = True
        self.client.request_most_recent()

    def restart_onboarding(self) -> None:
        """用户主动「重新认识一次」：先让引擎把 run 打开，再按正常路径开场。

        引擎只在全新安装时自动跑引导（`onboarding.state` 的 `run`：数据根里有密钥、会话或档案
        就算「用过」）。想再跑一次就走 `onboarding.set_run {run: true}`，
        不删任何文件。
        """
        if self.client is None:
            return
        self.open_chat()
        self._greeted = False
        self.client.restart_onboarding()

    def _greet_if_needed(self, force: bool = False) -> None:
        """问一次「该不该开场」（引擎按 `run`/`eligible` 自己判断，做过就什么都不做）。

        引擎现在是后台拉起的，开窗那一刻可能还没连上——发送失败就保持 `_greeted=False`，
        等 `connected` 信号再来一次，否则首次见面和「还没配置模型」那条黄提示都不会出现。

        `force=True` 用在「刚刚才有可用模型」的时刻（保存密钥、模型清单里出现当前模型）：
        先开窗、后配密钥的人，第一遍问过去时引擎还没有 provider（`eligible=false`），
        不补问一次，引导就永远不开始——见过的现象就是「我跑完了，没弹出来」。
        """
        if self.client is None or self.chat_panel is None:
            return
        if self._greeted and not force:
            return
        self._greeted = True
        self.client.greet()

    def _ask_approval(self, ask) -> None:  # noqa: ANN001 (ApprovalPayload)
        """她停下来等人点头：弹一张卡片，选完把结果交回引擎。

        关窗、按 Esc 都算拒绝——沉默不是同意。
        """
        from app.approval_dialog import ApprovalDialog

        def answer(choice: str) -> None:
            if dialog in self._approval_dialogs:
                self._approval_dialogs.remove(dialog)
            self._approval_dialog = self._approval_dialogs[-1] if self._approval_dialogs else None
            if self.client is not None:
                reply_id = getattr(ask, "reply_id", None) or getattr(ask, "request_id", "")
                self.client.answer_approval(reply_id, choice)
            if self.chat_panel is not None:
                label = {
                    "once": "允许一次",
                    "session": "本次会话都允许",
                    "always": "以后都允许",
                }.get(choice, "拒绝")
                detail = "\n".join(
                    part for part in (getattr(ask, "description", ""), getattr(ask, "command", "")) if part
                )
                self.chat_panel.show_tool(
                    "approval", f"你选择：{label}", ok=choice != "deny", body=detail
                )

        dialog = ApprovalDialog(ask, answer)
        dialog.request_id = str(getattr(ask, "request_id", "") or "")
        self._approval_dialogs.append(dialog)
        self._approval_dialog = dialog
        dialog.show_near(self.geometry())

    def _send_user_text(self, message: str, source: str = "typed") -> None:
        self._last_user_text = (message or "").strip()
        panel = self.chat_panel
        if panel is not None and getattr(panel, "pending_clarify", None) and panel.pending_clarify():
            if panel.answer_clarify_text(message):
                return
        if self.client is None or not self.client.is_running():
            if self.chat_panel is not None:
                self.chat_panel.show_error("引擎没起来，这句话没发出去")
            return
        if self.chat_panel is None or not self.chat_panel.is_busy():
            self._reply_text = ""
        self._turn_stopped = False
        self._stop_voice()
        if self.chat_panel is not None:
            self.chat_panel.clear_status_notice("turn")
            self.chat_panel.set_busy(True)
        items, self._sending_attachments = self._sending_attachments, []
        logger.info("[附件] 这一轮带 %d 个附件", len(items))
        self._start_process_watch()
        if source == "typed":
            try:
                from app import voice_vocab

                voice_vocab.remember(message)
            except Exception:  # noqa: BLE001
                logger.debug("[词表] 这条语料没记上", exc_info=True)
        self.client.submit_with_attachments(message, items, queued=source != "voice")
        self._maybe_learn_vocab()

    def _maybe_learn_vocab(self) -> None:
        """攒够新语料就在后台重算一次识别词表。

        不够量时只读一个文件，几乎不花时间；真要算的时候要扫全部语料，
        所以一律放后台线程，不占界面。起线程失败也只是词表不更新。
        """
        if self._vocab_learning:
            return
        try:
            self._vocab_learning = True
            threading.Thread(target=self._learn_vocab_worker, daemon=True).start()
        except Exception:  # noqa: BLE001
            self._vocab_learning = False
            logger.debug("[词表] 起不了后台线程", exc_info=True)

    def _learn_vocab_worker(self) -> None:
        try:
            from app import voice_vocab

            voice_vocab.learn()
        except Exception:  # noqa: BLE001
            logger.debug("[词表] 这次没算成", exc_info=True)
        finally:
            self._vocab_learning = False

    def _attach_picked(self, items: list) -> None:
        """点发送那一刻界面把附件交过来：先攒着，等 `_send_user_text` 把整轮交给引擎。"""
        self._sending_attachments = list(items)

    def _attachment_problem(self, name: str, reason: str) -> None:
        """附件没收下：把待发送条上那一条撤掉，再把原因说清楚。"""
        if self.chat_panel is None:
            return
        self.chat_panel.drop_attachment_by_name(name)
        self.chat_panel.show_error(f"{name} 没发出去：{reason}")

    def _note_voice_failure(self, msg: str) -> None:
        """语音出问题说给用户听：日志一份，输入框上方一条状态（不是聊天记录里的一行）。

        为什么不能写进聊天记录：那行字一旦落进气泡就永远留在那儿——把 Key 填好、换成 Edge，
        它还在，看着像"没生效"。状态条是同一件事的"当前状态"：念出声、换档位或改完设置就撤掉。

        颜色分两档：缺 Key 是"这一档暂时发不出声"（黄，语音不是必需的，没它也能纯文字聊）；
        其它（网络、Key 格式、服务端报错）才是真的出错（红）。
        """
        say(f"[语音] {msg}")
        panel = self.chat_panel
        if panel is not None and msg != self._voice_failure_shown:
            self._voice_failure_shown = msg
            level = "note" if "缺少 API Key" in msg else "warn"
            panel.show_notice("speech", f"[语音] {msg}", level=level)

    def _voice_ok(self) -> None:
        """语音又能用了（念出来了，或者刚改完设置）：撤掉那条提示，并允许下次再提醒。"""
        self._voice_failure_shown = ""
        panel = self.chat_panel
        if panel is not None:
            panel.clear_status_notice("speech")

    def on_turn_started(self) -> None:
        """引擎开了一轮（`message.start`）：不管谁发起的，按钮都该是「停止」。

        引擎自己续跑的那一轮以前完全没有通知：界面一直写「发送」，只能眼睁睁看着它跑。
        """
        if self._turn_stopped:
            return
        panel = self.chat_panel
        if panel is not None:
            self._turn_stopped = False
            panel.open_turn_gate()
            if not panel.is_busy():
                panel.set_busy(True)

    def on_status_text(self, kind: str, text: str) -> None:
        """引擎的状态句子（`status.update`）：放到输入框上方那行进度的位置。"""
        panel = self.chat_panel
        if panel is None or not text:
            return
        if kind in {"ready", "idle"} and not panel.is_busy():
            panel.set_activity("")
            return
        panel.set_activity(text)

    def on_delta(self, text: str) -> None:
        """引擎的 message.delta：直接进气泡，界面不解释内容。"""
        if self._turn_stopped:
            return
        if text:
            self._reply_text += text
        panel = self.chat_panel
        if self._rewind_pending is not None and panel is not None:
            self._apply_pending_rewind(panel)
        if panel is not None and text:
            if not panel.is_busy():
                panel.set_busy(True)
            panel.clear_status_notice("turn")
            panel.append_chunk(text)

    def on_complete(self, payload: dict) -> None:
        """message.complete：正文早就按增量进了气泡，这里只收尾。"""
        panel = self.chat_panel
        if panel is not None:
            panel.bind_turn_receipt(payload.get("persisted_turn") or {})
            if str(payload.get("status") or "") == "error":
                if payload.get("partial"):
                    failure = str(payload.get("error") or payload.get("text") or "").strip()
                else:
                    failure = str(payload.get("text") or payload.get("error") or "").strip()
                if failure:
                    panel.show_notice("turn", failure, level="error")
            else:
                panel.clear_status_notice("turn")
            panel.end_assistant()
            panel.set_busy(False)
        self._refresh_processes()
        reply, self._reply_text = self._reply_text, ""
        if reply and not self._turn_stopped:
            self._speak_reply(reply)
        self._turn_stopped = False

    def on_turn_settled(self, status: str) -> None:
        """`turn.done`：这一轮真的完了（被中断的轮次走不到 message.complete，只有这一帧）。

        界面在收到它之后才可以重新收字——按停后飘回来的残片都排在这一帧前面，
        放闸早了就会被写进下一轮的气泡。顺带把按钮收回「发送」：引擎自己中断的轮次
        （不是用户按的停）以前没人叫醒按钮，会一直停在「停止」上。
        """
        panel = self.chat_panel
        if panel is None:
            return
        if self._new_session_pending:
            if not self._new_session_sent:
                self._send_new_session()
            return
        if self._rewind_pending is not None:
            self._apply_pending_rewind(panel)
        panel.set_busy(False)
        panel.settle_turn()
        panel.expire_clarify()
        self._turn_stopped = False
        self._reply_text = ""

    def on_tool_event(self, event: dict) -> None:
        """tool.start / tool.complete：标题、副标题、计数、正文全部由模型层算（`app/tool_view`）。

        界面不再自己拼摘要：`build_tool_view()` 负责算，
        失败与否、显示哪几段、哪个字段当副标题，都由它决定，这里只把结果交给聊天窗。
        """
        panel = self.chat_panel
        if panel is None:
            return
        self._apply_pending_rewind(panel)
        kind = str(event.get("kind") or "")
        if self._turn_stopped and kind in {"tool.generating", "tool.progress"}:
            return
        if kind == "tool.generating":
            name = str(event.get("name") or "").strip()
            if name and panel is not None and panel.tool_progress_enabled():
                panel.set_activity(f"正在调用 {name}…")
            return
        if kind == "tool.progress":
            name = str(event.get("name") or "").strip()
            preview = str(event.get("message") or event.get("preview") or "").strip()
            percent = event.get("percent")
            if preview and panel is not None and panel.tool_progress_enabled():
                text = f"{name or '工具'} · {preview}"
                if isinstance(percent, (int, float)):
                    text += f" · {int(percent)}%"
                panel.set_activity(text)
            return
        if kind not in {"tool.start", "tool.complete"}:
            return
        if panel is not None:
            panel.set_activity("")
        if str(event.get("name") or "") == "clarify":
            return
        if str(event.get("name") or "") == "setup_beat" or str(event.get("toolset") or "") == "onboarding":
            return
        from app.tool_view.adapter import view_for
        from app.tool_view.delegate import DELEGATE_TOOL
        from app.tool_view.model import tool_copy_payload

        view, part = view_for(event)
        panel.show_tool_view(
            view,
            tool_id=part.tool_call_id,
            tool_name=part.tool_name,
            pending=str(event.get("kind")) == "tool.start",
            copy_payload=tool_copy_payload(part, view),
            delegate_event=event if part.tool_name == DELEGATE_TOOL else None,
        )

    def on_subagent_event(self, event: dict) -> None:
        """`subagent.*`：子代理跑起来后调了什么工具、跑到哪一步，刷到那张子代理卡片上。"""
        panel = self.chat_panel
        if panel is None:
            return
        panel.update_delegate(event)

    def on_display_config(self, display: dict) -> None:
        """`config.yaml` 的 display 段：画不画思考段、工具行由引擎配置说了算。

        `show_reasoning` 默认开（引擎默认值），`tool_progress` 默认 all；
        字符串 "false"/"off"/0 这类写法也当关（桌面端同样认）。
        """
        panel = self.chat_panel
        if panel is None:
            return
        panel.set_show_reasoning(_flag(display.get("show_reasoning"), default=True))
        panel.set_tool_progress(str(display.get("tool_progress") or "all"))

    def on_status(self, state: str) -> None:
        if self.chat_panel is not None and state:
            self.chat_panel.set_state(state)

    def on_notice(self, message: str) -> None:
        """引擎发来的普通消息（保存成功、切换中…）：灰色一行，不是错误。"""
        if not message:
            return
        if self.chat_panel is not None:
            self.chat_panel.show_info(message)
        say(f"[引擎] {message}")

    def on_notice_show(self, payload: dict) -> None:
        """引擎的提示（`notification.show`）：输入框上方一行，到点自己消失，也能被 clear 撤掉。

        以前这类帧全被丢掉——压缩提醒、auto-prune、额度提示都只在日志里留下过。
        """
        text = str(payload.get("text") or "").strip()
        if not text or self.chat_panel is None:
            return
        key = str(payload.get("key") or text)
        ttl = payload.get("ttl_ms")
        try:
            ttl_ms = int(ttl) if ttl else 0
        except (TypeError, ValueError):
            ttl_ms = 0
        self.chat_panel.show_notice(key, text, ttl_ms=ttl_ms, level=str(payload.get("level") or ""))
        logger.info("[提示] %s", text)

    def on_clarify_request(self, payload: dict) -> None:
        """她在问「你要选哪个」：卡显示在对话里，选项点了就回引擎。"""
        if self.chat_panel is None:
            return
        self.chat_panel.show_clarify(payload)

    def on_request_cancelled(self, kind: str, request_id: str) -> None:
        """引擎收回一个提问（`request.cancel`）：那张卡当场作废。

        不作废的话 `pending_clarify()` 一直为真，用户下一条消息会被当成旧卡的答案吃掉。
        `secret`/`sudo` 是模态输入框，引擎撤回时没法从外面关，答了也只会落到一个失效的 reply_id 上。
        """
        if self.chat_panel is None or kind != "clarify":
            return
        self.chat_panel.expire_clarify(request_id)

    def on_approvals_cancelled(self, request_ids: list) -> None:
        """引擎自己撤了审批卡（超时 / 那一轮作废）：卡跟着关，别让人对着废卡点头。

        同时可能开着好几张（并行工具各自要授权），所以一张一张按 request_id 认：
        载荷里的 request_ids 会漏掉「没有 request_id」的那几条，列表为空时照关全部；
        只有明明列了 id、又不是这张卡的，才留着手上的卡。
        """
        wanted = {str(item) for item in (request_ids or [])}
        closed = 0
        for dialog in list(self._approval_dialogs):
            mine = str(getattr(dialog, "request_id", "") or "")
            if wanted and mine and mine not in wanted:
                continue
            if dialog in self._approval_dialogs:
                self._approval_dialogs.remove(dialog)
            dialog.dismiss()
            closed += 1
        if not closed:
            return
        self._approval_dialog = self._approval_dialogs[-1] if self._approval_dialogs else None
        if self.chat_panel is not None:
            self.chat_panel.show_tool("approval", "这张审批卡已失效（引擎撤回了这一轮）", ok=False)

    def on_secret_request(self, payload: dict) -> None:
        """引擎要一个密钥 / sudo 密码：问一句，答了按 `{"value": …}` 回；取消 = 空串（跳过）。"""
        if self.client is None:
            return
        method = str(payload.get("method") or "secret")
        reply_id = payload.get("reply_id")
        prompt = str(payload.get("prompt") or "")
        title = "需要 sudo 密码" if method == "sudo" else "需要一个密钥"
        if method == "sudo":
            prompt = prompt or "这个命令要管理员权限，请输入 sudo 密码。"
        value, ok = QtWidgets.QInputDialog.getText(
            self,
            title,
            prompt + "\n（取消 = 跳过，她就不会拿到这个值）",
            QtWidgets.QLineEdit.Password,
        )
        self.client.answer_secret(reply_id, str(value) if ok else "")

    def on_reaction(self, kind: str) -> None:
        """你说 ily / ♥ / 谢谢 的时候引擎会发 `reaction`：头像上飘几颗心，别的什么都不做。

        主窗口本身就是那个头像（`FloatingAvatar`），没有单独的 `avatar` 成员——以前这里去
        `self.avatar` 找一个不存在的东西，所以这帧到了也什么都没发生。
        """
        self.flash_hearts()

    def _answer_clarify(self, request_id: str, question_id: str, answer: str) -> None:
        if self.client is not None:
            self.client.answer_clarify(request_id, question_id, answer)

    def _skip_clarify(self, reply_id: str) -> None:
        if self.client is not None:
            self.client.skip_clarify(int(reply_id) if str(reply_id).isdigit() else reply_id)

    def _kill_process(self, process_id: str) -> None:
        if self.client is not None:
            self.client.kill_process(process_id)

    def on_setup_needed(self, message: str) -> None:
        """还没配模型/供应商：黄色临时提示，配好（`apply_models` 看到模型可用）就按 key 撤掉。

        不写进聊天记录——否则配完模型它还在，用户会以为没生效。黄字不是红字：还没配好
        不等于装坏了，红字留给真正的失败。
        """
        if not message:
            return
        if self.chat_panel is not None:
            self.chat_panel.show_notice("setup", message, level="note")
        say(f"[引擎] {message}")

    def on_engine_error(self, message: str) -> None:
        """某条请求被引擎拒了：红色错误行（这是真失败，跟通知分开）。

        **不能**顺手把这一轮判死：引擎对任何请求都可能回 error（config.get、每 5 秒一次
        process.list、session.interrupt…），一轮回答还在流式输出时收到其中一条，
        按钮就会从「停止」跳回「发送」，之后再也叫不停那一轮。
        """
        if not message:
            return
        if self._new_session_sent:
            self._new_session_pending = False
            self._new_session_sent = False
            self._new_session_timer.stop()
        if self.chat_panel is not None:
            self.chat_panel.show_error(message)
            if not self.chat_panel.is_busy():
                self._reply_text = ""
        if self._rewind_pending is not None:
            self._rewind_pending = None
            if self.chat_panel is not None:
                self.chat_panel.clear_status_notice("resubmit")
                self.chat_panel.cancel_rewind()
        say(f"[引擎][错误] {message}")

    def _apply_pending_rewind(self, panel) -> None:
        """引擎真接下这次截断重发（开始吐东西了）：窗口这才跟着回到那一刻。"""
        if self._rewind_pending is None:
            return
        msg, body = self._rewind_pending
        self._rewind_pending = None
        panel.clear_status_notice("resubmit")
        panel.apply_rewind(msg, body)

    def on_reasoning_delta(self, text: str) -> None:
        if self.chat_panel is not None and text:
            self._apply_pending_rewind(self.chat_panel)
            self.chat_panel.show_reasoning(text)

    def on_reasoning_block(self, text: str) -> None:
        """不流式的供应商一次给全的思考（引擎 `reasoning.available`）。"""
        if self.chat_panel is not None and text:
            self._apply_pending_rewind(self.chat_panel)
            self.chat_panel.show_reasoning_block(text)

    def on_provider_wait(self, text: str) -> None:
        """引擎在等供应商（`thinking.delta` 里那几句）：底部状态行，不进正文。"""
        if self.chat_panel is not None and text:
            self.chat_panel.set_provider_wait(text)

    def on_session_ready(self, result: dict) -> None:
        sid = str(result.get("session_id") or "")
        say(f"[会话] {sid}")
        self._start_process_watch()
        info = result.get("info") or {}
        if "reasoning_effort" in info:
            self.on_reasoning_reported(
                str(info.get("reasoning_effort") or ""), str(info.get("reasoning_effort_wire") or "")
            )
        else:
            self._reasoning_fresh = False
            self.request_reasoning()
        panel = self.chat_panel
        if panel is not None and (
            bool(result.get("running"))
            or str(result.get("status") or "") == "running"
            or result.get("auto_continue")
        ):
            panel.set_busy(True)
        self._turn_stopped = False
        self._reply_text = ""
        requested = self._new_session_pending
        self._new_session_pending = False
        self._new_session_timer.stop()
        queued, self._new_session_queued = self._new_session_queued, 0
        if panel is not None and should_clear_on_session_ready(
            result, requested=requested, has_transcript=panel.has_transcript()
        ):
            panel.clear_conversation("新会话就绪" if requested else "重连后仍为空")
            if requested:
                if queued:
                    panel.show_notice(
                        "session-queue", f"上一场排队中的 {queued} 条没发出去", level="warn"
                    )
                else:
                    panel.show_notice("session", "已开新会话", ttl_ms=2500)

    def on_history(self, rows: list) -> None:
        if self.chat_panel is not None and rows:
            self.chat_panel.set_history(rows)
            say(f"[历史] 铺回 {len(rows)} 条")

    def on_user_row_stamped(self, row_id: int) -> None:
        """引擎回报这次提交落在历史的第几行：记在那条消息上，重新生成/编辑重发靠它定位。"""
        if self.chat_panel is not None:
            self.chat_panel.stamp_user_row(int(row_id))

    def _resubmit_turn(self, msg, text: str, regenerate: bool, confirm_empty: bool = False,
                       confirm_deep: bool = False) -> None:
        """重新生成 / 编辑重发：让引擎从这条用户消息起截断重来。

        窗口先不动手，等第一段新文字回来才收（见 `_apply_pending_rewind`）——请求被拒时
        屏幕上不能先少掉那几条、引擎里却还在。
        """
        panel = self.chat_panel
        if panel is None or self.client is None or not isinstance(msg, dict):
            return
        row_id = msg.get("row_id")
        if not isinstance(row_id, int):
            panel.cancel_rewind()
            panel.show_notice("resubmit", "这条消息没有历史行号，重发不了", level="warn")
            return
        if panel.is_busy():
            panel.cancel_rewind()
            panel.show_notice("resubmit", "这一轮还在跑，等它结束再重发", level="warn")
            return
        panel.show_notice("resubmit", "正在重新生成…" if regenerate else "正在重发…")
        self._rewind_pending = (msg, text)
        self.client.submit_rewind(text, row_id, confirm_empty, confirm_deep)

    def on_models(self, items: list, current: str) -> None:
        self.apply_models(items, current)
        self.refresh_reasoning_menu()
        if current:
            self._greet_if_needed(force=True)

    def on_config_value(self, key: str, value: str) -> None:
        panel = self.chat_panel
        if key in {"approval_mode", "approvals.mode"} and value:
            if panel is not None:
                panel.set_approval_mode(value)
        elif key == "reasoning":
            self._reasoning_current = value
            if self.client is not None:
                self.client.set_reasoning_effort(value)
            if panel is not None and not self._reasoning_pending and not self._reasoning_fresh:
                panel.set_reasoning_state(
                    {"current": value, "can_disable": self._can_disable_reasoning(), "wire": self._reasoning_wire}
                )

    def on_reasoning_pending(self, pending: bool) -> None:
        """恢复会话时挡档位还不知道：先不画具体档（照桌面端 `reasoningEffortPending`）。"""
        self._reasoning_pending = bool(pending)
        panel = self.chat_panel
        if panel is None:
            return
        if self._reasoning_pending:
            self._reasoning_fresh = False
            self._reasoning_wire = ""
            panel.set_reasoning_state(
                {"current": "", "can_disable": self._can_disable_reasoning(), "pending": True, "wire": ""}
            )
        elif self._reasoning_current:
            panel.set_reasoning_state(
                {"current": self._reasoning_current, "can_disable": self._can_disable_reasoning(),
                 "wire": self._reasoning_wire}
            )

    def on_reasoning_wire(self, wire: str) -> None:
        """引擎报「这条路由实际会发的那档」：只在降档时用来加一句说明，不影响勾选。"""
        self._reasoning_wire = str(wire or "")

    def on_reasoning_reported(self, effort: str, wire: str = "") -> None:
        """引擎报到这场会话的档位（首条消息建好 agent 后才有）：以它为准，解除 pending。

        照桌面端：第一次回报就解除，哪怕回的是空串——那说明引擎这场没设过档，界面退回默认。
        """
        self._reasoning_pending = False
        self._reasoning_fresh = True
        self._reasoning_wire = str(wire or "")
        if effort:
            self._reasoning_current = effort
            if self.client is not None:
                self.client.set_reasoning_effort(effort)
        panel = self.chat_panel
        if panel is not None:
            panel.set_reasoning_state(
                {"current": effort or (self._reasoning_current or ""),
                 "can_disable": self._can_disable_reasoning(), "wire": self._reasoning_wire}
            )

    def refresh_reasoning_menu(self) -> None:
        """模型清单换了（切过模型）之后重摆一次档位菜单：能不能「关掉思考」看的是当前这个模型。"""
        panel = self.chat_panel
        if panel is None:
            return
        if self._reasoning_pending:
            panel.set_reasoning_state(
                {"current": "", "can_disable": self._can_disable_reasoning(), "pending": True, "wire": ""}
            )
            return
        if self._reasoning_current is None:
            return
        panel.set_reasoning_state(
            {"current": self._reasoning_current, "can_disable": self._can_disable_reasoning(), "wire": self._reasoning_wire}
        )

    def _can_disable_reasoning(self) -> bool:
        """当前这个模型允不允许关掉思考（引擎 `model.options` 的 `capabilities.<模型>.can_disable_reasoning`）。

        只有允许关的模型才在菜单里摆「关闭思考」：不允许的摆了也只是个点不动的档。
        """
        current = str((self._models or {}).get("current") or "")
        for item in (self._models or {}).get("items") or []:
            if str(item.get("id") or "") == current:
                return bool((item.get("caps") or {}).get("can_disable_reasoning"))
        return False

    def on_approval_request(self, params: dict) -> None:
        from types import SimpleNamespace

        ask = SimpleNamespace(
            request_id=str(params.get("request_id") or ""),
            reply_id=params.get("reply_id"),
            command=str(params.get("command") or ""),
            description=str(params.get("description") or ""),
            allow_session=params.get("allow_session", True),
            allow_permanent=params.get("allow_permanent", True),
        )
        self._ask_approval(ask)

    def on_setup_beat(self, beat: dict) -> None:
        """引导流程要摆「名字」那张卡：弹输入框，回帧用 reply_id（`srq-...`）。

        引擎一共七张卡，界面只做名字这一张；其余在 EngineWire 里直接回空帧关掉。
        """
        from app.name_dialog import NameDialog

        if self._name_dialog is not None:
            return
        reply_id = beat.get("reply_id")

        def answer(name: str) -> None:
            self._name_dialog = None
            if self.client is not None:
                self.client.answer_setup_beat(reply_id, name)
            if self.chat_panel is not None and name:
                self.chat_panel.show_tool("onboarding", f"你告诉我：叫你 {name}", ok=True)

        self._name_dialog = NameDialog(beat, answer)
        self._name_dialog.show_near(self.geometry())

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt
        if event.key() == QtCore.Qt.Key_Escape:
            if self._tray is not None:
                self.hide()
                self._sync_toggle_text()
            else:
                QtWidgets.QApplication.quit()
            return
        super().keyPressEvent(event)

    def _on_video_frame(self, image: QtGui.QImage) -> None:
        """显示区的新一帧（已抠像、带 alpha）。待机视频和互动数字人共用这一条。"""
        self._frame = image
        if self._hit_size != (self.width(), self.height()):
            self._rebuild_hit_region()

    @QtCore.Slot()
    def _paint_latest_frame(self) -> None:
        """把最新一帧同步画到屏幕上（定时器推着走，不依赖收帧时机）；没有新帧就不白画。"""
        if self._frame is not None and self._frame is not self._painted_frame:
            self._painted_frame = self._frame
            self._request_paint()

    def _request_paint(self) -> None:
        """同步把窗口画一遍。单独一层是为了测试能拦下来数「画了几次」。"""
        self.repaint()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        target = self.rect()
        source = self._frame
        if source is not None and not source.isNull():
            painter.drawImage(target, source)
            self._draw_hearts(painter)
            return
        painter.drawPixmap(target, self._pixmap.scaled(
            target.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        ))
        self._draw_hearts(painter)


    def flash_hearts(self) -> None:
        """冒几颗心，2.2 秒散掉。只是反应，不改任何状态。"""
        self._hearts_at = time.monotonic()
        if self._hearts_timer is None:
            self._hearts_timer = QtCore.QTimer(self)
            self._hearts_timer.setInterval(33)
            self._hearts_timer.timeout.connect(self._tick_hearts)
        self._hearts_timer.start()
        self._request_paint()

    def _tick_hearts(self) -> None:
        if time.monotonic() - self._hearts_at > 2.2:
            self._hearts_timer.stop()
            self._request_paint()
            return
        self._request_paint()

    def _draw_hearts(self, painter) -> None:
        elapsed = time.monotonic() - self._hearts_at
        if elapsed > 2.2:
            return
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        for index, (x_ratio, size, delay) in enumerate(HEARTS):
            progress = (elapsed - delay) / 1.5
            if progress <= 0 or progress >= 1:
                continue
            alpha = int(200 * (1 - progress))
            font = QtGui.QFont(self.font())
            font.setPointSizeF(size)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(255, 120, 160, alpha))
            x = self.width() * x_ratio + 5 * math.sin(progress * 6 + index)
            y = self.height() * 0.62 - progress * self.height() * 0.30
            painter.drawText(QtCore.QPointF(x, y), "♥")

    def moveEvent(self, event) -> None:  # noqa: N802
        """头像窗一挪，贴在上面的审批卡片跟着挪——不然卡片会留在原地。"""
        super().moveEvent(event)
        self._follow_approval()
        self._sync_chat_anchor()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._follow_approval()
        self._sync_chat_anchor()
        self._hit_timer.start(120)
        if self.idle_player is not None:
            self.idle_player.set_target_size(self.size())
        if self._avatar is not None:
            self._avatar.set_target_size(self.size())

    def _rebuild_hit_region(self) -> None:
        """按当前画面（视频帧优先）重算「点得中」的轮廓；算不出来就整块都能点。

        按网格抠轮廓要几万次 `QRegion.united`，尺寸没变就别重算——同一个尺寸反复算，
        缩放时界面就被拖住了。
        """
        size = (self.width(), self.height())
        if self._hit_size == size:
            return
        source = self._frame if (self._frame is not None and not self._frame.isNull()) else self._pixmap
        if isinstance(source, QtGui.QPixmap):
            image = source.toImage()
        elif isinstance(source, QtGui.QImage):
            image = source
        else:
            image = QtGui.QImage()
        region = hit_region(image, self.size())
        self._hit_region = None if region.isEmpty() else region
        self._hit_size = size

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt
        """透明的地方不挡鼠标：点在她身上才算点她，空白处交给下面的窗口（比如桌面图标）。

        无边框透明窗**整块矩形**都会吃鼠标事件，透明像素也吃——所以拖到桌面图标上面之后，
        那一片图标就点不动了（用户实际报的毛病）。Windows 的 WM_NCHITTEST 回 HTTRANSPARENT，
        系统就把这次点击交给下面的窗口。
        """
        if sys.platform == "win32" and self._hit_region is not None and event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_WinMsg)).contents
                if msg.message == WM_NCHITTEST:
                    if not self._hit_region.contains(self.mapFromGlobal(QtGui.QCursor.pos())):
                        return True, HTTRANSPARENT
            except Exception:
                logger.debug("WM_NCHITTEST 解析失败，按普通窗口处理", exc_info=True)
        return super().nativeEvent(event_type, message)

    def _follow_approval(self) -> None:
        """贴身的卡片跟着人物走（审批、引擎起不来都是同一套位置规则）。"""
        for attr in ("_approval_dialog", "_takeover_dialog"):
            dialog = getattr(self, attr, None)
            if dialog is not None and dialog.isVisible():
                dialog.move_near(self.geometry())
        for dialog in list(getattr(self, "_approval_dialogs", ())):
            if dialog is not getattr(self, "_approval_dialog", None) and dialog.isVisible():
                dialog.move_near(self.geometry())

    def ask_takeover(self, message: str) -> None:
        """数据根被命令行会话占着：贴人物下方问一句，同意后才动手。

        同意后按「会话 Ctrl+C 退出 → 杀掉残留进程 → 启动」做：那台会话只要还开着，下一句话
        就会再拉一条 gateway 把数据根占回去，所以先请它退出才有意义。全过程一个命令行工具都不起
        （`app/winproc.py`），不会闪黑窗口；每一步都写日志，聊天窗里也留一行说明。
        """
        from app.engine_prompt import EngineProblemDialog
        from app.engine_takeover import occupied_pid

        action = "退出会话、结束后台进程并启动"

        def tell(text: str, error: bool = False) -> None:
            """写日志 + 写进聊天窗。聊天窗这会儿可能还没建起来（引擎没就绪时它是 None）——
            2026-10-25 就是在这里抛了 AttributeError，点了确认什么都没发生。"""
            say(f"[引擎] {text}")
            panel = getattr(self, "chat_panel", None)
            if panel is None:
                return
            if error:
                panel.show_error(text)
            else:
                panel.show_info(text)

        def answered(agree: bool) -> None:
            self._takeover_dialog = None
            if not agree:
                tell("没有结束任何进程")
                return
            wire = getattr(self, "client", None)
            if wire is None:
                tell("引擎通道没建起来，没法接管", error=True)
                return
            pid = occupied_pid(message)
            if pid is None:
                tell("报错里没写进程号，没动任何进程", error=True)
                return
            tell(f"引擎被占：先让占着数据目录的 pid {pid} 退出，再启动")
            from app.engine_takeover import release
            import threading

            panel = getattr(self, "chat_panel", None)
            if panel is not None:
                panel.show_notice("takeover", "正在请占着数据目录的进程退出…", level="busy")

            def work() -> None:
                try:
                    ok, note = release(getattr(wire, "_root", None), pid)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("接管时出错")
                    ok, note = False, f"接管出错：{exc}"
                QtCore.QMetaObject.invokeMethod(
                    self,
                    "_finish_takeover",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(bool, ok),
                    QtCore.Q_ARG(str, note),
                )

            threading.Thread(target=work, name="noesia-takeover", daemon=True).start()

        try:
            self._takeover_dialog = EngineProblemDialog(message, action, answered, self)
            self._takeover_dialog.show_near(self.geometry())
            logger.info("接管：已弹卡片等用户确认")
        except Exception as exc:  # noqa: BLE001
            logger.exception("接管的卡片没能弹出来")
            tell(f"没能弹出接管卡片：{exc}", error=True)

    @QtCore.Slot(bool, str)
    def _finish_takeover(self, ok: bool, note: str) -> None:
        """后台线程放行完进程，回到这里重启引擎并回话（QProcess 只能在主线程碰）。"""
        panel = getattr(self, "chat_panel", None)
        if panel is not None:
            panel.clear_status_notice("takeover")
        wire = getattr(self, "client", None)
        if ok and wire is not None:
            wire.start()
            note = f"{note}；已重新启动引擎" if note else "已重新启动引擎"
        say(f"[引擎] {note}")
        if panel is not None:
            if ok:
                panel.show_info(note)
            else:
                panel.show_error(note)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.LeftButton:
            return
        edge = edges_at(event.position().toPoint(), self.size())
        if edge:
            self._resize_edge = edge
            self._resize_start_size = self.size()
            self._resize_start_global = event.globalPosition().toPoint()
            self._drag_offset = None
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.globalPosition().toPoint()
        if self._resize_edge and self._resize_start_size is not None and self._resize_start_global is not None:
            new_size = size_from_drag(
                self._resize_edge,
                self._resize_start_size,
                pos - self._resize_start_global,
                self._aspect,
                min_width=MIN_WIDTH,
                max_width=self._max_size.width(),
                max_height=self._max_height(),
            )
            old = self.geometry()
            new_geo = QtCore.QRect(old.topLeft(), new_size)
            if self._resize_edge & QtCore.Qt.Edge.LeftEdge:
                new_geo.moveRight(old.right())
            if self._resize_edge & QtCore.Qt.Edge.TopEdge:
                new_geo.moveBottom(old.bottom())
            self.setGeometry(new_geo)
            return
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(pos - self._drag_offset)
            return
        self.setCursor(cursor_for(edges_at(event.position().toPoint(), self.size())))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        resized = self._resize_edge is not None
        self._resize_edge = None
        self._resize_start_size = None
        self._resize_start_global = None
        self._drag_offset = None
        self.unsetCursor()
        if resized:
            self._save_geometry()

    def release_audio(self) -> None:
        """退出前把麦克风还回去。

        退出走的是 `QApplication.quit()`（右键菜单那条路），它**不触发 closeEvent**——
        只挂在 closeEvent 上的清理等于永远不执行，蓝牙耳机那头会一直显示「正在使用」。
        """
        panel = self.chat_panel
        if panel is not None:
            panel.release_audio()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_process_watch()
        self._save_geometry()
        self.release_audio()
        for dialog in list(self._approval_dialogs):
            dialog.dismiss()
        self._approval_dialogs.clear()
        self._approval_dialog = None
        takeover = getattr(self, "_takeover_dialog", None)
        if takeover is not None and takeover.isVisible():
            takeover.close()
        super().closeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        """藏起来（Esc / 托盘「隐藏」）就当没在看：解码和 30fps 重画都不该继续。

        以前只 `hide()`，待机视频照旧 24fps 解码 + 整帧抠像、画帧定时器照跑，
        笔记本上就是「关到托盘之后风扇还不停」。
        """
        self._hidden = True
        player = getattr(self, "idle_player", None)
        if player is not None:
            player.pause()
        timer = getattr(self, "_paint_timer", None)
        if timer is not None:
            timer.stop()
        self._hit_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        """重新露出来：视频、画帧、命中区域都接回去（互动画面正占着显示位时不抢回来）。"""
        self._hidden = False
        if not self._interactive_shown:
            player = getattr(self, "idle_player", None)
            if player is not None:
                player.resume()
        timer = getattr(self, "_paint_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()
        self._hit_timer.start(120)
        super().showEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.LeftButton:
            self.open_chat()
            return
        super().mouseDoubleClickEvent(event)


def _our_browser_daemon(cmdline: list[str], exe: str, root: str) -> bool:
    """这条进程是不是**我们这套安装**拉起来的 browser 守护进程（判断只用命令行和 exe 路径）。

    为什么要收它：它是引擎给 browser 工具起的常驻进程，故意比一次调用活得久；但它把
    `data/workspace` 当工作目录，App 退出后没人管它退，于是整个 `data` 还“在用”——
    用户手删 data 会报“另一个程序正在使用此文件，进程无法访问”，而文件本身一个都没被锁。
    别人的安装目录里也有同名进程，所以必须按本项目根目录认，不能见一个杀一个。
    """
    if "browser_harness.daemon" not in " ".join(cmdline or []):
        return False
    base = os.path.normcase(os.path.abspath(str(root)))
    if not base:
        return False
    prefix = base + os.sep
    for item in [*list(cmdline or []), exe or ""]:
        if not item:
            continue
        try:
            path = os.path.normcase(os.path.abspath(str(item)))
        except Exception:  # noqa: BLE001
            continue
        if path == base or path.startswith(prefix):
            return True
    return False


def _reap_browser_daemons() -> int:
    """收掉残留的 browser 守护进程（连它拉起的浏览器一起），返回收掉几个。

    调用点两处：启动时（上次被强杀留下的）和退出时（这次自己留下的）。一处不清，
    用户下次想删 `data` 还是会撞上“正在使用”。
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        logger.debug("没有 psutil，跳过 browser 守护进程清理")
        return 0
    root = str(Path(__file__).resolve().parents[2])
    victims = []
    for proc in psutil.process_iter(["pid", "cmdline", "exe"]):
        try:
            info = proc.info
            if _our_browser_daemon(info.get("cmdline") or [], info.get("exe") or "", root):
                victims.append(proc)
        except (psutil.Error, OSError):
            continue
    for proc in victims:
        try:
            tree = proc.children(recursive=True) + [proc]
        except (psutil.Error, OSError):
            tree = [proc]
        for child in tree:
            try:
                child.kill()
            except (psutil.Error, OSError):
                continue
        try:
            psutil.wait_procs(tree, timeout=3)
        except (psutil.Error, OSError):
            pass
    if victims:
        logger.info("[退出] 收掉 %d 个残留的 browser 守护进程", len(victims))
    return len(victims)


def notify_existing_instance(command: str = "show") -> bool:
    """已有实例在跑就给它发一条命令，返回 True（本进程应退出）。

    为什么要等回执：socket 一析构，没送出去的数据就随对象一起没了；而第二个实例发完命令
    立刻退出，靠时序赌它送出去迟早会翻车。所以对端处理完回一个 "ok"，这里等它。
    """
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(INSTANCE_NAME)
    if not sock.waitForConnected(500):
        return False

    sock.write(f"{command}\n".encode())
    sock.flush()

    deadline = time.monotonic() + 1.0
    acked = False
    while time.monotonic() < deadline:
        if sock.bytesAvailable():
            if bytes(sock.readAll()).strip() == b"ok":
                acked = True
                break
        if sock.state() != QtNetwork.QLocalSocket.LocalSocketState.ConnectedState:
            break
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
        sock.waitForReadyRead(20)

    sock.disconnectFromServer()
    if sock.state() != QtNetwork.QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(200)
    return acked


def start_instance_server(window) -> QtNetwork.QLocalServer | None:  # noqa: ANN001
    """监听单实例通道，让后来启动的进程能显示/关闭已有窗口。"""
    QtNetwork.QLocalServer.removeServer(INSTANCE_NAME)
    server = QtNetwork.QLocalServer(window)
    if not server.listen(INSTANCE_NAME):
        say(f"[单实例] 监听失败：{server.errorString()}（不影响窗口使用）")
        return None

    def handle_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read_command() -> None:
            raw = bytes(conn.readAll()).decode("utf-8", "ignore").strip()
            if not raw:
                return
            if raw == "show":
                window.show()
                window.raise_()
                window.activateWindow()
                window._sync_toggle_text()
                say("[单实例] 收到 show，窗口已唤回")
            elif raw == "quit":
                QtWidgets.QApplication.quit()
            else:
                say(f"[单实例] 收到未知命令：{raw}")

            conn.write(b"ok\n")
            conn.flush()

        conn.readyRead.connect(read_command)
        if conn.bytesAvailable():
            read_command()
        conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(handle_connection)
    return server


_ENGINE_RESTART_DELAYS = (1000, 2000, 4000)


def engine_restart_delay(attempt: int) -> int | None:
    """引擎断开后第 attempt 次重连等多久（毫秒）；超过最后一档返回 None，不再试。"""
    if attempt < 0 or attempt >= len(_ENGINE_RESTART_DELAYS):
        return None
    return _ENGINE_RESTART_DELAYS[attempt]


def _setup_gui_log(root: Path) -> Path | None:
    """把前端日志写到 `data/runtime/gui.log`。

    pythonw 启动没有控制台，`print` 全丢；气泡这类「只在界面上体现」的问题
    出事时必须有地方能查，否则只能靠猜。
    """
    try:
        log_dir = root / "data" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "gui.log"
        root_logger = logging.getLogger()
        if any(
            isinstance(h, logging.FileHandler) and str(getattr(h, "baseFilename", "")).endswith("gui.log")
            for h in root_logger.handlers
        ):
            return path
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        return path
    except OSError:
        return None


def _install_crash_logging() -> None:
    """把「没人接住的异常」和「Qt 自己吐的警告」都写进 gui.log。

    `pythonw` 启动没有控制台：槽函数里抛异常，进程直接消失、`app.exec()` 也不返回，
    界面上只看到「闪一下没了」，日志里一个字都没有（2026-10-27 查「打开就闪退」卡了很久就是这个）。
    这里只做记录，不改任何行为——闪退照旧可能发生，但死因留得下来。
    """
    global _CRASH_LOGGING_INSTALLED
    if _CRASH_LOGGING_INSTALLED:
        return
    _CRASH_LOGGING_INSTALLED = True
    log = logging.getLogger("app.crash")

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("未捕获的异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook

    def _qt_message(mode, context, message):
        level = {
            QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
            QtCore.QtMsgType.QtInfoMsg: logging.INFO,
            QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
            QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
            QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(mode, logging.INFO)
        log.log(level, "Qt: %s", message)

    QtCore.qInstallMessageHandler(_qt_message)


def main() -> int:
    from .engine_wire import prepend_bundled_paths

    prepend_bundled_paths(REPO_ROOT)
    _setup_gui_log(PROJECT_ROOT)
    _install_crash_logging()
    cfg, resolver = _load_avatar_config()
    catalog.pin_engine_home(cfg, resolver.root)
    front = resolver.resolve(cfg.avatar.dir) / cfg.avatar.reference.front
    idle_path = resolver.resolve(cfg.avatar.idle_video)
    _setup_gui_log(resolver.root)

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(_TIP_QSS)
    app.setQuitOnLastWindowClosed(False)
    if notify_existing_instance("show"):
        say("[单实例] 已有一个 Noesia 在运行，窗口已唤回（本进程退出）")
        return 0
    _reap_browser_daemons()

    from .boot_splash import BootSplash

    splash = BootSplash(front)
    splash.set_step("正在准备界面…")
    splash.show()
    window = FloatingAvatar(front, cfg.ui.floating, idle_path if idle_path.is_file() else None)
    window.boot_splash = splash
    splash.skipped.connect(window.finish_boot)
    QtCore.QTimer.singleShot(12000, window.finish_boot)
    has_tray = window.setup_tray()
    window.instance_server = start_instance_server(window)
    if window.idle_player is not None:
        if window.idle_player.start():
            say(f"[待机视频] 已开始播放：{idle_path}")
            def _report() -> None:
                stats = window.idle_player.stats if window.idle_player else {}
                say(f"[待机视频] 解码累计 {int(stats.get('frames', 0))} 帧，"
                    f"抠像中位 {stats.get('median_key_ms', 0)}ms（24fps 预算 41.7ms）")

            QtCore.QTimer.singleShot(6000, _report)
        else:
            say(f"[待机视频] 启动失败，退回静态图：{idle_path}")
    else:
        say(f"[待机视频] 文件不存在，只用静态图：{idle_path}")
    from app.chat_panel import _voice_mode

    if _voice_mode() == "interactive":
        window.preload_avatar()
    say(f"[托盘] {'已启用：左键显示/隐藏，右键菜单退出' if has_tray else '不可用：Esc 只能退出'}")
    say(f"[菜单] {' / '.join(a.text() for a in window.build_menu().actions() if a.text())}")

    workdir = workspace_from_config(Path(resolver.root) / cfg.engine.home,
                                    Path(resolver.root) / cfg.engine.workdir)
    try:
        ensure_runtime(root=Path(resolver.root), home=Path(resolver.root) / cfg.engine.home,
                       workdir=workdir)
        overrides: dict = {}
        overrides["terminal"] = {**(overrides.get("terminal") or {}), "cwd": str(workdir)}
        sync_engine_config(
            Path(resolver.root) / cfg.engine.core_path,
            Path(resolver.root) / cfg.engine.home,
            overrides,
        )
    except Exception:  # noqa: BLE001
        logger.exception("同步引擎配置失败")

    wire = EngineWire(Path(resolver.root), workspace=workdir)
    window.client = wire

    def _after_ready(_payload: dict) -> None:
        """引擎就绪后再问东西：模型清单、上次的会话、两个档位。"""
        say("[引擎] 已就绪")
        window._engine_ready = True
        engine_restarts["n"] = 0
        if window.chat_panel is not None:
            window.chat_panel.clear_status_notice("link")
        window._history_asked = False
        wire.request_models()
        window.request_history()
        wire.config_get("approval_mode")
        wire.config_get("reasoning")
        wire.request_display()
        window.finish_boot()

    def _after_recent(result: dict) -> None:
        sid = str(result.get("session_id") or "")
        if sid:
            wire.resume_session(sid)
        else:
            wire.create_session()

    wire.ready.connect(_after_ready)
    wire.attachment_failed.connect(window._attachment_problem)
    wire.most_recent.connect(_after_recent)
    wire.session_ready.connect(window.on_session_ready)
    wire.history.connect(window.on_history)
    wire.user_row_stamped.connect(window.on_user_row_stamped)
    wire.delta.connect(window.on_delta)
    wire.turn_started.connect(window.on_turn_started)
    wire.status_text.connect(window.on_status_text)
    wire.reasoning_delta.connect(window.on_reasoning_delta)
    wire.reasoning_done.connect(lambda _t: window.chat_panel and window.chat_panel.flush_reasoning())
    wire.reasoning_block.connect(window.on_reasoning_block)
    wire.provider_wait.connect(window.on_provider_wait)
    wire.error.connect(window.on_engine_error)
    wire.setup_needed.connect(window.on_setup_needed)
    wire.completed.connect(window.on_complete)
    wire.turn_settled.connect(window.on_turn_settled)
    wire.tool.connect(window.on_tool_event)
    wire.subagent.connect(window.on_subagent_event)
    wire.status.connect(window.on_status)
    wire.notice.connect(window.on_notice)
    wire.review_summary.connect(window.on_review_summary)
    wire.notice_show.connect(window.on_notice_show)
    wire.notice_clear.connect(lambda key: window.chat_panel and window.chat_panel.clear_status_notice(key))
    wire.clarify.connect(window.on_clarify_request)
    wire.clarify_expired.connect(
        lambda rid: window.chat_panel and window.chat_panel.expire_clarify(rid)
    )
    wire.secret_prompt.connect(window.on_secret_request)
    wire.reaction.connect(window.on_reaction)
    wire.processes.connect(lambda rows: window.chat_panel and window.chat_panel.set_processes(rows))
    wire.voice_text.connect(
        lambda path, text: window.chat_panel and window.chat_panel.apply_voice_transcript(text, path)
    )
    wire.voice_failed.connect(
        lambda path, message: window.chat_panel and window.chat_panel.voice_transcribe_failed(message, path)
    )
    wire.voice_model.connect(window._on_voice_model)
    wire.approval.connect(window.on_approval_request)
    wire.request_cancelled.connect(window.on_request_cancelled)
    wire.approvals_cancelled.connect(window.on_approvals_cancelled)
    wire.setup_beat.connect(window.on_setup_beat)
    wire.key_saved.connect(lambda _slug: window._greet_if_needed(force=True))
    wire.models.connect(window.on_models)
    wire.provider_rows.connect(window.apply_provider_rows)
    wire.provider_rows_failed.connect(window.on_provider_rows_failed)
    wire.save_result.connect(window.on_save_result)
    wire.config_value.connect(window.on_config_value)
    wire.reasoning_pending.connect(window.on_reasoning_pending)
    wire.reasoning_reported.connect(window.on_reasoning_reported)
    wire.reasoning_wire.connect(window.on_reasoning_wire)
    wire.display_config.connect(window.on_display_config)

    from app.engine_takeover import looks_like_occupied

    def _drop_rewind() -> None:
        """这次截断重发作废：清了它，陈旧截断点就不会落到之后任何一轮上。

        以前只有引擎回 error 帧才清。断线/没送出去时它一直挂着，重连后第一条无关的
        delta 一落地就触发 `_apply_pending_rewind`，把聊天窗截到那条旧消息——后半段历史当场消失。
        """
        if getattr(window, "_rewind_pending", None) is None:
            return
        window._rewind_pending = None
        panel = getattr(window, "chat_panel", None)
        if panel is not None:
            panel.clear_status_notice("resubmit")
            panel.cancel_rewind()

    def _on_failed(message: str) -> None:
        say(f"[引擎] {message}")
        window.boot_failed(message)
        _drop_rewind()
        panel = getattr(window, "chat_panel", None)
        if panel is not None and getattr(window, "_boot_done", False):
            panel.abandon_turn(message)
            panel.set_busy(False)
        try:
            offer = looks_like_occupied(message)
        except Exception:  # noqa: BLE001
            logger.exception("判断要不要弹接管卡片时出错")
            offer = False
        if not getattr(window, "_takeover_offered", False) and offer:
            window._takeover_offered = True
            QtCore.QTimer.singleShot(600, lambda: window.ask_takeover(message))

    wire.failed.connect(_on_failed)
    wire.closed.connect(lambda: say("[引擎] 通道已关闭"))
    wire.closed.connect(_drop_rewind)
    wire.closed.connect(lambda: window.chat_panel and window.chat_panel.abandon_turn("引擎断开"))
    wire.closed.connect(lambda: window.chat_panel and window.chat_panel.set_busy(False))

    engine_restarts = {"n": 0}
    quitting = {"v": False}

    def _reconnect_engine() -> None:
        if quitting["v"]:
            return
        delay = engine_restart_delay(engine_restarts["n"])
        if delay is None:
            if window.chat_panel is not None:
                window.chat_panel.show_error("引擎一直没连上，重开一次 Noesia 才能恢复")
            return
        attempt = engine_restarts["n"]
        engine_restarts["n"] = attempt + 1
        if window.chat_panel is not None:
            window.chat_panel.show_notice(
                "link", f"与引擎的连接已断开，正在重连（第 {attempt + 1} 次）…", level="busy"
            )
        say(f"[引擎] 连接断了，{delay}ms 后第 {attempt + 1} 次重连")
        QtCore.QTimer.singleShot(delay, wire.start)

    wire.closed.connect(_reconnect_engine)

    try:
        from app.engine_wire import close_console_sessions, quit_process

        notes = close_console_sessions(Path(resolver.root))
        owner = wire.blocking_owner()
        if owner is not None:
            _ok, note = quit_process(owner)
            notes.append(note)
        for note in notes:
            say(f"[引擎] {note}")
            if window.chat_panel is not None:
                window.chat_panel.show_info(note)
    except Exception:  # noqa: BLE001
        logger.exception("清理命令行会话时出错")

    wire.start()
    window.boot_step("正在启动引擎…")
    def _boot_timeout() -> None:
        """三十秒还没开完：说清楚卡在哪，别一律甩锅给引擎。

        引擎早就绪、只是模型清单还没回来时写「引擎还没就绪」是假话，
        用户照着这句去查引擎会白查一轮。
        """
        if getattr(window, "_engine_ready", False):
            window.boot_failed("模型清单还没回来，先打开界面")
        else:
            window.boot_failed("引擎还没就绪，先打开界面")

    QtCore.QTimer.singleShot(30000, _boot_timeout)

    def _shutdown_engine() -> None:
        """退出顺序：收起窗口 → 关引擎通道让它自己收尾 → 退出。"""
        try:
            window.hide()
            if window.idle_player is not None:
                window.idle_player.stop()
        except Exception:  # noqa: BLE001
            logger.debug("退出时收起窗口失败", exc_info=True)
        quitting["v"] = True
        wire.shutdown()
        _reap_browser_daemons()

    app.aboutToQuit.connect(_shutdown_engine)
    app.aboutToQuit.connect(window.release_audio)
    app.aboutToQuit.connect(window._save_geometry)
    if getattr(window, "_avatar", None) is not None:
        app.aboutToQuit.connect(window._avatar.shutdown)
    if window.idle_player is not None:
        app.aboutToQuit.connect(window.idle_player.stop)
    say(f"[引擎] 通道：stdio JSON-RPC（根目录 {resolver.root}）")

    say(f"[{QT_BINDING}] 悬浮窗已启动：{front}")
    if not front.is_file():
        say(f"[WARN] 形象图不存在，显示空窗口：{front}")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
