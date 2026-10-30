"""文本模型偏好：每家供应商各自上次选的模型，只存在 `data/settings/text.json`。

这里**只记偏好**，不记凭证、也不记「现在用哪家」：
- API Key 是凭证 → `data/NoesiaHome/.env`（唯一入口 `app/env_keys.py`）
- 现在生效的模型 → 引擎配置 `config.yaml` 的 `model`
同一个事实两处存，就一定会出现一处是旧的。这里只回答一个问题：切到那家时，下拉该停在哪个模型。
"""

from __future__ import annotations

import json
from pathlib import Path

from settings import ConfigError, RESOLVER

PREFS_PATH = "data/settings/text.json"
_TEMPLATE = {"models": {}}


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


def load() -> dict:
    """读 text.json：没有就写一份空的；读不懂就报错，不拿代码默认值悄悄盖过去。"""
    target = path()
    if not target.is_file():
        _write({"models": {}})
        return {"models": {}}
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读不懂 {target}：{exc}") from None
    models = stored.get("models") if isinstance(stored, dict) else None
    if not isinstance(models, dict):
        raise ConfigError(f"{target} 里要有 models 这一项（每家供应商上次选的模型）")
    return {"models": {str(key): str(value) for key, value in models.items() if value}}


def _write(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remembered(provider: str) -> str:
    """这家上次选的模型；没记过就是空串（调用方回落到引擎给的第一项）。"""
    return str((load().get("models") or {}).get(provider) or "")


def remember(provider: str, model: str) -> dict:
    return remember_many({provider: model})


def remember_many(picks: dict) -> dict:
    """一次记下好几家的模型：界面上一次配多家，保存时一起落盘。"""
    data = load()
    for provider, model in (picks or {}).items():
        if provider and model:
            data["models"][str(provider)] = str(model)
    _write(data)
    return data
