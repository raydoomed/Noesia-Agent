"""语音互动（数字人）：用哪家厂商（`avatar.json`）+ 凭证怎么取（`env_keys`）。

两件事分开存，各在各家：

- **配置**（用哪家）→ `data/settings/avatar.json`，只有 `provider` 一个键，模板显式落地；
- **Key** → `data/NoesiaHome/.env`，由 `env_keys.PROVIDERS` 里属于 `avatar` 的那几行决定
  （Vidu 是 `VIDU_TOKEN`）。读写只有 `env_keys.read/write` 一套，这里不再自己实现。

加一家厂商 = `env_keys.PROVIDERS` 加一行（第四列 `avatar`）+ 在
`model_dialog._TOKEN_VERIFIERS` 挂它自己的只读校验函数（没有就不挂）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import env_keys
from settings import ConfigError, RESOLVER

PREFS_PATH = Path("data/settings/avatar.json")

# 界面上的厂商列表：[(id, 名字, .env 变量名)]
PROVIDERS = env_keys.providers("avatar")

# 数字人模型：界面上给个下拉，值只有这两个（官方组件版 `model` 枚举就这么多）
MODELS = (
    ("vidu-s2", "vidu-s2（清晰，888×1184）"),
    ("vidu-s1", "vidu-s1（稳定，624×832）"),
)

_TEMPLATE = {"provider": "vidu", "model": "vidu-s2"}
_REQUIRED = tuple(_TEMPLATE)


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


def load() -> dict:
    """读 avatar.json：没有就按模板写一份；有但缺键就报错，不拿代码默认值兜着。"""
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
        raise ConfigError(f"{target} 缺 {', '.join(missing)}（语音互动的配置只有这一份，缺键不猜）")
    data = dict(_TEMPLATE)
    data.update(stored)
    return data


def save(provider: str | None = None, model: str | None = None) -> dict:
    """存"用哪家、用哪个模型"；Key 走 `write_token()`，一律不落 json。"""
    data = load()
    if provider:
        if provider not in {row[0] for row in PROVIDERS}:
            raise ValueError(f"不认识的语音互动厂商：{provider}")
        data["provider"] = provider
    if model:
        if model not in {row[0] for row in MODELS}:
            raise ValueError(f"不认识的数字人模型：{model}")
        data["model"] = model
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def env_key(provider: str | None = None) -> str:
    return env_keys.env_key(provider or current()["provider"])


def current() -> dict:
    """生效值：`{"provider", "label", "env_key", "model"}`，界面和引擎都按这个取。"""
    stored = load()
    chosen = stored["provider"]
    for pid, label, key in PROVIDERS:
        if pid == chosen:
            return {
                "provider": pid,
                "label": label,
                "env_key": key,
                "model": stored["model"],
            }
    raise ConfigError(f"avatar.json 里的 provider 不认识：{chosen}")


def read_token(provider: str | None = None) -> str:
    """读凭证；没填返回空串（界面判断"填没填过"用它，不抛异常）。"""
    return env_keys.read(env_key(provider))


def write_token(value: str, provider: str | None = None) -> None:
    env_keys.write(env_key(provider), value)


def token_issue(value: str) -> str:
    """填之前先看格式：返回空串表示没问题，否则返回给人看的原因。"""
    if not value:
        return "还没填凭证"
    if not value.isascii() or any(ch.isspace() for ch in value):
        return "这个 Token 里混了中文或空格，重新粘贴一次（只要那一整串）"
    if len(value) < 20:
        return f"这个 Token 只有 {len(value)} 位，肯定不完整：到控制台复制整串再粘一次"
    return ""
