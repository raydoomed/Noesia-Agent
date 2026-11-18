"""语音输入记住用哪只麦克风、用哪个键按住说话。

设备只存 GUID、**不存设备名**——名字会随系统语言变、用户能改，GUID 不会；
蓝牙重连、换 USB 口之后名字可能跟着变，GUID 是端点的身份证。

文件 `data/settings/voice_input.json`，首次运行写全模板。
`device` 空串表示「跟随系统默认」，`hotkey` 空串表示空格。
"""

from __future__ import annotations

from pathlib import Path

from app.prefs_io import atomic_write_json, read_json
from settings import ConfigError, runtime_resolver

PREFS_PATH = "data/settings/voice_input.json"

_TEMPLATE: dict = {
    "device": "",
    "hotkey": "",
    "global_hotkey": "",
}

_REQUIRED = ("device",)


def path() -> Path:
    return runtime_resolver().resolve(PREFS_PATH)


_CACHE: dict = {"stamp": None, "data": None}


def _write(data: dict) -> None:
    atomic_write_json(path(), data)


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
    stored = read_json(target, _REQUIRED)
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
