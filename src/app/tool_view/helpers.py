"""工具渲染用到的底层小工具函数（纯 Python，不依赖 PySide6）。

文本、路径、媒体类型、命令摘要、链接归一、结果元数据、渲染分类、ANSI 剥离。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# JS 语义小垫片（只用于本文件内部，不导出）
# --------------------------------------------------------------------------

_CAMEL_BOUNDARY_RE = re.compile(r'(?<!^)(?=[A-Z])')


def _camel_to_snake(name: str) -> str:
    return _CAMEL_BOUNDARY_RE.sub('_', name).lower()


def source_field(source: Any, name: str, default: Any = None) -> Any:
    """取值适配：dict 用键、对象用同名属性。

    snake_case 别名只是为了让 Python 的 dataclass/dict 也能直接传入，
    不改变任何取值逻辑。
    """
    if isinstance(source, dict):
        if name in source:
            return source[name]
        return source.get(_camel_to_snake(name), default)

    if hasattr(source, name):
        return getattr(source, name)

    snake = _camel_to_snake(name)
    if hasattr(source, snake):
        return getattr(source, snake)

    return default


def _number_to_string(value: float | int) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    return str(value)


def _js_falsy(value: Any) -> bool:
    """JS falsy：null/undefined/false/0/NaN/''。"""
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0 or (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value == ''
    return False


def _js_string(value: Any) -> str:
    """`String(value)` 的近似：标量完全一致，容器退化为 Python 的 str()。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, (int, float)):
        return _number_to_string(value)
    return str(value)


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    return _js_string(value)


def normalize(value: Any) -> str:
    return as_text(value).strip().lower()


def capitalize(value: str) -> str:
    return value[0].upper() + value[1:] if value else value


def first_string_field(record: dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


# --------------------------------------------------------------------------
# 路径比较
# --------------------------------------------------------------------------

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')


def is_windows_absolute_path(path: str) -> bool:
    return bool(_WINDOWS_ABSOLUTE_PATH_RE.match(path))


# --------------------------------------------------------------------------
# 媒体
# --------------------------------------------------------------------------

MEDIA_BY_EXT: dict[str, dict[str, str]] = {
    'avi': {'kind': 'video', 'mime': 'video/x-msvideo'},
    'bmp': {'kind': 'image', 'mime': 'image/bmp'},
    'flac': {'kind': 'audio', 'mime': 'audio/flac'},
    'gif': {'kind': 'image', 'mime': 'image/gif'},
    'jpeg': {'kind': 'image', 'mime': 'image/jpeg'},
    'jpg': {'kind': 'image', 'mime': 'image/jpeg'},
    'm4a': {'kind': 'audio', 'mime': 'audio/mp4'},
    'mkv': {'kind': 'video', 'mime': 'video/x-matroska'},
    'mov': {'kind': 'video', 'mime': 'video/quicktime'},
    'mp3': {'kind': 'audio', 'mime': 'audio/mpeg'},
    'mp4': {'kind': 'video', 'mime': 'video/mp4'},
    'ogg': {'kind': 'audio', 'mime': 'audio/ogg'},
    'opus': {'kind': 'audio', 'mime': 'audio/ogg; codecs=opus'},
    'png': {'kind': 'image', 'mime': 'image/png'},
    'svg': {'kind': 'image', 'mime': 'image/svg+xml'},
    'wav': {'kind': 'audio', 'mime': 'audio/wav'},
    'webm': {'kind': 'video', 'mime': 'video/webm'},
    'webp': {'kind': 'image', 'mime': 'image/webp'},
}

_QUERY_OR_FRAGMENT_RE = re.compile(r'[?#]')
_FILE_MEDIA_PATH_RE = re.compile(r'^(?:file:|/|~/|[a-z]:[\\/]|\\\\)', re.IGNORECASE)


def _media_info(path: str) -> dict[str, str] | None:
    ext = _QUERY_OR_FRAGMENT_RE.split(path, maxsplit=1)[0].split('.')[-1].lower()
    return MEDIA_BY_EXT.get(ext) if ext else None


def media_kind(path: str) -> str:
    info = _media_info(path)
    return info['kind'] if info else 'file'


def is_file_media_path(path: str) -> bool:
    return bool(_FILE_MEDIA_PATH_RE.match(path))


# --------------------------------------------------------------------------
# 命令摘要
# --------------------------------------------------------------------------

SILENT_HEADS = frozenset(
    ['cd', 'pushd', 'popd', 'export', 'set', 'unset', 'source', '.', 'true', 'false', ':']
)
PIPE_TAIL_HEADS = frozenset(['head', 'tail', 'wc', 'sort', 'uniq'])

_WS_RE = re.compile(r'\s')
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_]\w*=')
_REDIRECT_RE = re.compile(r'^\d*(?:>>?|<)$')
_FD_REDIRECT_RE = re.compile(r'^\d*(?:>&|<&)\d+$')
_FD_REDIRECT2_RE = re.compile(r'^\d*>&\d+$')
_BOUNDARY_ECHO_RE = re.compile(r'-{2,}|_exit=|(?:^|\s|=)\$[?{]|PIPESTATUS')


def _basename(head: str) -> str:
    return head.split('/')[-1] or head


def _split_compound_command(value: str) -> list[str]:
    segments: list[str] = []
    buf = ''
    quote: str | None = None

    i = 0
    while i < len(value):
        ch = value[i]

        if quote:
            buf += ch
            if ch == quote and (i == 0 or value[i - 1] != '\\'):
                quote = None
            i += 1
            continue

        if ch == '"' or ch == "'":
            quote = ch
            buf += ch
            i += 1
            continue

        if value.startswith('&&', i) or value.startswith('||', i):
            op = value[i:i + 2]
        elif ch == ';' or ch == '\n':
            op = ch
        else:
            op = ''

        if op:
            segments.append(buf)
            buf = ''
            # JS: i += op.length - 1，随后 for 的 i += 1
            i += len(op)
            continue

        buf += ch
        i += 1

    segments.append(buf)

    return [seg for seg in (_strip_pipe_tail(s.strip()) for s in segments) if seg]


def _split_words(segment: str) -> list[str]:
    words: list[str] = []
    buf = ''
    quote: str | None = None

    i = 0
    while i < len(segment):
        ch = segment[i]

        if quote:
            buf += ch
            if ch == quote and (i == 0 or segment[i - 1] != '\\'):
                quote = None
            i += 1
            continue

        if ch == '"' or ch == "'":
            quote = ch
            buf += ch
            i += 1
            continue

        if _WS_RE.match(ch):
            if buf:
                words.append(buf)
                buf = ''
            i += 1
            continue

        buf += ch
        i += 1

    if buf:
        words.append(buf)

    return words


def _head_word(segment: str) -> str:
    tokens = _split_words(segment)
    index = 0

    while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
        index += 1

    return _basename(tokens[index] if index < len(tokens) else '')


def _strip_pipe_tail(segment: str) -> str:
    words = _split_words(segment)
    out: list[str] = []

    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ''
        if word == '|' and _basename(nxt) in PIPE_TAIL_HEADS:
            break
        out.append(word)

    return ' '.join(out).strip()


def _clean_segment(segment: str) -> str:
    words = _split_words(segment)
    out: list[str] = []

    i = 0
    while i < len(words):
        word = words[i]

        if _REDIRECT_RE.match(word):
            i += 2
            continue

        if _FD_REDIRECT_RE.match(word) or _FD_REDIRECT2_RE.match(word):
            i += 1
            continue

        out.append(word)
        i += 1

    return ' '.join(out).strip()


def _is_boundary_echo(segment: str) -> bool:
    words = _split_words(segment)

    if _basename(words[0] if words else '') != 'echo':
        return False

    rest = ' '.join(words[1:])

    return bool(_BOUNDARY_ECHO_RE.search(rest))


def summarize_shell_command(raw: str) -> str:
    original = (raw or '').strip()

    if not original:
        return ''

    segments = _split_compound_command(original)

    if len(segments) <= 1:
        return _clean_segment(original) or original

    core = [
        segment
        for segment in (s for s in (_clean_segment(s) for s in segments))
        if segment and _head_word(segment) not in SILENT_HEADS and not _is_boundary_echo(segment)
    ]

    if len(core) == 0:
        return original

    if len(core) == 1:
        return core[0]

    return f"{core[0]} + {len(core) - 1} {'command' if len(core) == 2 else 'commands'}"


# --------------------------------------------------------------------------
# 外链
# --------------------------------------------------------------------------

_DOMAIN_RE = re.compile(
    r'^(?:www\.)?[a-z0-9](?:[a-z0-9-]*\.)+[a-z]{2,}(?::\d+)?(?:[/?#][^\s]*)?$', re.IGNORECASE
)


def normalize_external_url(value: str) -> str:
    trimmed = value.strip()

    if not trimmed or re.match(r'^https?://', trimmed, re.IGNORECASE):
        return trimmed

    return f'https://{trimmed}' if _DOMAIN_RE.match(trimmed) else trimmed


# --------------------------------------------------------------------------
# 结果元数据
# --------------------------------------------------------------------------


def tool_result_record(source: Any) -> dict:
    value = source_field(source, 'result')

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = None

    record = value if isinstance(value, dict) else {}

    metadata = source_field(source, 'toolResultMetadata')

    # 权威结果字段覆盖可失败的展示提示
    merged = dict(metadata) if isinstance(metadata, dict) else {}
    merged.update(record)
    return merged


def envelope_error_text(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        metadata = {}

    error = metadata.get('error')
    text = error if isinstance(error, str) else metadata.get('message')

    return text.strip() if isinstance(text, str) else ''


# --------------------------------------------------------------------------
# 结果摘要
# --------------------------------------------------------------------------

WRAPPER_KEYS = ('data', 'result', 'output', 'response', 'payload')

PRIORITY_KEYS = (
    'title',
    'name',
    'path',
    'file',
    'filepath',
    'url',
    'href',
    'link',
    'status',
    'id',
    'message',
    'summary',
    'description',
)

ERROR_KEYS = ('error', 'errors', 'failure', 'exception')
# 刻意不含 'stderr'：很多 CLI 在 stderr 上打信息（npm 进度、git hint:），不是错误
ERROR_MSG_KEYS = ('message', 'reason', 'detail')
NON_ERROR_TEXT = frozenset(
    ['', '0', 'false', 'none', 'null', 'nil', 'ok', 'success', 'n/a', 'na']
)

_JSON_START_RE = re.compile(r'^[{[]|^"')
_TITLE_SPLIT_RE = re.compile(r'[_\-\.]+')


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _try_json(value: str) -> Any:
    t = value.strip()

    if not t:
        return ''

    if not _JSON_START_RE.match(t):
        return value

    try:
        return json.loads(t)
    except (ValueError, TypeError):
        return value


def _norm(value: Any) -> Any:
    return _try_json(value) if isinstance(value, str) else value


def _title_case(key: str) -> str:
    return ' '.join(capitalize(part) for part in _TITLE_SPLIT_RE.split(key) if part)


def _pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _clip_inline(value: str, max_chars: int = 180) -> str:
    c = re.sub(r'\s+', ' ', value).strip()
    return f'{c[:max_chars - 1]}…' if len(c) > max_chars else c


def _clip_block(value: str, max_chars: int = 1800, max_lines: int = 18) -> str:
    t = value.strip()

    if not t:
        return ''

    lines = t.split('\n')
    text = '\n'.join(lines[:max_lines])
    clipped = len(lines) > max_lines or len(text) > max_chars

    if len(text) > max_chars:
        text = text[:max_chars - 1].rstrip()

    return f'{text}…' if clipped and not text.endswith('…') else text


def _first_string(record: dict, keys: Sequence[str]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _ordered_keys(keys: Sequence[str]) -> list[str]:
    priority = [k for k in PRIORITY_KEYS if k in keys]
    rest = [k for k in keys if k not in priority]

    return [*priority, *rest]


def _is_wrapper_key(key: str) -> bool:
    return key in WRAPPER_KEYS


def _skip_field(key: str, value: Any) -> bool:
    return _is_wrapper_key(key) or ((key == 'success' or key == 'ok') and value is True)


def _summarize_scalar(value: Any) -> str:
    if isinstance(value, str):
        return _clip_inline(value)

    if isinstance(value, (int, float, bool)):
        return _js_string(value)

    return ''


def _summarize_record_inline(record: dict, depth: int) -> str:
    if depth > 3:
        return _pluralize(len(record), 'field')

    title = _first_string(
        record, ('title', 'name', 'path', 'file', 'filepath', 'url', 'href', 'link', 'id')
    )
    status = _first_string(record, ('status', 'category', 'type'))
    message = _first_string(record, ('snippet', 'summary', 'description', 'message'))

    if title and status:
        return f'{_clip_inline(title, 110)} ({_clip_inline(status, 54)})'

    if title and message and title != message:
        return f'{_clip_inline(title, 90)} - {_clip_inline(message, 84)}'

    if title:
        return _clip_inline(title, 150)

    pairs: list[str] = []
    for key in _ordered_keys(list(record.keys())):
        if _skip_field(key, record[key]):
            continue
        s = _summarize_scalar(record[key])
        if s:
            pairs.append(f'{_title_case(key)}: {s}')

    pairs = pairs[:2]

    return ' · '.join(pairs) if pairs else _pluralize(len(record), 'field')


def _summarize_list_item(item: Any, depth: int) -> str:
    v = _norm(item)

    if isinstance(v, str):
        return _clip_inline(v)

    if isinstance(v, (int, float, bool)):
        return _js_string(v)

    if v is None:
        return ''

    if isinstance(v, list):
        return _pluralize(len(v), 'item')

    if _is_record(v):
        return _summarize_record_inline(v, depth + 1)

    return _clip_inline(_js_string(v))


def _format_field_value(value: Any, depth: int) -> str:
    v = _norm(value)
    scalar = _summarize_scalar(v)

    if scalar:
        return scalar

    if v is None:
        return ''

    if isinstance(v, list):
        if not v:
            return ''

        scalars = [s for s in (_summarize_scalar(item) for item in v) if s]

        if len(scalars) == len(v) and len(v) <= 4:
            return _clip_inline(', '.join(scalars))

        first = _summarize_list_item(v[0], depth + 1)

        return f'{_pluralize(len(v), "item")} ({first})' if first else _pluralize(len(v), 'item')

    if _is_record(v):
        return _summarize_record_inline(v, depth + 1)

    return _clip_inline(_js_string(v))


def _format_array_summary(value: list, depth: int) -> str:
    if not value:
        return ''

    max_items = 6

    lines = [
        f'- {line}'
        for line in (
            _summarize_list_item(item, depth + 1) for item in value[:max_items]
        )
        if line
    ]

    if not lines:
        return ''

    if len(value) > max_items:
        remaining = len(value) - max_items
        lines.append(f'- … {remaining} more {"item" if remaining == 1 else "items"}')

    return '\n'.join(lines)


def _format_record_summary(record: dict, depth: int) -> str:
    keys = list(record.keys())

    if not keys:
        return ''

    if depth <= 2:
        direct = _first_string(
            record, ('message', 'summary', 'description', 'preview', 'text', 'content')
        )
        meaningful = [
            k for k in keys if not _skip_field(k, record[k]) and not _is_wrapper_key(k)
        ]

        if direct and len(meaningful) <= 1:
            return _clip_block(direct)

    candidates = [k for k in _ordered_keys(keys) if not _skip_field(k, record[k])]
    max_lines = 8
    lines: list[str] = []

    for key in candidates:
        v = _format_field_value(record[key], depth + 1)

        if not v:
            continue

        lines.append(f'- {_title_case(key)}: {v}')

        if len(lines) >= max_lines:
            break

    if not lines:
        return ''

    if len(candidates) > len(lines):
        remaining = len(candidates) - len(lines)
        lines.append(f'- … {remaining} more {"field" if remaining == 1 else "fields"}')

    return '\n'.join(lines)


def _format_summary_value(value: Any, depth: int) -> str:
    if depth > 4:
        return ''

    v = _norm(value)

    if isinstance(v, str):
        return _clip_block(v)

    if isinstance(v, (int, float, bool)):
        return _js_string(v)

    if v is None:
        return ''

    if isinstance(v, list):
        return _format_array_summary(v, depth + 1)

    if _is_record(v):
        return _format_record_summary(v, depth + 1)

    return _clip_inline(_js_string(v))


def _unwrap_payload(value: Any) -> Any:
    cur = _norm(value)

    for _ in range(4):
        if not _is_record(cur):
            return cur

        key = next((k for k in WRAPPER_KEYS if cur.get(k) is not None), None)

        if not key:
            return cur

        cur = _norm(cur[key])

    return cur


def _has_meaningful_error_value(value: Any) -> bool:
    v = _norm(value)

    if v is None:
        return False

    if isinstance(v, str):
        return normalize(v) not in NON_ERROR_TEXT

    if isinstance(v, bool):
        return v

    if isinstance(v, (int, float)):
        return v != 0

    if isinstance(v, list):
        return any(_has_meaningful_error_value(item) for item in v)

    if _is_record(v):
        return len(v) > 0

    return True


def _has_error_signal(record: dict) -> bool:
    status = record.get('status') if isinstance(record.get('status'), str) else ''

    return (
        record.get('success') is False
        or record.get('ok') is False
        or bool(re.match(r'^(error|failed|failure|fatal|exception)$', status.strip(), re.IGNORECASE))
        or any(_has_meaningful_error_value(record.get(k)) for k in ERROR_KEYS)
    )


def _value_error_text(value: Any) -> str:
    v = _norm(value)

    if isinstance(v, str):
        return _clip_block(v, 700, 12) if _has_meaningful_error_value(v) else ''

    if isinstance(v, list):
        joined = '; '.join([t for t in (_value_error_text(i) for i in v) if t][:3])
        return _clip_block(joined, 700, 12)

    if _is_record(v):
        direct = _first_string(v, ERROR_MSG_KEYS)

        if direct:
            return _clip_block(direct, 700, 12)

    return ''


def _find_nested_error(value: Any, depth: int, seen: set, failed: bool = False) -> str:
    if depth > 5:
        return ''

    v = _norm(value)

    # JS: `!v || typeof v !== 'object' || seen.has(v)`
    if _js_falsy(v) or not isinstance(v, (dict, list)) or id(v) in seen:
        return ''

    seen.add(id(v))

    if isinstance(v, list):
        for item in v:
            nested = _find_nested_error(item, depth + 1, seen, failed)
            if nested:
                return nested

        return ''

    record = v

    if record.get('success') is True or record.get('ok') is True:
        return ''

    for key in ERROR_KEYS:
        if not _has_meaningful_error_value(record.get(key)):
            continue

        text = _value_error_text(record.get(key))

        if text:
            return text

    if _has_error_signal(record):
        direct = _first_string(record, ERROR_MSG_KEYS)

        if direct:
            return _clip_block(direct, 700, 12)

    # 返回的数据与 stdout 也可能描述失败，但只在信封已经声明失败时才展开
    failure_declared = failed or _has_error_signal(record)
    nested_keys = (
        [*ERROR_KEYS, *WRAPPER_KEYS, 'details'] if failure_declared else list(ERROR_KEYS)
    )

    for key in nested_keys:
        nested = _find_nested_error(record.get(key), depth + 1, seen, failure_declared)

        if nested:
            return nested

    return ''


def format_tool_result_summary(value: Any) -> str:
    return _format_summary_value(_unwrap_payload(value), 0) or _format_summary_value(value, 0)


def extract_tool_error_message(value: Any) -> str:
    return _find_nested_error(value, 0, set())


# --------------------------------------------------------------------------
# 渲染分类
# --------------------------------------------------------------------------

FILE_EDIT_TOOL_NAMES = frozenset(['edit_file', 'patch', 'write_file'])

CARD_TOOL_NAMES = (
    'clarify',
    'delegate_task',
    'image_generate',
    'manage_catalog',
    'manage_connections',
    'set_theme',
    'setup_beat',
)

# 运行分片器给被判定为卡片的 manage_connections part 用的名字
CONNECTION_CARD_KEY = 'manage_connections:card'

SILENT_TOOL_NAMES = frozenset(['react_to_message', 'todo', 'todo_list'])


def is_file_edit_tool(tool_name: str) -> bool:
    return tool_name in FILE_EDIT_TOOL_NAMES


def is_card_tool_name(tool_name: str) -> bool:
    return tool_name in CARD_TOOL_NAMES


def is_card_tool(tool_name: str) -> bool:
    return is_card_tool_name(tool_name) or tool_name == CONNECTION_CARD_KEY or is_file_edit_tool(tool_name)


def is_silent_tool(tool_name: str) -> bool:
    return tool_name in SILENT_TOOL_NAMES


# --------------------------------------------------------------------------
# ANSI
# --------------------------------------------------------------------------

_ESC = chr(27)
_BEL = chr(7)

_ANSI_CSI_RE = re.compile(_ESC + r'\[[0-?]*[ -/]*[@-~]')
_ANSI_INCOMPLETE_CSI_RE = re.compile(_ESC + r'\[[0-?]*[ -/]*(?=' + _ESC + r'|\n|$)')
_ANSI_OSC_RE = re.compile(_ESC + r'\][\s\S]*?(?:' + _BEL + '|' + _ESC + r'\\)')
_ANSI_STRING_RE = re.compile(_ESC + r'[PX^_][\s\S]*?(?:' + _BEL + '|' + _ESC + r'\\)')
_ANSI_NON_CSI_ESC_SEQ_RE = re.compile(_ESC + r'(?!\[|\]|P|X|\^|_)[ -/]*[0-~]')
_ANSI_STRAY_ESC_RE = re.compile(_ESC + r'(?!\[)[\s\S]?')
_CONTROL_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0D\x0E-\x1A\x1C-\x1F\x7F]')


def strip_ansi(value: str) -> str:
    # 顺序：OSC → 字符串 → 残缺 CSI → CSI → 残缺 CSI → 非 CSI ESC → 游离 ESC → 控制字节
    value = _ANSI_OSC_RE.sub('', value)
    value = _ANSI_STRING_RE.sub('', value)
    value = _ANSI_INCOMPLETE_CSI_RE.sub('', value)
    value = _ANSI_CSI_RE.sub('', value)
    value = _ANSI_INCOMPLETE_CSI_RE.sub('', value)
    value = _ANSI_NON_CSI_ESC_SEQ_RE.sub('', value)
    value = _ANSI_STRAY_ESC_RE.sub('', value)
    return _CONTROL_RE.sub('', value)
