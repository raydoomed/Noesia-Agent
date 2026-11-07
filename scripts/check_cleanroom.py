#!/usr/bin/env python3
"""干净房间体检：在本机代码上挂一个空的 data/，看能不能真的跑起来。

为什么要有这个脚本：本机上的 `data/`（真实记忆、真实会话）会掩盖「代码本身能不能跑」的问题——
所以这里在临时目录里造一个空的 data/，代码就地用仓库里的那份（不复制），
`init_data` → 起引擎 → 选一次模型 → 发一句话 → 收到回复，全程不碰本机的数据。

用法：
    python scripts/check_cleanroom.py          # 临时目录跑一遍，跑完删掉
    python scripts/check_cleanroom.py --keep   # 留下临时目录，方便看引擎日志

退出码：0 正常 / 1 有问题（遵循 docs/conventions.md §7）。

注意：
- 需要一个能用的模型凭据：根目录 `.env` 的变量带进进程环境，`data/NoesiaHome/.env` 复制进房间
  （引擎主要认这一份）；记忆、会话、人设一个字节都不带；
- 通道就是引擎自己的 stdio JSON-RPC（`python -m tui_gateway.entry`）：没有端口、没有握手包。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


class Engine:
    """最小的 JSON-RPC 客户端：一行一个帧，只做体检要用的那几个方法。"""

    def __init__(self, room: Path, python: str) -> None:
        engine = PROJECT_ROOT / "src" / "engine"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = str(engine)
        env["NOESIA_HOME"] = str(room / "data" / "NoesiaHome")
        for key, value in _dotenv(PROJECT_ROOT / ".env").items():
            env.setdefault(key, value)
        self.log_path = room / "engine.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        source_env = PROJECT_ROOT / "data" / "NoesiaHome" / ".env"
        env_file = Path(env["NOESIA_HOME"]) / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        self.has_key = source_env.exists() and source_env.stat().st_size > 0
        if self.has_key:
            shutil.copyfile(source_env, env_file)
        self.proc = subprocess.Popen(
            [python, "-u", "-m", "tui_gateway.entry"],
            cwd=str(engine),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        self._id = 0
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, name="cleanroom-stdout", daemon=True).start()
        print(f"[4] 引擎 pid={self.proc.pid}，日志 {self.log_path}")

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put("")

    def send(self, method: str, params: dict | None = None) -> int:
        self._id += 1
        frame = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        return self._id

    def _next_frame(self, timeout: float) -> dict:
        """取下一帧；等到 `timeout` 秒还没字就当场报错（不许无限期挂着）。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(0.0, remaining))
            except queue.Empty:
                raise RuntimeError(
                    f"等了 {timeout:.0f}s 没等到引擎的输出：\n" + self._tail()
                ) from None
            if line == "":
                raise RuntimeError("引擎的输出断了（进程可能已经退出）：\n" + self._tail())
            line = line.strip()
            if line:
                return json.loads(line)

    def wait_ready(self, timeout: float = 120.0) -> None:
        """第一帧必须是 gateway.ready：引擎起不来就当场报，别等到发消息超时。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("引擎没起来：\n" + self._tail())
            frame = self._next_frame(deadline - time.monotonic())
            if (frame.get("params") or {}).get("type") == "gateway.ready":
                print("[5] 引擎就绪（gateway.ready）")
                return
        raise RuntimeError(f"等了 {timeout:.0f}s 还没收到 gateway.ready：\n" + self._tail())

    def wait_result(self, rid: int, timeout: float = 60.0) -> dict:
        """等某一条请求的回复；中间夹着的事件帧直接跳过。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._next_frame(deadline - time.monotonic())
            if frame.get("id") == rid:
                if "error" in frame:
                    raise RuntimeError(f"引擎报错：{frame['error']}")
                return frame.get("result") or {}
        raise RuntimeError(f"等回复超时（id={rid}）")

    def pick_model(self, session_id: str = "") -> str:
        """照本机那份配置选一次模型：房间是空的，不选引擎不知道用哪家。

        读的是本机 `data/NoesiaHome/config.yaml` 里 `model.provider` / `model.default`——
        也就是「你在本机用的是什么」，房间里没有就先照它设一次，属于新用户第一次选模型那一步。
        """
        cfg = PROJECT_ROOT / "data" / "NoesiaHome" / "config.yaml"
        provider = model = ""
        if cfg.exists():
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
        if not provider or not model:
            print("[6] 本机配置里没有当前模型，跳过选择（房间里还是未连接状态）")
            return ""
        choice = f"{provider}:{model}"
        params = {"key": "model", "value": choice}
        if session_id:
            params["session_id"] = session_id
        result = self.wait_result(self.send("config.set", params))
        if isinstance(result, dict) and result.get("error"):
            alt = self.first_model(provider)
            if alt and alt != choice:
                print(f"[6] 本机配置的 {choice} 引擎不认（{result['error'].get('code')}），改用清单里的 {alt}")
                choice = alt
                params["value"] = choice
                result = self.wait_result(self.send("config.set", params))
        print(f"[6] 按本机配置选模型：{choice} → {json.dumps(result, ensure_ascii=False)[:80]}")
        return choice

    def first_model(self, provider: str) -> str:
        """房间里引擎认的、这一家的第一个模型（`provider:model`）；问不到就回空。"""
        try:
            result = self.wait_result(self.send("model.options", {"explicit_only": True}), timeout=60)
        except Exception:  # noqa: BLE001
            return ""
        for row in (result or {}).get("providers") or []:
            if not isinstance(row, dict) or str(row.get("slug") or "") != provider:
                continue
            for name in row.get("models") or []:
                if isinstance(name, str) and name:
                    return f"{provider}:{name}"
        return ""

    def talk(self, text: str, timeout: float = 180.0) -> str:
        """开一个会话、发一句话，按 message.delta 拼回复。"""
        self.send("session.create", {})
        deadline = time.monotonic() + timeout
        parts: list[str] = []
        first: float | None = None
        started: float | None = None
        while time.monotonic() < deadline:
            frame = self._next_frame(deadline - time.monotonic())
            if "error" in frame:
                raise RuntimeError(f"引擎报错：{frame['error']}")
            result = frame.get("result")
            if isinstance(result, dict) and result.get("session_id") and started is None:
                started = time.perf_counter()
                self.pick_model(str(result["session_id"]))
                self.send("prompt.submit", {"session_id": result["session_id"], "text": text})
                continue
            params = frame.get("params") or {}
            kind = params.get("type") or ""
            payload = params.get("payload") or {}
            if os.environ.get("CLEANROOM_DEBUG"):
                print("   frame:", kind, sorted(payload)[:6], "id" in frame, str(frame.get("method") or ""))
            if kind == "message.delta":
                if first is None:
                    first = (time.perf_counter() - (started or time.perf_counter())) * 1000
                parts.append(str(payload.get("text", "")))
            elif kind == "message.complete":
                if not parts and payload.get("text"):
                    parts.append(str(payload["text"]))
                if payload.get("error"):
                    print(f"[错误] {payload.get('error')}")
                break
        total = (time.perf_counter() - (started or time.perf_counter())) * 1000
        print(f"[6] 首字 {(first or total):.0f}ms / 全文 {total:.0f}ms")
        return "".join(parts)

    def _tail(self, limit: int = 800) -> str:
        self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="ignore")[-limit:]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="干净房间体检")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--python", default=sys.executable, help="用哪个解释器起引擎")
    args = parser.parse_args(argv)

    room = Path(tempfile.mkdtemp(prefix="noesia_cleanroom_"))
    engine: Engine | None = None
    try:
        build_room(room)
        init_data(room, args.python)
        engine = Engine(room, args.python)
        engine.wait_ready()
        if not engine.has_key:
            print("[SKIP] 本机 data/NoesiaHome/.env 里没有密钥：只验到「引擎起来了」，对话这步跳过")
            print("[SKIP] 这次不算通过：填好密钥再跑一遍才算验完")
            return 2
        reply = engine.talk("你好，介绍一下你自己")
        print(f"[7] 回复：{reply.strip()[:120]}")
        if not reply.strip():
            raise RuntimeError("没收到回复")
        print("[PASS] 干净环境下 clone → init_data → 引擎 → 对话 全通")
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
