"""模型清单的本地快照。

引擎是独立进程，冷启动要四五秒（导插件、扫供应商、开库），所以 `models` 回包会迟到；
界面如果一直等它，按钮上就挂着一句「选模型」转圈。这里把上一次的清单存在前端目录，
开窗先按它画出来，真正的清单到了再覆盖——只是一份显示用的缓存，当前模型仍以引擎为准。
"""

from __future__ import annotations

import json
from pathlib import Path

CACHE_NAME = "models-cache.json"


def cache_path(root: Path | str) -> Path:
    """放在 <项目根>/data/runtime/ 下，跟日志放一起。"""
    return Path(root) / "data" / "runtime" / CACHE_NAME


def save(root: Path | str, payload: dict) -> None:
    """写快照。写失败不算事：下次开窗顶多再等一次引擎。"""
    path = cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load(root: Path | str) -> dict | None:
    """读快照。文件不在、坏了、格式变了都当没有——缓存不能拦住开窗。"""
    path = cache_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data
