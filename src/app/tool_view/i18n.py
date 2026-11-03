"""工具行文案的查表函数。

规则：先查当前语言，查不到回落到英文，
再查不到就直接返回键名本身。值是模板函数时把参数传进去。

**默认是英文**。产品是中文界面，启动时由 `app/main.py` 调 `set_runtime_i18n_locale("zh")`。
"""

from __future__ import annotations

from typing import Any

from app.tool_view.labels import LABELS
from app.tool_view.labels_extra import EXTRA

# 生成脚本的源里没有后加的引擎工具：把它们并进来，别让行标题露出 `assistant.tool.titles.xxx.done`
for _locale, _items in EXTRA.items():
    LABELS.setdefault(_locale, {}).update(_items)

DEFAULT_LOCALE = "en"

# 当前语言的全局状态
_runtime_locale = DEFAULT_LOCALE


def set_runtime_i18n_locale(locale: str) -> None:
    """切语言只影响之后画的那些行。"""
    global _runtime_locale
    _runtime_locale = locale if locale in LABELS else DEFAULT_LOCALE


def _render(value: Any, args: tuple) -> str | None:
    if value is None:
        return None
    if callable(value):
        return str(value(*args))
    return str(value)


def tr(key: str, *args: object, locale: str | None = None) -> str:
    active_locale = locale or _runtime_locale
    active = _render(LABELS.get(active_locale, {}).get(key), args)
    if active is not None:
        return active
    if active_locale != DEFAULT_LOCALE:
        fallback = _render(LABELS[DEFAULT_LOCALE].get(key), args)
        if fallback is not None:
            return fallback
    return key
