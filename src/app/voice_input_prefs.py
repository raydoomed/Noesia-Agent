"""语音输入记住用哪只麦克风、用哪个键按住说话。

设备只存 GUID、**不存设备名**——名字会随系统语言变、用户能改，GUID 不会；
蓝牙重连、换 USB 口之后名字可能跟着变，GUID 是端点的身份证。

文件 `data/settings/voice_input.json`，首次运行写全模板。
`device` 空串表示「跟随系统默认」，`hotkey` 空串表示空格。
"""

from __future__ import annotations

import json
from pathlib import Path

from settings import ConfigError, RESOLVER

PREFS_PATH = "data/settings/voice_input.json"

_TEMPLATE: dict = {
    # 空 = 跟随系统默认录音设备；否则是 WASAPI 端点 GUID
    "device": "",
    # 窗口内按住说话用的键，Qt 写法（"Space" / "Ctrl+Space" / "F2"）；空 = 空格
    "hotkey": "",
    # 系统级（窗口在后台也生效）按住说话用的键；空 = Shift+Space
    "global_hotkey": "",
}

_REQUIRED = ("device",)


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


def load() -> dict:
    """读配置；没有就按模板写一份。"""
    target = path()
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
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
    return {key: stored.get(key, _TEMPLATE[key]) for key in _TEMPLATE}


def device_guid() -> str:
    """上次选的麦克风 GUID；空串 = 跟随系统默认。"""
    try:
        return str(load().get("device") or "")
    except ConfigError:
        # 配置坏了不该让人连话筒都用不了：当作没选过，回落到系统默认
        return ""


def remember(guid: str) -> None:
    """记住这次用的麦克风。传空串 = 恢复跟随系统默认。"""
    data = load()
    data["device"] = (guid or "").strip()
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def hotkey() -> str:
    """按住说话用的键，Qt 写法；空串 = 空格。"""
    try:
        return str(load().get("hotkey") or "").strip()
    except ConfigError:
        # 配置坏了不该让键按不动：当作没改过，回落到空格
        return ""


def remember_hotkey(sequence: str) -> None:
    """记住按住说话的键。传空串 = 恢复空格。"""
    data = load()
    data["hotkey"] = (sequence or "").strip()
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def global_hotkey() -> str:
    """系统级按住说话的键，Qt 写法；空串 = Shift+Space。"""
    try:
        return str(load().get("global_hotkey") or "").strip()
    except ConfigError:
        # 配置坏了不该让快捷键失效：当作没改过
        return ""


def remember_global_hotkey(sequence: str) -> None:
    """记住系统级的键。传空串 = 恢复 Shift+Space。"""
    data = load()
    data["global_hotkey"] = (sequence or "").strip()
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
