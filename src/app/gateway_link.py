"""外壳连引擎的唯一通道：这个 home 的 gateway 的 `/api/ws`。

引擎里一个 home 只有一个常驻 owner（`gateway run`）：会话、模型、工具、审批、定时任务都在
那一个进程里，终端聊天、桌面、dashboard 都是它的客户端，靠 `/api/ws` 接进同一场。外壳退出
只关掉自己这条连接，网关按空闲自己退；下次 `ensure_gateway_runtime` 再接回同一个 owner。

线程模型：Qt 线程里不能跑 asyncio，所以连接、读帧、发帧都在一条后台线程里（它自己的事件
循环）。收帧用信号送回 Qt 线程；发帧先进队列，由那条循环里的一个任务搬上连接。
"""

from __future__ import annotations

import asyncio
from collections import deque
import os
import sys
import threading
import time
from pathlib import Path
from typing import cast

import PySide6.QtCore as QtCore
from websockets.typing import Subprotocol

# `ensure_gateway_runtime` 的结论翻成人话；不认识的再退回状态名，绝不把英文 reason_code 甩给用户。
_STATE_TEXT = {
    "starting": "引擎还在启动",
    "starting: deadline": "引擎启动超时",
    "draining": "引擎正在重启",
    "runtime_exited": "引擎刚起来就退了",
    "not_ready": "引擎没准备好",
    "dead": "引擎没起来",
    "absent": "这个 home 没有引擎",
}


def _describe(result) -> str:
    reason = str(getattr(result, "reason_code", "") or "")
    state = str(getattr(result, "state", "") or "")
    text = _STATE_TEXT.get(reason) or _STATE_TEXT.get(state) or "引擎连不上"
    detail = str(getattr(result, "detail", "") or "").strip()
    return f"{text}（{detail[-200:]}）" if detail else text


def gateway_pid() -> int:
    """这个 home 现在那个 owner 的 pid；拿不到就 0。"""
    try:
        from gateway import status as gateway_status

        return int(gateway_status.get_running_pid(cleanup_stale=False) or 0)
    except Exception:  # noqa: BLE001 — 只是「别把自己的进程当别人的」，查不到就当没有
        return 0


class GatewayLink(QtCore.QObject):
    """一条到 `/api/ws` 的连接：连接、读、写全在自己的后台线程里。"""

    frame = QtCore.Signal(str)
    ready = QtCore.Signal(int)
    gone = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._home: Path | None = None
        self._engine: Path | None = None
        self._workspace: Path | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stop = asyncio.Event()
        self._out: deque[str] = deque()
        self._lock = threading.Lock()
        self._connected = False
        self._pid = 0
        # 网关的 HTTP 根（`http://127.0.0.1:<port>`）：REST 和 `/api/ws` 是同一个服务，
        # 历史分页走它的 `/api/sessions/...`。连上才有值。
        self.api_origin = ""
        # 连上时留下端点：REST 要凭一张一次性原生票据（见 `native_http_ticket`）。
        self._endpoint = None
        self._api_home = None

    def configure(self, *, home, engine, workspace) -> None:
        self._home = Path(home)
        self._engine = Path(engine)
        self._workspace = Path(workspace)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="gateway-link", daemon=True)
        self._thread.start()

    def is_running(self) -> bool:
        return self._connected

    def pid(self) -> int:
        return self._pid

    def send(self, text: str) -> bool:
        """把一整帧丢进发送队列；没连上就返回 False，调用方据此报「没送出去」。"""
        if not self._connected:
            return False
        with self._lock:
            self._out.append(text)
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return False
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            return False
        return True

    def native_http_ticket(self) -> str:
        """给 REST 签一张原生票据：和 WS 同一个签发口（网关的私有控制通道），只是 purpose 不同。

        本地桌面不带 dashboard 会话令牌，`web_server.auth_middleware` 会把裸 `/api/...` 请求 401 掉；
        带这张票（`x-noesia-gateway-ticket` 头、回环、无 Origin）才按「本机同用户」放行。
        票据一次性，每次请求现签。
        """
        if self._endpoint is None or self._api_home is None:
            return ""
        try:
            from noesia_cli.gateway_client import _session_ticket

            return _session_ticket(self._api_home, self._endpoint, purpose="native-http")
        except Exception:  # noqa: BLE001 — 签不出来就让调用方报「没取到」，别把开窗拖在这儿
            return ""

    def stop(self, timeout: float = 8.0) -> None:
        # 退出前刚排进去的那几帧（例如让人不在时的审批/回合收尾的 `session.interrupt`）
        # 要真的发出去：一 stop 就取消写任务，队列里的帧会烂在这儿。
        deadline = time.monotonic() + 0.5
        while self._out and time.monotonic() < deadline:
            time.sleep(0.01)
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None
        self._connected = False

    # ---- 后台线程 ----

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 — 连不上就报出来，界面自己决定怎么显示
            self._connected = False
            self.failed.emit(f"引擎连接失败：{type(exc).__name__}: {exc}")
            self.gone.emit("引擎连接失败")

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        engine, home = self._engine, self._home
        if engine is None or home is None:
            raise RuntimeError("GatewayLink 还没 configure：没有数据根和引擎目录")
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        os.environ["NOESIA_HOME"] = str(home)
        os.environ["NOESIA_DESKTOP"] = "1"
        if self._workspace is not None:
            os.environ.setdefault("TERMINAL_CWD", str(self._workspace))

        from noesia_cli.gateway_client import (
            GATEWAY_WS_PROTOCOL, GatewayClientError, _session_ticket, gateway_ws_target,
        )
        from noesia_cli.gateway_runtime import attachable, ensure_gateway_runtime
        from websockets.asyncio.client import connect

        # 断线不是终局：引擎会因空闲退出（`gateway.unmanaged_idle_exit_seconds`），
        # 网络/管道也会断。一次连不上就退出去，界面从那一秒起就再也收不到东西
        # （聊天停在最后一帧，看起来像「一直转圈」）。退避重连，连上就复位。
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._attach_once(ensure_gateway_runtime, attachable, connect,
                                        _session_ticket, gateway_ws_target, GATEWAY_WS_PROTOCOL,
                                        home, GatewayClientError)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 记录并重试，不把界面卡死在断线上
                self._connected = False
                self.failed.emit(f"引擎连接失败：{type(exc).__name__}: {exc}")
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)

    async def _attach_once(self, ensure_gateway_runtime, attachable, connect, _session_ticket,
                           gateway_ws_target, GATEWAY_WS_PROTOCOL, home, GatewayClientError) -> None:
        result = await asyncio.to_thread(ensure_gateway_runtime, home, idle_exit=True)
        if not attachable(result):
            message = _describe(result)
            self.failed.emit(message)
            self.gone.emit(message)
            return
        endpoint = result.endpoint
        self.api_origin = str(getattr(endpoint, "api_origin", "") or "").rstrip("/")
        self._endpoint = endpoint
        self._api_home = home
        ticket = await asyncio.to_thread(_session_ticket, home, endpoint)
        url, protocols = gateway_ws_target(endpoint, ticket)
        try:
            async with connect(url, subprotocols=cast("list[Subprotocol]", protocols) if protocols else None,
                               open_timeout=10, max_size=8 * 1024 * 1024, proxy=None) as ws:
                if protocols and ws.subprotocol != GATEWAY_WS_PROTOCOL:
                    self.failed.emit("引擎协议对不上，需要更新或重启引擎")
                    return
                self._connected = True
                self._pid = gateway_pid()
                self.ready.emit(self._pid)
                tasks = [
                    asyncio.create_task(self._read(ws)),
                    asyncio.create_task(self._write(ws)),
                    asyncio.create_task(self._stop.wait()),
                ]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if task is tasks[2]:
                            continue
                        error = task.exception()
                        if error is not None:
                            self.failed.emit(
                                f"引擎连接断了：{type(error).__name__}: {error}"
                            )
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except GatewayClientError as exc:
            self.failed.emit(str(exc))
        finally:
            self._connected = False
            self.gone.emit("引擎断开")

    async def _read(self, ws) -> None:
        async for message in ws:
            self.frame.emit(
                message if isinstance(message, str) else message.decode("utf-8", "replace")
            )

    async def _write(self, ws) -> None:
        wake = self._wake
        if wake is None:
            return
        while True:
            if not self._out and not wake.is_set():
                await wake.wait()
            wake.clear()
            with self._lock:
                batch = list(self._out)
                self._out.clear()
            for text in batch:
                await ws.send(text)
