"""语音输入记住用哪只麦克风。

只存一个键：设备 GUID。**不存设备名**——名字会随系统语言变、用户能改，GUID 不会；
蓝牙重连、换 USB 口之后名字可能跟着变，GUID 是端点的身份证。

文件 `data/settings/voice_input.json`，首次运行写全模板。空串表示「跟随系统默认」。
"""

from __future__ import annotations

import json
from pathlib import Path

from settings import ConfigError, RESOLVER

PREFS_PATH = "data/settings/voice_input.json"

_TEMPLATE: dict = {
    # 空 = 跟随系统默认录音设备；否则是 WASAPI 端点 GUID
    "device": "",
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
