"""把引擎的 `tool.start` / `tool.complete` 帧转成统一的 `ToolPart`。

引擎给的是自己的字段（`name` / `args` / `result` / `is_error`），模型认的是
`toolName` / `args` / `result` / `isError` / `completedAt`——差别只在这一层，模型本身不动。
"""

from __future__ import annotations

import time
from typing import Any

from app.tool_view.helpers import strip_ansi
from app.tool_view.delegate import DELEGATE_TOOL, card_view
from app.tool_view.model import build_tool_view, inline_diff_from_result, strip_inline_diff_chrome
from app.tool_view.types import ToolPart, ToolView

COMPLETE_KIND = "tool.complete"

# diff 是**单独一个入参**传给 `build_tool_view(part, inline_diff)`，也就意味着它不在 result 里
# 再被摘要一遍。我们的引擎会把 `inline_diff` / `diff` 塞在 result 里，若原样交给模型，
# 副标题/正文会把整段 diff 当字段再列一次（屏幕上就是「raw diff + 彩色 diff」两遍）。
DIFF_KEYS = ("inline_diff", "diff")


def _without_diff(result: Any) -> Any:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return {key: value for key, value in result.items() if key not in DIFF_KEYS}
    return result


def part_from_event(event: dict[str, Any], now: float | None = None) -> ToolPart:
    """一帧工具事件 → ToolPart。进度帧不当成完成：`result` 只有 complete 那一帧才有。"""
    kind = str(event.get("kind") or "")
    complete = kind == COMPLETE_KIND
    return ToolPart(
        tool_name=str(event.get("name") or ""),
        args=event.get("args"),
        result=_without_diff(event.get("result")) if complete else None,
        is_error=(bool(event.get("is_error")) if complete else None),
        interrupted=bool(event.get("interrupted")) if event.get("interrupted") is not None else None,
        tool_call_id=str(event.get("tool_call_id") or event.get("tool_id") or ""),
        completed_at=(now if now is not None else time.time()) if complete else None,
    )


def view_for(event: dict[str, Any], now: float | None = None) -> tuple[ToolView, ToolPart]:
    """事件 → (ToolView, ToolPart)。行高亮要的 `tool_id` 从刚建好的 part 里取。"""
    part = part_from_event(event, now)
    # diff 要在**剥离之前**取：引擎放在 result 里，帧上偶尔也有一份（帧上优先）。
    diff = str(event.get("inline_diff") or "") or inline_diff_from_result(event.get("result"))
    # 引擎给的 diff 是终端画好的一份（带 `  ┊ review diff` 抬头和真彩色转义）：
    # 桌面端拿到后先洗掉转义再交给 shiki 上色，我们同一套——不然逐行分类认不出 `+`/`-`，
    # 转义还会原样铺到界面上。
    diff = strip_inline_diff_chrome(strip_ansi(diff))
    view = build_tool_view(part, diff)
    if part.tool_name == DELEGATE_TOOL:
        # 子代理那张卡片：引擎给的是英文键值，本体换成中文的「几个任务 / 状态 / 实时记录」
        view = card_view(view, part)
    return view, part
