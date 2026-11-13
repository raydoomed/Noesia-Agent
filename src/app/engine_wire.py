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
import time
import traceback
import uuid
from collections.abc import MutableMapping
from pathlib import Path

from app.gateway_link import GatewayLink
from app.status_stack import CONFIRM_TTL_MS

STOP_TIMEOUT = 8

import PySide6.QtCore as QtCore

logger = logging.getLogger(__name__)

# 定时任务、子代理、工具、看板跑出来的会话不是「用户自己聊的那场」：冷启动和恢复挑会话时跳过。
# 引擎的自动恢复名单只挡 kanban/tool/oneshot，cron 的会话在它眼里同样「像人」。
NON_CHAT_SOURCES = frozenset({"cron", "kanban", "tool", "oneshot", "subagent", "bot"})
# 面板对引擎报的身份：引擎的 SURFACES 只认 gui/tui/cli/acp/tool/oneshot（桌面就是 gui），
# 不报的话会话会被当成 cli，思考这类「本地窗口才显示」的东西整轮都不会发上来。
SESSION_SOURCE = "gui"


def chat_session_id(rows) -> str:
    """从会话清单里挑一场真正的对话（引擎给的是最新在前），挑不到就空串。"""
    for row in rows or []:
        if not isinstance(row, dict) or str(row.get("source") or "") in NON_CHAT_SOURCES:
            continue
        sid = str(row.get("session_id") or row.get("id") or "")
        if sid:
            return sid
    return ""


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
    history = QtCore.Signal(list)
    user_row_stamped = QtCore.Signal(int)
    sessions_listed = QtCore.Signal(list)
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
    key_saved = QtCore.Signal(str)
    notice = QtCore.Signal(str)
    notice_show = QtCore.Signal(dict)
    notice_clear = QtCore.Signal(str)
    clarify = QtCore.Signal(dict)
    secret_prompt = QtCore.Signal(dict)
    reaction = QtCore.Signal(str)
    approvals_cancelled = QtCore.Signal(list)
    request_cancelled = QtCore.Signal(str, str)
    processes = QtCore.Signal(list)
    voice_text = QtCore.Signal(str, str)
    voice_failed = QtCore.Signal(str, str)
    voice_model = QtCore.Signal(dict)
    cron_result = QtCore.Signal(str, dict)
    cron_changed = QtCore.Signal()
    setup_needed = QtCore.Signal(str)
    error = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    closed = QtCore.Signal()

    def __init__(self, project_root: Path, parent: QtCore.QObject | None = None,
                 workspace: Path | str | None = None) -> None:
        super().__init__(parent)
        self._root = Path(project_root)
        if workspace is None:
            raise ValueError("引擎必须有工作区目录：workspace 不能为空")
        self._workspace = Path(workspace)
        self._link = GatewayLink(self)
        self._link.frame.connect(self._on_frame_text)
        self._link.ready.connect(self._on_link_ready)
        self._link.failed.connect(self._on_link_failure)
        self._link.gone.connect(self._on_link_gone)

        self._voice_seq = 0
        self._voice_jobs: dict[int, str] = {}
        self._next_id = 0
        self._pending: dict[int, str] = {}
        self._session_id = ""
        self._pending_switch = ""
        self._pending_switch_token = ""
        self._pending_reasoning = ""
        self._models_after_settings = False
        self._reasoning_effort = ""
        self._onboarding_started = False
        self._stored_id = ""
        self._revision = 0
        self._generation = 0
        self._rewind_text = ""
        self._rewind_row = 0
        self._switch_payload: dict = {}
        self._switch_token = ""
        self._reasoning_level = ""
        self._sent: dict[int, dict] = {}
        self._retried: set[int] = set()
        self._replay: list[dict] = []
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._recover_rounds = 0
        self._pending_text = ""
        self._pending_turns: list[tuple[str, list, bool]] = []
        self._turn: dict | None = None
        self._turn_seq = 0
        self._attach_names: dict[int, tuple[str, bool]] = {}
        self._started = False


    def _engine_home(self) -> Path:
        """数据根（和写进网关环境变量的是同一个值）。"""
        return _engine_home_path(self._root)

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
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._recover_rounds = 0
        self._attach_names.clear()
        self._session_id = ""
        self._pending_text = ""
        self._pending_switch = ""
        self._pending_switch_token = ""
        self._pending_reasoning = ""
        self._models_after_settings = False
        self._pending_turns.clear()
        self._turn = None

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

    def create_session(self, cwd: str | None = None) -> None:
        if self._attaching:
            return
        self._attaching = True
        self._attach_since = time.monotonic()
        params = {"cwd": cwd or str(self._workspace), "cwd_explicit": True, "source": SESSION_SOURCE}
        if self._reasoning_effort:
            params["reasoning"] = self._reasoning_effort
        rid = self._call("session.create", params, tag="session.create")
        if rid not in self._sent:
            self._attaching = False
            self._attach_since = 0.0
        self.reasoning_pending.emit(False)

    def resume_session(self, session_id: str) -> None:
        if self._attaching:
            return
        self._stored_id = session_id or self._stored_id
        self._attaching = True
        self._attach_since = time.monotonic()
        self.reasoning_pending.emit(True)
        rid = self._call("session.resume", {"session_id": session_id, "display_projection": True,
                                            "source": SESSION_SOURCE}, tag="session.resume")
        if rid not in self._sent:
            self._attaching = False
            self._attach_since = 0.0

    def list_sessions(self, limit: int = 50) -> None:
        """库里存过的会话（含来源）：没有「上一场」存档时，靠它挑一场真正的对话。"""
        self._call("session.list", {"scope": "stored", "limit": limit}, tag="session.list")

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

    def answer_approval(self, reply_id, choice: str) -> None:
        """审批是引擎的「服务端请求」，用**帧 id** 回帧（`srq-...`），不走方法表。

        `ApprovalResult` 只有 `choice`（`once` / `session` / `always` / `deny`）和可选的 `all`；
        回多一个 `decision` 字段没有用，回错 id 则等于没回——引擎会等到超时然后把命令撤回，
        模型只能看到「审批被撤回」，于是转头教用户手动执行。
        """
        if not reply_id:
            return
        self._reply(reply_id, {"choice": choice})

    def answer_setup_beat(self, reply_id, name: str = "") -> None:
        """起名卡的回答，同样是 server 请求，用**帧 id** 回。

        契约在引擎 `tools/setup_beat_tool.py::_reply_event`：
        - 回答了 → `{"answer": {"name": "..."}}`（`_accepts` 要求 name 是非空字符串）
        - 跳过/关掉 → `{}`（引擎记 `TimedOut`，引导继续往下走，不是退出）
        """
        if not reply_id:
            return
        clean = str(name or "").strip()
        self._reply(reply_id, {"answer": {"name": clean}} if clean else {})

    def set_model(self, model: str, token: str = "") -> None:
        """`token` 只进 tag：设置窗每一轮带一个编号，迟到的上一轮回执才认得出来。"""
        self._switch_model(model, token)

    def _switch_model(self, model: str, token: str = "") -> None:
        """切换模型；先保证有会话。

        走 `/api/ws` 时 `config.set key=model` 会被直接拒（`use_session_mutation_model`）：
        canonical 换模型是 `session.mutate operation="model"`，payload 里 provider 和模型名分开给。

        没会话时 mutate 没有 session_id 可挂，所以先建会话，等 `session_ready` 回来再切：
        这才是「保存并启用」该走的顺序。
        """
        if not model:
            return
        self._pending_switch = model
        self._pending_switch_token = token
        if self._session_id:
            self._pending_switch = ""
            self._pending_switch_token = ""
            self._switch_payload = _model_payload(model)
            self._switch_token = token
            self._send_switch_model()
        else:
            self.create_session("")

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
        """
        if not level:
            return
        if not self._session_id:
            self._pending_reasoning = level
            self.create_session("")
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

    def greet(self) -> bool:
        """第一次见面：先问引擎「这台机器该不该跑引导」，该跑且有模型能撑起第一轮，才让它开场。

        为什么不是直接 `onboarding.start`：引导要一个可用的供应商才能起（引擎回 5080），
        而「界面开窗」和「密钥配好」不是同一时刻——先开窗后填 key 的人会永远错过引导。
        所以每次猜「可能要跑」都先问 `onboarding.state`，由引擎按
        `run`（配置/从没用过）和 `eligible`（有 provider）自己判断；已经做过就什么都不发。
        """
        rid = self._call("onboarding.state", {}, tag="onboarding.state")
        return rid in self._sent

    def restart_onboarding(self) -> None:
        """用户主动重跑引导：先把引擎的 `run` 打开，再走正常流程。

        引擎只在全新安装时自动跑（`onboarding.state.run`：这个数据根里没有密钥、没有会话、
        没有档案才算全新；`.machine` 标记也在同一个根里，删根就是真的重新开始）。正规开关是
        `onboarding.set_run {run: true}`——「重跑引导」走的也是这一条，不删任何文件。
        """
        self._call("onboarding.set_run", {"run": True}, tag="onboarding.set_run")

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
            "app_ui": False,
            "first_task": False,
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
        self._call("cron.manage", {"action": "runs", "job_id": job_id, "limit": limit}, tag="cron.runs")

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
        self._call("cron.manage", {"action": "trigger", "job_id": job_id}, tag="cron.trigger",
                   long_lived=True)

    def cron_delete(self, job_id: str) -> None:
        self._call("cron.manage", {"action": "remove", "name": job_id}, tag="cron.remove")

    def _call(self, method: str, params: dict, *, tag: str = "", long_lived: bool = False) -> int:
        cutoff = time.monotonic() - self._SENT_TTL_S
        for old, frame in list(self._sent.items()):
            if frame.get("long_lived"):
                continue
            if float(frame.get("at") or 0) < cutoff:
                self._sent.pop(old, None)
                self._pending.pop(old, None)
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
        """
        if self._attaching and self._attach_since:
            if time.monotonic() - self._attach_since >= self.ATTACH_TIMEOUT_S:
                self._attaching = False
                self._attach_since = 0.0
                self.failed.emit("引擎一直没回会话，这次没接上")
        return not self._session_id or self._attaching

    def _session_ready(self, session_id: str, stored_id: str = "") -> None:
        """会话真到手了：在途结束、恢复计数清零、运行时 id 换上。

        「会话可用」只有这一个出口；发起失败（error 分支）和超时/退出（`_reset_transport_state`）
        各自把 `_attaching` 清掉，三条路一起保证不会留下「标志还是 True、之后只入队不发」的死状态。

        存储 id 是这份对话的名字（重启、重开都靠它找回），运行时 id 只在本次后端进程里有效；
        引擎没回存储 id 就保留原来那个，别把唯一的线索抹掉。
        """
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._recover_rounds = 0
        self._session_id = session_id
        if stored_id:
            self._stored_id = stored_id

    def _ensure_session(self) -> None:
        """会话没好就去起一场：有存储 id 先续上；已经在起（或正在恢复）就不再发第二次。

        冷启动窗口期用户连点两句时，两次都走这里——只发一次 create/resume，
        否则两场会话里各有一半对话。
        """
        if self._session_id or self._attaching or self._recovering:
            return
        self._attaching = True
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
        （`session_id is required` 同样是 4001），认错就会把真错误当成可恢复状态吞掉。"""
        text = str(message).lower()
        return code in (4001, 4007) and ("not found" in text or "not exist" in text or "不存在" in text)

    def _recover(self, failed: dict | None, method: str, rid=None) -> None:
        """会话句柄被回收（4001）：按存储 id 重新挂上，再把刚才那条原样重发。

        手里没有存储 id（或连 resume 自身都被拒）就先问引擎最近那场对话；
        连着救不回来就开一场新会话——宁可新开，也不能把用户卡在一条红字上。
        """
        if failed is not None:
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
            self._attaching = True
            self.create_session()
            return
        self._recovering = True
        if method in {"session.resume", "session.create"} or not self._stored_id:
            self._call("session.list", {"scope": "stored", "limit": 50}, tag="recover.recent")
            return
        self._call("session.resume", {"session_id": self._stored_id, "source": SESSION_SOURCE},
                   tag="session.resume")

    def answer_clarify(self, request_id: str, question_id: str, answer: str) -> None:
        """答掉引擎那一个问题：一次锁一个，全部锁完引擎自己结束这次提问。"""
        self._call(
            "clarify.lock",
            {"answer": answer, "question_id": question_id, "request_id": request_id},
            tag="clarify.lock",
        )

    def skip_clarify(self, reply_id) -> None:
        """整个提问跳过：空回包就是「一个都没选」。"""
        self._reply(reply_id, {})

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
            "error": {"code": -32601, "message": f"桌面端没有 {method} 这个 handler"},
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
        """连线上来的一段文本是一帧（服务端也可能一次带多行）。"""
        if not self._started:
            return
        for line in text.splitlines():
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
        params = dict(frame.get("params") or {})
        params["reply_id"] = frame.get("id")
        if method == "approval":
            self.approval.emit(params)
            return
        if method == "setup_beat":
            if str(params.get("beat") or "") == "name":
                self.setup_beat.emit(params)
            else:
                self._reply(frame.get("id"), {})
            return
        if method == "clarify":
            params["reply_id"] = frame.get("id")
            params["request_id"] = str(frame.get("id") or "")
            self.clarify.emit(params)
            return
        if method in {"secret", "sudo"}:
            params["method"] = method
            params["reply_id"] = frame.get("id")
            self.secret_prompt.emit(params)
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
            if tag == "model.options:settings":
                self.provider_rows_failed.emit(str(message))
                return
            if tag == "voice.transcribe.download":
                self.voice_model.emit({"ready": False, "failed": str(message)})
                return
            if tag.startswith("voice.transcribe:"):
                job = tag.split(":", 1)[1]
                path = self._voice_jobs.pop(int(job), "") if job.isdigit() else ""
                self.voice_failed.emit(path, str(message))
                return
            if tag == "prompt.submit" and code == 4009:
                self.error.emit("这一轮还没停下，等它真结束再重发")
                return
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
                        return
                    if tag == "reasoning.set" and self._reasoning_level:
                        self.set_reasoning(self._reasoning_level)
                        return
                    if tag.startswith("model.set") and self._switch_payload:
                        self._send_switch_model()
                        return
                if tag == "rewind":
                    self._rewind_text = ""
                    self.error.emit(str(message))
                    return
                if tag == "reasoning.set":
                    self._reasoning_level = ""
                    self.error.emit(str(message))
                    return
                self._switch_payload = {}
                self.save_result.emit(tag, {"ok": False, "message": str(message)})
                return
            if tag == "session.interrupt":
                data = err.get("data") if isinstance(err, dict) else None
                fresh = data.get("execution_generation") if isinstance(data, dict) else None
                if isinstance(fresh, int) and fresh != self._generation:
                    self._generation = fresh
                    self.interrupt()
                    return
                if isinstance(data, dict) and data.get("reason") == "stale_generation":
                    return  # 那一轮已经不在跑了，停止按钮不该再弹错
                self.error.emit(str(message))
                return
            if tag.startswith("model.save_key") or tag.startswith("model.set"):
                if tag.startswith("model.save_key"):
                    self._pending_switch = ""
                    self._pending_switch_token = ""
                self.save_result.emit(tag, {"ok": False, "message": str(message)})
                return
            if tag.startswith("cron."):
                self.cron_result.emit(tag, {"ok": False, "message": str(message)})
                return
            if code == 5080 or looks_unconfigured(message, code):
                self.setup_needed.emit(NO_PROVIDER_HINT)
                return
            if tag.startswith("attach:"):
                name = self._attach_taken(rid)[0]
                self.attachment_failed.emit(name or "附件", str(message))
                self._settle_turn(tag)
                return
            self.error.emit(f"{message}")
            return
        result = frame.get("result") or {}
        gen = result.get("execution_generation")
        if isinstance(gen, int):
            self._generation = gen
        if tag == "rewind":
            self._revision = int(result.get("revision") or self._revision or 0)
            text, self._rewind_text = self._rewind_text, ""
            if text:
                self.submit(text)
            return
        if tag == "prompt.submit":
            row_id = result.get("user_row_id")
            if isinstance(row_id, int):
                self.user_row_stamped.emit(row_id)
            return
        if tag == "voice.transcribe.ready":
            self.voice_model.emit(result if isinstance(result, dict) else {})
            return
        if tag == "voice.transcribe.download":
            self.check_voice_model()
            return
        if tag.startswith("voice.transcribe:"):
            job = tag.split(":", 1)[1]
            path = self._voice_jobs.pop(int(job), "") if job.isdigit() else ""
            text = str(result.get("text") or "").strip()
            if text:
                self.voice_text.emit(path, text)
            else:
                self.voice_failed.emit(path, str(result.get("reason") or "这句没听清，再说一次"))
            return
        if tag in {"session.create", "session.resume"}:
            self._session_ready(
                str(result.get("session_id") or result.get("id") or ""),
                str(result.get("stored_session_id") or ""),
            )
            if tag == "session.resume":
                self._revision = int(result.get("revision") or 0)
                self._generation = int(result.get("execution_generation") or 0)
                rows = [
                    {
                        "role": str(row.get("role", "")),
                        "text": str(row.get("text") or row.get("content") or ""),
                        "display_kind": str(row.get("display_kind") or ""),
                        "row_id": row.get("row_id"),
                        "timestamp": row.get("timestamp"),
                        # 工具行没有正文：引擎给的是名字 + 80 字预览（`context`），
                        # 界面上那一行就是照它画的，别在半路丢掉。
                        "name": str(row.get("name") or ""),
                        "context": str(row.get("context") or ""),
                        "tool_call_id": row.get("tool_call_id"),
                        "labels": row.get("labels"),
                        # 回答的思考段：与正文同一行回来，重铺时一并画。
                        "reasoning": str(row.get("reasoning") or row.get("reasoning_content") or ""),
                    }
                    for row in (result.get("display_messages") or result.get("messages") or [])
                    if isinstance(row, dict) and str(row.get("display_kind") or "") != "hidden"
                ]
                self.history.emit(rows)
                if self._rewind_text and self._rewind_row:
                    self._send_rewind()
            self.session_ready.emit(result)
            if self._pending_switch:
                self._switch_model(self._pending_switch, self._pending_switch_token)
            if self._pending_reasoning:
                level, self._pending_reasoning = self._pending_reasoning, ""
                self.set_reasoning(level)
            self._flush_after_attach()
        elif tag == "recover.recent":
            sid = chat_session_id(result.get("sessions")) or str(result.get("session_id") or "")
            if sid:
                self.resume_session(sid)
            else:
                self._recovering = False
                self._attaching = True
                self.create_session()
        elif tag == "session.list":
            self.sessions_listed.emit(list(result.get("sessions") or []))
        elif tag == "onboarding.start":
            self._stored_id = str(result.get("stored_session_id") or self._stored_id)
            self._session_id = str(result.get("session_id") or "")
            self.session_ready.emit({"session_id": self._session_id,
                                     "stored_session_id": self._stored_id})
            if self._stored_id:
                self._attaching = True
                self.resume_session(self._stored_id)
        elif tag == "process.list":
            rows = [row for row in (result.get("processes") or []) if isinstance(row, dict)]
            self.processes.emit(rows)
        elif tag == "process.kill":
            self.list_processes()
        elif tag == "clarify.lock":
            if str(result.get("status") or "") == "expired":
                self.clarify_expired.emit("")
                self.notice_show.emit({"text": "这个问题已经结束，不用再答了", "level": "info", "ttl_ms": CONFIRM_TTL_MS,
                                       "key": "clarify.expired"})
        elif tag == "model.options:settings":
            if rid != getattr(self, "_settings_rid", rid):
                return
            raw = result.get("providers") if isinstance(result, dict) else None
            rows_now = [dict(row) for row in (raw or []) if isinstance(row, dict)]
            cur_model = str((result or {}).get("model") or "").strip() if isinstance(result, dict) else ""
            cur_provider = str((result or {}).get("provider") or "").strip() if isinstance(result, dict) else ""
            current = f"{cur_provider}:{cur_model}" if cur_model and cur_provider else cur_model
            self.provider_rows.emit(rows_now, current)
            if getattr(self, "_models_after_settings", False):
                self._models_after_settings = False
                self.request_models()
        elif tag == "model.options":
            if rid != getattr(self, "_models_rid", rid):
                return
            items, current = _flatten_options(result)
            self.models.emit(items, current)
        elif tag == "onboarding.set_run":
            self._onboarding_started = False
            self.greet()
        elif tag == "onboarding.state":
            due = result if isinstance(result, dict) else {}
            if due.get("run") and due.get("eligible") and not self._onboarding_started:
                self._start_onboarding()
        elif tag.startswith("model.save_key"):
            row = result.get("provider") or {}
            self.notice.emit(f"已保存 {row.get('slug') or '密钥'}")
            self.request_provider_rows(refresh=True)
            self._models_after_settings = True
            self.key_saved.emit(str(row.get("slug") or ""))
            self.save_result.emit(tag, {"ok": True,
                                        "slug": tag.split(":", 1)[1] if ":" in tag else str(row.get("slug") or "")})
            if self._pending_switch:
                self._switch_model(self._pending_switch, self._pending_switch_token)
        elif tag.startswith("model.set"):
            self._revision = int(result.get("revision") or self._revision or 0)
            gen = result.get("execution_generation")
            if isinstance(gen, int):
                self._generation = gen
            self._switch_payload = {}
            self.request_models()
            self.save_result.emit(tag, {"ok": True})
        elif tag == "config.set:approval":
            self.config_value.emit("approval_mode", str(result.get("value") or ""))
        elif tag == "reasoning.set":
            self._revision = int(result.get("revision") or self._revision or 0)
            gen = result.get("execution_generation")
            if isinstance(gen, int):
                self._generation = gen
            self._reasoning_level = ""
            self._call("config.get", {"key": "reasoning", "session_id": self._session_id},
                       tag="config.get:reasoning")
        elif tag == "config.get:full":
            self.display_config.emit(dict((result.get("config") or {}).get("display") or {}))
        elif tag.startswith("attach:"):
            self._on_attach_done(rid, tag, result)
        elif tag.startswith("cron."):
            payload = dict(result) if isinstance(result, dict) else {}
            # 引擎有些回包是「成功帧里带失败」：`ok: true` 配 `success: false` + `error`（新建走的
            # cronjob 工具就这样）。照单全收会当成保存成功、把编辑页关掉再报错，所以两者都要看。
            payload["ok"] = bool(payload.get("ok", True)) and payload.get("success") is not False
            if not payload["ok"] and not payload.get("message"):
                payload["message"] = str(payload.get("error") or "操作失败")
            self.cron_result.emit(tag, payload)
        elif tag.startswith("config.get:"):
            key = tag.split(":", 1)[1]
            if key == "reasoning":
                self.reasoning_wire.emit(str(result.get("wire") or ""))
            self.config_value.emit(key, str(result.get("value", "")))

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

    def _on_event(self, params: dict) -> None:
        kind = params.get("type") or ""
        sid = str(params.get("session_id") or "")
        if sid and self._session_id and sid != self._session_id:
            return
        payload = params.get("payload") or {}
        for holder in (params, payload):
            gen = holder.get("execution_generation") if isinstance(holder, dict) else None
            if isinstance(gen, int):
                self._generation = gen
        if kind == "gateway.ready":
            self.ready.emit(payload)
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": self._next_id, "method": "client.capabilities",
                         "params": {"server_requests": True}})
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
            running = payload.get("running")
            if running is not None:
                self.status.emit("thinking" if running else "idle")
            effort = payload.get("reasoning_effort")
            if effort is not None:
                self.reasoning_reported.emit(str(effort), str(payload.get("reasoning_effort_wire") or ""))
        elif kind == "cron.changed":
            self.cron_changed.emit()
        elif kind == "approval.cancelled":
            ids = payload.get("request_ids")
            self.approvals_cancelled.emit([str(item) for item in ids] if isinstance(ids, list) else [])
        elif kind == "request.cancel":
            self.request_cancelled.emit(str(payload.get("method") or ""), str(payload.get("id") or ""))
        elif kind == "session.reclaimed":
            self._session_id = ""
        elif kind == "notification.show":
            self.notice_show.emit(dict(payload))
            return
        elif kind == "notification.clear":
            self.notice_clear.emit(str(payload.get("key") or ""))
            return
        elif kind == "reaction":
            self.reaction.emit(str(payload.get("kind") or ""))
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
        self._reset_transport_state()
        if had_turn:
            self.attachment_failed.emit("整条消息", "引擎断开，这句话没发出去")
        if had_pending:
            self.attachment_failed.emit("整条消息", "引擎断开，这条消息没发出去")
        if typed_but_unsent:
            self.attachment_failed.emit("整条消息", "引擎断开，你刚打的这句还没发出去")
        self.closed.emit()
