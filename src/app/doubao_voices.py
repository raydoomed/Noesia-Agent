"""豆包语音的音色清单：数据在 `src/tts_catalog.json`，这里只负责读，不把 247 条写死在代码里。

一个音色属于哪个资源（资源 id 就是豆包的「模型」）由数据文件决定：`mars` / `moon` 是上一代 1.0
的资源，拿 `seed-tts-2.0` 调会报 `resource ID is mismatched`，所以音色挂在资源下面，换资源时
整套音色一起换。

为什么音色写在数据里而不是调接口拉：豆包没有「列音色」的接口，官方只给文档清单，而且清单里有的
音色实际调不通（实测 249 个里 247 个能出声）。这份是逐个实测过的。顺序：中文（女→男）→ 英语 →
亚洲语种 → 欧洲语种，同一语言里女声在前，所以第一项仍是默认的「温柔淑女」。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from paths import resource_file

logger = logging.getLogger(__name__)

# 随包发布的数据文件：源码树里在 `src/` 下，装成 wheel 后在 `share/noesia/`
# （`paths.resource_file` 两处都找）。写死 `__file__` 旁边那份的话，非 editable 安装读不到。
CATALOG_PATH = resource_file("tts_catalog.json")


@lru_cache(maxsize=1)
def catalog() -> dict:
    """整份语音元信息：读不到、不是合法 JSON 就直接报出来，别拿空清单顶。"""
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"读不到语音清单 {CATALOG_PATH}：{exc}") from None
    except ValueError as exc:
        raise RuntimeError(f"语音清单不是合法 JSON（{CATALOG_PATH}）：{exc}") from None


def _safe(getter, default):
    """清单坏了也要让窗口起得来。

    `catalog()` 保持「坏了就抛」（打包错要有人看见），但 `url()`/`resources()` 这些
    在模块导入期就被算一次的取值不能带着异常冲出去——那等于一个坏 JSON 让整个窗口打不开，
    用户连「去设置里换回系统音色」都点不到。这里降级成空值 + 一行 ERROR，问题留在设置页显示。
    """
    try:
        return getter()
    except RuntimeError as exc:
        global _warned
        if not _warned:
            _warned = True
            logger.error("%s（先用空音色清单顶着，设置页会显示没音色）", exc)
        return default


_warned = False


def _doubao() -> dict:
    return dict(_safe(lambda: catalog().get("doubao") or {}, {}))


def url() -> str:
    """豆包语音合成的接口地址。"""
    return str(_doubao().get("url") or "")


def resources() -> dict[str, dict]:
    """所有资源：{资源 id: {label, skus, default_sku, voices}}。"""
    return dict(_doubao().get("resources") or {})


def default_resource() -> str:
    """默认资源 id：用户没在 speech.json 里指定时用它。"""
    return str(_doubao().get("default_resource") or "")


def default_voices() -> tuple[dict, ...]:
    """默认资源下的音色；第一个就是默认音色（中文女声「温柔淑女」）。"""
    block = resources().get(default_resource()) or {}
    return tuple(dict(item) for item in block.get("voices") or [])


DOUBAO_VOICES: tuple[dict, ...] = default_voices()


def languages() -> list[str]:
    """界面分类下拉用：按清单顺序去重，中文排最前。"""
    seen: list[str] = []
    for voice in DOUBAO_VOICES:
        if voice["lang"] not in seen:
            seen.append(voice["lang"])
    return seen
