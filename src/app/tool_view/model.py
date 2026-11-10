"""工具块的标题 / 副标题 / 正文 / 状态 / 计数 / preview 计算。

JS 正则里的 `\\d` / `\\b` 是 ASCII 语义，相应 pattern 统一加 `re.A`。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Mapping

from app.redact import redact
from app.tool_view.i18n import tr
from app.tool_view.types import (
    CountMetric,
    SearchResultRow,
    ToolMeta,
    ToolMetaSpec,
    ToolPart,
    ToolStatus,
    ToolTitleAction,
    ToolTitleParts,
    ToolView,
)

from app.tool_view.fmt import (
    browser_exec_step_label,
    compact_preview,
    context_value,
    format_duration_seconds,
    is_neutral_failure,
    is_record,
    number_value,
    parse_maybe_object,
    unwrap_tool_payload,
)
from app.tool_view.targets import find_first_url, hostname_of, looks_like_path, looks_like_url
from app.tool_view.helpers import (
    CONNECTION_CARD_KEY,
    capitalize,
    envelope_error_text,
    extract_tool_error_message,
    first_string_field,
    format_tool_result_summary,
    is_card_tool,
    is_file_edit_tool,
    is_file_media_path,
    is_silent_tool,
    media_kind,
    normalize,
    normalize_external_url,
    strip_ansi,
    summarize_shell_command,
    tool_result_record,
)

from app.tool_view.skill_activity import skill_activity_title as _skill_activity_title

__all__ = [
    "CONNECTION_CARD_KEY",
    "is_card_tool",
    "is_file_edit_tool",
    "is_silent_tool",
    "build_tool_view",
]


def _num(value: float | int) -> str:
    """JS 数字不分整浮点，Python 需要把整数值的 float 打印成整数。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _round_half_up(value: float) -> int:
    """JS `Math.round` 是四舍五入（.5 进一位），Python `round` 是银行家舍入。"""
    return int(math.floor(value + 0.5))




# ---- countDiffLineStats ----


def count_diff_line_stats(diff: str) -> dict[str, int]:
    added = 0
    removed = 0

    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    return {"added": added, "removed": removed}


# ---- fileEditPath ----


def file_edit_path(args: dict[str, Any], result: dict[str, Any]) -> str:
    return (
        first_string_field(args, ["path", "file", "filepath"])
        or first_string_field(result, ["path", "file", "filepath", "resolved_path"])
        or html_path_from_inline_diff(first_string_field(result, ["inline_diff", "diff"]))
    )


# ---- fileEditBasename ----


def file_edit_basename(path: str) -> str:
    normalized = re.sub(r"\\", "/", path).strip()
    parts = [part for part in normalized.split("/") if part]

    return parts[-1] if parts else normalized


def numeric_field(record: dict[str, Any], key: str) -> float | None:
    value = record.get(key)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value) if math.isfinite(value) else None


def read_file_line_label(args: dict[str, Any], result: dict[str, Any]) -> str:
    if numeric_field(args, "offset") is None and numeric_field(args, "limit") is None:
        return ""

    content = first_string_field(result, ["content"])
    offset = numeric_field(args, "offset")
    limit = numeric_field(args, "limit")

    if offset is not None and offset > 0:
        if limit is None or limit <= 1:
            return f"L{_num(offset)}"

        lines = [
            int(match.group(1))
            for match in (READ_FILE_LINE_RE.match(line) for line in content.split("\n"))
            if match
        ]
        if lines:
            start, end = min(lines), max(lines)
            return f"L{start}" if start == end else f"L{start}-{end}"
        return f"L{_num(offset)}-{_num(offset + limit - 1)}"

    lines = [
        int(match.group(1))
        for match in (READ_FILE_LINE_RE.match(line) for line in content.split("\n"))
        if match
    ]

    if len(lines) == 0:
        return ""

    start = lines[0]
    end = lines[-1]

    return f"L{start}" if start == end else f"L{start}-{end}"


READ_FILE_LINE_RE = re.compile(r"^([0-9]+)\|", re.A)


def read_file_display_target(args: dict[str, Any], result: dict[str, Any]) -> str:
    inherited = first_string_field(args, ["context", "preview"])

    if inherited:
        return inherited

    path = first_string_field(args, ["path", "file", "filepath"])

    if not path:
        return ""

    line_label = read_file_line_label(args, result)

    return " ".join(part for part in [file_edit_basename(path), line_label] if part)


def shell_command(args: dict[str, Any]) -> str:
    return redact(
        first_string_field(args, ["command", "code"])
        or first_string_field(args, ["context", "preview"])
        or context_value(args)
    )


# ---- TOOL_META ----

TOOL_META: dict[str, ToolMetaSpec] = {
    "browser_click": ToolMetaSpec(icon="globe", tone="browser"),
    "browser_fill": ToolMetaSpec(icon="globe", tone="browser"),
    "browser_navigate": ToolMetaSpec(icon="globe", tone="browser"),
    "browser_snapshot": ToolMetaSpec(icon="globe", tone="browser"),
    "browser_take_screenshot": ToolMetaSpec(icon="file-media", tone="browser"),
    "browser_type": ToolMetaSpec(icon="globe", tone="browser"),
    "clarify": ToolMetaSpec(icon="question", tone="agent"),
    "cronjob": ToolMetaSpec(icon="watch", tone="agent"),
    "cronjob_manage": ToolMetaSpec(icon="watch", tone="agent"),
    "edit_file": ToolMetaSpec(icon="edit", tone="file"),
    "execute_code": ToolMetaSpec(icon="terminal", tone="terminal"),
    "image_generate": ToolMetaSpec(icon="file-media", tone="image"),
    "list_files": ToolMetaSpec(icon="files", tone="file"),
    "memory": ToolMetaSpec(icon="brain", tone="agent"),
    "patch": ToolMetaSpec(icon="edit", tone="file"),
    "read_file": ToolMetaSpec(icon="file", tone="file"),
    "search_files": ToolMetaSpec(icon="search", tone="file"),
    "session_search_recall": ToolMetaSpec(icon="search", tone="agent"),
    "skill_manage": ToolMetaSpec(icon="edit", tone="file"),
    "skill_view": ToolMetaSpec(icon="file", tone="file"),
    "skills_list": ToolMetaSpec(icon="files", tone="file"),
    "terminal": ToolMetaSpec(icon="terminal", tone="terminal"),
    "todo": ToolMetaSpec(icon="tools", tone="agent"),
    "todo_list": ToolMetaSpec(icon="tools", tone="agent"),
    "vision_analyze": ToolMetaSpec(icon="eye", tone="image"),
    "web_extract": ToolMetaSpec(icon="globe", tone="web"),
    "web_search": ToolMetaSpec(icon="search", tone="web"),
    "write_file": ToolMetaSpec(icon="edit", tone="file"),
    "annotate_preview": ToolMetaSpec(icon="edit", tone="file"),
    "close_terminal": ToolMetaSpec(icon="terminal", tone="terminal"),
    "computer_use": ToolMetaSpec(icon="tools", tone="agent"),
    "delegate_task": ToolMetaSpec(icon="brain", tone="agent"),
    "desktop_preview": ToolMetaSpec(icon="eye", tone="image"),
    "desktop_project": ToolMetaSpec(icon="files", tone="file"),
    "drive_preview": ToolMetaSpec(icon="eye", tone="image"),
    "feishu_doc_read": ToolMetaSpec(icon="file", tone="file"),
    "feishu_drive_add_comment": ToolMetaSpec(icon="edit", tone="file"),
    "feishu_drive_list_comment_replies": ToolMetaSpec(icon="files", tone="file"),
    "feishu_drive_list_comments": ToolMetaSpec(icon="files", tone="file"),
    "feishu_drive_reply_comment": ToolMetaSpec(icon="edit", tone="file"),
    "focus_pane": ToolMetaSpec(icon="tools", tone="agent"),
    "gui_tour": ToolMetaSpec(icon="question", tone="agent"),
    "manage_catalog": ToolMetaSpec(icon="tools", tone="agent"),
    "manage_connections": ToolMetaSpec(icon="globe", tone="web"),
    "process_manage": ToolMetaSpec(icon="terminal", tone="terminal"),
    "react_to_message": ToolMetaSpec(icon="question", tone="agent"),
    "read_terminal": ToolMetaSpec(icon="terminal", tone="terminal"),
    "read_window_below": ToolMetaSpec(icon="eye", tone="image"),
    "session_search": ToolMetaSpec(icon="search", tone="agent"),
    "set_theme": ToolMetaSpec(icon="tools", tone="agent"),
    "setup_beat": ToolMetaSpec(icon="question", tone="agent"),
    "show_tip": ToolMetaSpec(icon="question", tone="agent"),
    "text_to_speech": ToolMetaSpec(icon="tools", tone="agent"),
    "video_analyze": ToolMetaSpec(icon="eye", tone="image"),
    "video_generate": ToolMetaSpec(icon="file-media", tone="image"),
    "x_search": ToolMetaSpec(icon="search", tone="web"),
    "yb_query_group_info": ToolMetaSpec(icon="search", tone="agent"),
    "yb_query_group_members": ToolMetaSpec(icon="search", tone="agent"),
    "yb_search_sticker": ToolMetaSpec(icon="file-media", tone="image"),
    "yb_send_dm": ToolMetaSpec(icon="globe", tone="web"),
    "yb_send_sticker": ToolMetaSpec(icon="file-media", tone="image"),
}


def is_tool_title_key(name: str) -> bool:
    return name in TOOL_META


INLINE_CODE_SPLIT_RE = re.compile(r"(`[^`\n]+`)")
CITATION_MARKER_RE = re.compile(r"(?<=[\w)\]\.,!?:;\"'”’])\[[0-9]+(?:\s*,\s*[0-9]+)*\](?!\()")
BACKTICK_NOISE_RE = re.compile(r"`{3,}")


# ---- selectMessageRunning ----


# ---- titleForTool ----


def title_for_tool(name: str) -> str:
    normalized = re.sub(r"^browser_", "", name)
    normalized = re.sub(r"^web_", "", normalized)

    return " ".join(capitalize(part) for part in normalized.split("_") if part) or name


PREFIX_META: list[dict[str, str]] = [
    {"prefix": "browser_", "label_key": "browser", "icon": "globe", "tone": "browser"},
    {"prefix": "web_", "label_key": "web", "icon": "globe", "tone": "web"},
]


def tool_meta(name: str) -> ToolMeta:
    if is_tool_title_key(name):
        meta = TOOL_META[name]

        return ToolMeta(
            done=tr(f"assistant.tool.titles.{name}.done"),
            pending=tr(f"assistant.tool.titles.{name}.pending"),
            pending_action=tr(f"assistant.tool.titles.{name}.pendingAction"),
            icon=meta.icon,
            tone=meta.tone,
        )

    action = title_for_tool(name)
    prefix = next((item for item in PREFIX_META if name.startswith(item["prefix"])), None)

    if prefix:
        prefix_label = tr(f"assistant.tool.prefixes.{prefix['label_key']}")

        return ToolMeta(
            done=tr("assistant.tool.titleTemplates.prefixedDone", prefix_label, action),
            pending=tr("assistant.tool.titleTemplates.runningPrefixedTool", prefix_label, action),
            pending_action=tr("assistant.tool.actions.running"),
            icon=prefix["icon"],
            tone=prefix["tone"],  # type: ignore[arg-type]
        )

    return ToolMeta(
        done=action,
        pending=tr("assistant.tool.titleTemplates.runningTool", action),
        pending_action=tr("assistant.tool.actions.running"),
        tone="default",
    )


COUNT_FIELD_KEYS = (
    "count",
    "total",
    "result_count",
    "results_count",
    "num_results",
    "match_count",
    "matches_count",
    "file_count",
    "files_count",
    "item_count",
    "items_count",
    "search_count",
    "searches_count",
    "source_count",
    "sources_count",
    "document_count",
    "documents_count",
    "updated",
    "added",
    "removed",
    "deleted",
    "created",
    "changed",
    "processed",
    "steps",
)

COUNT_ARRAY_KEYS = "results", "items", "matches", "files", "documents", "sources", "rows"

COUNT_EXCLUDED_KEYS = {"duration_s", "exit_code", "status_code"}

COUNT_NOUN_BY_FIELD: dict[str, str] = {
    "count": "",
    "total": "",
    "result_count": "result",
    "results_count": "result",
    "num_results": "result",
    "match_count": "match",
    "matches_count": "match",
    "file_count": "file",
    "files_count": "file",
    "item_count": "item",
    "items_count": "item",
    "search_count": "search",
    "searches_count": "search",
    "source_count": "source",
    "sources_count": "source",
    "document_count": "document",
    "documents_count": "document",
    "updated": "item",
    "added": "item",
    "removed": "item",
    "deleted": "item",
    "created": "item",
    "changed": "item",
    "processed": "item",
    "steps": "step",
}

COUNT_NOUN_BY_ARRAY: dict[str, str] = {
    "documents": "document",
    "files": "file",
    "items": "item",
    "matches": "match",
    "results": "result",
    "rows": "row",
    "sources": "source",
}

DEFAULT_COUNT_NOUN_BY_TOOL: dict[str, str] = {
    "browser_snapshot": "item",
    "list_files": "file",
    "search_files": "result",
    "session_search_recall": "result",
    "todo": "todo",
    "todo_list": "todo",
    "web_search": "result",
}


def count_from_unknown(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        return len(value) if len(value) > 0 else None

    n = number_value(value)

    if n is None or n <= 0:
        return None

    return _round_half_up(n)


def singularize_noun(noun: str) -> str:
    normalized = normalize(noun)

    if not normalized:
        return ""

    if normalized.endswith("ies") and len(normalized) > 3:
        return f"{normalized[:-3]}y"

    if re.search(r"(xes|zes|ches|shes|sses)$", normalized, re.A) and len(normalized) > 3:
        return normalized[:-2]

    if normalized.endswith("s") and len(normalized) > 2 and not normalized.endswith("ss"):
        return normalized[:-1]

    return normalized


def pluralize_noun(noun: str, count: int) -> str:
    if count == 1:
        return noun

    if noun == "search":
        return "searches"

    if noun.endswith("y") and len(noun) > 1 and not re.search(r"[aeiou]y$", noun, re.I | re.A):
        return f"{noun[:-1]}ies"

    if re.search(r"(s|x|z|ch|sh)$", noun, re.I | re.A):
        return f"{noun}es"

    return f"{noun}s"


def format_count_label(metric: CountMetric) -> str:
    return f"{metric.count} {pluralize_noun(metric.noun, metric.count)}"


def count_metric(count: int, noun: str) -> CountMetric:
    return CountMetric(count=count, noun=singularize_noun(noun) or "item")


def normalize_metric_for_tool(tool_name: str, metric: CountMetric) -> CountMetric:
    if tool_name == "web_search":
        return count_metric(metric.count, "result")

    return metric


def fallback_count_noun(tool_name: str) -> str:
    return DEFAULT_COUNT_NOUN_BY_TOOL.get(tool_name) or "item"


def dynamic_count_noun_from_key(key: str, fallback_noun: str) -> str:
    normalized = key.lower()

    if normalized in ("count", "total"):
        return fallback_noun

    stripped = re.sub(r"_(count|total)$", "", normalized, flags=re.I | re.A)
    stripped = re.sub(r"^num_", "", stripped)
    stripped = re.sub(r"^total_", "", stripped)
    if stripped in ("", "total", "num"):
        return fallback_noun

    return singularize_noun(stripped) or fallback_noun


def count_from_record(record: dict[str, Any], fallback_noun: str) -> CountMetric | None:
    for key in COUNT_FIELD_KEYS:
        value = record.get(key)
        count = count_from_unknown(value)

        if count is not None:
            return count_metric(count, COUNT_NOUN_BY_FIELD.get(key) or fallback_noun)

    for key in COUNT_ARRAY_KEYS:
        value = record.get(key)
        count = count_from_unknown(value)

        if count is not None:
            return count_metric(count, COUNT_NOUN_BY_ARRAY.get(key) or fallback_noun)

    for key, value in record.items():
        if key in COUNT_EXCLUDED_KEYS:
            continue

        if not re.search(r"_count$|_total$", key, re.I | re.A):
            continue

        count = count_from_unknown(value)

        if count is not None:
            return count_metric(count, dynamic_count_noun_from_key(key, fallback_noun))

    return None


COUNT_TEXT_UNIT_RE = re.compile(
    r"\b([0-9]+)\s+(results?|items?|files?|matches?|documents?|sources?|searches?|steps?|rows?)\b", re.I | re.A
)
COUNT_TEXT_VERB_RE = re.compile(
    r"\b(?:did|found|returned|listed|searched|matched|updated|created|deleted|processed)\s+([0-9]+)\b", re.I | re.A
)


def count_from_text(value: str, fallback_noun: str) -> CountMetric | None:
    text = value.strip()

    if not text:
        return None

    unit_match = COUNT_TEXT_UNIT_RE.search(text) or COUNT_TEXT_VERB_RE.search(text)

    if unit_match and unit_match.group(1):
        n = float(unit_match.group(1))
        second = unit_match.group(2) if unit_match.re.groups >= 2 else None
        noun = singularize_noun(second) if second else fallback_noun

        return count_metric(_round_half_up(n), noun) if math.isfinite(n) and n > 0 else None

    return None


def tool_result_count(
    part: ToolPart, args_record: dict[str, Any], result_record: dict[str, Any]
) -> CountMetric | None:
    if part.result is None:
        return None

    fallback_noun_by_tool = fallback_count_noun(part.tool_name)

    if part.tool_name == "web_search":
        hits = collect_result_items(part.result)

        if hits:
            return count_metric(len(hits), "result")

    if part.tool_name == "memory":
        entry_total = count_from_unknown(result_record.get("entry_count"))

        if entry_total is not None:
            return count_metric(entry_total, "entry")

    direct_count = count_from_record(result_record, fallback_noun_by_tool)

    if direct_count is not None:
        return normalize_metric_for_tool(part.tool_name, direct_count)

    payload = unwrap_tool_payload(part.result)

    if is_record(payload):
        payload_count = count_from_record(payload, fallback_noun_by_tool)

        if payload_count is not None:
            return normalize_metric_for_tool(part.tool_name, payload_count)

    summary_text = first_string_field(result_record, ["summary", "message", "detail"]) or fallback_detail_text(
        args_record, result_record
    )

    text_metric = count_from_text(summary_text, fallback_noun_by_tool)

    return normalize_metric_for_tool(part.tool_name, text_metric) if text_metric else None


# ---- looksRedundant ----


def looks_redundant(title: str, detail: str) -> bool:
    if not detail:
        return True

    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.lower()).strip()

    return norm(title) == norm(detail)


LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


# ---- cleanVisibleText ----


def clean_visible_text(text: str) -> str:
    out: list[str] = []

    for part in INLINE_CODE_SPLIT_RE.split(text):
        if part.startswith("`"):
            out.append(part)
        else:
            cleaned = BACKTICK_NOISE_RE.sub("", part)
            cleaned = CITATION_MARKER_RE.sub("", cleaned)
            cleaned = LINK_RE.sub(
                lambda match: f"{match.group(1)} {normalize_external_url(match.group(2))}", cleaned
            )
            out.append(cleaned)

    return "".join(out)


BROWSER_BUTTON_RE = re.compile(r'button\s+"[^"]+"')
BROWSER_LINK_RE = re.compile(r'link\s+"[^"]+"')
BROWSER_INPUT_RE = re.compile(r'(?:textbox|combobox|searchbox)\s+"[^"]+"')
BROWSER_CONTROL_RE = re.compile(r'(?:button|link|combobox|textbox)\s+"([^"]+)"')


# ---- summarizeBrowserSnapshot ----


def summarize_browser_snapshot(snapshot: str) -> str:
    def count(regex: re.Pattern[str]) -> int:
        return len(regex.findall(snapshot))

    stats = " · ".join(
        [
            f"{count(BROWSER_BUTTON_RE)} buttons",
            f"{count(BROWSER_LINK_RE)} links",
            f"{count(BROWSER_INPUT_RE)} inputs",
        ]
    )

    labels = [match.group(1).strip() for match in BROWSER_CONTROL_RE.finditer(snapshot)]
    labels = [label for label in labels if label][:4]

    return f"{stats}\nTop controls: {', '.join(labels)}" if labels else stats


RESULT_ITEM_KEYS = (
    "web",
    "results",
    "search_results",
    "sources",
    "web_sources",
    "items",
    "organic_results",
    "organic",
    "matches",
    "documents",
)


# ---- collectResultItems ----


def collect_result_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value

    record = parse_maybe_object(value)

    for key in RESULT_ITEM_KEYS:
        candidate = record.get(key)

        if isinstance(candidate, list):
            return candidate

        if is_record(candidate):
            nested = collect_result_items(candidate)

            if nested:
                return nested

    payload = unwrap_tool_payload(record)

    return [] if payload is record or payload == record else collect_result_items(payload)


# ---- extractSearchResults ----


def extract_search_results(result: Any, limit: int = 6) -> list[SearchResultRow]:
    rows: list[SearchResultRow] = []

    for item in collect_result_items(result):
        row = parse_maybe_object(item)

        rows.append(
            SearchResultRow(
                title=clean_visible_text(first_string_field(row, ["title", "name"])),
                url=first_string_field(row, ["url", "href", "link"]),
                snippet=clean_visible_text(first_string_field(row, ["snippet", "description", "body"])),
            )
        )

    rows = [row for row in rows if row.title or row.url]

    return rows[:limit]


# ---- toolErrorText ----


def tool_error_text(part: ToolPart, result: dict[str, Any]) -> str:
    extracted_error = extract_tool_error_message(part.result)

    if part.is_error:
        return (
            extracted_error
            or envelope_error_text(part.tool_result_metadata)
            or (part.result.strip() if isinstance(part.result, str) and part.result.strip() else "")
            or "Tool returned an error."
        )

    if extracted_error:
        return extracted_error

    if result.get("success") is False or result.get("ok") is False:
        return first_string_field(result, ["message", "reason", "detail"]) or "Tool returned success=false."

    status = result.get("status")

    if isinstance(status, str) and re.fullmatch(r"(error|failed|failure|fatal|exception)", status.strip(), re.I | re.A):
        return first_string_field(result, ["message", "reason", "detail"]) or f'Tool returned status "{status}".'

    exit_code = number_value(result.get("exit_code"))

    if exit_code is not None and exit_code != 0:
        return f"Command failed with exit code {_num(exit_code)}."

    return ""


# ---- toolStatus ----


def tool_status(part: ToolPart, result_record: dict[str, Any]) -> ToolStatus:
    if part.result is None and part.completed_at is None:
        return "running"

    if part.result is None and not part.is_error:
        return "notice" if part.interrupted else "warning"

    if result_record.get("success") is True or result_record.get("ok") is True:
        return "success"

    error = tool_error_text(part, result_record)

    if not error:
        return "success"

    if part.tool_name == "read_file" and re.match(r"File not found:", error, re.I):
        return "notice"

    if part.tool_name == "terminal" and error == "Command failed with exit code 1.":
        return "notice"

    if is_neutral_failure(part.tool_name, part.result):
        return "notice"

    return "warning" if part.tool_name == "memory" else "error"


# ---- durationLabel ----


def duration_label(result_record: dict[str, Any]) -> str | None:
    raw = result_record.get("duration_s")
    if raw is None:
        raw = result_record.get("duration_seconds")
    if raw is None:
        return None

    seconds = number_value(raw)

    if seconds is None or seconds < 0:
        return None

    return format_duration_seconds(seconds)


NON_DELIVERABLE_TOOLS = {
    "annotate_preview",
    "drive_preview",
    "gui_tour",
    "list_files",
    "read_file",
    "search_files",
}


# ---- toolPreviewTarget ----


def tool_preview_target(tool_name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
    if tool_name == "tool_call":
        raw_calls = args.get("calls")
        if isinstance(raw_calls, list):
            calls = raw_calls
        elif isinstance(raw_calls, dict):
            calls = [raw_calls]
        else:
            calls = [args]
        call = parse_maybe_object(calls[0]) if calls else {}

        if len(calls) == 1 and isinstance(call.get("name"), str):
            return tool_preview_target(call["name"], parse_maybe_object(call.get("arguments")), result)

    if tool_name in NON_DELIVERABLE_TOOLS or (tool_name == "desktop_preview" and args.get("action") != "open"):
        return ""

    direct = (
        first_string_field(result, ["preview", "url", "target"])
        or first_string_field(args, ["preview", "url", "target", "path", "file", "filepath"])
        or first_string_field(result, ["path", "file", "filepath"])
    )

    if direct and (looks_like_url(direct) or looks_like_path(direct)):
        return direct

    if tool_name in ("browser_navigate", "web_extract", "web_search"):
        explicit = first_string_field(args, ["url", "search_term", "query"]) or first_string_field(result, ["url"])

        return explicit if looks_like_url(explicit) else find_first_url(args, result)

    if is_file_edit_tool(tool_name):
        return html_path_from_inline_diff(first_string_field(result, ["inline_diff", "diff"]))

    return ""


# ---- toolImageUrl ----


def tool_image_url(args: dict[str, Any], result: dict[str, Any]) -> str:
    candidate = first_string_field(result, ["image_url", "url", "path", "image_path"]) or first_string_field(
        args, ["image_url", "url", "path"]
    )

    if not candidate:
        return ""

    is_data_image = candidate.lower().startswith("data:image/")
    is_remote_image = re.match(r"https?://", candidate, re.I) is not None and re.search(
        r"\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)", candidate, re.I
    ) is not None
    is_local_image = is_file_media_path(candidate) and media_kind(candidate) == "image"

    return candidate if is_data_image or is_remote_image or is_local_image else ""


INLINE_DIFF_CHROME_RE = re.compile(r"^\s*┊\s*review diff\s*\n", re.I)


# ---- stripInlineDiffChrome ----


def strip_inline_diff_chrome(value: str) -> str:
    if not value:
        return ""

    return INLINE_DIFF_CHROME_RE.sub("", strip_ansi(value)).strip()


INLINE_DIFF_HTML_RE = re.compile(r"(?:^|\s)(?:[ab]/)?([^\s]+\.html?)(?=\s|$)", re.I)


# ---- htmlPathFromInlineDiff ----


def html_path_from_inline_diff(value: str) -> str:
    cleaned = strip_inline_diff_chrome(value)

    for match in INLINE_DIFF_HTML_RE.finditer(cleaned):
        candidate = match.group(1).strip() if match.group(1) else ""

        if candidate:
            return candidate

    return ""


DIVIDER_LINE_RE = re.compile(r"^[-=]{3,}\s*$")


# ---- stripDividerLines ----


def strip_divider_lines(value: str) -> str:
    return "\n".join(line for line in value.split("\n") if not DIVIDER_LINE_RE.match(line.strip())).strip()


# ---- inlineDiffFromResult ----


def inline_diff_from_result(result: Any) -> str:
    record = parse_maybe_object(result)

    for key in ("inline_diff", "diff"):
        value = record.get(key)

        if isinstance(value, str) and value.strip():
            return strip_inline_diff_chrome(value)

    return ""


def minimal_value_summary(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return _num(value)

    return ""


# ---- fallbackDetailText ----


def fallback_detail_text(args: Any, result: Any) -> str:
    arg_context = context_value(args)
    result_context = context_value(result)

    if result_context and result_context != arg_context:
        return result_context

    if arg_context:
        return arg_context

    if result is not None:
        return format_tool_result_summary(result) or minimal_value_summary(result)

    return format_tool_result_summary(args) or minimal_value_summary(args)


# ---- cronScalar ----


def cron_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""

    return _num(value) if math.isfinite(value) else ""


# ---- formatCronTime ----


def format_cron_time(iso: str) -> str:
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()

    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.strftime('%I:%M %p')}"


# ---- cronjobSubtitle ----


def cronjob_subtitle(args_record: dict[str, Any], result_record: dict[str, Any]) -> str:
    jobs = result_record.get("jobs") if isinstance(result_record.get("jobs"), list) else None

    if jobs is not None:
        if not jobs:
            return "No cron jobs"

        return f"{len(jobs)} cron job{'' if len(jobs) == 1 else 's'}"

    message = first_string_field(result_record, ["message"])

    if message:
        return message

    action = first_string_field(args_record, ["action"]) or "manage"
    name = first_string_field(result_record, ["name"]) or first_string_field(args_record, ["name", "job_id"])
    label = capitalize(action)

    return f"{label} {name}" if name else f"Cron {action}"


# ---- cronjobDetail ----


def cronjob_detail(args_record: dict[str, Any], result_record: dict[str, Any]) -> str:
    jobs = result_record.get("jobs") if isinstance(result_record.get("jobs"), list) else None

    if jobs is not None:
        if not jobs:
            return "No cron jobs scheduled"

        lines: list[str] = []

        for job in jobs[:20]:
            row = job if is_record(job) else {}
            name = first_string_field(row, ["name", "id"]) or "job"
            sched = first_string_field(row, ["schedule_display", "schedule"])

            lines.append(f"- {name} · {sched}" if sched else f"- {name}")

        return "\n".join(lines)

    next_run = cron_scalar(result_record.get("next_run_at"))

    rows: list[tuple[str, str]] = [
        ("Schedule", cron_scalar(result_record.get("schedule"))),
        ("Repeat", cron_scalar(result_record.get("repeat"))),
        ("Delivery", cron_scalar(result_record.get("deliver"))),
        ("Next run", format_cron_time(next_run) if next_run else ""),
    ]

    lines = [f"{key}: {value}" for key, value in rows if value]

    return "\n".join(lines) if lines else fallback_detail_text(args_record, result_record)


# ---- toolSubtitle ----


def tool_subtitle(part: ToolPart, args_record: dict[str, Any], result_record: dict[str, Any]) -> str:
    tool_name = part.tool_name

    if tool_name == "browser_navigate":
        url = (
            first_string_field(args_record, ["url", "target"])
            or first_string_field(result_record, ["url"])
            or find_first_url(args_record, result_record)
        )

        return hostname_of(url) if url else "Navigated in browser"

    if tool_name == "browser_snapshot":
        snapshot = first_string_field(result_record, ["snapshot"])

        return summarize_browser_snapshot(snapshot) if snapshot else "Captured a browser accessibility snapshot"

    if tool_name == "browser_click":
        clicked = first_string_field(result_record, ["clicked"]) or first_string_field(args_record, ["ref", "target"])

        if not clicked:
            return "Clicked on page"

        return f"Clicked page element (internal ref {clicked})" if clicked.startswith("@") else f"Clicked {clicked}"

    if tool_name in ("browser_fill", "browser_type"):
        field = first_string_field(args_record, ["label", "field", "ref", "target"])
        value = first_string_field(args_record, ["value", "text"])

        joined = " · ".join(
            part_
            for part_ in [f"Field: {field}" if field else "", f"Value: {compact_preview(value, 42)}" if value else ""]
            if part_
        )

        return joined or "Filled page input"

    if tool_name == "web_search":
        query = first_string_field(args_record, ["search_term", "query"]) or context_value(args_record)

        return f"Query: {query}" if query else "Queried web sources"

    if tool_name in ("terminal", "execute_code"):
        output = first_string_field(result_record, ["output", "stdout", "stderr"])

        lines = (
            "\n".join(line for line in result_record["lines"] if isinstance(line, str))
            if isinstance(result_record.get("lines"), list)
            else ""
        )

        preview_source = (output or lines).strip()

        if preview_source:
            first_meaningful_line = next(
                (line.strip() for line in preview_source.split("\n") if len(line.strip()) > 0), None
            )

            if first_meaningful_line:
                return compact_preview(first_meaningful_line, 160)

        command = first_string_field(args_record, ["context", "preview", "command", "code"]) or context_value(
            args_record
        )

        return "" if command else "Executed command"

    if tool_name == "read_file" or is_file_edit_tool(tool_name):
        is_edit = is_file_edit_tool(tool_name)

        path = (
            file_edit_path(args_record, result_record)
            if is_edit
            else first_string_field(args_record, ["path", "file", "filepath"])
        )

        if path:
            return path

        if not is_edit:
            return fallback_detail_text(args_record, result_record)

        return "Changed file" if inline_diff_from_result(result_record) else ""

    if tool_name == "web_extract":
        url = (
            first_string_field(args_record, ["url"])
            or first_string_field(result_record, ["url"])
            or find_first_url(args_record, result_record)
        )

        return hostname_of(url) if url else "Fetched webpage"

    if tool_name == "memory":
        return first_string_field(result_record, ["message", "error"])

    if tool_name == "cronjob":
        return cronjob_subtitle(args_record, result_record)

    return (
        compact_preview(format_tool_result_summary(part.result), 120)
        or compact_preview(result_record, 120)
        or compact_preview(args_record, 120)
        or fallback_detail_text(args_record, result_record)
    )


# ---- toolDetailLabel ----


def tool_detail_label(tool_name: str) -> str:
    if tool_name == "web_search":
        return "Details"

    if tool_name == "browser_snapshot":
        return "Snapshot summary"

    return ""


# ---- toolDetailText ----


def file_search_detail(part: ToolPart, result_record: dict[str, Any], limit: int = 40) -> str:
    """搜索结果铺成人能看的样子：`路径:行号  命中的那一行`；只给文件清单时一行一个路径。

    以前这里落到通用兜底，而 `search_files` 的结果里没有 output/stdout 这些字段，兜底给出空串——
    于是行上写着「已搜索文件 · 1 file」，点开折叠区什么都没有。
    """
    lines: list[str] = []

    matches = result_record.get("matches")
    if isinstance(matches, list):
        for item in matches:
            row = parse_maybe_object(item)
            path = first_string_field(row, ["path", "file", "filepath", "name"])
            text = clean_visible_text(first_string_field(row, ["content", "text", "line_text", "match"]))
            line_no = number_value(row.get("line", row.get("line_number")))
            head = f"{path}:{int(line_no)}" if path and line_no is not None else path
            if head and text:
                lines.append(f"{head}\n    {text.rstrip()}")
            elif head or text:
                lines.append((head or text).rstrip())

    dense = first_string_field(result_record, ["matches_text"])
    if dense and not lines:
        lines.extend(line.rstrip() for line in dense.splitlines())

    files = result_record.get("files")
    if isinstance(files, list):
        lines.extend(str(item).strip() for item in files if str(item).strip())

    counts = result_record.get("counts")
    if isinstance(counts, dict):
        lines.extend(tr("assistant.tool.details.matchCount", path, count) for path, count in counts.items())

    total = number_value(result_record.get("total_count"))
    if total is not None and total > len(lines):
        lines.append(tr("assistant.tool.details.totalCount", int(total)) + (tr("assistant.tool.details.truncatedMark") if result_record.get("truncated") else ""))

    if not lines and part.result is not None and not any(
        first_string_field(result_record, [key]) for key in ("output", "stdout", "stderr", "content", "text", "message")
    ):
        return tr("assistant.tool.details.noMatches")

    return "\n".join(lines[:limit])


def tool_detail_text(part: ToolPart, args_record: dict[str, Any], result_record: dict[str, Any]) -> str:
    if part.tool_name == "browser_snapshot":
        snapshot = first_string_field(result_record, ["snapshot"])

        return summarize_browser_snapshot(snapshot) if snapshot else fallback_detail_text(args_record, result_record)

    if part.tool_name in ("terminal", "execute_code"):
        output = first_string_field(result_record, ["output", "stdout", "stderr"])

        lines = (
            "\n".join(line for line in result_record["lines"] if isinstance(line, str))
            if isinstance(result_record.get("lines"), list)
            else ""
        )

        if output or lines:
            return "\n".join(value for value in [output, lines] if value)

        if part.tool_name == "terminal":
            return ""

    if part.tool_name in ("search_files", "list_files"):
        return file_search_detail(part, result_record)

    if part.tool_name == "web_extract":
        direct = first_string_field(result_record, ["content", "text", "markdown", "body", "summary", "message"])

        if direct:
            return re.sub(r"\s*in\s+[0-9]+(?:\.[0-9]+)?s\s*$", "", direct, flags=re.I | re.A).strip()

        raw_results = result_record.get("results")
        results = raw_results if isinstance(raw_results, list) else []

        aggregated = "\n\n---\n\n".join(
            value
            for value in (
                first_string_field(parse_maybe_object(item), ["content", "text", "markdown", "body"])
                for item in results
            )
            if value
        )

        if aggregated:
            return aggregated

    if part.tool_name == "read_file" and part.result is not None:
        content = first_string_field(result_record, ["content", "text", "data", "body"])

        if content:
            return content

    if part.tool_name == "memory":
        return first_string_field(result_record, ["message", "error"])

    if is_file_edit_tool(part.tool_name):
        if inline_diff_from_result(part.result):
            return ""

        summary = first_string_field(result_record, ["message", "summary"])

        if summary:
            return summary

        if file_edit_path(args_record, result_record):
            return ""

        return fallback_detail_text(args_record, result_record)

    if part.tool_name == "web_search":
        detail = fallback_detail_text(args_record, result_record)
        seconds = number_value(result_record.get("duration_s"))
        duration = "" if seconds is None else format_duration_seconds(seconds)

        if not duration:
            return detail

        detail = re.sub(
            r"^\s*-\s*Duration\s+S\s*:\s*[-+]?[0-9.]+(?:e[-+]?[0-9]+)?\s*$",
            f"- Duration: {duration}",
            detail,
            flags=re.I | re.M | re.A,
        )

        return re.sub(r"\bDuration\s+S\s*:", "Duration:", detail, flags=re.I | re.A)

    if part.tool_name == "cronjob":
        return cronjob_detail(args_record, result_record)

    return fallback_detail_text(args_record, result_record)


# ---- toolCopyPayload ----


def tool_copy_payload(part: ToolPart, view: ToolView) -> dict[str, str]:
    copy = {
        "command": tr("assistant.tool.copyCommand"),
        "content": tr("assistant.tool.copyContent"),
        "code": tr("assistant.tool.copyCode"),
        "file": tr("assistant.tool.copyFile"),
        "output": tr("assistant.tool.copyOutput"),
        "path": tr("assistant.tool.copyPath"),
        "query": tr("assistant.tool.copyQuery"),
        "results": tr("assistant.tool.copyResults"),
        "url": tr("assistant.tool.copyUrl"),
        "generic": tr("common.copy"),
    }

    args = parse_maybe_object(part.args)
    result = parse_maybe_object(part.result)
    detail = view.detail.strip()
    has_substantial_output = len(detail) > 16

    if part.tool_name in ("terminal", "execute_code"):
        if has_substantial_output:
            return {"label": copy["output"], "text": detail}

        command = first_string_field(args, ["command", "code"]) or context_value(args)

        if command:
            return {"label": copy["command"], "text": command}

    if part.tool_name == "web_extract":
        if has_substantial_output:
            return {"label": copy["content"], "text": detail}

        url = first_string_field(args, ["url", "target"]) or find_first_url(args, result)

        if url:
            return {"label": copy["url"], "text": url}

    if part.tool_name == "browser_navigate":
        url = first_string_field(args, ["url", "target"]) or find_first_url(args, result)

        if url:
            return {"label": copy["url"], "text": url}

    if part.tool_name == "web_search":
        if view.search_hits:
            text = "\n\n".join(
                "\n".join(value for value in [hit.title, hit.url, hit.snippet] if value) for hit in view.search_hits
            )

            return {"label": copy["results"], "text": text}

        query = first_string_field(args, ["search_term", "query"]) or context_value(args)

        if query:
            return {"label": copy["query"], "text": query}

    if part.tool_name == "read_file" and part.result is not None:
        if has_substantial_output:
            return {"label": copy["file"], "text": detail}

        path = first_string_field(args, ["path", "file", "filepath"])

        if path:
            return {"label": copy["path"], "text": path}

    if is_file_edit_tool(part.tool_name):
        if view.inline_diff.strip():
            return {"label": copy["code"], "text": view.inline_diff}

        path = file_edit_path(args, result)

        if path:
            return {"label": copy["path"], "text": path}

    if detail:
        return {"label": copy["output"], "text": detail}

    return {"label": copy["generic"], "text": view.title}


# ---- titlePartsFromAction ----


def title_parts_from_action(title: str, action: str | None = None) -> ToolTitleParts:
    if not action:
        return ToolTitleParts(title=title)

    action_start = title.find(action)

    if action_start < 0:
        return ToolTitleParts(title=title)

    return ToolTitleParts(
        title=title,
        action=ToolTitleAction(
            prefix=title[:action_start],
            suffix=title[action_start + len(action) :],
            text=action,
        ),
    )


def without_leading_action(value: str, action: str) -> str:
    verb = action.strip()
    text = value.lstrip()
    boundary_match = re.search(r"\s", text)

    if not verb or boundary_match is None:
        return value

    boundary = boundary_match.start()

    if text[:boundary].lower() == verb.lower():
        return text[boundary + 1 :].lstrip()

    return value


# ---- dynamicTitle ----


def dynamic_title(
    part: ToolPart,
    args: dict[str, Any],
    result: dict[str, Any],
    fallback: ToolTitleParts,
) -> ToolTitleParts:
    skill_title = _skill_activity_title(part) or ""

    if skill_title:
        return ToolTitleParts(title=skill_title)

    def verb(gerund: str, past: str) -> str:
        return gerund if part.result is None else past

    def titled_action(action: str, title: str) -> ToolTitleParts:
        return title_parts_from_action(title, action if part.result is None else None)

    if part.tool_name == "web_extract":
        url = find_first_url(args, result)
        action = verb(tr("assistant.tool.actions.reading"), tr("assistant.tool.actions.read"))

        return (
            titled_action(action, tr("assistant.tool.titleTemplates.actionTarget", action, hostname_of(url)))
            if url
            else fallback
        )

    if part.tool_name == "browser_navigate":
        url = find_first_url(args, result)

        if not url:
            return fallback

        failed = (
            bool(part.is_error)
            or result.get("success") is False
            or result.get("ok") is False
            or bool(first_string_field(result, ["error"]))
        )

        if failed:
            fail_action = tr("assistant.tool.actions.failedToOpen")

            return titled_action(
                fail_action,
                tr("assistant.tool.titleTemplates.actionTarget", fail_action, hostname_of(url)),
            )

        action = verb(tr("assistant.tool.actions.opening"), tr("assistant.tool.actions.opened"))

        return titled_action(action, tr("assistant.tool.titleTemplates.actionTarget", action, hostname_of(url)))

    if part.tool_name == "web_search":
        query = first_string_field(args, ["search_term", "query"]) or context_value(args)

        action = verb(tr("assistant.tool.actions.searching"), tr("assistant.tool.actions.searched"))

        return (
            titled_action(
                action,
                tr("assistant.tool.titleTemplates.actionQuoted", action, compact_preview(query, 48)),
            )
            if query
            else fallback
        )

    if part.tool_name == "read_file":
        target = read_file_display_target(args, result)
        action = verb(tr("assistant.tool.actions.reading"), tr("assistant.tool.actions.read"))

        return (
            titled_action(action, tr("assistant.tool.titleTemplates.actionTarget", action, target))
            if target
            else fallback
        )

    if part.tool_name in ("terminal", "execute_code"):
        command = shell_command(args)

        if command:
            action = (
                verb(tr("assistant.tool.actions.runningCode"), tr("assistant.tool.actions.ranCode"))
                if part.tool_name == "execute_code"
                else verb(tr("assistant.tool.actions.running"), tr("assistant.tool.actions.ran"))
            )

            return titled_action(
                action,
                tr(
                    "assistant.tool.titleTemplates.actionCommand",
                    action,
                    without_leading_action(compact_preview(summarize_shell_command(command), 160), action),
                ),
            )

    if part.tool_name == "browser_exec":
        label = browser_exec_step_label(first_string_field(args, ["code"]))

        if label:
            return ToolTitleParts(title=label)

    if is_file_edit_tool(part.tool_name):
        path = file_edit_path(args, result)

        if path:
            return ToolTitleParts(title=file_edit_basename(path))

    return fallback


# ---- buildToolView ----


def build_tool_view(part: ToolPart | Mapping[str, Any], inline_diff: str) -> ToolView:
    part = ToolPart.from_mapping(part)
    args_record = parse_maybe_object(part.args)
    result_record = tool_result_record(part)
    meta = tool_meta(part.tool_name)
    status = tool_status(part, result_record)
    error = "" if status == "success" else tool_error_text(part, result_record)
    memory_missed = part.tool_name == "memory" and part.result is not None and status != "success"

    if part.result is None:
        base_title = meta.pending
    elif memory_missed:
        base_title = tr("assistant.tool.memoryWriteNoted")
    else:
        base_title = meta.done

    title_parts = dynamic_title(
        part,
        args_record,
        result_record,
        title_parts_from_action(base_title, meta.pending_action if part.result is None else None),
    )

    unavailable = part.result is None and part.completed_at is not None

    title = (
        tr("assistant.tool.resultInterrupted" if part.interrupted else "assistant.tool.resultUnavailable")
        if unavailable
        else title_parts.title
    )

    title_enriched = title != base_title
    base_subtitle = error or tool_subtitle(part, args_record, result_record)

    keep_subtitle_with_title = (
        part.tool_name in ("terminal", "execute_code")
        or (is_file_edit_tool(part.tool_name) and bool(base_subtitle.strip()))
    )

    subtitle = "" if title_enriched and not error and not keep_subtitle_with_title else base_subtitle
    detail_body = strip_divider_lines(tool_detail_text(part, args_record, result_record))

    if error:
        deduped: list[str] = []

        for value in [error, detail_body]:
            if not value:
                continue
            if any(entry.strip() == value.strip() for entry in deduped):
                continue
            deduped.append(value)

        detail = "\n\n".join(deduped)
    else:
        detail = detail_body

    search_hits = (
        extract_search_results(part.result) if part.tool_name == "web_search" and status != "error" else None
    )

    search_query = (
        (first_string_field(args_record, ["search_term", "query"]) or context_value(args_record))
        if part.tool_name == "web_search"
        else ""
    )

    result_count = None if status == "error" else tool_result_count(part, args_record, result_record)

    renders_ansi = part.tool_name in ("terminal", "execute_code")
    stdout = first_string_field(result_record, ["stdout"]) if renders_ansi else ""
    stderr_raw = first_string_field(result_record, ["stderr"]) if renders_ansi else ""
    has_split_streams = renders_ansi and (bool(stdout) or bool(stderr_raw))
    terminal_command = shell_command(args_record) if part.tool_name == "terminal" else None
    terminal_exit_code = numeric_field(result_record, "exit_code") if part.tool_name == "terminal" else None

    return ToolView(
        count_label=format_count_label(result_count) if result_count else None,
        detail=detail,
        detail_label="Error details" if error else tool_detail_label(part.tool_name),
        duration_label=duration_label(result_record),
        icon=meta.icon,
        image_url=tool_image_url(args_record, result_record),
        inline_diff=inline_diff,
        preview_target=tool_preview_target(part.inner_tool_name or part.tool_name, args_record, result_record),
        renders_ansi=renders_ansi or None,
        search_query=search_query or None,
        search_hits=search_hits if search_hits else None,
        stderr=(stderr_raw or None) if has_split_streams else None,
        terminal_command=terminal_command,
        terminal_exit_code=terminal_exit_code,
        stdout=(stdout or None) if has_split_streams else None,
        status=status,
        subtitle=subtitle,
        title=title,
        title_action=None if unavailable else title_parts.action,
        tone=meta.tone,
    )
