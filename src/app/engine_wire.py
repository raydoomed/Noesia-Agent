"""Qt 前端连引擎的薄客户端：走这个 home 的 gateway 的 `/api/ws`，说引擎自己的 JSON-RPC。

设计底线：这里只做运输和分发——把界面上的动作翻译成引擎的方法调用，把引擎的事件翻译成
Qt 信号。会话、模型、工具、审批的判定全部留在引擎里，前端不复制一份逻辑。

进程模型：网关是引擎自己的常驻 owner（`gateway run`），外壳不拉进程、不挂父子关系，靠
`GatewayLink`（后台线程里的 WebSocket）接进去；关掉界面只是断开连接，网关按空闲自己退。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import MutableMapping
from pathlib import Path

from app.gateway_link import GatewayLink
from app.status_stack import CONFIRM_TTL_MS

STOP_TIMEOUT = 8

import PySide6.QtCore as QtCore

logger = logging.getLogger(__name__)

# 左栏只认「你自己聊出来的会话」：桌面（gui）、老版本遗留的 local、终端 cli/tui 都算。
# 定时任务、看板、子代理、工具、机器人跑出来的会话一律不列。
CHAT_SOURCES = frozenset({"local", "gui", "desktop", "cli", "tui"})
# 面板对引擎报的身份：引擎的 SURFACES 只认 gui/tui/cli/acp/tool/oneshot（桌面就是 gui），
# 不报的话会话会被当成 cli，思考这类「本地窗口才显示」的东西整轮都不会发上来。
SESSION_SOURCE = "gui"


def chat_sessions(rows) -> list:
    """清单里属于「用户自己聊」的那几场（引擎给的是最新在前）。"""
    return [
        row for row in rows or []
        if isinstance(row, dict) and str(row.get("source") or "").lower() in CHAT_SOURCES
    ]


def _engine_home_path(root: "Path | str") -> Path:
    """数据根：跟 `start()` 写进环境变量的是同一个值（环境变量优先）。"""
    value = os.environ.get("NOESIA_HOME")
    return Path(value) if value else Path(root) / "data" / "NoesiaHome"


def bundled_bash(root: "Path | str") -> Path | None:
    """随包的 bash（`vendor/git`，Git for Windows 的 PortableGit，跟仓库一起分发）。

    为什么要有它：引擎的工具（`terminal`、`read_file`、`search_files`）全靠 bash 跑命令，
    而引擎自己在 Windows 上不会装 bash——`pm/shell.py` 的候选表里，`NOESIA_GIT_BASH_PATH`
    排第一，其次是 Program Files/LocalAppData 里的 Git，最后才是 PATH。用户机器上没装 Git
    就一个都找不到，直接报 “No shell found. Noesia needs bash”。

    所以带一份进来、起引擎时把变量指过去：跟用户装没装 Git 无关，也不用联网。
    找不到就返回 None，让引擎退回它自己的候选表（开发机上有系统 Git 照样能用）。
    """
    candidate = Path(root) / "vendor" / "git" / "usr" / "bin" / "bash.exe"
    return candidate if candidate.is_file() else None


def bundled_bash_path_dirs(root: "Path | str") -> list[Path]:
    """随包 git 里要挂进 PATH 的目录，顺序就是 MSYS 自己的顺序：coreutils → git.exe → shim。

    光把 bash 指过去不够：MSYS 的 bash 只按 PATH 找命令，用户机器上没装 Git 时 PATH 里
    没有 `usr\\bin`，`ls`/`grep`/`sed`/`git` 全是 `command not found`（实测 rc=127）。
    """
    base = Path(root) / "vendor" / "git"
    return [d for d in (base / "usr" / "bin", base / "mingw64" / "bin", base / "cmd") if d.is_dir()]


def bundled_tool_dirs(root: "Path | str") -> list[Path]:
    """随包里所有要挂进 PATH 的目录：git 三件套 + ffmpeg/bin。

    ffmpeg 也得挂：`app/speech_stream.py` 是拿裸名字 `ffmpeg` 起管道解 Edge TTS 的 mp3，
    Qt 主进程找不到就是 `FileNotFoundError`（语音档一句都读不出来）。
    """
    dirs = bundled_bash_path_dirs(root)
    ffmpeg = Path(root) / "vendor" / "ffmpeg" / "bin"
    if ffmpeg.is_dir():
        dirs.append(ffmpeg)
    return dirs


def with_bundled_path(env: MutableMapping[str, str], root: "Path | str") -> None:
    """把随包目录挂到给定环境字典的 PATH 最前（已在里面的不重复挂）。

    只动传进来的这份环境，不碰系统环境变量——用户自己的 PATH 一个字节都不改。
    """
    dirs = [str(d) for d in bundled_tool_dirs(root)]
    if not dirs:
        return
    lowered = {d.lower() for d in dirs}
    old = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    kept = [p for p in old.split(os.pathsep) if p and p.lower() not in lowered]
    env["PATH"] = os.pathsep.join(dirs + kept)


def prepend_bundled_paths(root: "Path | str") -> list[Path]:
    """启动最早处调用：把随包目录挂进本进程 PATH，之后拉起的 Qt 侧进程、引擎子进程都继承。"""
    with_bundled_path(os.environ, root)
    return bundled_tool_dirs(root)


_PROVIDER_WAIT_RE = re.compile(
    r"^(?:⏳|⚠|↻|⚙)\s*(?:(?:still\s+)?waiting on|loading|processing prompt|"
    r"no (?:output|response)|model returned|rate limited|"
    r"provider (?:overloaded|temporarily unavailable))",
    re.IGNORECASE,
)


def provider_wait_text(text: str) -> str:
    """是「在等供应商」就说出来（进底部状态行），是转圈表情就返回空串。"""
    value = (text or "").strip()
    return value if _PROVIDER_WAIT_RE.match(value) else ""


def _approval_from_event(params: dict, payload: dict) -> dict:
    """把引擎推的 `approval.request` 事件折成审批卡认得的那一份。

    事件里 `choices` 是引擎允许的档位（`once`/`deny`，可选 `session`/`always`；smart 判死的
    只有前两个），`prompt_id` 是审批队列自己的 id——回票（`approval.respond`）按它认，
    不是帧 id。`execution_generation` 也得原样带回去，引擎拿它当栅栏。
    """
    choices = [item for item in (payload.get("choices") or []) if isinstance(item, str)]
    prompt_id = str(payload.get("prompt_id") or "")
    return {
        "request_id": prompt_id,
        "prompt_id": prompt_id,
        "command": str(payload.get("command") or ""),
        "description": str(payload.get("description") or ""),
        "allow_session": "session" in choices,
        "allow_permanent": "always" in choices,
        "execution_generation": payload.get("execution_generation"),
        "timeout_s": payload.get("timeout_s"),
        "session_id": str(params.get("session_id") or ""),
    }


def _clarify_from_event(params: dict, payload: dict) -> dict:
    """把引擎推的 `clarify.request` 事件折成澄清卡认得的那一份（`questions` 一条）。

    事件里一个 prompt 只带**一个**问题：`question` / `choices` / `multi_select` 是平铺字段，
    没有 question_id；回票走 `clarify.respond`（`{session_id, execution_generation, prompt_id,
    answer}`，实测回 `{"status": "resolved"}`）。
    """
    question = str(payload.get("question") or "").strip()
    prompt_id = str(payload.get("prompt_id") or "")
    questions = [{
        "qid": "",
        "question": question,
        "choices": [str(item) for item in (payload.get("choices") or []) if isinstance(item, str)],
        "multi_select": bool(payload.get("multi_select")),
    }] if question else []
    return {
        "request_id": prompt_id,
        "prompt_id": prompt_id,
        "questions": questions,
        "execution_generation": payload.get("execution_generation"),
        "timeout_s": payload.get("timeout_s"),
        "session_id": str(params.get("session_id") or ""),
    }


def ui_locale() -> str:
    """界面的语言标记，交给引擎挑引导文案。

    界面文案全是中文写死的，所以这里就是 `zh-CN`——不跟系统语言走：系统是英文、界面还是中文，
    报 en-US 只会让引擎用英文开场。
    """
    return "zh-CN"


def _model_payload(option_id: str) -> dict:
    """界面内部的 `slug:model` → `session.mutate operation="model"` 的 payload。

    canonical 换模型把供应商和模型名分开给（payload 只认 `model`/`provider`/`confirm`/`reasoning`
    四个键），不拼 CLI 那种 `--provider` 字符串。
    """
    slug, sep, name = str(option_id or "").partition(":")
    if sep and slug and name:
        return {"provider": slug, "model": name}
    return {"model": str(option_id or "")}


def _provider_row(providers, provider: str, model: str = "") -> dict:
    """引擎回的那一行：先按 slug 认，认不出再按「这家有当前模型」认。"""
    want = str(provider or "").strip()
    for row in providers or []:
        if isinstance(row, dict) and str(row.get("slug") or "") == want:
            return row
    for row in providers or []:
        if isinstance(row, dict) and row.get("is_current") and model and model in (row.get("models") or []):
            return row
    return {}


def _provider_title(providers, provider: str) -> str:
    row = _provider_row(providers, provider)
    return str(row.get("name") or row.get("label") or provider or "")


def _provider_warning(providers, provider: str) -> str:
    return str(_provider_row(providers, provider).get("warning") or "")


def _provider_usable(providers, provider: str, model: str) -> bool:
    row = _provider_row(providers, provider, model)
    return bool(row.get("authenticated"))


def _flatten_options(result: dict) -> tuple[list[dict], str]:
    """把引擎的 `model.options` 摊成界面用的平表：一家供应商的每个模型一行。

    字段名照引擎的来（`slug` / `models` / `authenticated` / `warning`），只做形状转换，
    不补默认值、不改语义——能不能用是引擎说的，界面只照画。
    """
    providers = result.get("providers") if isinstance(result, dict) else None
    rows: list[dict] = []
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        slug = str(provider.get("slug") or "")
        models = [name for name in (provider.get("models") or []) if isinstance(name, str) and name]
        if not models:
            continue
        title = str(provider.get("name") or slug)
        for name in models:
            rows.append(
                {
                    "id": f"{slug}:{name}",
                    "label": f"{title} · {name}",
                    "backend": slug,
                    "model": name,
                    "available": bool(provider.get("authenticated")),
                    "reason": str(provider.get("warning") or ""),
                    "caps": dict((provider.get("capabilities") or {}).get(name) or {}),
                    "current": bool(provider.get("is_current")) and name == str(result.get("model") or ""),
                }
            )
    provider = str(result.get("provider") or "") if isinstance(result, dict) else ""
    model = str(result.get("model") or "") if isinstance(result, dict) else ""
    current = f"{provider}:{model}" if provider and model else ""
    ids = {str(row.get("id") or "") for row in rows}
    if current not in ids:
        hit = next((str(row.get("id") or "") for row in rows if row.get("current")), "")
        if not hit and model:
            hit = next(
                (str(row.get("id") or "") for row in rows
                 if str(row.get("model") or "") == model and row.get("available")),
                "",
            )
        current = hit or current

    ids = {str(row.get("id") or "") for row in rows}
    if current and current not in ids:
        rows.append(
            {
                "id": current,
                "label": f"{_provider_title(providers, provider)} · {model or current}",
                "backend": provider or str(current).split(":", 1)[0],
                "model": model or str(current).split(":", 1)[-1],
                "available": _provider_usable(providers, provider, model),
                "reason": _provider_warning(providers, provider) or "这一家的模型清单这次没取到，点下面的「刷新模型」重试",
                "caps": {},
                "current": True,
            }
        )
    return rows, current


NO_PROVIDER_HINT = (
    "还没有可用的模型。点右上角「设置」→「密钥设置」填上供应商的 API Key（比如 DeepSeek 就填 "
    "DEEPSEEK_API_KEY）并点确定，再到「模型设置」选一个模型点「保存并启用」，这条提示就会自己消失。"
)


def looks_unconfigured(message, code=None) -> bool:
    """引擎在说「没配供应商」：认 code，也认几种英文原话（不同层给的说法不一样）。"""
    if str(code or "") in {"no_provider_configured", "provider_not_configured"}:
        return True
    low = str(message or "").lower()
    return (
        "not connected to any ai provider" in low
        or "no provider is set up" in low
        or "no provider is configured" in low
    )


class EngineWire(QtCore.QObject):
    """引擎进程 + JSON-RPC 通道。业务侧只连信号、只调方法。"""

    ready = QtCore.Signal(dict)
    session_ready = QtCore.Signal(dict)
    # 历史走 REST 分页：resume 只报条数（`defer_history` 回 `messages=[]`），界面按页取。
    history_count = QtCore.Signal(int)
    history_page = QtCore.Signal(list, bool, str)
    history_failed = QtCore.Signal(str, str)
    history_stale = QtCore.Signal(str)
    user_row_stamped = QtCore.Signal(int)
    sessions_panel = QtCore.Signal(list)
    session_changed = QtCore.Signal(str, str)
    config_value = QtCore.Signal(str, str)
    reasoning_reported = QtCore.Signal(str, str)
    reasoning_pending = QtCore.Signal(bool)
    reasoning_wire = QtCore.Signal(str)
    attachment_failed = QtCore.Signal(str, str)
    display_config = QtCore.Signal(dict)
    models = QtCore.Signal(list, str)
    provider_rows = QtCore.Signal(list, str)
    provider_rows_failed = QtCore.Signal(str)
    save_result = QtCore.Signal(str, dict)
    delta = QtCore.Signal(str)
    interim_text = QtCore.Signal(str)
    reasoning_delta = QtCore.Signal(str)
    reasoning_done = QtCore.Signal(str)
    reasoning_block = QtCore.Signal(str)
    agent_step = QtCore.Signal(int)
    provider_wait = QtCore.Signal(str)
    review_summary = QtCore.Signal(str)
    tool = QtCore.Signal(dict)
    subagent = QtCore.Signal(dict)
    completed = QtCore.Signal(dict)
    turn_settled = QtCore.Signal(str)
    turn_started = QtCore.Signal()
    status = QtCore.Signal(str)
    status_text = QtCore.Signal(str, str)
    clarify_expired = QtCore.Signal(str)
    approval = QtCore.Signal(dict)
    setup_beat = QtCore.Signal(dict)
    window_read = QtCore.Signal(dict)
    key_saved = QtCore.Signal(str)
    notice = QtCore.Signal(str)
    notice_show = QtCore.Signal(dict)
    notice_clear = QtCore.Signal(str)
    clarify = QtCore.Signal(dict)
    secret_prompt = QtCore.Signal(dict)
    reaction = QtCore.Signal(str)
    message_reaction = QtCore.Signal(dict)
    approvals_cancelled = QtCore.Signal(list)
    request_cancelled = QtCore.Signal(str, str)
    processes = QtCore.Signal(list)
    voice_text = QtCore.Signal(str, str)
    voice_failed = QtCore.Signal(str, str)
    voice_model = QtCore.Signal(dict)
    cron_result = QtCore.Signal(str, dict)
    cron_changed = QtCore.Signal()
    setup_needed = QtCore.Signal(str)
    onboarding_ended = QtCore.Signal(str)
    greet_resolved = QtCore.Signal()
    error = QtCore.Signal(str)
    paused = QtCore.Signal(object)
    paused_discard_failed = QtCore.Signal()
    failed = QtCore.Signal(str)
    closed = QtCore.Signal()
    cron_delivery = QtCore.Signal(str)

    def __init__(self, project_root: Path, parent: QtCore.QObject | None = None,
                 workspace: Path | str | None = None) -> None:
        super().__init__(parent)
        self._root = Path(project_root)
        if workspace is None:
            raise ValueError("引擎必须有工作区目录：workspace 不能为空")
        self._workspace = Path(workspace)
        self._link = GatewayLink(self)
        self._link.frame.connect(self._on_frame_text)
        self.history_stale.connect(self._resume_stale_session)
        self._link.ready.connect(self._on_link_ready)
        self._link.failed.connect(self._on_link_failure)
        self._link.gone.connect(self._on_link_gone)

        self._voice_seq = 0
        self._voice_jobs: dict[int, str] = {}
        self._next_id = 0
        self._pending: dict[int, str] = {}
        self._session_id = ""
        self._session_ids: set[str] = set()
        self._pending_switch = ""
        self._pending_switch_token = ""
        self._pending_reasoning = ""
        self._models_after_settings = False
        self._reasoning_effort = ""
        self._onboarding_started = False
        self._history_retry = 0
        self._history_recover = 0
        self._stored_id = ""
        self._revision = 0
        self._generation = 0
        self._paused: dict | None = None
        self._paused_sending = False
        self._resolving: dict = {}
        self._pending_rows: list[dict] = []
        # 引擎按连接记「这个客户端回答 server→client 请求」：`client.capabilities` 落地之前问
        # `onboarding.state`，答案里的 `eligible` 一定是 false（引擎把它当 CLI/远端）。开场问早
        # 一步就是黄字常驻、引导不起。所以开场先排队，capabilities 发出去再问。
        self._caps_ready = False
        self._greet_waiting = False
        self._greet_probe = False
        self._rewind_text = ""
        self._rewind_row = 0
        self._switch_payload: dict = {}
        self._switch_token = ""
        self._reasoning_level = ""
        self._sent: dict[int, dict] = {}
        self._seen_requests: set[str] = set()
        self._harvest_at = 0.0
        self._onboarding_run = ""
        self._retried: set[int] = set()
        self._replay: list[dict] = []
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        # 想在飞的那条挂载落地之后再补挂的会话（开场新建的那一场撞上启动挂载时用）。
        self._attach_after = ""
        self._attach_want = ""
        self._recover_rounds = 0
        self._attach_timeouts = 0
        self._pending_text = ""
        self._pending_turns: list[tuple[str, list, bool]] = []
        # 引擎当「会话事件」推过来的待答项（审批 / 澄清）：prompt_id → 那一代。
        # 这两类不走帧 id，回票得靠 `approval.respond` / `clarify.respond`，见各自的方法。
        self._event_approvals: dict[str, dict | None] = {}
        self._event_clarifies: dict[str, dict | None] = {}
        self._turn: dict | None = None
        self._turn_seq = 0
        self._attach_names: dict[int, tuple[str, bool]] = {}
        self._started = False


    def _engine_home(self) -> Path:
        """数据根（和写进网关环境变量的是同一个值）。"""
        return _engine_home_path(self._root)

    def persist_default_model(self, option_id: str) -> bool:
        """没有会话时把「启用这个模型」落到 profile 默认（`config.yaml` 的 `model.*`）。

        引擎的 `config.set model` 在没有会话时直接拒（4001：桌面这条只能靠会话承载），
        而首装引导要等这个默认模型才算还没跑完——不落盘的话「保存并启用」只是个假动作：
        引擎一直回 `eligible=false`，引导永远不起。所以这一笔由界面写进配置（跟「工作目录」
        同一个写入口），下一次 `onboarding.state` 就能看到选的模型。
        """
        slug, sep, name = str(option_id or "").partition(":")
        if not sep or not slug or not name:
            return False
        try:
            from settings import load_settings
            from backends.catalog import engine_core
            from app.engine_config import set_engine_config
            cfg, resolver = load_settings(root=Path(self._root))
            set_engine_config(engine_core(cfg, resolver.root), self._engine_home(),
                              {"model.provider": slug, "model.default": name})
            logger.info("没有会话：默认模型已写进配置 %s / %s", slug, name)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("写默认模型失败：%s", option_id)
            return False

    def _reset_transport_state(self) -> None:
        """重新拉引擎之前，把上一趟的传输状态清干净（第一次启动时它们本来就是空的）。

        新后端里没有上一趟那些 rid 的上下文，所以攒着待补发的请求、附件登记一律作废；
        `_recovering` 要是崩在救回途中卡住，会把后面的恢复永久关掉，必须一起复位。
        运行时 id 只属于刚退出的那个后端进程；存储 id（`_stored_id`）跨重启不变，留着。
        """
        self._pending.clear()
        self._sent.clear()
        self._retried.clear()
        self._replay.clear()
        # 新连接要重新 advertise，否则开场问又落到「引擎当我是 CLI」的那一版答案上。
        self._caps_ready = False
        self._greet_waiting = False
        self._greet_probe = False
        self._attach_want = ""
        self._attach_after = ""
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._recover_rounds = 0
        self._attach_timeouts = 0
        self._attach_names.clear()
        self._session_id = ""
        self._pending_text = ""
        self._pending_switch = ""
        self._pending_switch_token = ""
        self._pending_reasoning = ""
        self._models_after_settings = False
        self._pending_turns.clear()
        self._turn = None
        self._paused = None
        self._paused_sending = False
        self._resolving = {}
        self._pending_rows = []

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._reset_transport_state()
        engine = self._root / "src" / "engine"

        # 网关是引擎自己的常驻 owner：外壳只把「哪个数据根、哪个工作区」告诉它，不挂父子关系
        # （不传 NOESIA_PARENT_PID），关掉界面网关还在，重开接回同一场。
        os.environ["NOESIA_HOME"] = str(self._engine_home())
        os.environ["NOESIA_DESKTOP"] = "1"
        os.environ["TERMINAL_CWD"] = str(self._workspace)
        bash = bundled_bash(self._root)
        if bash is not None:
            os.environ["NOESIA_GIT_BASH_PATH"] = str(bash)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._link.configure(
            home=self._engine_home(), engine=engine, workspace=self._workspace
        )
        self._link.start()
        logger.info("引擎连接：/api/ws（home %s，工作区 %s）", self._engine_home(), self._workspace)

    def shutdown(self) -> None:
        """断开连接；网关归引擎自己，按空闲自己退，这里不杀进程。"""
        logger.info("[引擎] 主动 shutdown（调用方：%s）", " <- ".join(
            frame.rsplit("\n", 1)[-1].strip() for frame in traceback.format_stack(limit=3)[:-1]
        ))
        self._link.stop(STOP_TIMEOUT)

    def is_running(self) -> bool:
        return self._link.is_running()

    @property
    def session_id(self) -> str:
        return self._session_id


    @property
    def workspace(self) -> Path:
        """当前工作区（新会话的 cwd 就是它）。"""
        return self._workspace

    def set_workspace(self, path: str | Path) -> None:
        """换工作区：下一个新会话就在这个目录里干活（当前会话不动，也不用重启程序）。"""
        text = str(path or "").strip()
        if not text:
            raise ValueError("工作目录不能是空字符串")
        self._workspace = Path(text)

    def create_session(self, cwd: str | None = None) -> bool:
        """发一帧 session.create，返回「这一帧真发出去了没有」。

        False 有两种来源：已经有一场 attach 在飞（启动 / 重连 / rewind 的 resume），或者写不出去。
        调用方必须看这个返回值——「新会话」那条链上把没发出去的当成发了，那条 attach 的回包就会被
        认成「新会话就绪」，清掉整屏再提示一句「已开新会话」，用户其实还在原来那一场。
        """
        if self._attaching:
            return False
        self._attaching = True
        self._attach_since = time.monotonic()
        self._attach_want = ""
        params = {"cwd": cwd or str(self._workspace), "cwd_explicit": True, "source": SESSION_SOURCE}
        if self._reasoning_effort:
            params["reasoning"] = self._reasoning_effort
        rid = self._call("session.create", params, tag="session.create")
        sent = rid in self._sent
        if not sent:
            self._attaching = False
            self._attach_since = 0.0
        self.reasoning_pending.emit(False)
        return sent

    # 一页历史多少条：跟 Hermes 桌面版一个数（`LATEST_SESSION_MESSAGES_LIMIT = 120`）。
    HISTORY_PAGE = 120

    def resume_session(self, session_id: str) -> bool:
        """挂回一场已有会话；返回「这一帧真发出去了没有」（同 `create_session`）。"""
        if self._attaching:
            return False
        # 切到别场才算换：同一场重挂（引导里引擎换 id、历史 404 重挂）得把认过的 id 留着。
        switching = bool(session_id) and session_id != self._stored_id
        self._stored_id = session_id or self._stored_id
        self._attaching = True
        self._attach_since = time.monotonic()
        # 这一条挂载想要的会话：迟到的旧挂载回包不能把 `_session_id` 拉回上一场
        # （开场新建会话后，启动时那条 resume 的回包晚到，之后打的字就进了旧聊天）。
        self._attach_want = session_id or ""
        # 切到别场：集合当场上只剩这一场，上一场的帧（后台 cron、别的聊天）从这一刻起丢，
        # 不然它们会画在刚打开的面板上。
        if switching:
            self._session_ids = {session_id}
        self.reasoning_pending.emit(True)
        rid = self._call("session.resume", {"session_id": session_id, "source": SESSION_SOURCE,
                                            "omit_messages": True, "defer_history": True},
                         tag="session.resume")
        sent = rid in self._sent
        if not sent:
            self._attaching = False
            self._attach_since = 0.0
        return sent

    def fetch_history(self, offset: int = 0, limit: int | None = None, tag: str = "older") -> None:
        """取一页历史（REST）：`offset` 从最新一条往前数。

        resume 不再背整份 transcript（`omit_messages`），历史由界面按页要——这样开窗成本跟
        会话多长无关，滚到顶再往前取。

        `limit` 给 `None`/0 就取一整页（`HISTORY_PAGE`）；补洞时界面按「还差几条」给，
        这里再夹到一整页以内——洞只有几条就不白取一整页。

        `tag` 原样跟着这一页回来：`"older"` 是往上翻的下一页，`"fill"` 是补洞要的那一段。
        界面上两条路要的处理完全不同（接在最前面 / 接在窗口尾巴上再判够没够到洞底），
        靠位置猜不可靠，就让它自己说。
        """
        sid = self._stored_id or self._session_id
        base = str(getattr(self._link, "api_origin", "") or "")
        if not sid:
            # 还没有会话（引导那场还没开）：没有历史可取，这不是错，不该往聊天窗顶上挂红字。
            return
        if not base:
            # 通道刚断或刚起，HTTP 口子还没报上来：等一会儿再取，别把「还没连上」当错误弹给人。
            if self._history_retry < 6:
                self._history_retry += 1
                QtCore.QTimer.singleShot(
                    600, lambda: self.fetch_history(offset, limit, tag))
                return
            self.history_failed.emit("引擎还没连上，历史没取到", tag)
            return
        self._history_retry = 0
        page = int(limit) if limit else 0
        if page <= 0 or page > int(self.HISTORY_PAGE):
            # 0/没给 = 一整页；要得比一整页多也按一整页来（页大小只在这里定）。
            page = int(self.HISTORY_PAGE)
        threading.Thread(target=self._fetch_history_job, args=(base, sid, int(offset), page, tag),
                         name=f"history-page-{tag}", daemon=True).start()

    def _rest_json(self, url: str, timeout: int, method: str = "GET",
                   body: dict | None = None) -> dict:
        """带本机票据取一条 REST JSON（历史、运行详情、会话改动共用一口子），失败原样抛给对方判。"""
        headers = {"Accept": "application/json"}
        ticket = self._link.native_http_ticket()
        if ticket:
            # 本地桌面的 REST 凭证：网关按「本机同用户」认这张一次性票据，不认裸请求。
            headers["x-noesia-gateway-ticket"] = ticket
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _fetch_history_job(self, base: str, session_id: str, offset: int, page: int, tag: str) -> None:
        url = (f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages"
               f"?limit={int(page)}&offset={offset}&order=latest&include_compacted=true")
        try:
            payload = self._rest_json(url, timeout=15)
        except Exception as exc:  # noqa: BLE001 — 取不到就报给界面，别把开窗卡在这儿
            code = getattr(exc, "code", "")
            if str(code) == "404":
                # 404 只有一种意思：库里没有这个 id。要么界面还拿着重启前的运行时 id
                # （引擎重启后全作废），要么这一场还没落库——两种都不是「取不到」。
                if not self._stored_id:
                    logger.info("[历史] 库里还没有这一场（%s），先不铺", session_id[:12])
                    return
                if not self._history_recover:
                    self._history_recover = 1
                    logger.warning("[历史] 会话 %s 在库里找不到，按存储 id 重挂", session_id[:12])
                    self.history_stale.emit(self._stored_id)
                    return
            logger.warning("[历史] %s 没取到（%s）：%s", url, tag, exc)
            self.history_failed.emit(
                f"历史没取到：{type(exc).__name__}{f' {code}' if code else ''}", tag
            )
            return
        raw = payload.get("messages") or []
        rows = self._page_rows(raw)
        pagination = payload.get("pagination") or {}
        returned = int(pagination.get("returned") or len(raw))
        limit = int(pagination.get("limit") or page)
        logger.info("[历史] 取回 offset=%s（%s），%s 条", offset, tag, len(rows))
        self._history_recover = 0
        # 够不够一页由服务端说：回来的条数少于这一页的 limit，就是再往前没有了。
        self.history_page.emit(rows, returned >= limit, tag)

    def read_session_messages(self, session_id: str, max_rows: int = 2000) -> list:
        """整份读一场会话的历史：详情窗看一次运行的内容，只读、不碰当前会话。

        和 `fetch_history` 同一个口子，但按 `order=oldest` 从头往后翻（服务端单页上限 500），
        取不到就抛，界面把原因写给人看。
        """
        sid = str(session_id or "").strip()
        base = str(getattr(self._link, "api_origin", "") or "")
        if not sid:
            raise ValueError("这条运行记录没有会话 id")
        if not base:
            raise RuntimeError("引擎还没连上")
        rows: list = []
        while len(rows) < max_rows:
            url = (f"{base}/api/sessions/{urllib.parse.quote(sid, safe='')}/messages"
                   f"?limit=500&offset={len(rows)}&order=oldest&include_compacted=true")
            try:
                raw = self._rest_json(url, timeout=20).get("messages") or []
            except urllib.error.HTTPError as exc:
                if int(exc.code or 0) == 404:
                    raise LookupError("库里找不到这场运行的会话") from exc
                raise
            rows.extend(self._page_rows(raw))
            if len(raw) < 500:
                break
        return rows[:max_rows]

    def _resume_stale_session(self, stored_id: str) -> None:
        """历史 404：按存储 id 重挂那场会话（`history_stale` 从取数线程发过来，这里是主线程）。"""
        if stored_id:
            self.resume_session(stored_id)

    @staticmethod
    def _page_rows(raw: list) -> list:
        """REST 回来的一页库行 → 界面行。

        引擎那边已经按显示规范投影过：压缩摘要换成显示版（`display_content`）、
        脚手架提示标成 `hidden`、插话换成用户原话——这里只补它不做的那部分：
        工具行拿参数拼 80 字预览（`agent.display.build_tool_preview`，和 resume 同一个函数），
        再把字段名统一成界面认的。
        """
        args_by_call = {}
        for row in raw:
            for call in (row.get("tool_calls") or ()) if isinstance(row, dict) else ():
                fn = (call or {}).get("function") or {}
                call_id = (call or {}).get("id")
                if not (call_id and fn.get("name")):
                    continue
                try:
                    args_by_call[call_id] = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args_by_call[call_id] = {}
        out = []
        for row in raw:
            if not isinstance(row, dict) or str(row.get("display_kind") or "") == "hidden":
                continue
            role = str(row.get("role") or "")
            stamp = row.get("timestamp")
            try:
                stamp = float(stamp) if stamp not in (None, "") else None
            except (TypeError, ValueError):
                stamp = None
            if role == "tool":
                name = str(row.get("tool_name") or "tool")
                args = args_by_call.get(row.get("tool_call_id")) or {}
                out.append({
                    "role": "tool",
                    "text": "",
                    "display_kind": "",
                    "row_id": row.get("id"),
                    "timestamp": stamp,
                    "name": name,
                    # 有结果原文时正文走原文，80 字预览没人读，不必算。
                    "context": "" if row.get("content") is not None else EngineWire._tool_preview(name, args),
                    "args": args,
                    # 结果原文一起带上：展开行要铺正文（`tool_view.model.tool_detail_text`），
                    # 只给 80 字预览的话点开是空的。
                    "content": row.get("content"),
                    "tool_call_id": row.get("tool_call_id"),
                    "labels": None,
                    "reasoning": "",
                    # 成败跟着结果走：REST 只给原文，`{"error": …}` 就是失败，历史行要画成 ✗。
                    "ok": not EngineWire._tool_row_failed(row.get("content")),
                })
                continue
            shown = row.get("display_content")
            out.append({
                "role": role,
                "text": EngineWire._content_text(shown if shown is not None else row.get("content")),
                "display_kind": str(row.get("display_kind") or ""),
                "row_id": row.get("id"),
                "timestamp": stamp,
                "name": "",
                "context": "",
                "tool_call_id": None,
                "labels": None,
                "reasoning": str(row.get("reasoning") or row.get("reasoning_content") or ""),
                # 反应（`react_to_message`）落在这一格里：历史重铺要从它把 emoji 画回气泡。
                "display_metadata": row.get("display_metadata") or {},
            })
        return out

    @staticmethod
    def _tool_row_failed(content) -> bool:
        """这一行的结果是不是报错：引擎把失败写成 `{"error": …}`，其余都是正常输出。"""
        if isinstance(content, str) and content.lstrip().startswith("{"):
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                return False
            return isinstance(parsed, dict) and bool(parsed.get("error"))
        return False

    @staticmethod
    def _tool_preview(name: str, args: dict) -> str:
        """工具行的参数预览：引擎的 `build_tool_preview`，和 resume 那一路同一个函数。"""
        try:
            from agent.display import build_tool_preview

            return build_tool_preview(name, args, max_len=80) or ""
        except Exception:  # noqa: BLE001 — 预览拼不出来就不显示，别拖垮整页
            for value in (args or {}).values():
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
            return ""

    @staticmethod
    def _content_text(content) -> str:
        """库里的 `content` 可能是字符串，也可能是分块列表：取出能显示的文字。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif part.get("type") == "image_url" or part.get("image_url"):
                        parts.append("[图片]")
            return "".join(parts)
        if content is None:
            return ""
        return str(content)

    def request_session_list(self, limit: int = 100) -> None:
        """左栏那份清单：引擎的 `/api/sessions`（直接读 state.db 那条 REST），只读。

        不走 `session.list scope=stored`：那条是「接续老库遗留对话」的挑会话接口，
        正常会话在那里一条都出不来（行带 `chat_id` 就被判成不是遗留行）。
        """
        base = str(getattr(self._link, "api_origin", "") or "")
        if not base:
            return
        threading.Thread(target=self._fetch_sessions_job, args=(base, int(limit)),
                         name="session-list", daemon=True).start()

    def _fetch_sessions_job(self, base: str, limit: int) -> None:
        # 这个接口单页上限 100，要更多得翻页；左栏只铺最新一页，够用。
        cap = max(1, min(int(limit), 100))
        url = (f"{base}/api/sessions?limit={cap}&order=recent&archived=exclude"
               f"&exclude_sources=cron,subagent")
        try:
            payload = self._rest_json(url, timeout=15)
        except Exception as exc:  # noqa: BLE001 — 取不到就当没有清单，界面保持原样
            logger.warning("[会话] 左栏清单没取到：%s", exc)
            return
        self.sessions_panel.emit(chat_sessions((payload or {}).get("sessions")))

    def rename_session(self, session_id: str, title: str) -> None:
        self._session_mutation(session_id, "PATCH", {"title": str(title or "").strip()})

    def set_session_pinned(self, session_id: str, pinned: bool) -> None:
        self._session_mutation(session_id, "PATCH", {"pinned": bool(pinned)})

    def delete_session(self, session_id: str) -> None:
        self._session_mutation(session_id, "DELETE")

    def _session_mutation(self, session_id: str, method: str, body: dict | None = None) -> None:
        """改一场会话（标题 / 置顶 / 删除）：走 REST 的 mutations，改完只报一声，清单由窗口那边重拉。"""
        base = str(getattr(self._link, "api_origin", "") or "")
        sid = str(session_id or "")
        if not base or not sid:
            return
        threading.Thread(target=self._session_mutation_job,
                         args=(base, sid, method, dict(body or {})),
                         name="session-edit", daemon=True).start()

    def _session_mutation_job(self, base: str, session_id: str, method: str, body: dict) -> None:
        """一次写：先拿 mutation-snapshot 当栅栏（revision/generation），再带 request_id 提交。

        引擎两种都不认白写：没有 request_id 回 `client_upgrade_required`，没有 expected_revision
        回 `mutation_identity_required`。request_id 同时是幂等回执的键，重试同一件事不会写两遍。
        """
        url = f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}"
        fences: dict[str, str | int] = {"request_id": uuid.uuid4().hex}
        try:
            snap = self._rest_json(f"{url}/mutation-snapshot", timeout=15)
            fences["expected_revision"] = int((snap or {}).get("runtime_revision") or 0)
            generation = (snap or {}).get("runtime_generation")
            if generation is not None:
                fences["expected_generation"] = int(generation)
            if method == "DELETE":
                query = urllib.parse.urlencode(fences)
                self._rest_json(f"{url}?{query}", timeout=15, method=method)
            else:
                self._rest_json(url, timeout=15, method=method, body={**body, **fences})
        except Exception as exc:  # noqa: BLE001 — 没改成就不动界面，清单保持原样
            logger.warning("[会话] %s %s 没成功：%s", method, session_id[:12], exc)
            return
        if method == "DELETE" and str(session_id) in {self._session_id, self._stored_id}:
            # 删掉的就是手里这场：句柄和存储 id 一起放掉——会话不在了就没得接着聊，
            # 下一句话由 `_ensure_session` 新开一场（和挂不上时同一条规矩）。
            self._session_id = ""
            self._stored_id = ""
        self.session_changed.emit(session_id, method.lower())

    def config_get(self, key: str) -> None:
        self._call("config.get", {"key": key, "session_id": self._session_id}, tag=f"config.get:{key}")

    def request_display(self) -> None:
        """要一份 config.yaml 的 display 段：界面照它决定画不画思考段、工具行。"""
        self._call("config.get", {"key": "full", "session_id": self._session_id}, tag="config.get:full")

    def submit(self, text: str, queued: bool = True) -> None:
        """发一句话。会话没好（或正在起）就先攒着并去起会话，好了再发。

        `queued=True`（默认，打字那条路）：明确告诉引擎「这句排在后面跑」，不是「插到这一轮里说」。
        不带它，引擎按默认模式处理——收尾那几十毫秒里发出去的话会被当成插话塞进正在死的那一轮，
        然后跟着一起作废（日志实测 `/steer ... as new user message` 之后 9 毫秒
        `Turn ended: reason=interrupted_by_user`）。

        `queued=False` 只给语音用：语音是插话，就是要引擎把这句话作为纠正递进当前这一轮。
        """
        if self._needs_attach():
            self._pending_text = f"{self._pending_text}\n\n{text}" if self._pending_text else text
            self._ensure_session()
            return
        if self._turn is not None and time.monotonic() - self._harvest_at > 3.0:
            # 这一轮还挂着（很可能引擎正等人回一张卡）：先把还开着的服务端请求捞回来。
            # 没有这一步，用户打的字只会排在卡后面，界面一直转圈——卡面根本没到。
            self._reconcile_open()
        params: dict = {"session_id": self._session_id, "text": text,
                        "submission_id": uuid.uuid4().hex, "clarify_batch": True}
        if queued:
            params["queued"] = True
        self._call("prompt.submit", params, tag="prompt.submit")

    def submit_rewind(self, text: str, row_id: int) -> None:
        """截断后重发：先把这场对话截到这条用户消息之前，再把正文当成新一轮发出去。

        网关只认两段：`session.mutate` 的 `rewind`（带 `target_message_id` = 行号）真正删行，
        删完再发一条普通 `prompt.submit`。
        """
        if self._needs_attach():
            self.error.emit("会话还没准备好，等这条回复出来再重发")
            return
        self._rewind_text = text
        self._rewind_row = int(row_id)
        self.resume_session(self._session_id)  # 先拿最新 revision，rewind 是按版本号 fence 的

    def _mutate(self, operation: str, payload: dict, tag: str, request_id: str = "") -> None:
        """`session.mutate` 一把梭：revision + execution_generation 都当并发栅栏带上。

        canonical 的策略类操作（model / reasoning / rewind …）都走这一个方法；少了
        `expected_generation` 引擎判 invalid_params，版本号对不上判 revision_conflict。
        """
        self._call("session.mutate", {
            "session_id": self._session_id,
            "request_id": request_id or f"{operation}:{uuid.uuid4().hex}",
            "expected_revision": int(self._revision or 0),
            "expected_generation": int(self._generation or 0),
            "operation": operation,
            "payload": dict(payload),
        }, tag=tag)

    def _send_rewind(self) -> None:
        self._mutate("rewind", {"target_message_id": int(self._rewind_row)}, tag="rewind",
                     request_id=f"rewind:{uuid.uuid4().hex}")

    IMAGE_ONLY_CAPTION = "What do you see in this image?"

    def submit_with_attachments(self, text: str, items=(), queued: bool = True) -> None:
        """发一句话，附件先挂上：文件带回的 `@file:` 引用拼在正文前（跟桌面 buildContextText 一致）。"""
        items = list(items or ())
        if not items:
            self.submit(text, queued=queued)
            return
        if self._needs_attach() or self._turn is not None:
            self._pending_turns.append((text, items, queued))
            self._ensure_session()
            return
        self._dispatch_turn(text, items, queued)

    def _dispatch_turn(self, text: str, items: list, queued: bool = True) -> None:
        self._turn_seq += 1
        tag = f"attach:turn:{self._turn_seq}"
        self._turn = {
            "text": text,
            "refs": [],
            "attached": 0,
            "left": len(items),
            "images": sum(1 for item in items if item.get("kind") == "image"),
            "tag": tag,
            "queued": queued,
        }
        for item in items:
            method, params, name = self._attach_frame(item)
            rid = self._call(method, {**params, "session_id": self._session_id}, tag=tag)
            self._attach_names[rid] = (name, method == "file.attach")
            if rid not in self._sent:
                self._on_attach_done(rid, tag, {"attached": False, "message": "引擎没接住这个附件"})

    @staticmethod
    def _attach_frame(item: dict) -> tuple[str, dict, str]:
        """一条附件对应引擎的哪个接口：图一律给字节，其它文件给路径。

        图不能走 `image.attach`（给路径那个）：它是 TUI 进程内的老接口，要的是同进程里那张
        `sessions` 表，走 `/api/ws` 只有 canonical 的 `session_id`，它认不出来就回 request_failed。
        `image.attach_bytes` 是给远程客户端的，字节到了就落盘挂上。
        """
        name = str(item.get("name") or "附件")
        data: bytes = item.get("data") or b""
        path = str(item.get("path") or "")
        if not data and item.get("kind") == "image" and path:
            try:
                data = Path(path).read_bytes()
            except OSError:
                data = b""
        if data:
            encoded = base64.b64encode(data).decode("ascii")
            return "image.attach_bytes", {"content_base64": encoded, "filename": name}, name
        return "file.attach", {"path": path, "name": name}, name

    def _finish_turn(self) -> None:
        """附件全挂完了：把 `@file:` 引用拼到正文前再发这句话。"""
        turn, self._turn = self._turn, None
        if turn is None:
            return
        text = "\n\n".join([*[ref for ref in turn["refs"] if ref], turn["text"]]).strip()
        if not text and turn["images"] and turn["attached"] == 0:
            self._flush_after_attach()
            return
        if not text and turn["images"]:
            text = self.IMAGE_ONLY_CAPTION
        if not text:
            self._flush_after_attach()
            return
        if not self._session_id or self._attaching:
            self._pending_text = f"{self._pending_text}\n\n{text}" if self._pending_text else text
            self._ensure_session()
            return
        params: dict = {"session_id": self._session_id, "text": text,
                        "submission_id": uuid.uuid4().hex, "clarify_batch": True}
        if turn.get("queued"):
            params["queued"] = True
        self._call("prompt.submit", params, tag="prompt.submit")
        self._flush_after_attach()

    def interrupt(self) -> None:
        if not self._session_id:
            return
        self._call("session.interrupt", {"session_id": self._session_id,
                                         "execution_generation": int(self._generation or 0)},
                   tag="session.interrupt")

    def answer_approval(self, prompt_id, choice: str) -> None:
        """回一张审批卡：`approval.respond` 的 params 必须**正好**这四个字段。

        引擎 `session_controls.respond` 拿 `set(params)` 跟
        `{session_id, execution_generation, prompt_id, choice}` 比，多一个少一个都判
        `invalid_params`；`choice` 还得在引擎给的那几个档里（`once`/`session`/`always`/`deny`）。
        回错 `prompt_id` 等于没回——引擎会等到超时把命令撤回，模型只能看到「审批被撤回」，
        于是转头教用户手动执行。
        """
        prompt_id = str(prompt_id or "")
        if not prompt_id or not self._session_id:
            return
        event_ask = self._event_approvals.get(prompt_id)
        if event_ask is None:  # 没收过这张卡，或已经答过 / 被引擎收走：再点也不发帧
            return
        self._event_approvals[prompt_id] = None
        gen = event_ask.get("generation")
        if not isinstance(gen, int) or gen < 0:
            gen = int(self._generation or 0)
        self._call("approval.respond", {
            "session_id": self._session_id,
            "execution_generation": gen,
            "prompt_id": prompt_id,
            "choice": str(choice or "deny"),
        }, tag="approval.respond")

    def answer_setup_beat(self, reply_id, name: str = "", session_id: str = "") -> None:
        """起名卡的回答，同样是 server 请求，用**帧 id** 回。

        契约在引擎 `tools/setup_beat_tool.py::_reply_event`：
        - 回答了 → `{"answer": {"name": "..."}}`（`_accepts` 要求 name 是非空字符串）
        - 「以后再说」→ `onboarding.skip`：引擎把这一轮记成
          跳过（`phase=skipped`）、撤掉等待中的卡，并关掉安装级开关，下次开程序不再问名字。
          只回空答复的话引擎按 `TimedOut` 处理——那张卡算超时，记录仍在进行中，于是每开一次
          程序都要再点一次「以后再说」。

        skip 用**这一帧自带的** `session_id`：引导开场引擎落盘时会换掉运行时 id，全局
        `_session_id` 可能还是过期那个，引擎回 `not in memory`，人点了「以后再说」等于没点。
        """
        if not reply_id:
            return
        clean = str(name or "").strip()
        logger.info("answer setup_beat id=%s", reply_id)
        if clean:
            self._reply(reply_id, {"answer": {"name": clean}})
            return
        sid = session_id or self._session_id
        if sid:
            self._call("onboarding.skip", {"session_id": sid}, tag="onboarding.skip")
        self._reply(reply_id, {})

    def answer_window_read(self, reply_id, payload: dict) -> None:
        """`window.read` 的答复：契约是 `ValueResult`，窗口元数据以 JSON 文本放进 `value`。"""
        if not reply_id:
            return
        self._reply(reply_id, {"value": json.dumps(payload, ensure_ascii=False)})

    def set_model(self, model: str, token: str = "") -> None:
        """`token` 只进 tag：设置窗每一轮带一个编号，迟到的上一轮回执才认得出来。"""
        self._switch_model(model, token)

    def _switch_model(self, model: str, token: str = "") -> None:
        """切换模型；没有会话就等会话，不替用户开一场。

        走 `/api/ws` 时 `config.set key=model` 会被直接拒（`use_session_mutation_model`）：
        canonical 换模型是 `session.mutate operation="model"`，payload 里 provider 和模型名分开给。

        没会话时 mutate 没有 session_id 可挂：只把选择记进 `_pending_switch`，等第一场真会话
        `session_ready`（见 `_resp_session_chain`）再发。以前这里会顺手 `create_session("")` 拿一场
        空会话当载体——首装「保存密钥并启用模型」时引导还没开场，那场空会话就留在侧栏里成了
        「无标题会话」，模型选择也落在它身上，引导反而跑默认模型。
        """
        if not model:
            return
        self._pending_switch = model
        self._pending_switch_token = token
        if not self._session_id:
            return
        self._pending_switch = ""
        self._pending_switch_token = ""
        self._switch_payload = _model_payload(model)
        self._switch_token = token
        self._send_switch_model()

    def _send_switch_model(self) -> None:
        """把待切的模型发出去。payload 要留着：撞版本号时拿新 revision 再发一次。"""
        if not self._switch_payload:
            return
        self._mutate("model", self._switch_payload, tag=self._switch_tag(self._switch_token),
                     request_id=f"model:{uuid.uuid4().hex}")

    @staticmethod
    def _switch_tag(token: str = "") -> str:
        """切换模型的 tag。带轮次编号：两轮「保存并启用」叠在一起时，上一轮的迟到回执
        才不会被当成这一轮的结果（否则窗口按旧结果关掉，引擎里跑的还是上一个模型）。"""
        return f"model.set:{token}" if token else "model.set"

    def set_approval_mode(self, mode: str) -> None:
        """审批档位：键名照引擎的 getter/setter 表（`approvals.mode`），回包带回来的值才算数。"""
        if not mode:
            return
        params = {"key": "approvals.mode", "value": mode}
        if self._session_id:
            params["session_id"] = self._session_id
        self._call("config.set", params, tag="config.set:approval")

    def set_reasoning_effort(self, level: str) -> None:
        """记下界面此刻的档位（引擎回读值），新建会话时带给引擎。"""
        self._reasoning_effort = str(level or "")

    def set_reasoning(self, level: str) -> None:
        """推理强度：canonical 是 `session.mutate operation="reasoning"`，payload `{"effort": ...}`。

        `config.set reasoning` 在 `/api/ws` 上仍可用，但它只改当前会话的显示值；策略档位要走
        mutate，而且这类操作必须带 `expected_generation`，少了它引擎直接判 invalid_params。

        没会话时只记住档位，不建会话：`_resp_session_chain` 在 `session_ready` 里补发（跟 `_switch_model`
        同一条规矩——首装引导期间不许因为一个设置多出一场空会话）。
        """
        if not level:
            return
        if not self._session_id:
            self._pending_reasoning = level
            return
        self._reasoning_level = level
        self._mutate("reasoning", {"effort": level}, tag="reasoning.set",
                     request_id=f"reasoning:{uuid.uuid4().hex}")

    def request_models(self, *, refresh: bool = False) -> None:
        """要一份「聊天窗菜单」用的清单：**只列用户自己配过的家**（引擎的 `explicit_only`）。

        引擎的 `list_authenticated_providers` 会把机器上自动发现的凭据（例如 GitHub CLI 登录态 →
        Copilot）也算成「已认证」；那些家不是用户在 Noesia 里填的 Key，不该出现在聊天菜单里。

        必须带 `session_id`：引擎的 `model.options` 用 `_model_picker_context(_session_agent(params))`
        取「当前用哪个模型」，没有会话时它读的是**磁盘配置**，而 `config.set model` 是就地把
        活着的 agent 换掉、不写磁盘。不带会话问，切完拉回来的 current 还是旧的那个，
        按钮和勾选就都不动——看着就是「切换没生效」。
        """
        params: dict = {"explicit_only": True}
        if self._session_id:
            params["session_id"] = self._session_id
        if refresh:
            params["refresh"] = True
        self._models_rid = self._call("model.options", params, tag="model.options")

    def request_provider_rows(self, *, refresh: bool = False) -> None:
        """要一份「设置页」用的供应商全表：`explicit_only=False` + `include_unconfigured=True`。

        `include_unconfigured` 让没配密钥的家也带着骨架行回来（`authenticated=false` + `key_env`），
        设置页才有一格能填 Key；`explicit_only=False` 保证已配的家一个不少。带 refresh 时引擎
        作废 1 小时的模型缓存、逐家重问线上清单。
        """
        params: dict = {"explicit_only": False, "include_unconfigured": True}
        if self._session_id:
            params["session_id"] = self._session_id
        if refresh:
            params["refresh"] = True
        self._settings_rid = self._call("model.options", params, tag="model.options:settings")

    def greet(self, *, probe: bool = False) -> bool:
        """第一次见面：先问引擎「这台机器该不该跑引导」，该跑且有模型能撑起第一轮，才让它开场。

        为什么不是直接 `onboarding.start`：引导要一个可用的供应商才能起（引擎回 5080），
        而「界面开窗」和「密钥配好」不是同一时刻——先开窗后填 key 的人会永远错过引导。
        所以每次猜「可能要跑」都先问 `onboarding.state`，由引擎按
        `run`（配置/从没用过）和 `eligible`（有 provider）自己判断；已经做过就什么都不发。

        `probe=True`：只问不改（启动动画还在、聊天窗没建时用）。黄字、模型名这类结论当场就能
        定下来；开场引导和空会话要等聊天窗真的建好再做，免得名字卡落在没人看的窗口里。
        """
        self._greet_probe = probe
        if not self._caps_ready:
            # capabilities 还没发：引擎此刻把这条连接当 CLI，eligible 必为 false。等 ready 再问。
            self._greet_waiting = True
            return True
        rid = self._call("onboarding.state", {}, tag="onboarding.state")
        return rid in self._sent

    def _end_onboarding(self, outcome: str) -> None:
        """一轮引导结束了（`onboarding.ended`，或重开发现这轮已经不是我们挂着的那轮）。

        结束只做三件事：撤掉「去配 Key」的黄字、关掉还开着的名字卡、把这一轮记成结束。
        `onboarding.run` 由引擎自己落盘，界面不再重开。

        会话一个字都不动：引导那场就是现在挂着的这场，引擎回给模型的收尾（实测 1.6 秒）
        走的还是它。结束只改 run 记录，帧照常按 `_session_id` 认。
        """
        if not self._onboarding_run and outcome != "done":
            return
        self._onboarding_run = ""
        self._onboarding_started = False
        self.request_cancelled.emit("setup_beat", "")
        self.onboarding_ended.emit(outcome or "done")

    def _start_onboarding(self) -> None:
        """参数与引导入口一致。

        `fresh_launch` 是「本次启动」的标记——关掉 app 打断的那轮引导不算数，会丢掉重来；
        不发它，引擎会把上次没跑完的那轮接着跑，用户看到的开场是半截的。
        `locale` 用界面自己的语言（中文界面就 zh-CN），引擎按它选文案；
        `facts_source` 是「后端就在这台机器上」，本机跑就是 local。
        """
        self._call("onboarding.start", {
            "fresh_launch": True,
            "locale": ui_locale(),
            "facts_source": "local",
        }, tag="onboarding.start", long_lived=True)
        self._onboarding_started = True

    def save_key(self, slug: str, api_key: str, then_model: str = "", token: str = "") -> None:
        """保存某家的密钥；存完自动刷清单，`then_model` 给了就接着切过去。

        参数名照引擎的契约（`ModelSaveKeyParams`：`slug` / `api_key` / `session_id`），
        多一个字段引擎会直接拒：invalid params ... Extra inputs are not permitted。
        """
        if then_model:
            self._pending_switch = then_model
            self._pending_switch_token = token
        params = {"slug": slug, "api_key": api_key}
        if self._session_id:
            params["session_id"] = self._session_id
        self._call("model.save_key", params, tag=f"model.save_key:{slug}")


    def cron_list(self, *, include_disabled: bool = True) -> None:
        """面板那份清单：`include_disabled` 要开着——暂停的任务也得留在列表里（否则看起来像被删了）。"""
        self._call("cron.manage", {"action": "list", "include_disabled": include_disabled}, tag="cron.list")

    def cron_detail(self, job_id: str) -> None:
        self._call("cron.manage", {"action": "detail", "job_id": job_id}, tag="cron.detail")

    def cron_runs(self, job_id: str, limit: int = 20) -> None:
        # tag 带上 job：连点两条任务、回包先后颠倒时，面板得知道这份运行记录属于哪条，
        # 不能把 A 的「运行中」画到 B 头上。
        self._call("cron.manage", {"action": "runs", "job_id": job_id, "limit": limit},
                   tag=f"cron.runs:{job_id}")

    def cron_save(self, job_id: str, fields: dict) -> None:
        """新建走 `add`（原样转发给 cronjob 工具），改走面板的 `update`（原样写回记录字段）。"""
        if job_id:
            self._call("cron.manage", {"action": "update", "job_id": job_id, "updates": dict(fields)},
                       tag="cron.update")
            return
        self._call("cron.manage", {"action": "add", **fields}, tag="cron.add")

    def cron_set_paused(self, job_id: str, paused: bool) -> None:
        action = "resume" if not paused else "pause"
        self._call("cron.manage", {"action": action, "name": job_id}, tag=f"cron.{action}")

    def cron_trigger(self, job_id: str) -> None:
        # 面板上的「立即触发」要等这一趟真跑完才回包，可能好几分钟：标成 long_lived，
        # 别让 120 秒的在途清理把它的 tag 摘掉（摘了回包就没人认领，界面一直停在「触发中」）。
        self._call("cron.manage", {"action": "trigger", "job_id": job_id}, tag=f"cron.trigger:{job_id}",
                   long_lived=True)

    def cron_delete(self, job_id: str) -> None:
        self._call("cron.manage", {"action": "remove", "name": job_id}, tag="cron.remove")

    def _call(self, method: str, params: dict, *, tag: str = "", long_lived: bool = False) -> int:
        cutoff = time.monotonic() - self._SENT_TTL_S
        for old, frame in list(self._sent.items()):
            if frame.get("long_lived"):
                continue
            if float(frame.get("at") or 0) < cutoff:
                # 别静默摘掉：走一遍失败回调，让 attach / rewind / submit 的结算路径都跑到。
                # 静默丢的后果是这些路径永远等不到回包——`_turn["left"]` 不清零，之后每句带附件的
                # 都在排队、界面一直 busy，一个字也不报。
                self._on_response({"id": old, "error": {
                    "code": -32000,
                    "message": f"引擎超过 {int(self._SENT_TTL_S)} 秒没回这一步，这次操作没完成",
                }})
        self._next_id += 1
        rid = self._next_id
        if tag:
            self._pending[rid] = tag
        self._sent[rid] = {
            "method": method, "params": dict(params), "tag": tag, "at": time.monotonic(),
            "long_lived": long_lived,
        }
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            self._on_response({"id": rid, "error": {"code": -32000, "message": "引擎没接住指令，这一步没送出去"}})
        return rid

    def _needs_attach(self) -> bool:
        """这句话该不该先攒着：还没会话，或者会话正在起。顺手把「起太久」的那种收回。

        `_attaching` 只能由回包、出错、换进程三条路摘掉。万一三条都没来（引擎收了建会话的帧
        却一直不回），它就一直挂着：之后每句话只往 `_pending_text` 里堆，一个字也送不出去，
        界面连报错都没有——只有重启程序能恢复。这里给一个时限。

        超时之后**不能**拿同一个存储 id 再赌一次：被强杀打断的会话在引擎那边停在 `unknown`，
        重连多少次都是同一个结果，用户看到的就是「消息一直排队」。所以第一次超时就丢掉存储 id、
        换一场新会话，让攒着的话自己发出去；连试几次都不行才收手报错——宁可让他看见红字。
        """
        if self._attaching and self._attach_since:
            if time.monotonic() - self._attach_since >= self.ATTACH_TIMEOUT_S:
                self._attaching = False
                self._attach_since = 0.0
                self._attach_timeouts += 1
                if self._stored_id and not self._session_id:
                    self._stored_id = ""
                    self.failed.emit("上一场会话接不上了，这条开一场新的发出去")
                    self._ensure_session()
                elif self._attach_timeouts == 1 or self._attach_timeouts % 5 == 0:
                    # 引擎彻底不回的时候别刷屏：说一次，之后每五次提一次。
                    self.failed.emit("引擎一直没回会话，这条还攒着——再等等，或者重开一次 Noesia")
        return not self._session_id or self._attaching

    def _session_ready(self, session_id: str, stored_id: str = "") -> bool:
        """会话真到手了：在途结束、恢复计数清零、运行时 id 换上。

        「会话可用」只有这一个出口；发起失败（error 分支）和超时/退出（`_reset_transport_state`）
        各自把 `_attaching` 清掉，三条路一起保证不会留下「标志还是 True、之后只入队不发」的死状态。

        存储 id 是这份对话的名字（重启、重开都靠它找回），运行时 id 只在本次后端进程里有效；
        引擎没回存储 id 就保留原来那个，别把唯一的线索抹掉。

        返回 False 表示这一帧被扔掉了（引导正在另一场里跑）——
        调用方不能再往下走：它既不能改 revision、也不能发 `session_ready`，否则窗口那边会切到
        一场引擎不认的会话，之后每一帧都被会话过滤丢掉。
        """
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._recover_rounds = 0
        self._attach_timeouts = 0
        # 认「是不是同一场」用存储 id：引导那场落盘时引擎会把运行时 id 换一套，拿 id 比会把
        # 引导自己那一场的 resume 回包也当成别人。引导期间只认这一场——启动时发出去的那条
        # 建会话回包要是晚到，套上去之后引导那一轮的思考和正文就被会话过滤全丢掉了。
        same_stored = bool(stored_id) and stored_id == self._stored_id
        if self._onboarding_started and stored_id and not same_stored and session_id:
            logger.info("skip late attach during onboarding: %s", session_id[:12])
            return False
        if self._session_ids and stored_id and not same_stored:
            # 落地的是另一场：上一场认过的 id 全部作废
            self._session_ids.clear()
        self._session_id = session_id
        if session_id:
            self._session_ids.add(session_id)
        if stored_id:
            self._stored_id = stored_id
        if self._stored_id:
            self._session_ids.add(self._stored_id)
        logger.info("session ready stored=%s runtime=%s", self._stored_id, self._session_id)
        # 在飞的那条落地了：如果它落地的不是我们真正想待的那一场（开场新建的那场），补挂过去。
        after, self._attach_after = self._attach_after, ""
        if after and after != self._stored_id:
            self._stored_id = after
            self.resume_session(after)
        else:
            self._reconcile_open()
        return True

    def _reconcile_open(self) -> None:
        """把「还挂着的服务端请求」捞回来。

        引擎只在卡发出的那一刻写到当时的传输上；界面晚一步挂上、或者中途断线重连，
        那张名字卡就永远看不到，回合停在原地等答案直到超时。`session.events.since` 的
        回包带 `open_requests`，是唯一能把它重新递到界面上的通路。
        """
        if not self._session_id or not self.is_running():
            return
        self._harvest_at = time.monotonic()
        self._call("session.events.since",
                   {"session_id": self._session_id, "last_seen": 0},
                   tag="session.events.since")

    def _ensure_session(self) -> None:
        """会话没好就去起一场：有存储 id 先续上；已经在起（或正在恢复）就不再发第二次。

        冷启动窗口期用户连点两句时，两次都走这里——只发一次 create/resume，
        否则两场会话里各有一半对话。

        **别在这里自己置 `_attaching`**：`resume_session` / `create_session` 进去第一件事就是
        `if self._attaching: return`，先置就等于把要发的那一帧吃掉——话说攒着，请求一个没发，
        界面还一声不响（用户看到的就是「消息一直排队」）。置位是它们自己的事。
        """
        if self._session_id or self._attaching or self._recovering:
            return
        if self._stored_id:
            self.resume_session(self._stored_id)
        else:
            self.create_session()

    def _flush_after_attach(self) -> None:
        """会话就绪之后：先把因 4001 失败的那条重发一次，再接着走攒着的那一轮。"""
        while self._replay:
            replay = self._replay.pop(0)
            params = dict(replay["params"])
            if replay["method"] not in {"session.resume", "session.create"}:
                params["session_id"] = self._session_id
            new_rid = self._call(replay["method"], params, tag=replay["tag"])
            if replay.get("attach") is not None:
                self._attach_names[new_rid] = replay["attach"]
        if not self._session_id or self._attaching:
            return
        if self._pending_turns:
            text, items, queued = self._pending_turns.pop(0)
            self._dispatch_turn(text, items, queued)
            return
        if self._pending_text:
            text, self._pending_text = self._pending_text, ""
            self._call("prompt.submit", {"session_id": self._session_id, "text": text,
                                         "submission_id": uuid.uuid4().hex,
                                         "clarify_batch": True}, tag="prompt.submit")

    @staticmethod
    def _is_session_gone(code, message) -> bool:
        """会话没了：4001 + 「找不到」。不能只认码——4001 也是参数错误码
        （`session_id is required` 同样是 4001），认错就会把真错误当成可恢复状态吞掉。

        引擎的存储错误用的是下划线形态（`not_found` / `session_not_found`），按空格形态
        一比就漏，恢复永远不触发——空会话/失效会话上每一次 `model.options`、`session.mutate`
        都直接红字上屏。两种写法都要认。"""
        if str(code) != "4001":
            return False
        text = str(message).lower().replace("_", " ")
        return "not found" in text or "not exist" in text or "不存在" in text

    def _recover(self, failed: dict | None, method: str, rid=None) -> None:
        """会话句柄被回收（4001）：按存储 id 重新挂上，再把刚才那条原样重发。

        手里没有存储 id（或连 resume 自身都被拒）就直接开一场新会话——宁可新开，也不能把用户
        卡在一条红字上。
        """
        if failed is not None and method != "onboarding.skip":
            # `onboarding.skip` 不重放：它带的是那一帧的会话句柄，句柄一旦失效就永远是 4001，
            # 重放只会把同一条拒绝刷几十遍（人再点一次「以后再说」就有了）。
            queued = dict(failed)
            queued.pop("at", None)
            if queued["tag"].startswith("attach:") and isinstance(rid, int):
                queued["attach"] = self._attach_names.pop(rid, ("", False))
            self._replay.append(queued)
        self._recover_rounds += 1
        if method in {"session.create", "session.resume"}:
            self._recovering = False
            self._attaching = False
        if self._recover_rounds > 3:
            logger.warning("连续 %d 次 4001：不再重新挂载，直接开一场新会话", self._recover_rounds)
            self._recover_rounds = 0
            self._recovering = False
            for queued in self._replay:
                if queued["method"] == "prompt.submit":
                    text = str(queued["params"].get("text") or "")
                    if text:
                        self._pending_text = f"{self._pending_text}\n\n{text}" if self._pending_text else text
            self._replay.clear()
            self._stored_id = ""
            self._session_id = ""
            self._session_ids.clear()
            # 置位是 `create_session` 自己的事：先置就把这一帧吃掉（见 `_ensure_session` 的说明）。
            self.create_session()
            return
        self._recovering = True
        if method in {"session.resume", "session.create"} or not self._stored_id:
            # 没有能挂回去的存储 id（或连 resume 都被拒）：直接开一场新的。
            self._recovering = False
            self.create_session()
            return
        self._call("session.resume", {"session_id": self._stored_id, "source": SESSION_SOURCE},
                   tag="session.resume")

    def answer_clarify(self, request_id: str, question_id: str, answer) -> None:
        """答掉一个问题：两条路，按 `request_id` 认。

        - **批量**（引擎当 srq `clarify` 请求发过来的，`request_id` 是帧 id `srq-...`）：
          逐题 `clarify.lock` `{request_id, question_id, answer}`，回 `{status:"ok", remaining:[...]}`；
          最后一题锁完引擎才收尾。`expired`＝问已经结束（超时/被撤），不算错。
        - **单问**（引擎当事件推过来的 `clarify.request`）：`clarify.respond` 正好
          `{session_id, execution_generation, prompt_id, answer}`（实测回 `{"status": "resolved"}`），
          一个 prompt 只带一个问题，没有 question_id 可锁。

        `answer=None`＝跳过。已经答过 / 被引擎收走的卡再点不发帧。
        """
        request_id = str(request_id or "")
        if not request_id:
            return
        if request_id in self._event_clarifies:
            event_ask = self._event_clarifies[request_id]
            if event_ask is None:  # 已答过，或引擎已经收走：再点也不回票
                return
            if not self._session_id:
                return
            self._event_clarifies[request_id] = None
            gen = event_ask.get("generation")
            if not isinstance(gen, int) or gen < 0:
                gen = int(self._generation or 0)
            self._call("clarify.respond", {
                "session_id": self._session_id,
                "execution_generation": gen,
                "prompt_id": request_id,
                "answer": "" if answer is None else str(answer),
            }, tag="clarify.respond")
            return
        self._call(
            "clarify.lock",
            {"answer": answer, "question_id": str(question_id or ""), "request_id": request_id},
            tag="clarify.lock",
        )

    def answer_secret(self, reply_id, value: str) -> None:
        """密钥 / sudo 密码：`value` 是字符串，空串等于跳过。"""
        self._reply(reply_id, {"value": value})

    def list_processes(self) -> None:
        """后台还在跑的进程（terminal background=true 起的那些）。"""
        if not self._session_id:
            return
        self._call("process.list", {"session_id": self._session_id}, tag="process.list")

    def kill_process(self, process_id: str) -> None:
        params = {"process_id": str(process_id)}
        if self._session_id:
            params["session_id"] = self._session_id
        self._call("process.kill", params, tag="process.kill")

    def check_voice_model(self) -> None:
        """问识别模型在不在。引擎顺手会在后台预热，进语音模式就该问这一下。"""
        self._call("voice.transcribe.ready", {}, tag="voice.transcribe.ready")

    def download_voice_model(self) -> None:
        """下识别模型（两百多兆）。引擎那边同步跑，所以登记在长活清单里。"""
        self._call("voice.transcribe.download", {}, tag="voice.transcribe.download", long_lived=True)

    def transcribe_audio(self, path: str) -> None:
        """把一段录音交给引擎识别。tag 里只放编号不放路径——路径带反斜杠和冒号，
        拼进 tag 会被切错，而 tag 又是失败重发时要靠的原件。"""
        self._voice_seq += 1
        job = self._voice_seq
        self._voice_jobs[job] = str(path)
        rid = self._call("voice.transcribe", {"path": str(path)}, tag=f"voice.transcribe:{job}")
        if rid not in self._sent:
            self._voice_jobs.pop(job, None)
            self.voice_failed.emit(str(path), "录音没送到引擎，再说一次")

    def _reply(self, request_id, result: dict) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _reply_unsupported(self, request_id, method: str) -> None:
        """回「没有这个 handler」：契约要的是 JSON-RPC `-32601`，不是一条成功空结果。"""
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"没有 {method} 这个 handler"},
        })

    ATTACH_TIMEOUT_S = 15.0
    _SENT_TTL_S = 120.0

    def _write(self, frame: dict) -> bool:
        """发一帧；没连上时返回 False（调用方据此报「没送出去」）。"""
        if not self.is_running():
            self.failed.emit("引擎未运行，指令没发出去")
            return False
        # 一个 WS 文本帧就是一整条 JSON：不加换行、不分块，服务端按帧解析。
        if self._link.send(json.dumps(frame, ensure_ascii=False)):
            return True
        self.failed.emit("引擎没接住指令，这一步没送出去")
        return False

    def _fail_inflight(self, message: str) -> None:
        for rid in list(self._sent):
            self._on_response({"id": rid, "error": {"code": -32000, "message": message}})

    def _on_frame_text(self, text: str) -> None:
        """连线上来的一段文本是一帧（服务端也可能一次带多行）。

        只按 `\\n` 切：`splitlines()` 还认 U+2028 / U+2029 / `\\x0b` / `\\x0c` / `\\x85`，
        而 JSON 允许正文里出现这些字符且 `ensure_ascii=False` 时原样上网——那样一整帧会被劈成
        几截、每截都不是合法 JSON、全被当「非 JSON 行」丢掉（`session.resume` 一回包就丢历史）。
        """
        if not self._started:
            return
        for line in text.split("\n"):
            line = line.strip()
            if line:
                self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("引擎输出了非 JSON 行（已跳过）：%s", line[:200])
            return

        if "method" in frame and isinstance(frame.get("id"), str):
            self._on_server_request(frame)
            return

        if "method" in frame and frame.get("method") == "event":
            self._on_event(frame.get("params") or {})
            return

        if "id" in frame:
            self._on_response(frame)
            return

    def _on_server_request(self, frame: dict) -> None:
        method = frame.get("method")
        rid = frame.get("id")
        if rid is not None:
            # 同一条请求可能既走实时帧、又从 `open_requests` 补递一次：同 id 只认第一次。
            if str(rid) in self._seen_requests:
                return
            self._seen_requests.add(str(rid))
            if len(self._seen_requests) > 400:
                self._seen_requests.clear()
        params = dict(frame.get("params") or {})
        params["reply_id"] = frame.get("id")
        logger.info("server request %s id=%s", method, rid)
        if method == "setup_beat":
            if str(params.get("beat") or "") == "name":
                self.setup_beat.emit(params)
            else:
                self._reply(frame.get("id"), {})
            return
        if method in {"secret", "sudo"}:
            params["method"] = method
            params["reply_id"] = frame.get("id")
            self.secret_prompt.emit(params)
            return
        if method == "window.read":
            self.window_read.emit(params)
            return
        if method == "clarify":
            # 批量提问：桌面自己提交时带 `clarify_batch: True`，引擎就把整批问题当**一个** srq 请求发，
            # 逐题用 `clarify.lock` 回（`{request_id, question_id, answer}`）。
            params["request_id"] = str(frame.get("id") or "")
            self.clarify.emit(params)
            return
        self._reply_unsupported(frame.get("id"), str(method))

    def _on_response(self, frame: dict) -> None:
        rid = frame.get("id")
        tag = self._pending.pop(rid, "") if isinstance(rid, int) else ""
        if "error" not in frame and isinstance(rid, int):
            self._sent.pop(rid, None)
        if "error" in frame:
            err = frame["error"]
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message", err) if isinstance(err, dict) else err
            logger.debug("RPC 失败 id=%s code=%s %s", rid, code, message)
            if self._is_session_gone(code, message):
                if rid in self._retried:
                    return
                if isinstance(rid, int):
                    self._retried.add(rid)
                failed = self._sent.pop(rid, None) if isinstance(rid, int) else None
                self._recover(failed, str(failed.get("method") if failed else ""), rid)
                return
            if tag in {"session.create", "session.resume"}:
                self._attaching = False
            if isinstance(rid, int):
                self._sent.pop(rid, None)
            if self._resp_error_head(tag, err, message):
                return
            if self._resp_error_prompt(tag, err, code, message):
                return
            if self._resp_error_model(tag, message):
                return
            if self._resp_error_cron(tag, message):
                return
            if self._resp_error_setup(code, message):
                return
            if self._resp_error_attach(rid, tag, message):
                return
            self.error.emit(f"{message}")
            return
        result = frame.get("result") or {}
        gen = result.get("execution_generation")
        if isinstance(gen, int):
            self._generation = gen
        if self._resp_prompt(tag, result):
            return
        if self._resp_voice(tag, result):
            return
        if self._resp_session_chain(tag, rid, result):
            return
        if self._resp_aux_chain(tag, rid, result):
            return
        if self._resp_model_chain(tag, rid, result):
            return
        if self._resp_tail_chain(tag, rid, result):
            return

    def _resp_session_chain(self, tag: str, rid, result: dict) -> bool:
        if tag in {"session.create", "session.resume"}:
            stored = str(result.get("stored_session_id") or "")
            if (tag == "session.resume" and self._attach_want and stored
                    and stored != self._attach_want):
                # 迟到的旧挂载：它属于上一场会话，套上去会把之后打的字送去旧聊天。
                # 但它落地了，`_attaching` 就得放开，再补发真正想挂的那一场。
                self._attaching = False
                self._attach_since = 0.0
                after, self._attach_after = self._attach_after, ""
                if after and after != self._stored_id:
                    self._stored_id = after
                if after:
                    self.resume_session(after)
                return True
            if not self._session_ready(
                str(result.get("session_id") or result.get("id") or ""),
                stored,
            ):
                return True
            if tag == "session.resume":
                self._revision = int(result.get("revision") or 0)
                self._generation = int(result.get("execution_generation") or 0)
                self._note_pending(result.get("pending"))
                # 引擎把正文省了（`defer_history` 回 `messages=[]`）：它只报条数，
                # 历史按页从 REST 取。
                if count := int(result.get("message_count") or 0):
                    self.history_count.emit(count)
                if self._rewind_text and self._rewind_row:
                    self._send_rewind()
            self.session_ready.emit(result)
            if self._pending_switch:
                self._switch_model(self._pending_switch, self._pending_switch_token)
            if self._pending_reasoning:
                level, self._pending_reasoning = self._pending_reasoning, ""
                self.set_reasoning(level)
            self._flush_after_attach()
            return True
        if tag == "session.events.since":
            for row in (result.get("open_requests") or []):
                if isinstance(row, dict) and row.get("method"):
                    self._on_server_request({"id": row.get("id"), "method": row.get("method"),
                                             "params": dict(row.get("params") or {})})
            return True
        if tag == "onboarding.start":
            self._onboarding_run = str(result.get("run_id") or "")
            stored = str(result.get("stored_session_id") or "")
            # 回包里的运行时 id 当场就能用：引擎推问候时带的就是它。但它只活到引擎把引导
            # 会话落盘那一步，之后会换一套——所以下面还要挂回去拿真的那个。
            self._session_id = str(result.get("session_id") or self._session_id)
            if self._session_id:
                self._session_ids = {self._session_id}
            if stored:
                self._stored_id = stored
            self.notice_clear.emit("setup")
            logger.info("onboarding started run=%s stored=%s", self._onboarding_run, self._stored_id)
            # start 之后主动挂回那场存储会话：`session.resume` 的回包
            # 才是落盘之后的运行时会话 id。
            if stored and self.resume_session(stored):
                return True
            self.session_ready.emit({"session_id": self._session_id,
                                     "stored_session_id": self._stored_id})
            return True
        return False

    def _resp_aux_chain(self, tag: str, rid, result: dict) -> bool:
        if tag == "process.list":
            rows = [row for row in (result.get("processes") or []) if isinstance(row, dict)]
            self.processes.emit(rows)
            return True
        if tag == "process.kill":
            self.list_processes()
            return True
        if tag == "clarify.lock":
            if str(result.get("status") or "") == "expired":
                self.clarify_expired.emit("")
                self.notice_show.emit({"text": "这个问题已经结束，不用再答了", "level": "info", "ttl_ms": CONFIRM_TTL_MS,
                                       "key": "clarify.expired"})
            return True
        if tag == "model.options:settings":
            if rid != getattr(self, "_settings_rid", rid):
                return True
            raw = result.get("providers") if isinstance(result, dict) else None
            rows_now = [dict(row) for row in (raw or []) if isinstance(row, dict)]
            cur_model = str((result or {}).get("model") or "").strip() if isinstance(result, dict) else ""
            cur_provider = str((result or {}).get("provider") or "").strip() if isinstance(result, dict) else ""
            current = f"{cur_provider}:{cur_model}" if cur_model and cur_provider else cur_model
            self.provider_rows.emit(rows_now, current)
            if getattr(self, "_models_after_settings", False):
                self._models_after_settings = False
                self.request_models()
            return True
        if tag == "model.options":
            if rid != getattr(self, "_models_rid", rid):
                return True
            items, current = _flatten_options(result)
            self.models.emit(items, current)
            return True
        if tag == "client.capabilities":
            # 引擎回执了才算 advertise 生效；这会儿补发排队的开场问，`eligible` 才是真的。
            self._caps_ready = True
            if self._greet_waiting:
                self._greet_waiting = False
                # 排队时的意图要原样补发：启动阶段问的是探路（只问不改），别在这一步变成真开场。
                self.greet(probe=self._greet_probe)
            return True
        if tag == "onboarding.state":
            due = result if isinstance(result, dict) else {}
            run_id = str(due.get("run_id") or "")
            probe = bool(getattr(self, "_greet_probe", False))
            if self._onboarding_run and run_id and run_id != self._onboarding_run and due.get("run"):
                # 这场已经不是当前挂着的那一轮（重开/另一窗口重跑）：当作结束处理。
                self._end_onboarding("skipped")
            elif due.get("run") and due.get("eligible") and not self._onboarding_started and not probe:
                self._start_onboarding()
            elif due.get("run") and not due.get("eligible"):
                # 要跑引导却还没有能撑起第一轮的模型：这一步没人会报错（引导根本不启动），
                # 黄字得由这里发，不然第一次进来的人不知道去哪儿填 Key。配好模型后
                # `model.set` 回包和模型清单都会按 `setup` 这个 key 撤掉它。
                self.setup_needed.emit(NO_PROVIDER_HINT)
            elif not self._session_id and not self._onboarding_started and not due.get("run") and not probe:
                # 这轮确定不跑引导：到这一刻才开一场空聊天。反过来，引擎说 run 但还不 eligible
                # （模型/密钥没配好）时绝不能先开一场——配好模型后引导会自己再开一场，那场空会话
                # 就成了孤儿，界面还可能停在它上面，引导里的问候、名字卡和正文全落在另一场里。
                self.create_session("")
            # 首屏判定完了（跑引导 / 要配 Key / 直接开聊天）：启动动画等的是这一条。
            self.greet_resolved.emit()
            return True
        return False

    def _resp_model_chain(self, tag: str, rid, result: dict) -> bool:
        if tag.startswith("model.save_key"):
            row = result.get("provider") or {}
            self.notice.emit(f"已保存 {row.get('slug') or '密钥'}")
            self.request_provider_rows()
            self._models_after_settings = True
            self.key_saved.emit(str(row.get("slug") or ""))
            self.save_result.emit(tag, {"ok": True,
                                        "slug": tag.split(":", 1)[1] if ":" in tag else str(row.get("slug") or "")})
            if self._pending_switch:
                self._switch_model(self._pending_switch, self._pending_switch_token)
            return True
        if tag.startswith("model.set"):
            self._revision = int(result.get("revision") or self._revision or 0)
            gen = result.get("execution_generation")
            if isinstance(gen, int):
                self._generation = gen
            self._switch_payload = {}
            self.request_models()
            # 模型刚被启用：这一刻才真正有 provider 能撑起第一轮，补问一次开场（做过就什么都不发）。
            self.greet()
            self.save_result.emit(tag, {"ok": True})
            return True
        if tag == "config.set:approval":
            self.config_value.emit("approval_mode", str(result.get("value") or ""))
            return True
        if tag == "reasoning.set":
            self._revision = int(result.get("revision") or self._revision or 0)
            gen = result.get("execution_generation")
            if isinstance(gen, int):
                self._generation = gen
            self._reasoning_level = ""
            self._call("config.get", {"key": "reasoning", "session_id": self._session_id},
                       tag="config.get:reasoning")
            return True
        return False

    def _resp_tail_chain(self, tag: str, rid, result: dict) -> bool:
        if tag == "config.get:full":
            self.display_config.emit(dict((result.get("config") or {}).get("display") or {}))
            return True
        if tag.startswith("attach:"):
            self._on_attach_done(rid, tag, result)
            return True
        if tag.startswith("cron."):
            payload = dict(result) if isinstance(result, dict) else {}
            # 引擎有些回包是「成功帧里带失败」：`ok: true` 配 `success: false` + `error`（新建走的
            # cronjob 工具就这样）。照单全收会当成保存成功、把编辑页关掉再报错，所以两者都要看。
            payload["ok"] = bool(payload.get("ok", True)) and payload.get("success") is not False
            if not payload["ok"] and not payload.get("message"):
                payload["message"] = str(payload.get("error") or "操作失败")
            self.cron_result.emit(tag, payload)
            return True
        if tag.startswith("config.get:"):
            key = tag.split(":", 1)[1]
            if key == "reasoning":
                self.reasoning_wire.emit(str(result.get("wire") or ""))
            self.config_value.emit(key, str(result.get("value", "")))
            return True
        return False

    def _resp_prompt(self, tag: str, result: dict) -> bool:
        if tag == "rewind":
            self._revision = int(result.get("revision") or self._revision or 0)
            text, self._rewind_text = self._rewind_text, ""
            if text:
                self.submit(text)
            return True
        if tag == "prompt.resolve_unknown":
            self._paused_sending = False
            resolved = (self._resolving or {}).get("admission_id")
            self._resolving = {}
            self._pending_rows = [row for row in self._pending_rows
                                  if row.get("admission_id") != resolved]
            self._refresh_paused()
            return True
        if tag == "prompt.submit":
            row_id = result.get("user_row_id")
            if isinstance(row_id, int):
                self.user_row_stamped.emit(row_id)
            return True
        return False

    def _resp_voice(self, tag: str, result: dict) -> bool:
        if tag == "voice.transcribe.ready":
            self.voice_model.emit(result if isinstance(result, dict) else {})
            return True
        if tag == "voice.transcribe.download":
            self.check_voice_model()
            return True
        if tag.startswith("voice.transcribe:"):
            job = tag.split(":", 1)[1]
            path = self._voice_jobs.pop(int(job), "") if job.isdigit() else ""
            text = str(result.get("text") or "").strip()
            if text:
                self.voice_text.emit(path, text)
            else:
                self.voice_failed.emit(path, str(result.get("reason") or "这句没听清，再说一次"))
            return True
        return False

    def _resp_error_head(self, tag: str, err, message) -> bool:
        if tag == "model.options:settings":
            self.provider_rows_failed.emit(str(message))
            return True
        if tag == "voice.transcribe.download":
            self.voice_model.emit({"ready": False, "failed": str(message)})
            return True
        if tag.startswith("voice.transcribe:"):
            job = tag.split(":", 1)[1]
            path = self._voice_jobs.pop(int(job), "") if job.isdigit() else ""
            self.voice_failed.emit(path, str(message))
            return True
        return False

    def _resp_error_prompt(self, tag: str, err, code, message) -> bool:
        if tag == "prompt.submit" and code == 4009:
            self.error.emit("这一轮还没停下，等它真结束再重发")
            return True
        if tag == "prompt.submit":
            data = err.get("data") if isinstance(err, dict) else None
            if isinstance(data, dict) and data.get("reason") == "unknown_execution":
                self.paused.emit(dict(self._paused) if self._paused else None)
                if self._paused:
                    self.error.emit("上一轮在重启时被打断了，这场会话卡着——先点横幅里的「丢弃那一轮」再发")
                else:
                    self.error.emit("上一轮在重启时被打断了，这场会话卡着——重开 Noesia 后它会自己接着跑")
                return True
        if tag == "prompt.resolve_unknown":
            self._paused_sending = False
            self._resolving = {}
            self.paused_discard_failed.emit()
            self.error.emit(f"丢弃那一轮没成功：{message}")
            return True
        if tag == "rewind" or tag == "reasoning.set" or tag.startswith("model.set"):
            data = err.get("data") if isinstance(err, dict) else None
            fresh_rev = data.get("revision") if isinstance(data, dict) else None
            fresh_gen = data.get("execution_generation") if isinstance(data, dict) else None
            if isinstance(fresh_rev, int) and fresh_rev != self._revision:
                self._revision = fresh_rev
                if isinstance(fresh_gen, int):
                    self._generation = fresh_gen
                if tag == "rewind" and self._rewind_text:
                    self._send_rewind()
                    return True
                if tag == "reasoning.set" and self._reasoning_level:
                    self.set_reasoning(self._reasoning_level)
                    return True
                if tag.startswith("model.set") and self._switch_payload:
                    self._send_switch_model()
                    return True
            if tag == "rewind":
                self._rewind_text = ""
                self.error.emit(str(message))
                return True
            if tag == "reasoning.set":
                self._reasoning_level = ""
                self.error.emit(str(message))
                return True
            self._switch_payload = {}
            self.save_result.emit(tag, {"ok": False, "message": str(message)})
            return True
        if tag == "session.interrupt":
            data = err.get("data") if isinstance(err, dict) else None
            fresh = data.get("execution_generation") if isinstance(data, dict) else None
            if isinstance(fresh, int) and fresh != self._generation:
                self._generation = fresh
                self.interrupt()
                return True
            if isinstance(data, dict) and data.get("reason") == "stale_generation":
                return True  # 那一轮已经不在跑了，停止按钮不该再弹错
            self.error.emit(str(message))
            return True
        return False

    def _resp_error_model(self, tag: str, message) -> bool:
        if tag.startswith("model.save_key") or tag.startswith("model.set"):
            if tag.startswith("model.save_key"):
                self._pending_switch = ""
                self._pending_switch_token = ""
            self.save_result.emit(tag, {"ok": False, "message": str(message)})
            return True
        return False

    def _resp_error_cron(self, tag: str, message) -> bool:
        if tag.startswith("cron."):
            self.cron_result.emit(tag, {"ok": False, "message": str(message)})
            return True
        return False

    def _resp_error_setup(self, code, message) -> bool:
        if code == 5080 or looks_unconfigured(message, code):
            self.setup_needed.emit(NO_PROVIDER_HINT)
            return True
        return False

    def _resp_error_attach(self, rid, tag: str, message) -> bool:
        if tag.startswith("attach:"):
            name = self._attach_taken(rid)[0]
            self.attachment_failed.emit(name or "附件", str(message))
            self._settle_turn(tag)
            return True
        return False

    def _attach_taken(self, rid) -> tuple[str, bool]:
        """取走这条附件的登记：(名字, 是否必须有 @file: 回执)。"""
        value = self._attach_names.pop(rid, ("", False)) if isinstance(rid, int) else ("", False)
        return value if isinstance(value, tuple) else (str(value or ""), False)

    def _on_attach_done(self, rid, tag: str, result: dict) -> None:
        """一个附件挂完了：收下文件回带 `@file:` 引用，等最后一个回来就发那句话。

        三种情况都算失败，界面要把那一条撤掉并说清名字：回包 `attached` 不为真（引擎明说没接）、
        回包是错误帧（`_on_response` 的错误分支）、文件没带回 `@file:` 引用（等于模型看不见这个文件）。
        """
        name, needs_ref = self._attach_taken(rid)
        if not result.get("attached"):
            self.attachment_failed.emit(name or "附件", str(result.get("message") or "引擎没接下这个附件"))
        elif needs_ref and not result.get("ref_text"):
            self.attachment_failed.emit(name or "附件", "引擎没给出 @file: 引用，这个文件没送进去")
        else:
            if self._turn is not None:
                self._turn["attached"] += 1
            if result.get("ref_text") and self._turn is not None:
                self._turn["refs"].append(str(result["ref_text"]))
        self._settle_turn(tag)

    def _settle_turn(self, tag: str) -> None:
        if self._turn is None or tag != self._turn.get("tag"):
            return
        self._turn["left"] -= 1
        if self._turn["left"] <= 0:
            self._finish_turn()

    def _note_pending(self, rows: object) -> None:
        """引擎的排队清单（`session.resume` 回包和 `session.info` 事件都带）：里面有一行
        `status='unknown'` 就说明这场会话被上一次重启打断了，整条队列等一个人拍板。"""
        if not isinstance(rows, list):
            return
        self._pending_rows = [row for row in rows if isinstance(row, dict)]
        self._refresh_paused()

    def _refresh_paused(self) -> None:
        lost: dict | None = None
        for row in self._pending_rows:
            if str(row.get("status") or "") != "unknown":
                continue
            admission_id = row.get("admission_id")
            if not isinstance(admission_id, str) or not admission_id:
                continue
            generation = row.get("execution_generation")
            lost = {"admission_id": admission_id,
                    "execution_generation": generation if isinstance(generation, int) else 0}
            break
        if lost == self._paused:
            return
        self._paused = lost
        self._paused_sending = False
        self.paused.emit(dict(lost) if lost else None)

    def resolve_unknown(self) -> None:
        """丢掉那一轮丢掉的输入（引擎侧的 `prompt.resolve_unknown`）：之后排队的第一条接着跑。"""
        if not self._paused or self._paused_sending or not self._session_id:
            return
        self._paused_sending = True
        self._resolving = dict(self._paused)
        self._call("prompt.resolve_unknown",
                   {"session_id": self._session_id, **self._resolving},
                   tag="prompt.resolve_unknown")

    def _on_event(self, params: dict) -> None:
        kind = params.get("type") or ""
        sid = str(params.get("session_id") or "")
        if sid and self._session_ids and sid not in self._session_ids:
            # 投递回执的通知卡设计上就跟当前开着哪场无关（只弹卡、不进正文），别把它拦掉。
            if kind != "session.appended":
                logger.info("丢帧 %s sid=%s 当前=%s", kind, sid, self._session_id)
                return
        payload = params.get("payload") or {}
        for holder in (params, payload):
            gen = holder.get("execution_generation") if isinstance(holder, dict) else None
            if isinstance(gen, int):
                self._generation = gen
        if kind == "gateway.ready":
            self.ready.emit(payload)
            # 一问一答地排队：帧是并发派发的，一次连发两条（capabilities + 开场问）会赛跑——
            # 开场问可能在引擎还没记下 advertise 时就到，那时它把连接当 CLI，`eligible` 必为 false。
            self._call("client.capabilities", {"server_requests": True}, tag="client.capabilities")
        elif kind == "message.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self.delta.emit(text)
        elif kind == "message.interim":
            # 工具旁白 / 收尾前那段最终回答：引擎说「没流全」的才是界面缺的正文，补画；
            # 已经流全的（`already_streamed`）再画一遍就成了重影。
            text = payload.get("text")
            if isinstance(text, str) and text.strip() and not payload.get("already_streamed"):
                self.interim_text.emit(text)
        elif kind == "reasoning.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self.reasoning_delta.emit(text)
        elif kind == "thinking.delta":
            text = str(payload.get("text") or "")
            wait = provider_wait_text(text)
            if wait:
                self.provider_wait.emit(wait)
        elif kind == "reasoning.available":
            text = str(payload.get("text") or "")
            if text:
                self.reasoning_done.emit(text)
                self.reasoning_block.emit(text)
        elif kind == "agent.step":
            step = payload.get("iteration")
            if isinstance(step, int) and step > 0:
                self.agent_step.emit(step)
        elif kind == "review.summary":
            text = re.sub(r"^[^\w]+", "", str(payload.get("text") or "")).strip()
            if text:
                self.review_summary.emit(text)
        elif kind.startswith("tool."):
            event = dict(payload)
            event["kind"] = kind
            self.tool.emit(event)
        elif kind.startswith("subagent."):
            event = dict(payload)
            event["kind"] = kind
            self.subagent.emit(event)
        elif kind == "message.complete":
            self.completed.emit(payload)
        elif kind == "turn.done":
            self.turn_settled.emit(str(payload.get("status") or ""))
        elif kind == "message.start":
            self.turn_started.emit()
        elif kind == "status.update":
            self.status_text.emit(str(payload.get("kind") or ""), str(payload.get("text") or ""))
        elif kind == "session.info":
            if "pending" in payload:
                self._note_pending(payload.get("pending"))
            running = payload.get("running")
            if running is not None:
                self.status.emit("thinking" if running else "idle")
            effort = payload.get("reasoning_effort")
            if effort is not None:
                self.reasoning_reported.emit(str(effort), str(payload.get("reasoning_effort_wire") or ""))
        elif kind == "cron.changed":
            self.cron_changed.emit()
        elif kind == "session.appended":
            # 投递直接写进某场会话的行（cron）：不进正文、不并历史，只弹一张通知卡。
            # 通知说的是「这条任务跑完了」，跟当前开着的是哪场会话无关；没有 job_id 的
            # （send_message 镜像）不弹。
            if payload.get("job_id"):
                self.cron_delivery.emit(str(payload.get("job_name") or ""))
        elif kind == "approval.request":
            # 引擎把审批当「会话事件」推给本机查看器（`session_pending_controls.register`），
            # 不是 srq 服务端请求：只认 `approval.respond` 回票，帧 id 那条路在这版引擎上不会来。
            ask = _approval_from_event(params, payload)
            if ask["prompt_id"]:
                self._event_approvals[ask["prompt_id"]] = {"generation": ask["execution_generation"]}
            self.approval.emit(ask)
        elif kind == "approval.settled":
            prompt_id = str(payload.get("prompt_id") or "")
            if prompt_id in self._event_approvals:
                self._event_approvals[prompt_id] = None  # 已经收走了：再点也不该发帧
            self.approvals_cancelled.emit([prompt_id])
        elif kind == "clarify.request":
            ask = _clarify_from_event(params, payload)
            if ask["prompt_id"]:
                self._event_clarifies[ask["prompt_id"]] = {"generation": ask["execution_generation"]}
            self.clarify.emit(ask)
        elif kind == "clarify.settled":
            prompt_id = str(payload.get("prompt_id") or "")
            if prompt_id in self._event_clarifies:
                self._event_clarifies[prompt_id] = None  # 已经收走了：再点也不该回票
            self.request_cancelled.emit("clarify", prompt_id)
        elif kind == "approval.cancelled":
            ids = payload.get("request_ids")
            request_ids = [str(item) for item in ids] if isinstance(ids, list) else []
            for request_id in request_ids:
                if request_id in self._event_approvals:
                    self._event_approvals[request_id] = None  # 会话收走了：再点也不发帧
            self.approvals_cancelled.emit(request_ids)
        elif kind == "request.cancel":
            self.request_cancelled.emit(str(payload.get("method") or ""), str(payload.get("id") or ""))
        elif kind == "session.reclaimed":
            # 引擎回收任何一场会话都广播这条（外层 session_id 是空串，载荷里才带 id 和存储 id）。
            # 不比对就清，等于别的会话一被回收就把手上这场当成没了：下一条消息先 resume 一轮
            # （消息排队、界面闪一下），还没拿到存储 id 的会话干脆重开一场。
            gone = str(payload.get("session_id") or "")
            gone_stored = str(payload.get("stored_session_id") or "")
            if (gone and gone == self._session_id) or (gone_stored and gone_stored == self._stored_id):
                self._session_id = ""
        elif kind == "onboarding.ended":
            run_id = str(payload.get("run_id") or "")
            if not run_id or not self._onboarding_run or run_id == self._onboarding_run:
                self._end_onboarding(str(payload.get("outcome") or "done"))
        elif kind == "notification.show":
            self.notice_show.emit(dict(payload))
            return
        elif kind == "notification.clear":
            self.notice_clear.emit(str(payload.get("key") or ""))
            return
        elif kind == "reaction":
            self.reaction.emit(str(payload.get("kind") or ""))
            return
        elif kind == "message.reaction":
            self.message_reaction.emit(dict(payload))
            return
        elif kind in {"notice", "error"}:
            text = str(payload.get("message") or payload.get("text") or "")
            if payload.get("code") == "provider_not_configured" or looks_unconfigured(text, payload.get("code")):
                self.setup_needed.emit(NO_PROVIDER_HINT)
            else:
                self.notice.emit(text)

    def _on_link_ready(self, pid: int) -> None:
        logger.info("引擎连上了（owner pid %s）", pid or "?")

    def _on_link_failure(self, message: str) -> None:
        """连接级错误：连不上、协议对不上、半路断了。没连上就复位 `_started` 让上面能重来。"""
        if not self.is_running():
            self._started = False
        self.failed.emit(str(message))

    def _on_link_gone(self, _reason: str) -> None:
        """连接没了：跟子进程退出一个意思——在途的、排队的、打了没发的都报「没发出去」。"""
        self._started = False
        typed_but_unsent = bool(self._pending_text)
        had_turn = self._turn is not None
        had_pending = bool(self._pending_turns)
        # 在途的 RPC 也要收账：不合成一条失败回包，等它们的界面就一直停在「正在…」
        # （`_sent` 随 `_reset_transport_state` 清掉，之后就再没人认领这些 id 了）。
        self._fail_inflight("引擎断开，这一步没做完")
        self._reset_transport_state()
        if had_turn:
            self.attachment_failed.emit("整条消息", "引擎断开，这句话没发出去")
        if had_pending:
            self.attachment_failed.emit("整条消息", "引擎断开，这条消息没发出去")
        if typed_but_unsent:
            self.attachment_failed.emit("整条消息", "引擎断开，你刚打的这句还没发出去")
        self.closed.emit()
