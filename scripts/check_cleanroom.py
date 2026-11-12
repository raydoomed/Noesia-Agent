#!/usr/bin/env python3
"""干净房间体检：在本机代码上挂一个空的 data/，看能不能真的跑起来。

为什么要有这个脚本：本机上的 `data/`（真实记忆、真实会话）会掩盖「代码本身能不能跑」的问题——
所以这里在临时目录里造一个空的 data/，代码就地用仓库里的那份（不复制），
`init_data` → 起网关 → 选一次模型 → 发一句话 → 收到回复，全程不碰本机的数据。

用法：
    python scripts/check_cleanroom.py          # 临时目录跑一遍，跑完删掉
    python scripts/check_cleanroom.py --keep   # 留下临时目录，方便看引擎日志

退出码：0 正常 / 1 有问题（遵循 docs/conventions.md §7）。

注意：
- 需要一个能用的模型凭据：根目录 `.env` 的变量带进进程环境，`data/NoesiaHome/.env` 复制进房间
  （引擎主要认这一份）；记忆、会话、人设一个字节都不带；
- 通道和界面外壳完全一样：常驻网关的 `/api/ws`（`ensure_gateway_runtime` 起/接，
  会话票 + 协议子协议），没有再走 stdio 的路。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

from websockets.typing import Subprotocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = PROJECT_ROOT / "src" / "engine"


def _dotenv(path: Path) -> dict[str, str]:
    """读 .env 成字典：只认 KEY=VALUE，忽略注释与空行。文件不存在就返回空。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def build_room(room: Path) -> None:
    """只建一个空的临时家目录，代码直接用仓库里现成的那份（不复制）。

    复制一份源码既慢又没意义：要验的是「这份代码 + 一个干净的 data/ 能不能跑起来」，
    代码在哪儿跑不影响结论；真要看「clone 下来能不能跑」，那是入库清单的事，不是这里。
    """
    if room.exists():
        shutil.rmtree(room)
    room.mkdir(parents=True)
    print(f"[1] 临时家目录：{room}（代码就地取自 {PROJECT_ROOT}）")


def init_data(room: Path, python: str) -> None:
    proc = subprocess.run(
        [python, "-c", "import sys; sys.path.insert(0, 'scripts'); import init_data; "
                       "raise SystemExit(init_data.main(sys.argv[1]))", str(room)],
        cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    print("[2] init_data:", (proc.stdout or proc.stderr).strip().replace("\n", " | "))
    if proc.returncode != 0:
        raise RuntimeError("init_data 失败")
    leftovers = [
        p.relative_to(room).as_posix()
        for p in (room / "data").rglob("*")
        if p.is_file() and ("SOUL.md" in p.parts or "memories" in p.parts)
    ]
    if leftovers:
        raise RuntimeError(f"新房间里不该有预置的人设/记忆文件：{leftovers}")
    print("[3] data/ 只有目录，没有预置人设或记忆文件（首次见面流程才会触发）")


def local_model() -> tuple[str, str]:
    """本机配置里当前用的模型（`provider`, `default`）；没有就两个空串。"""
    cfg = PROJECT_ROOT / "data" / "NoesiaHome" / "config.yaml"
    provider = model = ""
    if not cfg.exists():
        return provider, model
    in_model = False
    for raw in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        if raw.strip() == "model:":
            in_model = True
            continue
        if in_model and raw and not raw.startswith((" ", "\t")):
            break
        if in_model and ":" in raw:
            key, _, value = raw.strip().partition(":")
            value = value.strip().strip("'\"")
            if key == "provider" and value:
                provider = value
            elif key == "default" and value:
                model = value
    return provider, model


def seed_model(room: Path) -> str:
    """把本机在用的模型写进房间的配置里，相当于新用户第一次选模型那一步。

    必须写在起网关之前：带空模型建出来的本地会话策略是不合法的，引擎会把它当坏收据拒掉
    （`storage_unavailable`）；而模型选择本身又要一次 `session.mutate`——先有会话才能选，
    先有模型才能开会话。界面上这一步由首次设置走完，这里就照它的结果直接落配置。
    """
    provider, model = local_model()
    if not provider or not model:
        return ""
    cfg = room / "data" / "NoesiaHome" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"model:\n  provider: {provider}\n  default: {model}\n", encoding="utf-8")
    print(f"[4] 按本机配置预置模型：{provider}:{model}")
    return f"{provider}:{model}"


class Engine:
    """最小的网关客户端：连常驻网关的 `/api/ws`，只做体检要用的那几个方法。

    走的是界面外壳同一条路（`ensure_gateway_runtime` + 会话票 + 协议子协议），
    唯一的区别是这里用 `GatewayClient` 的阻塞封装，方便脚本一条条等结果。
    """

    def __init__(self, room: Path) -> None:
        self.room = room
        self.home = room / "data" / "NoesiaHome"
        self.log_path = room / "engine.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        source_env = PROJECT_ROOT / "data" / "NoesiaHome" / ".env"
        env_file = self.home / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        self.has_key = source_env.exists() and source_env.stat().st_size > 0
        if self.has_key:
            shutil.copyfile(source_env, env_file)
        os.environ["NOESIA_HOME"] = str(self.home)
        for key, value in _dotenv(PROJECT_ROOT / ".env").items():
            os.environ.setdefault(key, value)
        if str(ENGINE_DIR) not in sys.path:
            sys.path.insert(0, str(ENGINE_DIR))
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="cleanroom-ws", daemon=True)
        self._thread.start()
        self._client: Any = None
        self._ws: Any = None
        self._reader: Any = None
        self._pid = 0
        self._run(self._dial(), timeout=180.0)
        print(f"[5] 网关已连上 /api/ws（owner pid={self._pid}），日志 {self.log_path}")

    def _run(self, coro, timeout: float = 180.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _dial(self) -> None:
        from websockets.asyncio.client import connect

        from noesia_cli.gateway_client import (
            GATEWAY_WS_PROTOCOL, GatewayClient, _session_ticket, gateway_ws_target,
        )
        from noesia_cli.gateway_runtime import _owner_pid, attachable, ensure_gateway_runtime

        result = await asyncio.to_thread(ensure_gateway_runtime, self.home, idle_exit=True)
        if not attachable(result):
            raise RuntimeError(f"网关没起来：{result}")
        endpoint = result.endpoint
        ticket = await asyncio.to_thread(_session_ticket, self.home, endpoint)
        url, protocols = gateway_ws_target(endpoint, ticket)
        ws = await connect(url, subprotocols=cast("list[Subprotocol]", protocols), open_timeout=30,
                           max_size=8 * 1024 * 1024, proxy=None)
        if protocols and ws.subprotocol != GATEWAY_WS_PROTOCOL:
            await ws.close()
            raise RuntimeError("引擎协议对不上，需要更新引擎")
        client = GatewayClient(ws)
        await client.__aenter__()
        self._ws = ws
        self._client = client
        self._pid = _owner_pid(self.home) or 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self._client is not None
        return self._run(self._client.rpc(method, **(params or {})), timeout=180.0)

    async def _next_event(self, timeout: float) -> dict:
        assert self._client is not None
        return await asyncio.wait_for(self._client.events.get(), timeout)

    def _event(self, timeout: float) -> dict:
        return self._run(self._next_event(timeout), timeout=timeout + 30.0)

    def wait_ready(self, timeout: float = 120.0) -> None:
        """第一帧必须是 gateway.ready：引擎起不来就当场报，别等到发消息超时。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._event(max(1.0, deadline - time.monotonic()))
            if (frame.get("params") or {}).get("type") == "gateway.ready":
                print("[6] 引擎就绪（gateway.ready）")
                return
        raise RuntimeError(f"等了 {timeout:.0f}s 还没收到 gateway.ready：\n" + self._tail())

    def talk(self, text: str, timeout: float = 180.0) -> str:
        """开一个会话、发一句话，按 message.delta 拼回复（模型已在起网关前预置好）。"""
        created = self._rpc("session.create", {
            "cwd": str(self.room / "data" / "workspace"), "cwd_explicit": True,
        })
        session_id = str(created.get("session_id") or "")
        started = time.perf_counter()
        self._rpc("prompt.submit", {
            "session_id": session_id, "text": text, "submission_id": uuid.uuid4().hex,
        })
        deadline = time.monotonic() + timeout
        parts: list[str] = []
        first: float | None = None
        while time.monotonic() < deadline:
            frame = self._event(max(1.0, deadline - time.monotonic()))
            params = frame.get("params") or {}
            kind = params.get("type") or ""
            payload = params.get("payload") or {}
            if os.environ.get("CLEANROOM_DEBUG"):
                print("   frame:", kind, sorted(payload)[:6], str(frame.get("method") or ""))
            if kind == "message.delta":
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                parts.append(str(payload.get("text", "")))
            elif kind == "message.complete":
                if not parts and payload.get("text"):
                    parts.append(str(payload["text"]))
                if payload.get("error"):
                    print(f"[错误] {payload.get('error')}")
                break
        total = (time.perf_counter() - started) * 1000
        print(f"[7] 首字 {(first or total):.0f}ms / 全文 {total:.0f}ms")
        return "".join(parts)

    def _tail(self, limit: int = 800) -> str:
        self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="ignore")[-limit:]

    def close(self) -> None:
        async def _shutdown() -> None:
            client = self._client
            if client is not None:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            ws = self._ws
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

        if self._client is not None or self._ws is not None:
            try:
                self._run(_shutdown(), timeout=30.0)
            except Exception:  # noqa: BLE001
                pass
        if self._pid:
            for sig in (15, 9):
                try:
                    os.kill(self._pid, sig)
                except OSError:
                    break
                time.sleep(1.0)
                if not _alive(self._pid):
                    break
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._log.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="干净房间体检")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--python", default=sys.executable, help="用哪个解释器做 init_data")
    args = parser.parse_args(argv)

    room = Path(tempfile.mkdtemp(prefix="noesia_cleanroom_"))
    engine: Engine | None = None
    try:
        build_room(room)
        init_data(room, args.python)
        picked = seed_model(room)
        if not picked:
            print("[4] 本机配置里没有当前模型：新房间建不出合法会话，跳过")
            print("[SKIP] 这次不算通过：在本机选好模型再跑一遍才算验完")
            return 2
        engine = Engine(room)
        engine.wait_ready()
        if not engine.has_key:
            print("[SKIP] 本机 data/NoesiaHome/.env 里没有密钥：只验到「引擎起来了」，对话这步跳过")
            print("[SKIP] 这次不算通过：填好密钥再跑一遍才算验完")
            return 2
        reply = engine.talk("你好，介绍一下你自己")
        print(f"[8] 回复：{reply.strip()[:120]}")
        if not reply.strip():
            raise RuntimeError("没收到回复")
        print("[PASS] 干净环境下 clone → init_data → 网关 → 对话 全通")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {exc}")
        return 1
    finally:
        if engine is not None:
            engine.close()
        if args.keep:
            print(f"[keep] {room}")
        else:
            shutil.rmtree(room, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
