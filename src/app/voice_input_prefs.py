"""语音输入记住用哪只麦克风、用哪个键按住说话。

设备只存 GUID、**不存设备名**——名字会随系统语言变、用户能改，GUID 不会；
蓝牙重连、换 USB 口之后名字可能跟着变，GUID 是端点的身份证。

文件 `data/settings/voice_input.json`，首次运行写全模板。
`device` 空串表示「跟随系统默认」，`hotkey` 空串表示空格。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from settings import ConfigError, RESOLVER

PREFS_PATH = "data/settings/voice_input.json"

_TEMPLATE: dict = {
    "device": "",
    "hotkey": "",
    "global_hotkey": "",
}

_REQUIRED = ("device",)


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


_CACHE: dict = {"stamp": None, "data": None}


def _write(data: dict) -> None:
    target = path()
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


def load() -> dict:
    """读配置；没有就按模板写一份。"""
    target = path()
    try:
        stat = target.stat()
        stamp = (str(target), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if stamp is not None and _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
        return dict(_CACHE["data"])
    if not target.is_file():
        _write(dict(_TEMPLATE))
        return dict(_TEMPLATE)
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读不懂 {target}：{exc}") from None
    if not isinstance(stored, dict):
        raise ConfigError(f"{target} 的最外层要是一个对象")
    missing = [key for key in _REQUIRED if key not in stored]
    if missing:
        raise ConfigError(f"{target} 缺 {', '.join(missing)}")
    result = {key: stored.get(key, _TEMPLATE[key]) for key in _TEMPLATE}
    _CACHE["stamp"] = stamp
    _CACHE["data"] = dict(result)
    return result


def device_guid() -> str:
    """上次选的麦克风 GUID；空串 = 跟随系统默认。"""
    try:
        return str(load().get("device") or "")
    except ConfigError:
        return ""


def remember(guid: str) -> None:
    """记住这次用的麦克风。传空串 = 恢复跟随系统默认。"""
    data = load()
    data["device"] = (guid or "").strip()
    _write(data)


def hotkey() -> str:
    """按住说话用的键，Qt 写法；空串 = 空格。"""
    try:
        return str(load().get("hotkey") or "").strip()
    except ConfigError:
        return ""


def remember_hotkey(sequence: str) -> None:
    """记住按住说话的键。传空串 = 恢复空格。"""
    data = load()
    data["hotkey"] = (sequence or "").strip()
    _write(data)


def global_hotkey() -> str:
    """系统级按住说话的键，Qt 写法；空串 = Shift+Space。"""
    try:
        return str(load().get("global_hotkey") or "").strip()
    except ConfigError:
        return ""


def remember_global_hotkey(sequence: str) -> None:
    """记住系统级的键。传空串 = 恢复 Shift+Space。"""
    data = load()
    data["global_hotkey"] = (sequence or "").strip()
    _write(data)
