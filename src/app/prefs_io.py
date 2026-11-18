"""四份 JSON 偏好共用的读写动作：落盘先写临时文件再改名，读盘把三类坏文件一次判死。

引擎随时可能在读这些文件（一按说话就读一次音色配置）。直接覆盖时它可能正好读到写了一半
的内容——解不开 JSON 就成了「合成失败」，用户只看到一句莫名其妙的错。改名是原子的，
读的一方要么看到旧的、要么看到新的。

临时文件名带 pid 和线程号：同一进程多线程同时存时，各自的临时文件不会撞在一起。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable

from settings import ConfigError


def read_json(target: Path, required: Iterable[str] = (), *, hint: str = "") -> dict:
    """读一份 JSON：读不懂、最外层不是对象、缺必填键，都当场抛 ConfigError。

    缺键不拿代码默认值兜着——默认值只用来**写**新文件，读到的旧文件缺键说明有人动过它，
    悄悄补上会让用户以为设置生效了。`hint` 接在缺键那句后面，讲清这份为什么不能猜。
    """
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读不懂 {target}：{exc}") from None
    if not isinstance(stored, dict):
        raise ConfigError(f"{target} 的最外层要是一个对象")
    missing = [key for key in required if key not in stored]
    if missing:
        raise ConfigError(f"{target} 缺 {', '.join(missing)}{hint}")
    return stored


def atomic_write_json(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
