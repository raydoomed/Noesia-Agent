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
import threading
import time
import math
import sys
from pathlib import Path

from paths import project_root

from . import model_cache
from .status_stack import CONFIRM_TTL_MS

logger = logging.getLogger(__name__)

_CRASH_LOGGING_INSTALLED = False  # 崩溃记录只装一次（重复装会把每条日志写两遍）

def say(message: str) -> None:
    """启动期的一句话，写进日志，控制台在时也照旧打印。

    双击启动走的是 pythonw.exe，那里没有 stdout——直接 print 的话整行凭空消失，
    看日志时会把「没有输出」当成「没走到这里」。
    """
    logger.info(message)
    if sys.stdout is not None:
        print(message, flush=True)

PROJECT_ROOT = project_root()


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

# 工具行文案的语言：默认英文，我们是中文界面。
from .tool_view.i18n import set_runtime_i18n_locale  # noqa: E402

set_runtime_i18n_locale("zh")

QT_BINDING = ""
try:  # 优先 Qt6
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets  # type: ignore

    QT_BINDING = "PySide6"
except ImportError:
    try:  # 退化到 Qt5
        from PySide2 import QtCore, QtGui, QtNetwork, QtWidgets  # type: ignore

        QT_BINDING = "PySide2"
    except ImportError:
        QtCore = QtGui = QtNetwork = QtWidgets = None  # type: ignore

INSTANCE_NAME = "NoesiaAvatar"  # 单实例通道：第二次启动用它唤回已有窗口


def _flag(value, *, default: bool = True) -> bool:
    """配置里的开关：只有 `false` / `"false"` / `0` 算关（桌面端 `setShowReasoningFromConfig` 同一条判据）。"""
    if value is None:
        return default
    return not (value is False or value == "false" or value == 0)





def build_model_choices(items: list[dict]) -> list[dict]:
    """「设置模型」对话框的数据：引擎支持的**每一家**（不只是现在能用的那几家）。

    前端已经在手上的那份清单（回来时带 `backend`/`model`）用来补充/校正模型列表；供应商本身
    以引擎的供应商表为准——`minimax`、`xiaomi`、`stepfun` 这些还没配密钥的也必须出现在这里。
    """
    choices = catalog.provider_choices()
    by_provider = {row["provider"]: row for row in choices}

    for item in items or []:
        provider = str(item.get("backend") or "")
        model = str(item.get("model") or "")
        if not provider or not model:
            continue
        row = by_provider.get(provider)
        if row is None:
            row = {
                "provider": provider,
                "label": _provider_label(item, provider, model),
                "env_vars": list(catalog.provider_env_vars(provider)),
                "configured": catalog.provider_configured(provider),
                "models": [],
            }
            by_provider[provider] = row
            choices.append(row)
        if model not in row["models"]:
            row["models"] = [model, *row["models"]]

    # 顺序由 catalog 决定（配好的 → 文档里写明怎么配的 → 其余），这里不再排一遍：
    # 二次排序会把文档里那几家打散成字母序，用户第一眼看到的就是 `actual` 这种一家模型都没有的。
    return choices


def media_roots() -> list[Path]:
    """模型写的相对路径（`MEDIA:data/workspace/x.png`）该从哪儿找：工作目录 + 引擎家 + 仓库根。"""
    roots: list[Path] = []
    try:
        cfg, resolver = _load_avatar_config()
    except SystemExit:
        cfg = resolver = None
    # `_load_avatar_config()` 给的是 (RootConfig, resolver)，不是 dict：这里必须按属性取，
    # 且根目录以 resolver 为准（配置里可以把 root 指到别处）。
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


if QtWidgets is None:  # pragma: no cover - 依赖于本机是否装了 Qt
    say("未检测到 Qt 绑定（PySide6 / PySide2）。安装其一后重试：")
    say('  python -m pip install -e ".[qt]"')
    say("本文件只负责界面骨架，引擎可用 `noesia check-config` 单独验证。")
    raise SystemExit(0)


from backends import catalog
from app.chat_panel import ChatPanel

# 她说 ily / ♥ 时飘的那几颗心：(横向位置比例, 字号, 延迟秒)
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
    except OSError:  # 目录不在了之类，退回按字面比
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
        # 形象图/视频的固定构图比例（高 / 宽），缩放时锁住，避免把人脸压扁
        self._aspect = (self._pixmap.height() / self._pixmap.width()) if self._pixmap.width() else 1.333
        # 打开时的大小：形象原图 1344×1792，缩到 360 宽（480 高）放在桌面上不挡东西
        self._default_size = fit_width(DEFAULT_WIDTH, self._aspect)
        # 能拖到的最大：原图的一半（672×896），和以前一样
        self._max_size = fit_width(self._pixmap.width() * self._scale, self._aspect)
        self.setMinimumSize(MIN_WIDTH, round(MIN_WIDTH * self._aspect))
        self.setMaximumSize(self._max_size)
        self.resize(self._default_size)
        # 尺寸/位置记在系统设置里（QSettings 自己选平台合适的位置，不写死路径）
        self._settings = window_settings()
        self._resize_edge: QtCore.Qt.Edges | None = None
        self._resize_start_size: QtCore.QSize | None = None
        self._resize_start_global: QtCore.QPoint | None = None
        self._drag_offset: QtCore.QPoint | None = None
        # 鼠标只在她的轮廓上有反应：透明的地方让给下面的桌面图标（见 nativeEvent）
        self._hit_region: QtGui.QRegion | None = None
        self._hit_size: tuple[int, int] | None = None  # 这份命中区是按哪个尺寸算的：同尺寸不重算
        self._hit_timer = QtCore.QTimer(self)  # 缩放拖动中不逐帧重算，停手 120ms 后算一次
        self._hit_timer.setSingleShot(True)
        self._hit_timer.timeout.connect(self._rebuild_hit_region)
        self._tray: QtWidgets.QSystemTrayIcon | None = None
        self._toggle_action: QtGui.QAction | None = None
        # 无边框 Tool 窗不进任务栏，靠右键菜单 + 托盘提供显示/隐藏与退出
        self.setToolTip("拖动移动 · 拖边缘缩放 · 双击聊天 · Esc 隐藏 · 右键菜单")
        self.setMouseTracking(True)  # 不按键也要收到 mouseMove 才能换光标
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.chat_panel: ChatPanel | None = None  # 懒创建：双击或菜单「聊天」才出现
        self._pending_reviews: list[str] = []  # 聊天窗还没开时收到的自我回顾总结，开了再补进去
        self._greeted = False  # 只问一次「要不要她先开口」，别每次开窗都跑一次推理
        self._approval_dialog = None  # 审批卡片同时只开一张
        self._name_dialog = None      # 起名卡片同时只开一张
        self.client = None  # main() 里注入，用来发消息
        # 「新会话」按钮是用户主动要换的：这种回包一定要清屏。引擎自己回来的 `session.create`
        # （开窗、重连）不能清——那时屏幕上已经有工具行了（见 on_session_ready）。
        self._new_session_pending = False
        self._models = None  # 引擎回来的模型清单（引擎连上后由 on_envelope 填）
        self._models_refresh_token = 0  # 每次「刷新模型」+1：迟到的 30 秒兜底认这个号，过期就不吭声
        self._reasoning_current: str | None = None  # 引擎回读的推理强度，模型清单变了要重摆菜单
        self._reasoning_pending = False  # 恢复的会话还没回报档位：先不画（照桌面端 pending）
        self._reasoning_fresh = False  # 引擎已经报过这场的真档：之后 `config.get` 回的默认值不许再盖它
        self._reasoning_wire = ""  # 这条路实际会发的那档（引擎报的，跟选的那档不同时显示「→」）
        self._cache_root = project_root()  # 模型清单快照写在哪（main() 里换成配置根）
        self._boot_done = False  # 启动加载做完没有（做完才让窗口露面）
        self.boot_splash = None  # 启动动画（BootSplash，没有 Qt 时是 None）
        self._history_asked = False  # 这一段对话问过没有（连上/开窗都会补问一次）
        self._restore_geometry()

        # 待机视频：拿到帧就盖在静态图上画；失败就退回静态图，不影响窗口可用
        self._frame: QtGui.QImage | None = None
        self._painted_frame: QtGui.QImage | None = None  # 上一次画出去的那帧，没新帧就不重画
        # 她说 ily / ♥ 时飘的心：起始时刻 + 那个只在飘的时候才跑的小定时器
        self._hearts_at = -99.0
        self._hearts_timer: QtCore.QTimer | None = None
        self.idle_player: IdleVideoPlayer | None = None
        # 说话：回答念出来（文字 → ElevenLabs → 播放），失败只记日志不挡界面
        self.speaker = Speaker(self)
        # 语音互动那条链路：本进程内跑阿里云 ARTC 引擎，静音/纯语音时一个字节都不载
        self._avatar = None            # AvatarRtc，画面从云端 RTC 直接拿裸帧
        self._streamer = None          # StreamSpeaker，边合成边出声
        self._pending_speech = ""      # 会话还没就绪时先说一句「正在接通」，就绪后再念
        self._interactive_shown = False  # 互动画面是否正占着那块显示区（待机视频让位）
        # Vidu 按秒计费（1 积分/秒）：说完 5 秒还没下一句就挂断，别在闲置上烧钱
        # 下一句要重建会话（约 2.5 秒），但那个等待由「点输入框就开始建」吃掉
        self._avatar_idle = QtCore.QTimer(self)
        self._avatar_idle.setSingleShot(True)
        self._avatar_idle.setInterval(5_000)
        self._avatar_idle.timeout.connect(self._end_avatar_session)
        # 热身之后一直没说话：会话开着就按秒计钱（1 积分/秒），不能让它在后台空转。
        # 每点一下/敲一下输入框都会重新计时，手停 20 秒才算"这会儿不说了"，直接挂断。
        self._warm_idle = QtCore.QTimer(self)
        self._warm_idle.setSingleShot(True)
        self._warm_idle.setInterval(20_000)
        self._warm_idle.timeout.connect(self._end_avatar_session)
        self._finish_waits = 0  # 收尾时等画面出帧的重试次数
        self._reset_sent = True  # 这一句的"复位"信令发过没有（每句只发一次）
        self._avatar_block_until = 0.0
        # 冷却期间那句提示说什么，跟着失败原因走（积分不够 / Token 没配好）
        self._avatar_block_hint = "Vidu 积分不够，互动数字人暂时用不了：这句用纯语音念，音色不变。"  # 互动档失败（积分不足等）后的冷却截止时刻
        self._proc_timer: QtCore.QTimer | None = None  # 后台进程轮询：跑着的时候每 3 秒问一次
        self._voice_failure_shown = ""
        self.speaker.failed.connect(self._note_voice_failure)
        # 又能念了：下次再出问题要重新提醒（别被上次那条挡掉）
        self.speaker.started.connect(lambda _text: self._voice_ok())
        self._reply_text = ""  # 本轮回答的原文，on_complete 时整段丢给 Speaker
        self._vocab_learning = False  # 后台正在重算识别词表：同一时刻只跑一个
        self._turn_stopped = False  # 这一轮被用户叫停：complete 到达时别再整段念
        self._voice_model_downloading = False  # 识别模型正在下：别因为再查一次又发一遍
        self._voice_model_tried = False  # 这一轮已经下过一次了：失败后别再反复拉 230 MB
        self._sending_attachments: list = []  # 点发送时带的附件：等 _send_user_text 整轮交给引擎
        if idle_video is not None:
            self.idle_player = IdleVideoPlayer(idle_video, self)
            self.idle_player.set_target_size(self.size())
            self.idle_player.frame_ready.connect(self._on_video_frame)
            # 定时器必须在界面线程（就是这里）建好并启动；收帧回调所在的线程不一定能用定时器
            timer = QtCore.QTimer(self)
            timer.setInterval(33)  # 30fps，略高于视频的 24fps，永远画最新的那一帧
            timer.timeout.connect(self._paint_latest_frame)
            timer.start()
            self.idle_player.failed.connect(lambda msg: say(f"[待机视频] {msg}"))

    # ---- 尺寸与位置 ----

    def _screen_limit(self) -> QtCore.QRect:
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1920, 1080)

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
        # 存档比最小宽还小（以前用更小的下限存过）就钳到最小宽，不要因此跳回默认大窗
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
        if isinstance(saved_pos, QtCore.QPoint) and self._screen_limit().intersects(
            QtCore.QRect(saved_pos, self.size())
        ):
            self.move(saved_pos)
        else:
            self._center_on_screen()

    def _save_geometry(self) -> None:
        self._settings.setValue("window/size", self.size())
        self._settings.setValue("window/pos", self.pos())
        self._settings.sync()

    # ---- 托盘与显示/隐藏 ----

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
        # 「切换中…」由引擎回的 switching 状态驱动，这里不自己造进度
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
            return  # 这期间又点了一次，或者清单早就回来了（apply_models 会把号顶掉）
        if self.chat_panel is not None:
            self.chat_panel.set_models_refreshing(False)
        say("[模型] 刷新没等到引擎回话（30 秒）")

    def request_new_session(self) -> None:
        """聊天窗里的「新会话」：让引擎重建 agent（重读 SOUL.md / 记忆），换个新会话。"""
        if self.client is None:
            say("[会话] 还没连上引擎，换不了新会话")
            if self.chat_panel is not None:
                self.chat_panel.show_error("还没连上引擎，换不了新会话")
            return
        # 换会话等于这件事结束了：正在念的话要掐掉，按钮也别卡在「停止」
        self._stop_voice()
        self._stop_process_watch()  # 旧会话的后台进程不再问（新会话连上时会重新开始）
        if self.chat_panel is not None:
            self.chat_panel.set_busy(False)
        say("[会话] 请求开新会话")
        self._new_session_pending = True
        self.client.create_session("")

    def open_model_settings(self) -> None:
        """「设置」：打开真正的设置窗口（填密钥 + 选模型），不是把下拉框弹一下。

        不管清单有没有可用项都打开——清单没加载完、一家都没配都能填。两个坑都在这几行里：
        窗口要用模态（`exec`），否则主界面那个无边框置顶窗会把它压在后面，用户看到的是
        「点了没反应」；位置要钉在屏幕中央，不能跟父窗口走（聊天窗贴着头像，可能一半在屏幕外）。
        `exec` 关掉之后才发 IPC——嵌套事件循环里 asyncio 是停的，在里面发消息等于丢包。
        """
        panel = self.chat_panel
        if panel is None:
            return
        from app.model_dialog import ModelDialog

        try:
            rows = self.model_choices()
        except Exception as exc:  # noqa: BLE001 - 按钮点了没反应是最糟的反馈，宁可当场说出来
            logger.exception("打不开「设置模型」：%s", exc)
            panel.show_error(f"打不开设置窗口：{exc}")
            return

        dialog = ModelDialog(
            rows,
            parent=None,
            current=str(self._models.get("current") or ""),
            engine_save=self.save_model_key,
        )
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = dialog.frameGeometry()
            geometry.moveCenter(screen.availableGeometry().center())
            dialog.move(geometry.topLeft())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            # 一次保存可能同时落了六页：按 saved_pages() 逐项收尾，哪页真存了才动哪页，没改的页不碰
            pages = dialog.saved_pages()
            # 文本模型那页的 Key 和切换已经在窗口里等回执等完了（engine_save），这里不再重发
            for note in dialog.save_notes():
                if self.chat_panel is not None:
                    self.chat_panel.show_error(note)
            for notice in dialog.save_notices():
                if self.chat_panel is not None:
                    self.chat_panel.show_info(notice)
            if "avatar" in pages:
                # 刚填/改了 Token 或模型：之前"用不了"的结论作废，下一句就去重试
                self._avatar_block_until = 0.0
                self._avatar_block_hint = "Vidu 积分不够，互动数字人暂时用不了：这句用纯语音念，音色不变。"
                self._avatar_ok()
            if "voice" in pages:
                # 刚改完语音设置：上一条"缺 Key"的提示已经不成立了，当场撤掉
                self._voice_ok()
            if "soul" in pages and self.chat_panel is not None:
                # 人设改完了：引擎下一轮自己发现 SOUL.md 变了并重拼提示词，这里只回一句确认。
                # 人设本身没改就不出声（一次保存带着四页，别每次都弹一句"没有改动"）；
                # 只有当时就停在人设页、专门去存的，才回一句"还是原来那份"。
                if dialog.soul_changed():
                    self.chat_panel.show_notice("soul", "人设已保存——下一句话就按新的来。", ttl_ms=CONFIRM_TTL_MS)
                elif dialog.saved_tab() == ModelDialog.TAB_SOUL:
                    self.chat_panel.show_notice("soul", "人设没有改动，还是原来那份。", ttl_ms=CONFIRM_TTL_MS)
            if "memory" in pages and self.chat_panel is not None and dialog.memory_changed():
                # 记忆和上限都是引擎建会话时读一次：这一轮已经在跑的不受影响，新会话才按新的来
                self.chat_panel.show_notice("memory", "记忆已保存——新建会话才带进上下文。", ttl_ms=CONFIRM_TTL_MS)
            if "workspace" in pages and self.client is not None:
                target = dialog.saved_workspace()
                # 没真给目录就当没这回事，绝不把工作区清空；目录没变也不提示（"已改为"却和原来一样是假消息）
                if target.strip() and not same_workspace(getattr(self.client, "workspace", None), target):
                    # 换工作区是会话级的：下一个新会话就在新目录里干活，当前这轮不动，也不用重启程序
                    self.client.set_workspace(target)
                    if self.chat_panel is not None:
                        # 走输入框上方状态条：8 秒自己消失，不进聊天记录（写进正文就永远撤不掉了）
                        self.chat_panel.show_notice(
                            "workdir", f"工作目录已改为 {target}，新建会话生效。", ttl_ms=CONFIRM_TTL_MS
                        )
            self._sync_voice_button()

    def _end_avatar_session(self) -> None:
        """闲置挂断：会话不再计费。引擎留着（预加载过就不必再载 dll）。"""
        if self._avatar is not None and (self._streamer is None or not self._streamer.playing):
            self._avatar.hangup()

    def _stop_voice(self) -> None:
        """停下所有发声路径：纯语音的声卡、互动的流播放，以及数字人那边正在渲染的嘴。"""
        self.speaker.stop()
        if self._streamer is not None:
            self._streamer.stop()
        if self._avatar is not None:
            self._avatar.interrupt()
        self._show_avatar(False)  # 被打断：立刻回待机画面，不留一张定住的嘴

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
        if avatar is not None and avatar.frames_since_speak < 3 and self._finish_waits < 10:
            self._finish_waits += 1
            QtCore.QTimer.singleShot(200, self._voice_finished)
            return
        self._finish_waits = 0
        if avatar is not None:
            logger.info("[数字人] 云端回传音频 %d 帧（%.1f 秒，含静音段），这一句实际结束时长为 %.1f 秒",
                        avatar.audio_frames, avatar.audio_frames * 0.01,
                        avatar.audio_seconds)
            if not self._reset_sent:
                self._reset_sent = True
                avatar.baseline_reset()  # 说完这一句：画面复位到闭口静息态，别把姿态留到下一句
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

    def preload_avatar(self) -> None:
        """语音互动档的预加载：载 dll + 建引擎，约 105ms、0 积分，不建会话。"""
        avatar = self._ensure_avatar()
        avatar.set_target_size(self.size())  # 启动时 resizeEvent 早于预加载，这里补一次
        why = avatar.preload()
        if why and self.chat_panel is not None:
            # 黄字：互动这一档暂时开不起来，纯语音照样能用——报红等于说「你装坏了」
            self.chat_panel.show_notice("avatar", f"互动数字人引擎没载起来：{why}", level="note")

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
            self.idle_player.pause() if on else self.idle_player.resume()

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
            # 配置问题重试一万次也一样：一句都别再多花一轮等待，改成"先纯语音念，配好下一句就重试"
            hint = f"互动数字人还没配好：{message.strip().splitlines()[0][:70]}（这句用纯语音念；填好 Token 不用重启）"
            self._avatar_block_hint = hint
            self._avatar_block_until = time.monotonic() + 3600
            pending, self._pending_speech = self._pending_speech, ""
            if pending:
                self.speaker.speak(pending)
        elif "insufficient" in low or "creditsinsufficient" in low:
            hint = "Vidu 积分不够了，互动数字人建不了会话。充值后重试，或把语音切到「纯语音」。"
            # 建会话就没戏了：这句别等 15 秒兜底，直接交给纯语音（音色就是纯语音那个），
            # 接下来 5 分钟不再试建会话。
            self._avatar_block_until = time.monotonic() + 300
            pending, self._pending_speech = self._pending_speech, ""
            if pending:
                self.speaker.speak(pending)
        elif "并发" in message:
            hint = "互动数字人并发已满：上一次会话还没关掉，等十几秒再发一次。"
        else:
            hint = "互动数字人没连上：" + message.strip().splitlines()[0][:80]
        self._warm_idle.stop()
        # 黄字：已经退回纯语音把这句话念了，不是崩了——红字会让人以为语音废了
        self.chat_panel.show_notice("avatar", hint, level="note")

    def _flush_pending_speech(self) -> None:
        """会话刚就绪：把等着的那句话接上念。"""
        if not self._pending_speech:
            return
        text, self._pending_speech = self._pending_speech, ""
        if self.chat_panel is not None:
            # 那条提示是 `show_notice("avatar", …)` 写进输入框上方状态条的，撤它得用同一个入口
            self.chat_panel.clear_status_notice("avatar")
        self._avatar_idle.stop()
        self._start_interactive(text)

    def _speak_reply(self, text: str) -> None:
        """按输入框那个三档开关决定怎么念；互动档失败就退回纯语音，别这轮没声音。"""
        if not any(ch.isalnum() for ch in text or ""):
            logger.info("[语音] 这句只有表情/符号，跳过朗读")
            return
        from app.chat_panel import _voice_mode

        mode = _voice_mode()
        if mode != "interactive" or self._avatar_blocked():
            logger.info("[语音] 走纯语音链路（档位 %s，互动档冷却=%s）", mode, self._avatar_blocked())
            if mode == "interactive" and self.chat_panel is not None:
                # 冷却中：把"为什么没画面"写清楚，别一声不响就纯语音（同一条提示，不会越堆越多）
                self.chat_panel.show_notice("avatar", self._avatar_block_hint, level="note")
            self.speaker.speak(text)
            return
        avatar = self._ensure_avatar()
        panel = self.chat_panel
        if avatar.state != "ready":
            # 会话还没建好（启动就是互动档、或者刚切过来）：先显示「正在接通」，
            # 建好后再念——不能先出声后补画面，那样嘴对不上。
            if panel is not None:
                panel.speech_preparing()
            if self._pending_speech:
                # 上一句还在等会话：它不能被这句话顶掉（那半句就永远没声音了），先交给纯语音念完。
                self.speaker.speak(self._pending_speech)
            self._pending_speech = text
            avatar.warm()  # 到这一步才建会话：按秒计费，用户按下发送才开始花
            self._warm_idle.start()  # 建了却没念成（比如服务端不吭声）：20 秒挂断，别空转
            QtCore.QTimer.singleShot(15_000, self._interactive_timeout)
            return
        self._avatar_idle.stop()
        self._start_interactive(text)

    def _interactive_timeout(self) -> None:
        """15 秒还没连上：这句退回纯语音，别让她一直没声音。"""
        if not self._pending_speech:
            return
        text, self._pending_speech = self._pending_speech, ""
        if self.chat_panel is not None:
            # 那条提示是 `show_notice("avatar", …)` 写进输入框上方状态条的，撤它得用同一个入口
            self.chat_panel.clear_status_notice("avatar")
        self._warm_idle.stop()
        if self.chat_panel is not None:
            # 黄字：已自动用纯语音念了这句话，属于「这一档今天开不了」，不是错误
            self.chat_panel.show_notice("avatar", "互动数字人没连上，这句先用纯语音念。", level="note")
        self.speaker.speak(text)

    def _start_interactive(self, text: str) -> None:
        """会话已就绪：启动定速推帧 + 边合成边播。"""
        from app.speech_interactive import StreamSpeaker

        avatar = self._ensure_avatar()
        if not avatar.begin_speak():
            self.speaker.speak(text)
            return
        avatar.reset_audio_frames()  # 这一句的云端时间轴从 0 开始算
        self._reset_sent = False  # 新的一句：收尾时还要再复位一次
        self._warm_idle.stop()  # 已经在说话了：后面由"说完 5 秒挂断"那个计时器接手
        self._show_avatar(True)
        panel = self.chat_panel
        if self._streamer is None:
            self._streamer = StreamSpeaker(
                on_chunk=avatar.feed_pcm,
                limit_fn=lambda: avatar.audio_timeline_bytes,  # 进度看云端念到哪了
                quiet_fn=lambda: avatar.quiet_seconds,         # 收尾看远端音频有没有声音
                idle_fn=lambda: avatar.audio_idle_seconds,     # 兜底：云端断流（连静音帧都不回）
                play_local=False,                              # 声音由 RTC 出（和画面同一路，天然同步）
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
        from app.speech_speaker import speakable

        clean = speakable(text)
        avatar.say_text(clean)  # type=10：让 Vidu 知道这句话在说什么（不改口型，不上动作匹配）
        logger.info("[语音] 走互动链路：%d 字（预计 %d 秒 ≈ %d 积分）",
                    len(clean), max(1, len(clean) // 6), max(1, len(clean) // 6))
        self._streamer.speak(clean)

    def _sync_voice_button(self) -> None:
        """设置里可能改过语音，回到聊天窗把输入框旁那个开关同步过来。"""
        if self.chat_panel is None:
            return

        self.chat_panel.refresh_voice_state()

    def model_choices(self) -> list[dict]:
        """把清单按供应商整理成对话框要的形状：变量名、配没配、有哪些模型。"""
        items = self.chat_panel.models() if self.chat_panel is not None else []
        return build_model_choices(items)

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
            keys[provider] = str(selection["key"]).strip()  # 只交了一家 Key 的老调用方
        typed = keys.get(provider, "")
        if not keys and not option_id:
            say("[模型] 没填密钥也没选模型，什么都没改")
            if done is not None:
                done(True, [])
            return
        self._save_ctx = {
            "done": done,
            "waiting": 0,
            "lines": [],
            "all_ok": True,
            "provider": provider,
            "switch_pending": bool(option_id),
            "stamp": time.monotonic(),
        }
        ctx = self._save_ctx
        # 先存别的家（只写 Key、不切模型），最后存「启用」那家：切换跟着它一起发出去
        for slug, text in keys.items():
            if slug == provider:
                continue
            ctx["waiting"] += 1
            say(f"[模型] 请求保存 {slug} 的密钥")
            self.client.save_key(slug, text)
        if typed:
            ctx["waiting"] += 1
            say(f"[模型] 请求保存 {provider} 的密钥并启用 {option_id}")
            self.client.save_key(provider, typed, option_id)
        elif option_id:
            say(f"[模型] 请求启用 {option_id}（密钥不动）")
            self.client.set_model(option_id)
        if ctx["waiting"] == 0 and not ctx["switch_pending"]:
            self._finish_save(True)

    def on_save_result(self, tag: str, payload: dict) -> None:
        """引擎回执：设置窗口点了「保存并启用」之后在等这两条（写 Key / 切模型）。"""
        ctx = getattr(self, "_save_ctx", None)
        if not ctx:
            return
        if time.monotonic() - float(ctx.get("stamp") or 0.0) > 9.0:
            return  # 已经报过超时的旧回执，不能算到下一次保存头上
        data = dict(payload or {})
        ok = bool(data.get("ok"))
        if tag.startswith("model.save_key"):
            slug = tag.split(":", 1)[1] if ":" in tag else str(data.get("slug") or "")
            ctx["waiting"] = max(0, int(ctx["waiting"]) - 1)
            if ok:
                ctx["lines"].append(f"{slug}：已写进 .env")
            else:
                ctx["all_ok"] = False
                ctx["lines"].append(f"{slug}：没存成——{data.get('message') or '引擎没说原因'}")
                if slug == ctx["provider"]:
                    ctx["switch_pending"] = False  # Key 都没写进去，引擎不会去切模型
        elif tag == "config.set:model":
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
        self._models_refresh_token += 1  # 清单到手：那条 30 秒兜底别再喊
        if self.chat_panel is not None:
            self.chat_panel.set_models(rows, current, switching=switching)
            if chosen is not None and chosen.get("available"):
                # 模型真的能用了：「还没有配置模型」那条提示该撤了（只撤这一条，别把别的提示一起冲掉）
                self.chat_panel.clear_status_notice("setup")
        if self._tray is not None:
            self._tray.setToolTip(f"Noesia Agent · {label}")
        if not switching:
            # 档位是按「当前模型 + 路由」算出来的，换完模型必须重问一次，否则菜单是旧的
            self.request_reasoning()
        root = getattr(self, "_cache_root", None)
        if root is not None:
            model_cache.save(root, {"current": current, "items": rows, "switching": switching})
        finish = getattr(self, "finish_boot", None)
        if callable(finish):
            finish()  # 清单到手就算开完机：窗口该露脸了，不再等下一件事
    def quit_app(self) -> None:
        """退出：先打一行日志再走 Qt 的优雅退出（引擎由 aboutToQuit 关掉）。"""
        say("[退出] 正在关闭窗口、IPC 与引擎…")
        QtWidgets.QApplication.quit()

    def _show_menu(self, pos: QtCore.QPoint) -> None:
        menu = self.build_menu()
        try:
            menu.exec(self.mapToGlobal(pos))
        finally:
            menu.deleteLater()   # 带 parent 的菜单 exec 完不销毁，每右键一次就多挂一套

    # ---- 聊天：唯一的文字输入入口 ----

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
            del self._pending_reviews[:-20]  # 只留最近 20 条：聊天窗一直不开也别把内存喂大

    def _flush_pending_reviews(self) -> None:
        if self.chat_panel is None or not self._pending_reviews:
            return
        for text in self._pending_reviews:
            self.chat_panel.append_review_summary(text)
        self._pending_reviews.clear()

    def open_chat(self) -> None:
        """打开（或聚焦）聊天窗；第一次调用才建界面。"""
        if self.chat_panel is None:
            self.chat_panel = ChatPanel(None)
            self.chat_panel.set_media_roots(media_roots())
            self.chat_panel.submitted.connect(self._send_user_text)
            self.chat_panel.model_chosen.connect(self.choose_model)
            self.chat_panel.models_refresh_requested.connect(self.refresh_models)
            self.chat_panel.new_session_requested.connect(self.request_new_session)
            self.chat_panel.model_settings_requested.connect(self.open_model_settings)
            self.chat_panel.approval_requested.connect(self.request_approval_mode)
            self.chat_panel.stop_requested.connect(self.stop_turn)
            self.chat_panel.attachment_picked.connect(self._attach_picked)
            self.chat_panel.clarify_answered.connect(self._answer_clarify)
            self.chat_panel.clarify_skipped.connect(self._skip_clarify)
            self.chat_panel.process_kill_requested.connect(self._kill_process)
            self.chat_panel.attachment_failed.connect(self._attachment_problem)
            # 语音那条进度：合成中 → 播（带进度）→ 收；上面那个停止只掐朗读
            self.chat_panel.speech_stop_requested.connect(self._stop_voice)
            # 换语音档位：那条语音提示跟着撤，并允许下次再提醒（否则切回纯语音后不再提示缺 Key）
            self.chat_panel.voice_mode_changed.connect(lambda _mode: self._voice_ok())
            self.speaker.preparing.connect(self.chat_panel.speech_preparing)
            self.speaker.started.connect(lambda _text: self.chat_panel.speech_playing())
            self.speaker.progress.connect(self.chat_panel.speech_progress)
            self.speaker.finished.connect(self.chat_panel.speech_done)
            self.speaker.stopped.connect(self.chat_panel.speech_done)
            self.speaker.failed.connect(lambda _msg: self.chat_panel.speech_done())
            self.chat_panel.reasoning_requested.connect(self.request_reasoning)
            # 语音输入：松手后录的那段交给引擎识别，结果从 wire.voice_text 回来
            self.chat_panel.voice_transcribe_requested.connect(self._transcribe_voice)
            # 按下说话先掐朗读：不掐的话，麦克风收的是她自己正在念的这句
            self.chat_panel.voice_record_started.connect(self._stop_voice)
            # 进语音模式就问一句识别模型在不在：不在当场开始下，别等人说完才报错
            self.chat_panel.voice_input_opened.connect(self._ensure_voice_model)
            if self._models:  # 面板晚建：把已有清单补进去
                self.chat_panel.set_models(
                    list(self._models.get("items") or []), str(self._models.get("current") or "")
                )
        self._flush_pending_reviews()  # 窗还没开时收到的回顾总结，这会儿补上
        self.paint_cached_models()  # 引擎还没起来时先按上次的清单画，别让按钮空着
        self.request_history()  # 面板有了就要一次（没连上会等连上后重试）
        self.chat_panel.show_near(self.frameGeometry())
        self._greet_if_needed()
        self.request_approval_mode()  # 面板刚建，问一次引擎现在的档位
        self.request_reasoning()  # 当前推理强度：七档清单是固定的，这里只要「现在哪一档」

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
            self._voice_model_tried = True   # 齐了：这一轮不用再下了
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
            # 走到这儿只可能是「下完了又查一次还是不齐」：标志不能再吊在 True 上，
            # 否则之后再进语音模式会永远被挡住，既不重试也不说话，用户只能去重启。
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
            # 这一轮已经拉过一次了：别每进一次语音模式就重下 230 MB，但要说清现状
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
            ttl_ms=0,  # 常驻到有结果为止
        )
        wire = getattr(self, "client", None)
        if wire is not None:
            wire.download_voice_model()

    def _transcribe_voice(self, path: str) -> None:
        """录好的一段交给引擎识别。引擎还没起好就当场说清，别让球卡在「识别中」。"""
        wire = getattr(self, "client", None)
        panel = self.chat_panel
        if wire is None:
            if panel is not None:
                panel.voice_transcribe_failed("识别引擎还没起好，稍等一下再试", path)
            return
        wire.transcribe_audio(path)

    def stop_turn(self) -> None:
        """叫住这一轮：真停由引擎的 interrupt 做，界面只发一条 stop。"""
        self._turn_stopped = True  # 界面已经写了「已停止」：这一轮的 complete 到达时别再整段念
        self._stop_voice()  # 正在念的那句也停下，不然嘴还在动
        if self.chat_panel is not None:
            self.chat_panel.stop_streaming()  # 手上这段先收尾，之后飘回来的字不再写进气泡
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
            self._reasoning_pending = False  # 用户自己点的那一档：以他的选择为准，不用等回报
            self._reasoning_fresh = False  # 让回包照他点的这档画（引擎若夹档，以回包为准）
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
            return  # 已经就绪了：30 秒的兜底定时器这时才轮到，别去动已经收起的动画
        if getattr(self, "_boot_failure", ""):
            return  # 第一个原因是准的；后来的兜底超时别把它盖掉
        self._boot_failure = text
        splash = getattr(self, "boot_splash", None)
        if splash is not None:
            splash.mark_failed(text)
        say(f"[启动] {text}")
        logger.warning("启动失败：%s", text)
        panel = getattr(self, "chat_panel", None)
        if panel is not None:
            panel.show_error(text)
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

    def paint_cached_models(self) -> bool:
        """先按上次的快照把按钮和菜单画出来。

        引擎是独立进程，冷启动要四五秒，`models` 回包会迟到；等的这几秒按钮上什么都没有，
        看着就像「模型加载慢」。快照只是显示用，引擎的清单一到立刻覆盖。
        """
        if self.chat_panel is None:
            return False
        data = model_cache.load(self._cache_root)
        if not data:
            return False
        items = [item for item in data.get("items") or [] if isinstance(item, dict)]
        if not items:
            return False
        self.chat_panel.set_models(items, str(data.get("current") or ""))
        return True

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
        self.open_chat()  # 名字卡跟着聊天面板走，面板不在就没处放
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
            self._approval_dialog = None
            if self.client is not None:
                # 回帧用 reply_id（`srq-...`）；request_id 是引擎的业务 id，只能用于显示/去重
                reply_id = getattr(ask, "reply_id", None) or getattr(ask, "request_id", "")
                self.client.answer_approval(reply_id, choice)
            if self.chat_panel is not None:
                label = {
                    "once": "允许一次",
                    "session": "本次会话都允许",
                    "always": "以后都允许",
                }.get(choice, "拒绝")
                # 历史里要留得住「当时问的是什么」：命令原文和说明都进这一行，
                # 不然翻记录只剩一句「你选择：允许一次」，出事时说不清是谁让跑的
                detail = "\n".join(
                    part for part in (getattr(ask, "description", ""), getattr(ask, "command", "")) if part
                )
                self.chat_panel.show_tool(
                    "approval", f"你选择：{label}", ok=choice != "deny", body=detail
                )

        self._approval_dialog = ApprovalDialog(ask, answer)
        self._approval_dialog.show_near(self.geometry())

    def _send_user_text(self, message: str, source: str = "typed") -> None:
        panel = self.chat_panel
        if panel is not None and getattr(panel, "pending_clarify", None) and panel.pending_clarify():
            # 她在等一个回答（没有选项那种问题）：这句话就是答案，不发出去当新一轮
            if panel.answer_clarify_text(message):
                return
        if self.client is None:
            if self.chat_panel is not None:
                self.chat_panel.show_error("引擎还没起来，这句话没发出去")
            return
        # 新一轮开始：先说下一句，别让上一句还在念；按钮同时变「停止」
        self._reply_text = ""  # 上一轮失败/被叫停残留的正文不能拼进这一轮
        self._turn_stopped = False
        self._stop_voice()
        if self.chat_panel is not None:
            # 新一轮开始：上一轮的红字不能让它在「正在调用…」旁边继续挂着（内容还是上一轮的错，
            # 看着像这次也失败了）。要在发出之前撤，不是等第一个字回来。
            self.chat_panel.clear_status_notice("turn")
            self.chat_panel.set_busy(True)
        items, self._sending_attachments = self._sending_attachments, []
        logger.info("[附件] 这一轮带 %d 个附件", len(items))
        self._start_process_watch()
        if source == "typed":
            # 只有打字才算干净语料：语音识别出来的文字是模型听出来的，可能带错字。
            # 记不上只是词表不更新，绝不能拦着这句话发出去。
            try:
                from app import voice_vocab

                voice_vocab.remember(message)
            except Exception:  # noqa: BLE001
                logger.debug("[词表] 这条语料没记上", exc_info=True)
        # 打字排在后面跑（queued）：忙的时候点发送不该把你正在等的那一轮掐掉。
        # 语音是插话（不带 queued）：引擎把它当纠正递进当前这一轮，她接着往下说。
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
            # 学不出来只是词表不更新，识别照常；不能因为这个给用户弹错误
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

    def on_delta(self, text: str) -> None:
        """引擎的 message.delta：直接进气泡，界面不解释内容。"""
        if text:
            self._reply_text += text
        panel = self.chat_panel
        if panel is not None and text:
            panel.clear_status_notice("turn")  # 新一轮真的开始吐字了：上一轮的报错提示可以撤
            panel.append_chunk(text)

    def on_complete(self, payload: dict) -> None:
        """message.complete：正文早就按增量进了气泡，这里只收尾。"""
        panel = self.chat_panel
        if panel is not None:
            # 失败轮到提示卡片（输入框上方那条，`status_stack`），不进气泡：气泡只说模型的话。
            # 不带 ttl——没解决就该一直看得见，下一轮开始或成功时再撤。
            if str(payload.get("status") or "") == "error":
                # 引擎在失败轮里：`text` 是已经流出来的正文（有就原样带），真正的失败原因在
                # `error`，并用 `partial` 标出「有正文被保留」。所以有 partial 时必须读 error，
                # 否则红字里会出现她说到一半的那句话，用户看不到哪里出错。
                if payload.get("partial"):
                    failure = str(payload.get("error") or payload.get("text") or "").strip()
                else:
                    failure = str(payload.get("text") or payload.get("error") or "").strip()
                if failure:
                    panel.show_notice("turn", failure, level="error")
            else:
                panel.clear_status_notice("turn")
            panel.end_assistant()
            panel.set_busy(False)  # 这一轮结束：按钮从「停止」变回「发送」
        self._refresh_processes()  # 立刻查一次：这一轮里跑完的那个，行要从 ▶ 变 ✓
        reply, self._reply_text = self._reply_text, ""
        if reply and not self._turn_stopped:  # 按过停止：正文已经按增量显示过，别再整段念
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
        # 顺序要紧：先把这一轮收掉，再放开闸。反过来会把它刚排上队、
        # 正要发出去的那一条的「在跑」状态又抹回去（按钮显示成「发送」，引擎其实在跑）。
        panel.set_busy(False)
        panel.settle_turn()

    def on_tool_event(self, event: dict) -> None:
        """tool.start / tool.complete：标题、副标题、计数、正文全部由模型层算（`app/tool_view`）。

        界面不再自己拼摘要：`build_tool_view()` 负责算，
        失败与否、显示哪几段、哪个字段当副标题，都由它决定，这里只把结果交给聊天窗。
        """
        panel = self.chat_panel
        if panel is None:
            return
        kind = str(event.get("kind") or "")
        if self._turn_stopped and kind in {"tool.generating", "tool.progress"}:
            # 这一轮已经被叫停：引擎可能还飘回来几条进度，别再挂到输入框上方
            # （挂了就没人撤——那个工具的 tool.start 永远不会来了）
            return
        if kind == "tool.generating":
            # 引擎在拼这次工具调用：先在输入框上方说一句「马上要调谁了」，
            # 工具真的开跑（tool.start）就撤掉——桌面端也是这一行的前身
            name = str(event.get("name") or "").strip()
            if name and panel is not None and panel.tool_progress_enabled():
                panel.set_activity(f"正在调用 {name}…")
            return
        if kind == "tool.progress":
            # 跑着的时候的实时预览（MCP、长命令）：换成同一行的字，不新增行
            name = str(event.get("name") or "").strip()
            # 引擎这条帧给的是 `message`（不是 `preview`），有百分比就一起说
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
            panel.set_activity("")  # 工具真的开始了：那句「正在调用」让位给真正的工具行
        if str(event.get("name") or "") == "clarify":
            # 提问那张卡由引擎的 `clarify` 请求自己画（问题 + 可点的选项），不再多一行「正在提问」
            return
        if str(event.get("name") or "") == "setup_beat" or str(event.get("toolset") or "") == "onboarding":
            # 引导握手由界面那张中文名字卡负责，聊天窗里不再重复一行工具行。
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
            # 正文右上角那个复制按钮复制什么，也由模型层决定
            copy_payload=tool_copy_payload(part, view),
            # 子代理要那张能看进度的卡片：派发时画任务，跑完补结果，`subagent.*` 事件往上刷
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
        """某条请求被引擎拒了：红色错误行（这是真失败，跟通知分开）。"""
        if not message:
            return
        self._reply_text = ""  # 这一轮废了：残留正文别带进下一轮
        if self.chat_panel is not None:
            self.chat_panel.show_error(message)
            self.chat_panel.set_busy(False)  # 这一轮已经废了，别让按钮停在「停止」
        say(f"[引擎][错误] {message}")

    def on_reasoning_delta(self, text: str) -> None:
        if self.chat_panel is not None and text:
            self.chat_panel.show_reasoning(text)

    def on_reasoning_block(self, text: str) -> None:
        """不流式的供应商一次给全的思考（引擎 `reasoning.available`）。"""
        if self.chat_panel is not None and text:
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
        if "reasoning_effort" in info:  # 恢复的会话，引擎直接告诉我们这场是哪档
            self.on_reasoning_reported(
                str(info.get("reasoning_effort") or ""), str(info.get("reasoning_effort_wire") or "")
            )
        else:
            # 新建会话的回包不带档位：问一次引擎自己记下的那档（顺带拿到这条路由的 wire），
            # 否则面板会停在上一场路由的说明上。
            self._reasoning_fresh = False
            self.request_reasoning()
        panel = self.chat_panel
        requested = self._new_session_pending
        self._new_session_pending = False
        # 用户点了「新会话」：这一屏就该清掉（不看屏幕里有没有东西）。
        # 引擎自己回来的 `session.create`（开窗、重连）只在屏幕还空着时清——那时工具行
        # 已经画在窗口里了，清一次等于把刚显示的东西抹掉。
        if panel is not None and should_clear_on_session_ready(
            result, requested=requested, has_transcript=panel.has_transcript()
        ):
            panel.clear_conversation("新会话就绪" if requested else "重连后仍为空")

    def on_history(self, rows: list) -> None:
        if self.chat_panel is not None and rows:
            self.chat_panel.set_history(rows)
            say(f"[历史] 铺回 {len(rows)} 条")

    def on_models(self, items: list, current: str) -> None:
        self.apply_models(items, current)
        self.refresh_reasoning_menu()  # 换过模型：「能不能关掉思考」跟着当前模型走
        if current:  # 已经有能用的模型了：补问一次引导（先开窗后配密钥的人靠这一步）
            self._greet_if_needed(force=True)

    def on_config_value(self, key: str, value: str) -> None:
        panel = self.chat_panel
        # 聊天窗是懒建的：它还没开时也得把档位记进 wire，否则之后每次建会话都回落默认档，
        # 界面上勾着的和实际用的对不上。界面更新才依赖 panel。
        if key in {"approval_mode", "approvals.mode"} and value:
            if panel is not None:
                panel.set_approval_mode(value)
        elif key == "reasoning":
            self._reasoning_current = value
            if self.client is not None:  # 新建会话要把这档带过去，否则新会话回到默认「中」
                self.client.set_reasoning_effort(value)
            if panel is not None and not self._reasoning_pending and not self._reasoning_fresh:
                # pending 期间引擎回的是全局默认（恢复的会话还没建 agent）；已经报过真档时也不许盖回去。
                # 照着画就成了「显示中、实际低」。
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
            self._reasoning_wire = ""  # 上一场的说明不能带到这一场
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
        self._reasoning_fresh = True  # 引擎报的是这场的真档：后面的 config.get 默认值不许覆盖
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
        if self._reasoning_pending:  # 还不知道这场的档位：重摆菜单也不能顺手画个默认值上去
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
            reply_id=params.get("reply_id"),  # 回帧用的 `srq-...`
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

        if self._name_dialog is not None:  # 已经有张卡开着，别叠第二张
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

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == QtCore.Qt.Key_Escape:
            # 有托盘时隐藏（可找回），没托盘时隐藏就等于失联，只能退出
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
            # 按尺寸判，不按「区域是不是 None」判：算出来是空的（开头几帧全透明）时
            # 原来那个判据会每帧重算一遍整幅轮廓，几万次 pixelColor + united，窗口直接卡住。
            self._rebuild_hit_region()  # 第一帧到手就按她的轮廓定好「点得中」的范围
        # 收帧只存帧，画由 __init__ 里那个定时器负责：这扇透明置顶窗上，从收帧回调里
        # 调 update() 或 repaint() 实测都不会落到屏幕上（送进 2262 帧、paintEvent 只跑 20 次），
        # 而从 QTimer 里调 repaint() 每次都生效。

    @QtCore.Slot()
    def _paint_latest_frame(self) -> None:
        """把最新一帧同步画到屏幕上（定时器推着走，不依赖收帧时机）；没有新帧就不白画。"""
        if self._frame is not None and self._frame is not self._painted_frame:
            self._painted_frame = self._frame
            self._request_paint()

    def _request_paint(self) -> None:
        """同步把窗口画一遍。单独一层是为了测试能拦下来数「画了几次」。"""
        self.repaint()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
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

    # ---- 她说 ily / ♥ 时飘的心（桌面端 vibe-hearts） ----

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
        self._hit_timer.start(120)  # 拖动缩放时每秒几十个 resize 事件：合并成停手后算一次
        if self.idle_player is not None:
            self.idle_player.set_target_size(self.size())  # 缩放在抠像之前做，省算力
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
        # 视频帧是 `QImage`（没有 `toImage`），静态图是 `QPixmap`——两种都要收
        if isinstance(source, QtGui.QPixmap):
            image = source.toImage()
        elif isinstance(source, QtGui.QImage):
            image = source
        else:
            image = QtGui.QImage()
        region = hit_region(image, self.size())
        self._hit_region = None if region.isEmpty() else region
        self._hit_size = size

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt 命名)
        """透明的地方不挡鼠标：点在她身上才算点她，空白处交给下面的窗口（比如桌面图标）。

        无边框透明窗**整块矩形**都会吃鼠标事件，透明像素也吃——所以拖到桌面图标上面之后，
        那一片图标就点不动了（用户实际报的毛病）。Windows 的 WM_NCHITTEST 回 HTTRANSPARENT，
        系统就把这次点击交给下面的窗口。
        """
        if sys.platform == "win32" and self._hit_region is not None and event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_WinMsg)).contents
                if msg.message == WM_NCHITTEST:
                    # 用 Qt 的坐标：`QCursor.pos()` 是逻辑坐标，跟 `mapFromGlobal()` 同一套。
                    # 原来从 lParam 拆（物理像素）在缩放 100% 的屏上碰巧一致，一挪到 125%/150% 的屏上
                    # 就整体错位：判她身上是透明的、判透明处是她身上的，于是窗口在那块屏上点不动、抓不到边。
                    if not self._hit_region.contains(self.mapFromGlobal(QtGui.QCursor.pos())):
                        return True, HTTRANSPARENT
            except Exception:  # 结构体读不出来就当普通窗口：宁可挡着也别把整扇窗变成点不到
                logger.debug("WM_NCHITTEST 解析失败，按普通窗口处理", exc_info=True)
        return super().nativeEvent(event_type, message)

    def _follow_approval(self) -> None:
        """贴身的卡片跟着人物走（审批、引擎起不来都是同一套位置规则）。"""
        for attr in ("_approval_dialog", "_takeover_dialog"):
            dialog = getattr(self, attr, None)
            if dialog is not None and dialog.isVisible():
                dialog.move_near(self.geometry())

    def ask_takeover(self, message: str) -> None:
        """数据根被命令行会话占着：贴人物下方问一句，同意后才动手。

        同意后按「会话 Ctrl+C 退出 → 杀掉残留进程 → 启动」做：那台会话只要还开着，下一句话
        就会再拉一条 gateway 把数据根占回去，所以先请它退出才有意义。全过程一个命令行工具都不起
        （`app/winproc.py`），不会闪黑窗口；每一步都写日志，聊天窗里也留一行说明。
        """
        from app.engine_prompt import EngineProblemDialog
        from app.engine_takeover import occupied_pid, takeover

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
            try:
                ok, note = takeover(wire, True, pid)
            except Exception as exc:  # noqa: BLE001 - 接管里出错必须留下痕迹，不能悄悄什么都不做
                logger.exception("接管时出错")
                tell(f"接管出错：{exc}", error=True)
                return
            tell(note, error=not ok)

        # 卡片先弹出来——弹卡片这一步不摸进程表、不做任何可能失败的事。
        # 「谁在挡路」等用户点了同意再查（查错了也只是后面那步报错，卡片本身一定看得见）。
        try:
            self._takeover_dialog = EngineProblemDialog(message, action, answered, self)
            self._takeover_dialog.show_near(self.geometry())
            logger.info("接管：已弹卡片等用户确认")
        except Exception as exc:  # noqa: BLE001 - 卡片弹不出来也得说一声，不能静默
            logger.exception("接管的卡片没能弹出来")
            tell(f"没能弹出接管卡片：{exc}", error=True)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.LeftButton:
            return
        edge = edges_at(event.position().toPoint(), self.size())
        if edge:
            # 按在边缘/角上：进入缩放，别同时开始拖动
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
            # 拖左边/上边时，右下角保持不动（不然窗口会整体跑偏）
            if self._resize_edge & QtCore.Qt.Edge.LeftEdge:
                new_geo.moveRight(old.right())
            if self._resize_edge & QtCore.Qt.Edge.TopEdge:
                new_geo.moveBottom(old.bottom())
            self.setGeometry(new_geo)
            return
        if self._drag_offset is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(pos - self._drag_offset)
            return
        # 没按键：只是移动鼠标，按命中的边换指针形状
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
        self._stop_process_watch()  # 窗口都要没了：那个 5 秒轮询没有意义，别再问引擎
        self._save_geometry()
        self.release_audio()
        super().closeEvent(event)

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
    root = root.rstrip("\\/").lower()
    if not root:
        return False
    return root in " ".join(cmdline or []).lower() or (exe or "").lower().startswith(root)


def _reap_browser_daemons() -> int:
    """收掉残留的 browser 守护进程（连它拉起的浏览器一起），返回收掉几个。

    调用点两处：启动时（上次被强杀留下的）和退出时（这次自己留下的）。一处不清，
    用户下次想删 `data` 还是会撞上“正在使用”。
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 - 没 psutil 就跳过，别为了收尾把退出弄崩
        logger.debug("没有 psutil，跳过 browser 守护进程清理")
        return 0
    root = str(Path(__file__).resolve().parents[2])
    victims = []
    for proc in psutil.process_iter(["pid", "cmdline", "exe"]):
        try:
            info = proc.info
            if _our_browser_daemon(info.get("cmdline") or [], info.get("exe") or "", root):
                victims.append(proc)
        except (psutil.Error, OSError):  # 进程在枚举时没了 / 拿不到信息
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
        # 同进程内（例如测试）对端要靠事件循环才能回话，这里推一把
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
        sock.waitForReadyRead(20)

    sock.disconnectFromServer()
    if sock.state() != QtNetwork.QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(200)
    return acked


def start_instance_server(window) -> QtNetwork.QLocalServer | None:  # noqa: ANN001
    """监听单实例通道，让后来启动的进程能显示/关闭已有窗口。"""
    QtNetwork.QLocalServer.removeServer(INSTANCE_NAME)  # 上次崩溃留下的残留要先清掉
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

            conn.write(b"ok\n")  # 回执：让对方确认命令已被处理，而不是发完就猜
            conn.flush()

        conn.readyRead.connect(read_command)
        if conn.bytesAvailable():  # 数据可能在我们挂好信号之前就到了，别漏
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
            return path  # 已经装过（第一次启动脚本时先装过一次），别重复挂 handler 把每行写两遍
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
    if _CRASH_LOGGING_INSTALLED:  # 重复装会把每条日志写两遍
        return
    _CRASH_LOGGING_INSTALLED = True
    log = logging.getLogger("app.crash")

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):  # Ctrl+C 是正常收场，不算崩溃
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


def _provider_label(item: dict, provider: str, model: str) -> str:
    """供应商显示名。

    引擎给的 label 通常是「显示名 · 模型名」，只去掉末尾那一个 ` · <model>`：
    原来用 `split(" · ")[0]`，供应商名自己带「 · 」就会被切坏。
    """
    raw = str(item.get("label") or provider)
    suffix = f" · {model}"
    return raw[: -len(suffix)] if raw.endswith(suffix) else raw


def main() -> int:
    # 最先做：把随包的 bash/git/ffmpeg 挂进本进程 PATH。之后这个进程自己起的 ffmpeg
    # （语音解 mp3）、以及拉起的引擎子进程都继承这份 PATH —— 用户机器装没装都一样。
    from .engine_wire import prepend_bundled_paths

    prepend_bundled_paths(PROJECT_ROOT)
    _setup_gui_log(PROJECT_ROOT)  # 先装日志，load_config 里的提示才留得下来
    _install_crash_logging()  # 再把「没人接住的异常 / Qt 的警告」也接到这个日志上
    cfg, resolver = _load_avatar_config()
    # 先钉住引擎：`NOESIA_HOME` 指向项目内的 data/NoesiaHome，顺便把引擎代码目录记下来，
    # 这样「设置」在任何时候点开都认得 noesia_cli 和 `.env.example`。
    catalog.pin_engine_home(cfg, resolver.root)
    front = resolver.resolve(cfg.avatar.dir) / cfg.avatar.reference.front
    idle_path = resolver.resolve(cfg.avatar.idle_video)
    _setup_gui_log(resolver.root)  # 配置里若指了别的 root，换到那边；同一路径则什么都不做

    app = QtWidgets.QApplication(sys.argv)
    # 主界面是无边框的 `Qt.Tool` 窗（工具窗默认不参与「最后一个窗口关闭就退出」），聊天窗是它的
    # 子控件。于是任何一个真窗口关掉（比如「设置模型」对话框点了保存/取消）都会被算成
    # 「最后一个窗口关了」→ 整个程序退出。退出只有一条路：右键 → 退出 Noesia Agent。
    app.setQuitOnLastWindowClosed(False)
    if notify_existing_instance("show"):
        say("[单实例] 已有一个 Noesia 在运行，窗口已唤回（本进程退出）")
        return 0
    # 上次要是被强杀（任务管理器 / 断电），引擎拉起的 browser 守护进程会留下来，
    # 它把 data/workspace 当工作目录：不先收掉，用户退出后删 data 会报“正在使用”。
    _reap_browser_daemons()

    # 先只放加载动画：人物窗口等引擎连上、模型清单到手才露面（那之后开聊天窗就是即时的）
    from .boot_splash import BootSplash  # 依赖 PySide6：放进函数里，缺绑定时仍走友好提示

    splash = BootSplash()
    splash.set_step("正在准备界面…")
    splash.show()
    window = FloatingAvatar(front, cfg.ui.floating, idle_path if idle_path.is_file() else None)
    window._cache_root = resolver.root  # 模型快照跟着配置根走，不写死仓库路径
    window.boot_splash = splash
    has_tray = window.setup_tray()
    window.instance_server = start_instance_server(window)  # 挂在窗口上，防被回收
    if window.idle_player is not None:
        if window.idle_player.start():
            say(f"[待机视频] 已开始播放：{idle_path}")
            # 启动 6 秒后报一次：解码了多少帧、抠像多快（核对 24fps 预算）
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
        window.preload_avatar()  # 0 积分：只载引擎；会话要等用户按下发送才建
    say(f"[托盘] {'已启用：左键显示/隐藏，右键菜单退出' if has_tray else '不可用：Esc 只能退出'}")
    say(f"[菜单] {' / '.join(a.text() for a in window.build_menu().actions() if a.text())}")

    # 引擎生命周期归前端：没在跑就自己拉起，退出时关掉（外部起的常驻引擎不动）
    # 根目录用 resolver 定下的那个，不要用 project_root()：pin_engine_home 会把 NOESIA_HOME
    # 指向引擎家目录，那之后 project_root() 返回的是引擎家目录，引擎日志会写到 data/NoesiaHome/data/runtime/ 里
    # 引擎配置：每次启动把 `<home>/config.yaml` 补齐成"全量默认 + 用户改动"的完整文件。
    # 用户改过的键保留（引擎升级换默认值也不会冲掉），没动过的跟新默认，新键自动补。
    # 工作区：配置里写过 terminal.cwd（界面上的「工作目录」）就用它，没写过才用 app.json 的默认值。
    # 只认默认值的话，用户改了目录、重启一次就被自己的默认值盖回去，看着像"设了没用"。
    workdir = workspace_from_config(Path(resolver.root) / cfg.engine.home,
                                    Path(resolver.root) / cfg.engine.workdir)
    try:
        ensure_runtime(root=Path(resolver.root), home=Path(resolver.root) / cfg.engine.home,
                       workdir=workdir)
        # 工作区要显式写进引擎配置：`terminal.cwd: .` 会让 agent 的工作目录跟着进程走，
        # 进程一在源码目录里起，工具就往仓库写文件。用户自己改过 cwd 的，这里不覆盖。
        overrides: dict = {}
        overrides["terminal"] = {**(overrides.get("terminal") or {}), "cwd": str(workdir)}
        sync_engine_config(
            Path(resolver.root) / cfg.engine.core_path,
            Path(resolver.root) / cfg.engine.home,
            overrides,
        )
    except Exception:  # noqa: BLE001 - 写不进去也要能开窗，引擎侧按默认跑
        logger.exception("同步引擎配置失败")

    wire = EngineWire(Path(resolver.root), workspace=workdir)
    window.client = wire

    def _after_ready(_payload: dict) -> None:
        """引擎就绪后再问东西：模型清单、上次的会话、两个档位。"""
        say("[引擎] 已就绪")
        window._engine_ready = True
        engine_restarts["n"] = 0  # 连上了：重连次数归零
        if window.chat_panel is not None:
            # 「正在重连（第 N 次）」是当前状态，连上了就该消失——不能再留一行在聊天记录里
            window.chat_panel.clear_status_notice("link")
        window._history_asked = False  # 重连后要重新问一次最近会话，才能 resume 回来
        window.boot_step("正在读取模型清单…")
        wire.request_models()
        window.request_history()  # 走同一个入口：问过就别再问第二次
        wire.config_get("approval_mode")
        wire.config_get("reasoning")
        wire.request_display()  # display 段：思考段/工具行画不画，照引擎的配置

    def _after_recent(result: dict) -> None:
        sid = str(result.get("session_id") or "")
        if sid:
            wire.resume_session(sid)
        else:
            # 不传 cwd：默认落在工作区。传项目根会把 agent 的当前目录钉在源码树里，工具会往仓库写东西。
            wire.create_session()

    wire.ready.connect(_after_ready)
    wire.attachment_failed.connect(window._attachment_problem)  # 引擎拒收：撤掉条上那一条
    wire.most_recent.connect(_after_recent)
    wire.session_ready.connect(window.on_session_ready)
    wire.history.connect(window.on_history)
    wire.delta.connect(window.on_delta)
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
    wire.review_summary.connect(window.on_review_summary)  # 后台自我回顾的总结（记忆/技能）
    wire.notice_show.connect(window.on_notice_show)
    wire.notice_clear.connect(lambda key: window.chat_panel and window.chat_panel.clear_status_notice(key))
    wire.clarify.connect(window.on_clarify_request)
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
    wire.setup_beat.connect(window.on_setup_beat)
    wire.key_saved.connect(lambda _slug: window._greet_if_needed(force=True))
    wire.models.connect(window.on_models)
    wire.save_result.connect(window.on_save_result)
    wire.config_value.connect(window.on_config_value)
    wire.reasoning_pending.connect(window.on_reasoning_pending)
    wire.reasoning_reported.connect(window.on_reasoning_reported)
    wire.reasoning_wire.connect(window.on_reasoning_wire)
    wire.display_config.connect(window.on_display_config)

    from app.engine_takeover import looks_like_occupied

    def _on_failed(message: str) -> None:
        say(f"[引擎] {message}")
        window.boot_failed(message)
        # 数据根被命令行留下的后台进程占着：等人开口问一句（只问一次），
        # 同意就结束它再重试启动——否则只能干等它空闲超时（默认 600 秒）。
        try:
            offer = looks_like_occupied(message)
        except Exception:  # noqa: BLE001 - 判不出来就不问，照样把界面打开
            logger.exception("判断要不要弹接管卡片时出错")
            offer = False
        if not getattr(window, "_takeover_offered", False) and offer:
            window._takeover_offered = True
            QtCore.QTimer.singleShot(600, lambda: window.ask_takeover(message))

    wire.failed.connect(_on_failed)
    wire.closed.connect(lambda: say("[引擎] 通道已关闭"))
    wire.closed.connect(lambda: window.chat_panel and window.chat_panel.set_busy(False))
    # 引擎没了要说出来：不然工具行还在计时、按钮还是「发送」，看着像在跑，其实一个字都不会再来
    wire.closed.connect(lambda: window.chat_panel and window.chat_panel.abandon_turn("引擎断开"))

    # 引擎进程没了就自己重连（桌面端也是断了就重连，不是在原地等人重开程序）：
    # 1s / 2s / 4s 三次，连上后计数在 _after_ready 里归零；真连不上才让用户重开。
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
            # 状态条一条就够：重连 6 次就是 1 行字更新 6 次，不能往聊天记录里刷 6 行灰字。
            # 蓝色=正在进行（连上会被 `_after_ready` 撤掉）。
            window.chat_panel.show_notice(
                "link", f"与引擎的连接已断开，正在重连（第 {attempt + 1} 次）…", level="busy"
            )
        say(f"[引擎] 连接断了，{delay}ms 后第 {attempt + 1} 次重连")
        QtCore.QTimer.singleShot(delay, wire.start)

    wire.closed.connect(_reconnect_engine)

    # 启动之前先把命令行里开着的 noesia 会话请走：先替它按 Ctrl+C，等不到才结束（静默，不弹窗口）。
    # 不先关掉，那台会话会拉起/占着同一个数据根，界面才刚起来就跟它打架。
    try:
        from app.engine_wire import close_console_sessions, quit_process

        notes = close_console_sessions(Path(resolver.root))
        # 会话退了，它拉起的后台 gateway 不一定跟着走（实测常留着，攥住数据根 600 秒）。
        # 那本来就是我们刚关掉的那台的尾巴，顺手收掉——不收就得再走一遍失败 + 卡片。
        owner = wire.blocking_owner()
        if owner is not None:
            _ok, note = quit_process(owner)
            notes.append(note)
        for note in notes:
            say(f"[引擎] {note}")
            if window.chat_panel is not None:
                window.chat_panel.show_info(note)
    except Exception:  # noqa: BLE001 - 关不掉也要继续启动，别让这一步挡住窗口
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
        except Exception:  # noqa: BLE001 - 退出路径上什么都别再抛
            logger.debug("退出时收起窗口失败", exc_info=True)
        quitting["v"] = True  # 是我自己在退：别当成掉线去重连
        wire.shutdown()
        # 引擎停了，它拉起的 browser 守护进程还活着（工作目录就是 data/workspace），
        # 不一起收掉的话用户退出后删 data 会报“正在使用”。
        _reap_browser_daemons()

    app.aboutToQuit.connect(_shutdown_engine)
    # 退出这条路（右键 → 退出）不经过 closeEvent，麦克风得单独在这儿还回去
    app.aboutToQuit.connect(window.release_audio)
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
