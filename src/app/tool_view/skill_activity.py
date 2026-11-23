"""技能类工具的标题。

「正在加载技能」和「读取技能资源」不是一回事，两者也都不代表技能描述的任务已完成——
所以标题只描述这一次调用，不暗示结果。
"""

from __future__ import annotations

from typing import Any

from app.tool_view.fmt import is_neutral_failure, parse_maybe_object, tool_call_failed
from app.tool_view.helpers import first_string_field
from app.tool_view.i18n import tr

ACTIVITY_TOOLS = ("skill_view", "skill_manage", "skills_list", "tool_describe", "tool_search")


def skill_activity_title(part: Any, live: bool = True) -> str | None:
    if part.tool_name not in ACTIVITY_TOOLS:
        return None

    args = parse_maybe_object(part.args)
    failed = tool_call_failed(part)
    pending = live and part.result is None and part.completed_at is None
    missing = (not pending) and part.result is None
    file_path = first_string_field(args, ["file_path"])
    name = first_string_field(args, ["name"])
    if part.tool_name == "skill_manage":
        operations = args.get("operations")
        if isinstance(operations, list) and operations and isinstance(operations[0], dict):
            name = first_string_field(operations[0], ["name"]) or name
    elif part.tool_name in ("tool_describe", "tool_search"):
        items = args.get("names" if part.tool_name == "tool_describe" else "queries")
        name = ", ".join(item for item in items if isinstance(item, str)) if isinstance(items, list) else ""
    target = " → ".join(value for value in (name, file_path) if value)

    if part.tool_name == "skills_list":
        keys = {"pending": "listing", "done": "listed", "failed": "listFailed"}
    elif part.tool_name == "skill_manage":
        keys = {"pending": "editing", "done": "edited", "failed": "editFailed", "unchanged": "notChanged"}
    elif part.tool_name == "tool_describe":
        keys = {"pending": "viewing", "done": "viewed", "failed": "viewFailed"}
    elif part.tool_name == "tool_search":
        keys = {"pending": "searching", "done": "searched", "failed": "searchFailed"}
    elif file_path:
        keys = {"pending": "readingResource", "done": "readResource", "failed": "resourceFailed"}
    else:
        keys = {"pending": "loading", "done": "loaded", "failed": "loadFailed"}

    if failed:
        key = keys["failed"]
    elif missing:
        key = "unavailable"
    elif pending:
        key = keys["pending"]
    elif is_neutral_failure(part.tool_name, part.result):
        key = keys.get("unchanged") or keys["done"]
    else:
        key = keys["done"]

    label = tr(f"assistant.tool.skillActivity.{key}")
    return f"{label}: {target}" if target else label
