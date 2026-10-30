"""工具行视图用到的数据结构。

类型用 dataclass / Literal 表达。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypedDict

ToolTone = Literal["agent", "browser", "default", "file", "image", "terminal", "web"]
ToolStatus = Literal["error", "notice", "running", "success", "warning"]


class ToolResultMetadata(TypedDict, total=False):
    """工具结果的元数据（运行时就是 dict）。"""

    duration_s: float | None
    error: str | bool | None
    inline_diff: str | None
    message: str | None
    preview: str | None
    summary: str | None
    todos: Any


@dataclass
class ToolPart:
    """工具调用块。`type` 固定为 'tool-call'。"""

    tool_name: str
    type: Literal["tool-call"] = "tool-call"
    tool_result_metadata: ToolResultMetadata | None = None
    args: Any = None
    completed_at: float | None = None
    inner_tool_name: str | None = None
    interrupted: bool | None = None
    is_error: bool | None = None
    result: Any = None
    timestamp: float | None = None
    tool_call_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "ToolPart":
        """把 camelCase 字典适配成本 dataclass（入口适配用）。"""
        if isinstance(value, ToolPart):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"ToolPart 需要 mapping，收到 {type(value)!r}")

        return cls(
            tool_name=value.get("toolName") or "",
            type=value.get("type") or "tool-call",
            tool_result_metadata=value.get("toolResultMetadata"),
            args=value.get("args"),
            completed_at=value.get("completedAt"),
            inner_tool_name=value.get("innerToolName"),
            interrupted=value.get("interrupted"),
            is_error=value.get("isError"),
            result=value.get("result"),
            timestamp=value.get("timestamp"),
            tool_call_id=value.get("toolCallId"),
        )


@dataclass
class SearchResultRow:
    snippet: str
    title: str
    url: str


@dataclass
class ToolTitleAction:
    prefix: str
    suffix: str
    text: str


@dataclass
class CountMetric:
    count: int
    noun: str


@dataclass
class ToolView:
    """工具行视图；可选字段用 None。"""

    detail: str
    detail_label: str
    inline_diff: str
    status: ToolStatus
    subtitle: str
    title: str
    tone: ToolTone
    count_label: str | None = None
    duration_label: str | None = None
    icon: str | None = None
    image_url: str | None = None
    preview_target: str | None = None
    # 输出本身含 ANSI 转义的工具（terminal/execute_code），渲染层要过 ANSI 解析器。
    renders_ansi: bool | None = None
    # 原始查询词，显示在结构化搜索结果上方。
    search_query: str | None = None
    search_hits: list[SearchResultRow] | None = None
    # 后端把 stderr 当独立流返回时（terminal / execute_code），单独成块展示。
    stderr: str | None = None
    # Terminal 专用：展开转录里显示成提示符的命令。
    terminal_command: str | None = None
    # Terminal 专用：后端报告的进程退出码。
    terminal_exit_code: float | None = None
    # 有值时渲染层按 stdout / stderr 分节，忽略合并后的 detail。
    stdout: str | None = None
    title_action: ToolTitleAction | None = None


@dataclass
class ToolMeta:
    done: str
    pending: str
    pending_action: str
    tone: ToolTone
    icon: str | None = None


@dataclass
class ToolMetaSpec:
    tone: ToolTone
    icon: str | None = None




@dataclass
class ToolTitleParts:
    """标题拆出来的三段（内部用）。"""

    title: str
    action: ToolTitleAction | None = None
