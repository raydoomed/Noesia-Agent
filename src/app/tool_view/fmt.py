"""工具块的格式化函数：文本截断、JSON 美化、时长与数值的显示形态。

截断长度差异：JS `slice` 按 UTF-16 码元，Python 按码点（含 emoji 时可能差一位）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .helpers import extract_tool_error_message, source_field

_WS_RUN_RE = re.compile(r'\s+')


def _collapse_whitespace(value: str) -> str:
    """`value.replace(/\\s+/g, ' ').trim()`（JS 的 \\s 与 Python 的 \\s 略有出入）。"""
    return _WS_RUN_RE.sub(' ', value).strip()


def is_record(value: Any) -> bool:
    return isinstance(value, dict)


def browser_exec_step_label(code: str, max_chars: int = 80) -> str | None:
    first = code.strip().split('\n', 1)[0].strip() if code.strip() else ''

    if not first.startswith('#'):
        return None

    label = first.lstrip('#').strip()
    if not label:
        return None

    return f'{label[:max_chars - 1]}…' if len(label) > max_chars else label


def compact_preview(value: Any, max_chars: int = 72) -> str:
    if isinstance(value, str):
        raw: Any = value
    else:
        parsed = parse_maybe_object(value)
        raw = parsed.get('context')
        if not isinstance(raw, str):
            # 多数结果字典没有 `context`（`{"stdout": …}` / `{"message": …}`）：
            # 以前这里落到空串，副标题等于没有，词条只剩工具名。
            for key in ('message', 'text', 'summary', 'stdout', 'stderr', 'output', 'error'):
                candidate = parsed.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    raw = candidate
                    break

    if not isinstance(raw, str):
        if raw is None:
            raw = ''
        else:
            try:
                # JSON.stringify 的紧凑形态：无空格分隔符、非 ASCII 原样输出
                raw = json.dumps(raw, separators=(',', ':'), ensure_ascii=False)
            except (TypeError, ValueError):
                raw = str(raw)

    line = _collapse_whitespace(raw)

    return f'{line[:max_chars - 1]}…' if len(line) > max_chars else line


def context_value(value: Any) -> str:
    row = parse_maybe_object(value)

    if isinstance(row.get('context'), str):
        return row['context']

    if isinstance(row.get('preview'), str):
        return row['preview']

    return value if isinstance(value, str) else ''





def parse_maybe_object(value: Any) -> dict:
    if is_record(value):
        return value

    if not isinstance(value, str) or not value.strip():
        return {}

    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}

    return parsed if is_record(parsed) else {}


# 被拒的调用若是 agent 自己的重试循环（大小上限拒绝的技能写入、过期的 old_string），
# 不算用户需要处理的失败——除了回滚也失败的批次，那会留下半成品改动。
# 镜像 agent/display.py::is_neutral_failure。
NEUTRAL_FAILURE_TOOLS = frozenset(['skill_manage'])


def is_neutral_failure(tool_name: str | None, result: Any) -> bool:
    if (tool_name or '') not in NEUTRAL_FAILURE_TOOLS:
        return False

    # 只看错误文本：被拒的 patch 也会带回技能文件的 file_preview，其正文可能提到该标记
    error = parse_maybe_object(result).get('error')
    if error is None:
        error = result if isinstance(result, str) else ''

    return 'ROLLBACK FAILED' not in (error if isinstance(error, str) else str(error))


def tool_call_failed(part: Any) -> bool:
    result = parse_maybe_object(source_field(part, 'result'))
    raw_result = source_field(part, 'result')
    is_error = source_field(part, 'isError')

    return (
        not is_neutral_failure(source_field(part, 'toolName'), raw_result)
        and result.get('success') is not True
        and result.get('ok') is not True
        and bool(
            is_error
            or result.get('success') is False
            or result.get('ok') is False
            or extract_tool_error_message(raw_result)
        )
    )


def unwrap_tool_payload(value: Any) -> Any:
    record = parse_maybe_object(value)

    for key in ('data', 'result', 'output', 'response', 'payload'):
        payload = record.get(key)

        if key in record and payload is not None:
            return payload

    return value


def _js_number(value: Any) -> float:
    """`Number(value)` 的常见分支：字符串按 trim 后解析，空串为 0，容器/非法值为 NaN。

    JS 的 `Number([])===0`、`Number(['5'])===5` 也一并保留；其余容器返回 NaN。
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return float('nan')
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return 0.0
        if len(value) == 1:
            return _js_number(value[0])
        return float('nan')
    return float('nan')


def number_value(value: Any) -> int | float | None:
    n = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else _js_number(value)

    if not math.isfinite(n):
        return None

    return int(n) if n.is_integer() else n


def format_duration_seconds(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return ''

    if not math.isfinite(seconds) or seconds < 0:
        return ''

    if seconds < 1:
        # Math.round：.5 向上
        ms = max(1, math.floor(seconds * 1000 + 0.5))

        return f'{ms}ms'

    if seconds < 60:
        # toFixed(0) 等价于 Math.round；toFixed(1) 保留一位小数
        if seconds >= 10:
            return f'{math.floor(seconds + 0.5)}s'

        return f'{seconds:.1f}s'

    whole_seconds = math.floor(seconds + 0.5)
    minutes = whole_seconds // 60
    rem_seconds = whole_seconds % 60

    if minutes < 60:
        return f'{minutes}m {rem_seconds}s' if rem_seconds else f'{minutes}m'

    hours = minutes // 60
    rem_minutes = minutes % 60

    return f'{hours}h {rem_minutes}m' if rem_minutes else f'{hours}h'
