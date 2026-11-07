"""Qt 前端连引擎的薄客户端：直接说引擎自己的 newline JSON-RPC（tui_gateway）。

设计底线：这里只做运输和分发——把界面上的动作翻译成引擎的方法调用，把引擎的事件翻译成
Qt 信号。会话、模型、工具、审批的判定全部留在引擎里，前端不复制一份逻辑。

进程模型：QProcess 拉起 `python -m tui_gateway.entry`，stdin/stdout 就是 RPC 通道。
"""

from __future__ import annotations

import base64
from collections import deque

STOP_TIMEOUT = 8

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import MutableMapping
from pathlib import Path

from app import winproc
from app.status_stack import CONFIRM_TTL_MS

CREATE_NO_WINDOW = 0x08000000
START_TIME_DRIFT = 200

from PySide6 import QtCore

logger = logging.getLogger(__name__)


def _engine_home_path(root: "Path | str") -> Path:
    """数据根：跟 `start()` 写进环境变量的是同一个值（环境变量优先）。"""
    value = os.environ.get("NOESIA_HOME")
    return Path(value) if value else Path(root) / "data" / "NoesiaHome"


def _lock_owner(home: "Path | str") -> dict:
    """读 `<home>/gateway.lock`：谁正占着这个数据根（读不到 / 读不懂就空 dict）。"""
    try:
        data = json.loads((Path(home) / "gateway.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lock_owner_pid(home: "Path | str") -> int | None:
    """从 `<home>/gateway.lock` 里读出占着这个数据目录的 pid；读不到就 None（锁是引擎的，只读）。"""
    pid = _lock_owner(home).get("pid")
    return pid if isinstance(pid, int) else None


def engine_interpreter(root: "Path | str") -> Path:
    """引擎用哪个解释器：Windows 上用**控制台**的 `python.exe`。

    桌面端是 GUI 进程（自己没控制台），引擎是控制台程序。两种拉法：
    - `pythonw.exe`（GUI 子系统）：引擎没有控制台，于是它下面每一个控制台子进程（git、gh、bash、
      python、powershell…）都各自分配一个**可见**控制台——就是用户桌面上一闪一闪的黑窗，
      而且子进程再往下开的进程同样会闪，靠给每个调用点加 `CREATE_NO_WINDOW` 是盖不住的。
    - `python.exe` + `CREATE_NO_WINDOW`（见 `_EngineProcess`）：引擎自己拿到一个**隐藏**控制台，
      所有后代进程都继承它，一个窗口都不闪。stdio 照样是管道，RPC 不受影响。
    """
    base = Path(root) / ".venv"
    if sys.platform == "win32":
        for name in ("python.exe", "pythonw.exe"):
            candidate = base / "Scripts" / name
            if candidate.is_file():
                return candidate
    return base / "bin" / "python"


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


def project_processes(  # noqa: D401
    project_root: "Path | str",
    exclude: "set[int] | list[int]" = (),
) -> list[int]:
    """跑在本项目目录里的进程（按可执行文件路径判定），排除 `exclude` 里的 pid。

    一个子进程都不起：pid 列表来自 toolhelp 快照、路径来自 QueryFullProcessImageName
    （`app/winproc.py`——`tasklist`/`powershell` 在桌面端会各自闪一个黑窗口）。
    """
    root = os.path.normcase(str(Path(project_root).resolve()))
    skip = set(exclude) | {os.getpid()}
    found: list[int] = []
    for pid in winproc.parent_map():
        if pid in skip:
            continue
        image = winproc.image_path(pid)
        if image and os.path.normcase(os.path.abspath(image)).startswith(root):
            found.append(pid)
    return sorted(found)


def _process_commands() -> dict[int, str]:
    """pid → 命令行（Windows 问一次 PowerShell，**窗口隐藏**；别处读 /proc）。

    为什么非要知道命令行：认「是不是命令行里那台 noesia 会话」只有「入口名」这一条准信。
    只看可执行文件路径不行——项目 venv 里的 pytest、脚本路径里同样带 `noesia`。
    慢一点（1~2 秒）没关系，这条路只在「引擎被占着、要问用户怎么办」时才走。
    """
    if sys.platform != "win32":
        out: dict[int, str] = {}
        for entry in (Path("/proc").iterdir() if Path("/proc").exists() else []):
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
                out[int(entry.name)] = raw.strip()
            except OSError:
                continue
        return out

    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        done = winproc.run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=20
        )
        rows = json.loads(done.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    return {
        int(row["ProcessId"]): str(row.get("CommandLine") or "")
        for row in rows
        if isinstance(row, dict) and "ProcessId" in row
    }


def is_console_client(command: str, image: str) -> bool:
    """这个进程是不是命令行里那台 `noesia` 会话？

    只认入口：`noesia.exe`（`noesia` 命令本身），或 `-m noesia_entry` / `noesia_cli`。
    我们自己的引擎（`tui_gateway`）永远不算会话。
    """
    lowered = command.lower()
    if "tui_gateway" in lowered:
        return False
    if os.path.basename(image).lower() in {"noesia.exe", "noesia"}:
        return True
    return "noesia_entry" in lowered or "noesia_cli" in lowered


def console_clients(
    project_root: "Path | str",
    exclude: "set[int] | list[int]" = (),
) -> list[int]:
    """命令行里还开着的 noesia 会话（= 要请它按 Ctrl+C 退出、然后才轮到启动的那台）。

    判据两条同时成立：可执行文件在本项目目录里、入口是 noesia 自己（见 `is_console_client`）。
    """
    commands = _process_commands()
    if not commands:
        return []
    images = {pid: winproc.image_path(pid) for pid in commands}
    clients: list[int] = []
    for pid in project_processes(project_root, exclude):
        command = commands.get(pid, "")
        if command and is_console_client(command, images.get(pid, "")):
            clients.append(pid)
    return clients



def quit_process(pid: int) -> tuple[bool, str]:
    """请一个进程退出：先替它按 Ctrl+C（让它自己收尾），等不到再硬结束。

    不去调 `taskkill`：桌面端是 GUI 程序，`taskkill` 被它拉起时会闪一个黑窗口；而且中文系统上
    它的提示是乱码，按文字判断「进程不存在」必然失手。全部走 `winproc`（ctypes）。
    """
    if pid == os.getpid():
        return False, "不结束自己"
    return winproc.quiet_exit(pid)


def close_console_sessions(project_root) -> list[str]:
    """把命令行里开着的 noesia 会话请走：先按 Ctrl+C 让它自己收尾，等不到才连子进程一起结束。

    认谁：`winproc.console_sessions()` 只认映像路径（项目虚拟环境里的 `noesia.exe`），
    不问命令行、不起子进程。启动界面之前做这一步，命令行那台就不会跟界面抢数据根。
    """
    notes: list[str] = []
    for pid in winproc.console_sessions(project_root):
        _, note = winproc.quiet_exit(pid)
        logger.info("命令行会话 %s：%s", pid, note)
        notes.append(note)
    return notes


def _same_incarnation(pid: int, recorded_start: object) -> bool:
    """锁里写的 pid 现在还真的是当初那个进程吗？

    锁文件是**自证的**：pid、kind、argv 都是它自己写的。硬杀、崩溃、断电之后锁会留在盘上，
    Windows 再把那个 pid 复用给别的程序（浏览器、编辑器），只按「进程还活着 + argv 在项目里」
    判就会把陌生人连同它的子进程一起结束。引擎写锁时留了进程创建时间，就拿它比；
    锁里没有、或读数拿不到，就是「说不清」——说不清一律不动手，让用户点卡片确认。
    """
    if sys.platform != "win32":
        return True
    current = winproc.start_time(pid)
    if not isinstance(recorded_start, int) or current is None:
        logger.info("pid %s 的创建时间对不上账（锁里 %r，实际 %r），不认它", pid, recorded_start, current)
        return False
    return abs(current - recorded_start) <= START_TIME_DRIFT


def blocking_owner(home: "Path | str", project_root: "Path | str") -> int | None:
    """占着数据根、且确实是**本项目的**那个后台进程的 pid（不是就别动）。

    只认五个条件同时成立：锁里写的是 `noesia-gateway`、锁说的 home 就是我们的 home、
    argv 落在本项目目录里、**那个 pid 现在还活着**、**它的创建时间和锁里记的对得上**。
    任一条不成立就返回 None——宁可不杀，也不去结束别人的进程；而且硬杀/自退之后锁文件会留在
    原地（里面还是旧 pid），不判活、不比对创建时间，就会把复用了这个 pid 的陌生程序当成「还占着」。
    """
    data = _lock_owner(home)
    pid = data.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return None
    if not _process_alive(pid):
        return None
    if str(data.get("kind") or "") != "noesia-gateway":
        return None
    lock_home = str(data.get("noesia_home") or "")
    if lock_home and Path(lock_home).resolve() != Path(home).resolve():
        return None
    argv = data.get("argv") or []
    root = Path(project_root).resolve()
    if not _same_incarnation(pid, data.get("start_time")):
        logger.info("锁里的 pid %s 已经不是当初那个进程（pid 被复用了），不动它", pid)
        return None
    entry = str(argv[0]) if argv else f"{root}{os.sep}src{os.sep}engine{os.sep}tui_gateway{os.sep}entry.py"
    try:
        Path(entry).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return pid


def _process_alive(pid: int) -> bool:
    """这个 pid 还活着吗（`os.kill(pid, 0)` 在 Windows 上是 TerminateProcess，不能用）。"""
    return winproc.process_alive(pid)


def stop_blocking_owner(home: "Path | str", project_root: "Path | str") -> tuple[bool, str]:
    """结束占着数据根的那个本项目后台进程。返回 (成功?, 给人看的一句话)。"""
    pid = blocking_owner(home, project_root)
    if pid is None:
        return False, "看不到是谁占着（锁里没有可用信息），没有动任何进程"
    ok, note = quit_process(pid)
    return ok, note.replace("已结束", "已结束占着数据目录的后台进程", 1) if ok else note



def describe_engine_exit(code: int, stderr_tail, home: "Path | str") -> str:
    """引擎非正常退出时该给用户看的那句话。

    引擎抢不到数据根时只在 stderr 留一行
    `[gateway-exit] refused: <home> is owned by a running gateway; attach to it instead`
    然后 exit 1——界面原来把 stderr 记在 DEBUG、并且照样报「已就绪」，用户只看到人物能出来、
    说话没反应。这里把那一行翻译成「谁占着、怎么放开」。
    """
    lines = [str(line).strip() for line in (stderr_tail or []) if str(line).strip()]
    if any("is owned by a running gateway" in line for line in lines):
        pid = _lock_owner_pid(home)
        if pid:
            return (
                f"引擎没起来：数据目录被另一个 Noesia 后台进程占着（pid {pid}）。"
                "多半是命令行里聊过天、Ctrl+C 退出来的会话留下的。"
                "界面会问你要不要关掉那个会话、结束它再重试。"
            )
        return (
            f"引擎没起来：数据目录 {Path(home)} 被另一个 Noesia 后台进程占着。"
            "先把它结束掉（`noesia gateway status` 能看进程号），再重开界面。"
        )
    if lines:
        return f"引擎退出，退出码 {code}：{lines[-1]}"
    return f"引擎退出，退出码 {code}"

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


def _model_value(option_id: str) -> str:
    """界面内部的 `slug:model` → 引擎要的 `<模型> --provider <供应商> --global`。

    引擎用 CLI 的 `parse_model_switch_args` 解析 model 值（`methods_config_set.py::_set_model`）：
    少了 `--provider`，它拿不到是哪一家，只能在当前那家的清单里找。

    `--global` 不能省：不带持久化标志时 `resolve_persist_behavior` 判定为「探索性切换」
    （回包 scope=session），只就地换掉当前会话的 agent，不写 config.yaml。界面上选模型
    是改默认（桌面端 Settings → Models 也是写配置），所以新会话一开就退回磁盘里那个，
    看着就是「切了没生效」。
    """
    slug, sep, name = str(option_id or "").partition(":")
    if sep and slug and name:
        return f"{name} --provider {slug} --global"
    return str(option_id or "")


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


class _EnvView:
    """`QProcess.processEnvironment()` 的最小替身：够取值、够排错就行。"""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def value(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def keys(self) -> list[str]:
        return list(self._values)


class _EngineProcess(QtCore.QObject):
    """引擎子进程：`subprocess` + `CREATE_NO_WINDOW` 起的，不是 QProcess。

    为什么要自己起进程：PySide6 没绑 `QProcess.setCreateProcessArgumentsModifier`，加不了
    `CREATE_NO_WINDOW`；而 `pythonw.exe` 那种「没有控制台」的拉法会让引擎的每个控制台子进程各开一个
    可见黑窗。用控制台解释器 + 这个 flag，引擎有一个隐藏控制台，所有后代都继承它，没有窗口可闪。

    对外的名字和 Qt 的 QProcess 一一对应（`start`/`write`/`shutdown` 那些调用点不用改）。
    """

    readyReadStandardOutput = QtCore.Signal()
    readyReadStandardError = QtCore.Signal()
    finished = QtCore.Signal(int, int)
    errorOccurred = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proc: "subprocess.Popen[bytes] | None" = None
        self._out = b""
        self._err = b""
        self._lock = threading.Lock()
        self._env: dict[str, str] | None = None
        self._cwd: str | None = None

    def reset_buffers(self) -> None:
        """把管道上攒着没读的字节扔掉：换一趟引擎进程时，旧字节会和新的第一帧粘一起。"""
        with self._lock:
            self._out = b""
            self._err = b""


    def setProcessChannelMode(self, _mode) -> None:  # noqa: N802 —
        """占位：subprocess 天生就是两个独立管道，没有别的模式可选。"""

    def setProcessEnvironment(self, env: dict) -> None:  # noqa: N802
        self._env = dict(env)

    def setWorkingDirectory(self, path: str) -> None:  # noqa: N802
        self._cwd = path

    def workingDirectory(self) -> str:  # noqa: N802
        return self._cwd or ""

    def processEnvironment(self) -> "_EnvView":  # noqa: N802
        return _EnvView(self._env or {})

    def start(self, program: str, args: list[str]) -> None:  # noqa: N802
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                [program, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                bufsize=0,
                creationflags=flags,
            )
        except OSError as exc:
            self._proc = None
            self.errorOccurred.emit(str(exc))
            return
        threading.Thread(target=self._pump, args=(self._proc.stdout, True), daemon=True).start()
        threading.Thread(target=self._pump, args=(self._proc.stderr, False), daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _pump(self, stream, is_out: bool) -> None:
        """把管道里的字节攒起来并发信号——读的动作在后台线程，界面线程只管取。"""
        while True:
            try:
                chunk = os.read(stream.fileno(), 65536) if stream is not None else b""
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._lock:
                if is_out:
                    self._out += chunk
                else:
                    self._err += chunk
            if is_out:
                self.readyReadStandardOutput.emit()
            else:
                self.readyReadStandardError.emit()

    def _watch(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        self.finished.emit(code, 0)


    def write(self, data: bytes) -> int:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return -1
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError):
            return -1
        return len(data)

    def readAllStandardOutput(self) -> bytes:  # noqa: N802
        with self._lock:
            data, self._out = self._out, b""
        return data

    def readAllStandardError(self) -> bytes:  # noqa: N802
        with self._lock:
            data, self._err = self._err, b""
        return data


    def state(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return QtCore.QProcess.ProcessState.Running
        return QtCore.QProcess.ProcessState.NotRunning

    def processId(self) -> int:  # noqa: N802
        proc = self._proc
        return int(proc.pid) if proc is not None and proc.poll() is None else 0

    def closeWriteChannel(self) -> None:  # noqa: N802
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def terminate(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def waitForFinished(self, msecs: int = 30000) -> bool:  # noqa: N802
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=max(msecs, 0) / 1000)
        except subprocess.TimeoutExpired:
            return False
        return True


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
    most_recent = QtCore.Signal(dict)
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
    reasoning_delta = QtCore.Signal(str)
    reasoning_done = QtCore.Signal(str)
    reasoning_block = QtCore.Signal(str)
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
        self._proc = _EngineProcess(self)
        self._proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.readyReadStandardOutput.connect(self._read_stdout)
        self._proc.readyReadStandardError.connect(self._read_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)

        self._buffer = ""
        self._stderr_tail: list[str] = []
        self._voice_seq = 0
        self._voice_jobs: dict[int, str] = {}
        self._out_decoder = None
        self._err_decoder = None
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
        self._sent: dict[int, dict] = {}
        self._retried: set[int] = set()
        self._replay: list[dict] = []
        self._recovering = False
        self._attaching = False
        self._attach_since = 0.0
        self._outbox: deque[bytes] = deque()
        self._write_failed = False
        self._recover_rounds = 0
        self._pending_text = ""
        self._pending_turns: list[tuple[str, list]] = []
        self._turn: dict | None = None
        self._turn_seq = 0
        self._attach_names: dict[int, tuple[str, bool]] = {}
        self._started = False


    def _engine_home(self) -> Path:
        """数据根（和写进子进程环境变量的是同一个值）。"""
        return _engine_home_path(self._root)

    def blocking_owner(self) -> int | None:
        """谁占着数据根（只认本项目的后台进程）；没占用就是 None。"""
        return blocking_owner(self._engine_home(), self._root)

    def stop_blocking_owner(self) -> tuple[bool, str]:
        """结束占着数据根的本项目后台进程（只在确认是本项目的时候才动手）。"""
        return stop_blocking_owner(self._engine_home(), self._root)

    def _own_pids(self) -> set[int]:
        """不该被当成「别人的进程」的那几个：本进程 + 我们拉起的引擎。"""
        pids = {os.getpid()}
        if self._proc is not None:
            pids.add(int(self._proc.processId()))
        return pids

    def console_clients(self) -> list[int]:
        """命令行里还开着的那台 noesia 会话（不含我们自己的进程）。"""
        return console_clients(self._root, self._own_pids())

    def _reset_transport_state(self) -> None:
        """重新拉引擎之前，把上一趟的传输状态清干净（第一次启动时它们本来就是空的）。

        新后端里没有上一趟那些 rid 的上下文，所以攒着待补发的请求、附件登记一律作废；
        `_recovering` 要是崩在救回途中卡住，会把后面的恢复永久关掉，必须一起复位。
        运行时 id 只属于刚退出的那个后端进程；存储 id（`_stored_id`）跨重启不变，留着。
        """
        self._buffer = ""
        self._stderr_tail.clear()
        self._out_decoder = None
        self._err_decoder = None
        self._pending.clear()
        self._outbox.clear()
        self._write_failed = False
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
        self._proc.reset_buffers()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._reset_transport_state()
        python = engine_interpreter(self._root)
        engine = self._root / "src" / "engine"

        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONPATH"] = str(engine)
        env["NOESIA_HOME"] = str(self._engine_home())
        env["NOESIA_DESKTOP"] = "1"
        env["NOESIA_DASHBOARD_SESSION_TOKEN"] = uuid.uuid4().hex
        env["NOESIA_PARENT_PID"] = str(os.getpid())
        env["TERMINAL_CWD"] = str(self._workspace)
        env["PYTHONUNBUFFERED"] = "1"
        bash = bundled_bash(self._root)
        if bash is not None:
            env["NOESIA_GIT_BASH_PATH"] = str(bash)
        with_bundled_path(env, self._root)
        self._proc.setProcessEnvironment(env)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._proc.setWorkingDirectory(str(self._workspace))
        self._launch(str(python))
        logger.info("引擎启动：%s -m tui_gateway.entry（工作区 %s）", python, self._workspace)

    def _launch(self, python: str) -> None:
        """真正把进程拉起来；测试里换成只记录参数的假实现，不用真起引擎。"""
        self._proc.start(python, ["-m", "tui_gateway.entry"])

    def shutdown(self) -> None:
        """关掉引擎的输入让它自己收尾；不走就强杀——退出路径上不许留进程。"""
        logger.info("[引擎] 主动 shutdown（调用方：%s）", " <- ".join(
            frame.rsplit("\n", 1)[-1].strip() for frame in traceback.format_stack(limit=3)[:-1]
        ))
        if self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._proc.closeWriteChannel()
            if not self._proc.waitForFinished(STOP_TIMEOUT * 1000):
                logger.warning("引擎 %s 秒内没退出，强杀", STOP_TIMEOUT)
                self._proc.terminate()
                if not self._proc.waitForFinished(2000):
                    self._proc.kill()

    def is_running(self) -> bool:
        return self._proc.state() == QtCore.QProcess.ProcessState.Running

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
        self._attaching = True
        self._attach_since = time.monotonic()
        params = {"cwd": cwd or str(self._workspace), "cwd_explicit": True}
        if self._reasoning_effort:
            params["reasoning_effort"] = self._reasoning_effort
        rid = self._call("session.create", params, tag="session.create")
        if rid not in self._sent:
            self._attaching = False
            self._attach_since = 0.0
        self.reasoning_pending.emit(False)

    def resume_session(self, session_id: str) -> None:
        self._stored_id = session_id or self._stored_id
        self._attaching = True
        self._attach_since = time.monotonic()
        self.reasoning_pending.emit(True)
        rid = self._call("session.resume", {"session_id": session_id}, tag="session.resume")
        if rid not in self._sent:
            self._attaching = False
            self._attach_since = 0.0

    def request_most_recent(self) -> None:
        self._call("session.most_recent", {}, tag="session.most_recent")

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
        params: dict = {"session_id": self._session_id, "text": text}
        if queued:
            params["queued"] = True
        self._call("prompt.submit", params, tag="prompt.submit")

    def submit_rewind(self, text: str, row_id: int, confirm_empty: bool = False,
                      confirm_deep: bool = False) -> None:
        """截断后重发：引擎把这条用户消息（含它）之后的历史全删掉，正文当成新一轮再发。

        截断点只认行号：持久会话上「只给第几条」的截断会被引擎直接拒（4004），
        行号来自 `prompt.submit` 回包的 `user_row_id`，或 resume 历史行里的 `row_id`。

        引擎另有两道闸，得客户端明确点头才放行（我们按窗口里实际有几步来判断）：
        `confirm_empty_truncate` = 这一截会把整场对话清空（截的是第一句）；
        `confirm_deep_truncate` = 这条后面还有别的问答，会被一起删掉。
        """
        if self._needs_attach():
            self.error.emit("会话还没准备好，等这条回复出来再重发")
            return
        params = {
            "session_id": self._session_id,
            "text": text,
            "truncate_before_row_id": int(row_id),
            "confirm_truncate": True,
        }
        if confirm_empty:
            params["confirm_empty_truncate"] = True
        if confirm_deep:
            params["confirm_deep_truncate"] = True
        self._call("prompt.submit", params, tag="prompt.submit")

    IMAGE_ONLY_CAPTION = "What do you see in this image?"

    def submit_with_attachments(self, text: str, items=(), queued: bool = True) -> None:
        """发一句话，附件先挂上：文件带回的 `@file:` 引用拼在正文前（跟桌面 buildContextText 一致）。"""
        items = list(items or ())
        if not items:
            self.submit(text, queued=queued)
            return
        if self._needs_attach() or self._turn is not None:
            self._pending_turns.append((text, items))
            self._ensure_session()
            return
        self._dispatch_turn(text, items)

    def _dispatch_turn(self, text: str, items: list) -> None:
        self._turn_seq += 1
        tag = f"attach:turn:{self._turn_seq}"
        self._turn = {
            "text": text,
            "refs": [],
            "attached": 0,
            "left": len(items),
            "images": sum(1 for item in items if item.get("kind") == "image"),
            "tag": tag,
        }
        for item in items:
            method, params, name = self._attach_frame(item)
            rid = self._call(method, {**params, "session_id": self._session_id}, tag=tag)
            self._attach_names[rid] = (name, method == "file.attach")
            if rid not in self._sent:
                self._on_attach_done(rid, tag, {"attached": False, "message": "引擎没接住这个附件"})

    @staticmethod
    def _attach_frame(item: dict) -> tuple[str, dict, str]:
        """一条附件对应引擎的哪个接口：图给路径/字节，其它文件给路径。"""
        name = str(item.get("name") or "附件")
        if item.get("data"):
            encoded = base64.b64encode(item["data"]).decode("ascii")
            return "image.attach_bytes", {"content_base64": encoded, "filename": name}, name
        path = str(item.get("path") or "")
        if item.get("kind") == "image":
            return "image.attach", {"path": path}, name
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
        self._call("prompt.submit", {"session_id": self._session_id, "text": text}, tag="prompt.submit")
        self._flush_after_attach()

    def interrupt(self) -> None:
        if not self._session_id:
            return
        self._call("session.interrupt", {"session_id": self._session_id})

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

        引擎的 `config.set model` 只认带 `session_id` 的请求——没会话时它直接回 4001
        （"requires a live session"），既不会写配置也不会切。所以没有会话就先建一个，
        等 `session_ready` 回来再切：这才是「保存并启用」该走的顺序。

        value 是 CLI 的写法 `<模型> --provider <供应商>`（引擎用 `parse_model_switch_args`
        解析，见 `tui_gateway/methods_config_set.py::_set_model`）。界面内部用 `slug:model`
        当 id，传出去之前必须换成这个语法：直接发 `slug:model` 会被拆成
        provider=空、model=":slug:model"，引擎回 5001「模型不在这一家的清单里」，
        看起来就是「选了但没切过去」。
        """
        if not model:
            return
        value = _model_value(model)
        self._pending_switch = model
        self._pending_switch_token = token
        if self._session_id:
            self._pending_switch = ""
            self._pending_switch_token = ""
            self._call("config.set", {"key": "model", "value": value,
                                      "session_id": self._session_id}, tag=self._switch_tag(token))
        else:
            self.create_session("")

    @staticmethod
    def _switch_tag(token: str = "") -> str:
        """切换模型的 tag。带轮次编号：两轮「保存并启用」叠在一起时，上一轮的迟到回执
        才不会被当成这一轮的结果（否则窗口按旧结果关掉，引擎里跑的还是上一个模型）。"""
        return f"config.set:model:{token}" if token else "config.set:model"

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
        """推理强度：`config.set reasoning` 是会话级的（引擎 `_SESSION_SCOPED_KEYS` 里有它）。"""
        if not level:
            return
        if not self._session_id:
            self._pending_reasoning = level
            self.create_session("")
            return
        self._call("config.set", {"key": "reasoning", "value": level,
                                  "session_id": self._session_id}, tag="config.set:reasoning")

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

    def greet(self) -> None:
        """第一次见面：先问引擎「这台机器该不该跑引导」，该跑且有模型能撑起第一轮，才让它开场。

        为什么不是直接 `onboarding.start`：引导要一个可用的供应商才能起（引擎回 5080），
        而「界面开窗」和「密钥配好」不是同一时刻——先开窗后填 key 的人会永远错过引导。
        所以每次猜「可能要跑」都先问 `onboarding.state`，由引擎按
        `run`（配置/从没用过）和 `eligible`（有 provider）自己判断；已经做过就什么都不发。
        """
        self._call("onboarding.state", {}, tag="onboarding.state")

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
        }, tag="onboarding.start")
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


    def _call(self, method: str, params: dict, *, tag: str = "") -> int:
        cutoff = time.monotonic() - self._SENT_TTL_S
        for old, frame in list(self._sent.items()):
            if float(frame.get("at") or 0) < cutoff:
                self._sent.pop(old, None)
                self._pending.pop(old, None)
        self._next_id += 1
        rid = self._next_id
        if tag:
            self._pending[rid] = tag
        self._sent[rid] = {
            "method": method, "params": dict(params), "tag": tag, "at": time.monotonic()
        }
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            self._pending.pop(rid, None)
            self._sent.pop(rid, None)
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
            if replay["method"] not in {"session.resume", "session.create", "session.most_recent"}:
                params["session_id"] = self._session_id
            new_rid = self._call(replay["method"], params, tag=replay["tag"])
            if replay.get("attach") is not None:
                self._attach_names[new_rid] = replay["attach"]
        if not self._session_id or self._attaching:
            return
        if self._pending_turns:
            text, items = self._pending_turns.pop(0)
            self._dispatch_turn(text, items)
            return
        if self._pending_text:
            text, self._pending_text = self._pending_text, ""
            self._call("prompt.submit", {"session_id": self._session_id, "text": text}, tag="prompt.submit")

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
        if method in {"session.create", "session.resume", "session.most_recent"}:
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
            self._call("session.most_recent", {}, tag="recover.recent")
            return
        self._call("session.resume", {"session_id": self._stored_id}, tag="session.resume")

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
        self._call("voice.transcribe.download", {}, tag="voice.transcribe.download")

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

    WRITE_CHUNK = 32 * 1024
    ATTACH_TIMEOUT_S = 15.0
    _SENT_TTL_S = 120.0

    def _write(self, frame: dict) -> bool:
        """发一帧；返回是否真的进了管道（引擎没起、或管道当场拒收时 false）。"""
        if not self.is_running():
            self.failed.emit("引擎未运行，指令没发出去")
            return False
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        self._write_failed = False
        self._outbox.append(data)
        self._pump_writes()
        if self._write_failed:
            try:
                self._outbox.remove(data)
            except ValueError:
                pass
            return False
        return True

    def _pump_writes(self) -> None:
        """分块把待发字节推进管道，块与块之间让出一次事件循环。

        `proc.stdin` 是 `bufsize=0` 的管道：一张图 base64 之后可能几 MB，一次性 `write`
        在引擎来不及排空（管道缓冲满）时会**卡在 GUI 线程上**，界面就假死。分块后每次最多
        32KB，即使某一小块要等引擎，界面也能在块之间喘口气。
        """
        while self._outbox:
            if not self.is_running():
                self._outbox.clear()
                self._write_failed = True
                self.failed.emit("引擎未运行，指令没发出去")
                return
            chunk = self._outbox[0]
            if len(chunk) > self.WRITE_CHUNK:
                self._outbox[0] = chunk[self.WRITE_CHUNK:]
                written = self._proc.write(chunk[:self.WRITE_CHUNK])
                if written is not None and written < 0:
                    self._outbox.clear()
                    self._write_failed = True
                    self.failed.emit("引擎没接住指令，这一步没送出去")
                    return
                QtCore.QTimer.singleShot(0, self._pump_writes)
                return
            self._outbox.popleft()
            written = self._proc.write(chunk)
            if written is not None and written < 0:
                self._write_failed = True
                self.failed.emit("引擎没接住指令，这一步没送出去")
                return


    def _read_stdout(self) -> None:
        if not self._started:
            return
        decoder = self._out_decoder
        if decoder is None:
            import codecs

            decoder = self._out_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chunk = decoder.decode(bytes(self._proc.readAllStandardOutput()))
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self._dispatch(line)

    def _read_stderr(self) -> None:
        """引擎的 stderr：按 WARNING 落日志（原来写 DEBUG，出了事日志里一行都看不到），
        并且留住最后几行——退出码非 0 时用它拼出人话原因。"""
        decoder = self._err_decoder
        if decoder is None:
            import codecs

            decoder = self._err_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(bytes(self._proc.readAllStandardError())).strip()
        if not text:
            return
        for line in text.splitlines():
            line = line.strip()
            if line:
                self._stderr_tail.append(line)
        del self._stderr_tail[:-6]
        logger.warning("引擎 stderr：%s", text[:400])

    def _exit_reason(self, code: int, tail: list[str] | None = None) -> str:
        """引擎非正常退出时说清到底怎么了（别只丢一句「退出码 1」）。"""
        return describe_engine_exit(code, self._stderr_tail if tail is None else tail, self._engine_home())

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
            if tag.startswith("model.save_key") or tag.startswith("config.set:model"):
                if tag.startswith("model.save_key"):
                    self._pending_switch = ""
                    self._pending_switch_token = ""
                self.save_result.emit(tag, {"ok": False, "message": str(message)})
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
                rows = [
                    {
                        "role": str(row.get("role", "")),
                        "text": str(row.get("text", "")),
                        "display_kind": str(row.get("display_kind") or ""),
                        "row_id": row.get("row_id"),
                    }
                    for row in (result.get("messages") or [])
                    if isinstance(row, dict) and str(row.get("display_kind") or "") != "hidden"
                ]
                self.history.emit(rows)
            self.session_ready.emit(result)
            if self._pending_switch:
                self._switch_model(self._pending_switch, self._pending_switch_token)
            if self._pending_reasoning:
                level, self._pending_reasoning = self._pending_reasoning, ""
                self.set_reasoning(level)
            self._flush_after_attach()
        elif tag == "recover.recent":
            sid = str(result.get("session_id") or "")
            if sid:
                self.resume_session(sid)
            else:
                self._recovering = False
                self._attaching = True
                self.create_session()
        elif tag == "session.most_recent":
            self.most_recent.emit(result)
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
        elif tag.startswith("config.set:model"):
            self.request_models()
            self.save_result.emit(tag, {"ok": True})
        elif tag == "config.set:approval":
            self.config_value.emit("approval_mode", str(result.get("value") or ""))
        elif tag == "config.set:reasoning":
            self.reasoning_wire.emit(str(result.get("wire") or ""))
            self.config_value.emit("reasoning", str(result.get("value") or ""))
        elif tag == "config.get:full":
            self.display_config.emit(dict((result.get("config") or {}).get("display") or {}))
        elif tag.startswith("attach:"):
            self._on_attach_done(rid, tag, result)
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
        if kind == "gateway.ready":
            self.ready.emit(payload)
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": self._next_id, "method": "client.capabilities",
                         "params": {"server_requests": True}})
        elif kind == "message.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self.delta.emit(text)
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

    def _on_proc_error(self, message: str) -> None:
        """进程级错误：多半是根本没起来（解释器找不到、权限不够）。

        起不来就不会有 `finished`，`_started` 会一直挂在 True，之后每次 `start()` 直接 return，
        引擎再也拉不起来。这里只在「确实没在跑」时复位，运行中的偶发错误交给 `finished` 收。
        """
        if not self.is_running():
            self._started = False
        self.failed.emit(str(message))

    def _on_finished(self, code: int, _status) -> None:
        self._started = False
        stderr_tail = list(self._stderr_tail)
        typed_but_unsent = bool(self._pending_text)
        self._reset_transport_state()
        if self._turn is not None:
            self._turn = None
            self.attachment_failed.emit("整条消息", "引擎断开，这句话没发出去")
        if self._pending_turns:
            self._pending_turns.clear()
            self.attachment_failed.emit("整条消息", "引擎断开，这条消息没发出去")
        if typed_but_unsent:
            self.attachment_failed.emit("整条消息", "引擎断开，你刚打的这句还没发出去")
        self.closed.emit()
        if code not in (0, 15):
            self.failed.emit(self._exit_reason(code, stderr_tail))
