"""子代理（`delegate_task`）在界面上怎么说话。

引擎给的是英文键值（`status` / `mode` / `goals` / `live_transcripts` / `subagent_ids`…），
原样铺到界面上就是「Delegation Id / Live Transcripts / Mode: background」那一坨。
这里把一次委派翻成卡片：抬头「几个子代理 · 到哪一步了」，正文**一行一个子代理**——行首是它
的目标，下面一行是它最新在干什么。

每个子代理只占两行，活动行**原地换字**，不往下追加。追加有两个毛病：①她（模型）在跑的中途
会说一句话，追加的行就落到她的话下面，一张卡被切成两半；②子代理越活跃卡片越长。

三份数据来源：调用参数里是「派发了哪些目标」，结果里是「每个目标跑到什么状态」，运行中的
进度由 `subagent.*` 事件按子代理编号 / 任务序号 / 目标文字叠进同一行（见 `apply_event`）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

DELEGATE_TOOL = "delegate_task"

GOAL_MAX = 60
SUMMARY_MAX = 200
PREVIEW_MAX = 60

_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "sleeping": "等待中",
    "started": "已开始",
    "ok": "已完成",
    "completed": "已完成",
    "success": "已完成",
    "dispatched": "后台运行",
    "dispatching": "派发中",
    "timeout": "超时",
    "failed": "失败",
    "failure": "失败",
    "error": "失败",
    "interrupted": "已中断",
    "cancelled": "已取消",
    "canceled": "已取消",
}

_MODE_LABELS = {"background": "后台运行", "sync": "同步等结果", "foreground": "同步等结果"}

GLYPH_RUNNING = "◐"
GLYPH_PARKED = "·"
GLYPH_DONE = "✓"
GLYPH_FAILED = "✗"

_TASK_HEAD = re.compile(r"^---\s*[✓✗×]?\s*TASK\s+(\d+)/(\d+):\s*(.*?)\s*\((.*?)\)\s*---\s*$")
_DELEGATION_ID = re.compile(r"deleg_[0-9a-f]{6,}")
_TOTAL_DURATION = re.compile(r"Total duration:\s*([0-9.]+)s")
_STATUS_IN_HEAD = re.compile(r"status=([a-z_]+)")
_SECONDS_IN_HEAD = re.compile(r"([0-9.]+)s")
_MODEL_IN_HEAD = re.compile(r"Model:\s*([\w.:/-]+)")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _compact(value: Any, limit: int) -> str:
    """折成一行并按上限截断（卡片一行放不下整段目标）。"""
    line = " ".join(_text(value).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def status_label(status: str) -> str:
    return _STATUS_LABELS.get((status or "").strip().lower(), status or "未知")


def settled_status(status: str) -> str:
    """跑完之后的状态只分两种：成了 / 没成（超时、报错都算没成，不能显示成绿勾）。"""
    return "completed" if (status or "").strip().lower() in {"", "ok", "completed", "success"} else "failed"


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def goals(args: Any) -> list[str]:
    """这次委派派出去的目标，按任务顺序；单个任务写在 `goal` 上也算。"""
    record = args if isinstance(args, Mapping) else {}
    tasks = _records(record.get("tasks"))
    if tasks:
        return [_text(task.get("goal")) or f"任务 {index + 1}" for index, task in enumerate(tasks)]
    goal = _text(record.get("goal"))
    return [goal] if goal else []


def rows(args: Any, result: Any) -> list[dict]:
    """这次派发的每个子代理一行：目标、状态、用时、模型、最新活动。

    参数说派了谁，结果说各自跑到什么状态（没有结果的那一帧就是「还在派发」），
    `subagent.*` 事件再往上叠活动的字（见 `apply_event`）。
    """
    record = result if isinstance(result, Mapping) else {}
    finished = _records(record.get("results"))
    dispatched = _strings(record.get("goals"))
    ids = _strings(record.get("subagent_ids"))
    titles = goals(args) or dispatched or [f"任务 {index + 1}" for index in range(len(finished))]
    if len(finished) > len(titles):
        titles = titles + [
            _text(entry.get("goal")) or f"任务 {index + 1}"
            for index, entry in enumerate(finished[len(titles):], start=len(titles))
        ]
    idle_status = "dispatching" if result is None else "dispatched"

    out: list[dict] = []
    for index, goal in enumerate(titles):
        entry = finished[index] if index < len(finished) else None
        status = _text(entry.get("status")) if entry else idle_status
        summary = _text(entry.get("summary")) if entry else ""
        out.append(
            {
                "index": index,
                "goal": goal,
                "status": status,
                "state": settled_status(status) if entry else "running",
                "seconds": _number(entry.get("duration_seconds")) if entry else None,
                "model": _text(entry.get("model")) if entry else "",
                "summary": summary,
                "subagent_id": ids[index] if index < len(ids) else "",
                "session_id": "",
                "activity": _compact(summary, SUMMARY_MAX),
            }
        )
    return out


def mode_label(args: Any, result: Any) -> str:
    for source in (result, args):
        if isinstance(source, Mapping):
            mode = _text(source.get("mode"))
            if mode:
                return _MODE_LABELS.get(mode.lower(), mode)
    return ""


def _result_error(result: Any) -> str:
    record = result if isinstance(result, Mapping) else {}
    return _text(record.get("error")) or _text(record.get("message"))


def group_title(count: int, word: str = "") -> str:
    """卡片抬头：`子代理 · 2 个任务 · 运行中`。"""
    amount = f"{count} 个任务" if count != 1 else "1 个任务"
    return f"子代理 · {amount}" + (f" · {word}" if word else "")


def title(args: Any, result: Any, *, is_error: bool = False) -> str:
    """卡片的抬头：几个子代理 + 现在到哪一步了。"""
    items = rows(args, result)
    if result is None:
        return group_title(len(items), "派发中")
    if is_error or _result_error(result):
        return group_title(len(items), "派发失败")
    if any(item["state"] == "failed" for item in items):
        return group_title(len(items), "有失败")
    if all(item["state"] == "running" for item in items):
        return group_title(len(items), mode_label(args, result) or "运行中")
    return group_title(len(items), "已完成")


def running_title(args: Any) -> str:
    """后台派发之后、子代理真在跑的时候的抬头（秒数由界面的计时器接在后面）。"""
    return group_title(len(goals(args)), "运行中")


def status_glyph(row: Mapping[str, Any]) -> str:
    status = _text(row.get("status")).lower()
    if status == "dispatched":
        return GLYPH_PARKED
    state = row.get("state")
    if state == "completed":
        return GLYPH_DONE
    if state == "failed":
        return GLYPH_FAILED
    return GLYPH_RUNNING


def _seconds(value: Any) -> str:
    seconds = _number(value)
    return f"{seconds:.1f}s" if seconds is not None else ""


def row_lines(row: Mapping[str, Any]) -> tuple[str, str]:
    """一行子代理 →（抬头行，活动行）。活动行可以是空的（它还没调工具）。"""
    head = f"{status_glyph(row)} {_compact(row.get('goal'), GOAL_MAX)}"
    meta = [text for text in (_text(row.get("model")), _seconds(row.get("seconds"))) if text]
    if meta:
        head += " · " + " · ".join(meta)
    activity = _compact(row.get("activity"), PREVIEW_MAX)
    return head, f"   {activity}" if activity else ""


def apply_event(items: list[dict], payload: Any) -> list[dict]:
    """把一帧 `subagent.*` 叠到对应的那一行上（原地改那一行的字，不追加新行）。"""
    record = payload if isinstance(payload, Mapping) else {}
    target = _row_for(items, record)
    if target is None:
        return items

    kind = _text(record.get("kind"))
    status = _text(record.get("status"))
    model = _text(record.get("model"))
    if model:
        target["model"] = model
    subagent = _text(record.get("subagent_id"))
    if subagent:
        target["subagent_id"] = subagent
    session = _text(record.get("child_session_id")) or _text(record.get("session_id"))
    if session:
        target["session_id"] = session

    if kind.endswith("complete"):
        target["status"] = status or "completed"
        target["state"] = settled_status(target["status"])
        seconds = _number(record.get("duration_seconds"))
        if seconds is not None:
            target["seconds"] = seconds
        summary = _compact(record.get("summary"), SUMMARY_MAX)
        if summary:
            target["summary"] = summary
            target["activity"] = summary
        return items

    if status:
        target["status"] = status
        target["state"] = "running"
    tool = _text(record.get("tool_name"))
    if tool:
        preview = _compact(record.get("tool_preview") or record.get("text"), PREVIEW_MAX)
        target["activity"] = f"→ {tool}（{preview}）" if preview else f"→ {tool}"
    return items


def _row_for(items: list[dict], record: Mapping[str, Any]) -> dict | None:
    """这一帧说的是哪个子代理：编号最准，其次任务序号，再按目标文字，最后落到唯一在跑的那行。"""
    if not items:
        return None
    subagent = _text(record.get("subagent_id"))
    if subagent:
        for item in items:
            if item.get("subagent_id") == subagent:
                return item
    index = _number(record.get("task_index"))
    if index is not None and 0 <= int(index) < len(items):
        return items[int(index)]
    goal = _compact(record.get("goal"), GOAL_MAX)
    if goal:
        for item in items:
            head = _compact(item.get("goal"), 24)
            if head and (goal.startswith(head) or head.startswith(goal[:24])):
                return item
    running = [item for item in items if item.get("state") == "running"]
    if len(running) == 1:
        return running[0]
    return items[0] if len(items) == 1 else None


def is_background(result: Any) -> bool:
    """后台派发：工具调用当场就返回了，子代理还在跑。"""
    record = result if isinstance(result, Mapping) else None
    if record is None:
        return False
    if _text(record.get("mode")).lower() == "background":
        return True
    return _text(record.get("status")).lower() == "dispatched"


def card_view(view, part):
    """把通用工具视图换成子代理卡片：抬头是中文，正文是任务行。"""
    from dataclasses import replace

    items = rows(part.args, part.result)
    lines = [line for item in items for line in row_lines(item) if line]
    error = _result_error(part.result)
    if part.is_error or error:
        lines.append(f"✗ 派发失败：{_compact(error, SUMMARY_MAX) or '引擎没有给出原因'}")
    return replace(
        view,
        title=title(part.args, part.result, is_error=bool(part.is_error)),
        subtitle="",
        detail="\n".join(lines),
        detail_label="",
        stdout=None,
        stderr=None,
        status="error" if (part.is_error or any(item["state"] == "failed" for item in items)) else "success",
        tone="agent",
        duration_label=None,
        count_label=None,
    )


def completion_lines(content: Any) -> list[str]:
    """后端那条「ASYNC DELEGATION BATCH COMPLETE」消息 → 中文几行（它是给模型看的，不念原文）。"""
    text = _text(content)
    if not text:
        return []
    delegation = _DELEGATION_ID.search(text)
    total = _TOTAL_DURATION.search(text)
    lines = [f"子代理已结束{('（' + delegation.group(0) + '）') if delegation else ''}"
             + (f"，总用时 {float(total.group(1)):.1f}s" if total else "")]
    model = _MODEL_IN_HEAD.search(text)
    if model:
        lines[0] += f" · {model.group(1)}"

    for raw in text.splitlines():
        head = _TASK_HEAD.match(raw.strip())
        if head is None:
            continue
        index, total_tasks, goal = head.group(1), head.group(2), head.group(3)
        detail = head.group(4)
        status = _STATUS_IN_HEAD.search(detail)
        seconds = _SECONDS_IN_HEAD.search(detail)
        row = f"· 任务 {index}/{total_tasks} {_compact(goal, GOAL_MAX)} — {status_label(status.group(1) if status else '')}"
        if seconds:
            row += f" · {float(seconds.group(1)):.1f}s"
        lines.append(row)
    return lines
