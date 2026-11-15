"""四份 JSON 偏好共用的落盘动作：先写临时文件、再改名。

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
