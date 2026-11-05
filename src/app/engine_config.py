"""维护引擎家目录里那份**可直接改、直接生效**的 `config.yaml`。

规则：文件里始终是引擎的全部配置键（全量默认值打底），用户改过的值一律保留；
引擎升级后新增的键自动补进来，而"用户从没动过、还是老默认值"的键会跟着新默认值走。
判断"动过没动过"靠一份快照（`<home>/data/runtime/config-defaults.json`，内部状态，用户不用管）。

这样三个目标同时成立：
1. 打开文件就能看到有哪些参数、默认是多少（不是模板，是生效的那份）；
2. 手改的项不会被启动冲掉；
3. 引擎升级换了默认值，没被改过的项会自动跟上。

引擎目录、家目录都还没准备好时不该拦着开窗：调用方接住异常即可。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 「读不出来就别写」这一段保护是靠调用方的 `except (OSError, yaml.YAMLError)` 兜住的。
# 中文 Windows 上 config.yaml 很常被记事本/旧编辑器存成 GBK 或带 BOM 的 UTF-8，硬解 UTF-8
# 抛的是 `UnicodeDecodeError`——它哪一个都接不住，于是保护分支不执行，坏文件被默认值覆盖。
_BAD_TEXT = "这份文件不是能识别的文本（既不是 UTF-8 也不是 GBK）：另存为 UTF-8 后重开设置"


def _read_text(path: Path) -> str:
    """按 UTF-8（含 BOM）读；不成再退一步 GB18030（中文 Windows 的 ANSI）。

    两种编码都不成，就当成「读不出来」抛 `OSError`：既有的保护分支会照常执行
    （不写盘、保留原文件），用户看到的也是一句能懂的中文，而不是 `0xd6 in position 2`。
    """
    data = path.read_bytes()  # 读不出来（文件锁、权限）照样抛 OSError，交给调用方
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise OSError(f"{path}：{_BAD_TEXT}")


def _atomic_write(path: Path, text: str) -> None:
    """先写同目录的临时文件，再 `os.replace`：写一半断电/磁盘满，原文件还是完整的。

    直接 `write_text` 覆盖最坏：截成 0 字节的 config.yaml 下次启动解析失败，而解析失败按设计
    「不写盘」，于是这份配置永远停在坏的状态，模型、审批档、记忆上限全部读不出来。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_HEADER = (
    "# 引擎配置：全部键与当前值，直接改，重开应用生效（删掉一行 = 回默认）。\n"
    "# 键上方的说明译自引擎源码注释；引擎新增的键会自动补进来，你改过的值不会被覆盖。\n"
)

_WIDTH = 100

# 旧版本生成的只读参考文件（已被本文件取代），启动时顺手清掉，别在目录里留两份
_LEGACY_REFERENCE = "config.default.yaml"
_LEGACY_MARK = "# 引擎配置全量默认值"
_SNAPSHOT_REL = ("data", "runtime", "config-defaults.json")


def _is_ours(path: Path) -> bool:
    try:
        return _read_text(path).startswith(_LEGACY_MARK)
    except OSError:
        return False


def _merge_leaf(new_default: Any, disk: Any, was_default: Any, override: Any, missing: object) -> Any:
    """单个键怎么取值：用户改过就用磁盘的，没改过就跟 engine.config，再不然跟新默认。"""
    if disk is not missing and isinstance(disk, dict) and not isinstance(new_default, dict):
        return disk  # 磁盘上是段、默认表里是标量：以磁盘的结构为准（引擎归一整段写进来的）
    if disk is not missing and not (was_default is not missing and disk == was_default):
        return disk  # 用户改过（或快照丢了，宁可保留用户的）
    return new_default if override is missing else override


def _merge(defaults: Any, disk: Any, snapshot: Any, overrides: Any, missing: object) -> Any:
    if not isinstance(defaults, dict):
        return _merge_leaf(defaults, disk, snapshot, overrides, missing)
    disk = disk if isinstance(disk, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    overrides = overrides if isinstance(overrides, dict) else {}
    merged: dict[str, Any] = {}
    for key, value in defaults.items():
        merged[key] = _merge(
            value,
            disk.get(key, missing),
            snapshot.get(key, missing),
            overrides.get(key, missing),
            missing,
        )
    # 磁盘上有、引擎默认表里没有的键也要留住：引擎自己会读它们（典型是 CLI 写的
    # `agent.reasoning_effort`——思考档位就存在这儿），删掉等于替用户改配置。
    for key, value in disk.items():
        if key in defaults:
            continue
        if key in overrides:
            merged[key] = overrides[key]
        elif isinstance(value, dict):
            merged[key] = _merge(
                value,
                value,
                snapshot.get(key, missing),
                overrides.get(key, missing),
                missing,
            )
        else:
            merged[key] = value
    return merged


# 「默认值本身就是开关」的键：引擎是看 raw config 里有没有这一段来决定要不要提供某个功能的，
# 写出来就等于替用户按了开关。最典型的是 `moa.presets`：引擎对所有人都会合成一份 `default`
# preset，可只要它出现在 config.yaml 里，模型清单就会多出一家 `moa`，选中还会切换失败。
# 所以这些键在值等于引擎默认时就别写进文件（用户自己配了别的值才写）。
_OPTIN_KEYS: tuple[tuple[str, ...], ...] = (("moa", "presets"),)


def _prune_optin(merged: dict[str, Any], defaults: dict[str, Any]) -> None:
    """把「值还是默认」的开关型键从结果里摘掉，别让默认值变成用户的决定。"""
    for path in _OPTIN_KEYS:
        node: Any = merged
        base: Any = defaults
        for key in path[:-1]:
            if not isinstance(node, dict) or not isinstance(base, dict):
                node = None
                break
            node, base = node.get(key), base.get(key)
        if isinstance(node, dict) and isinstance(base, dict):
            leaf = path[-1]
            if leaf in node and node.get(leaf) == base.get(leaf):
                del node[leaf]


# 引擎自己归一化、外壳不代写的键。`model` 在 DEFAULT_CONFIG 里是个空串（旧结构），
# 但引擎真正落盘的是 `{default, provider, base_url, api_mode}` 这样一段映射。照默认表
# 写个空串进去，引擎启动就判定「还没连任何供应商」——每一次启动都报
# `No provider is set up for the first chat`。值等于默认（空）时干脆不写，让引擎自己管。
_DROP_WHEN_DEFAULT: tuple[tuple[str, ...], ...] = (("model",),)


def _prune_legacy(merged: dict[str, Any], defaults: dict[str, Any]) -> None:
    """把「照默认写出来反而有害」的键摘掉（见 `_DROP_WHEN_DEFAULT`）。"""
    for path in _DROP_WHEN_DEFAULT:
        key = path[0]
        if key in merged and merged.get(key) == defaults.get(key):
            del merged[key]


def _scalar(value: Any) -> str:
    import yaml

    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, sort_keys=False).strip()
    if text.endswith("..."):
        text = text[: -len("...")].strip()
    return text or "''"


def _render_section(
    node: dict[str, Any],
    comments: dict[tuple[str, ...], str],
    path: tuple[str, ...],
    indent: int,
    out: list[str],
) -> None:
    """按注释逐键排版：说明写在该键上方，缩进跟着层级走。"""
    import textwrap

    pad = "  " * indent
    for key, value in node.items():
        child = (*path, key)
        note = comments.get(child)
        if note:
            for line in textwrap.wrap(note, width=max(40, _WIDTH - len(pad))):
                out.append(f"{pad}# {line}")
        if isinstance(value, dict) and value:
            out.append(f"{pad}{key}:")
            _render_section(value, comments, child, indent + 1, out)
        elif isinstance(value, dict):
            # 空段写成 `{}`：只写 `key:` 会被引擎当成 null 并警告
            # （config.yaml has empty section(s) ... Remove the line(s) or set them to ``{}``）
            out.append(f"{pad}{key}: {{}}")
        else:
            out.append(f"{pad}{key}: {_scalar(value)}")


def _apply_overrides(merged: dict, overrides: dict) -> None:
    """外壳决定的键每次启动按当前值写，不跟磁盘协商。

    目前只有 `terminal.cwd`。它以前跟别的键走同一套「磁盘上跟快照不同就当用户改过」的规则，
    结果第一次启动写下的那个绝对路径被当成了用户的选择：之后搬了安装目录、或者改了
    app.json 里的 engine.workdir，config.yaml 里还是老路径，工具继续在旧目录里干活。
    """
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            _apply_overrides(merged[key], value)
        else:
            merged[key] = value


def sync_engine_config(
    core_path: Path,
    home: Path,
    overrides: dict[str, Any] | None = None,
) -> bool:
    """把 `<home>/config.yaml` 写成"全量默认 + 用户改动"；返回是否真的改动了文件。"""
    from engine_config_view import comment_texts, engine_defaults  # 引擎的 sys.path 只在 src/ 那一层动

    import yaml

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    snapshot_path = home.joinpath(*_SNAPSHOT_REL)

    disk: Any = None
    if config_path.exists():
        try:
            disk = yaml.safe_load(_read_text(config_path)) or {}
        except (OSError, yaml.YAMLError) as exc:
            # 读不出来就**不写**：以前这里会当成「磁盘上没有这个键」，于是拿引擎默认值
            # 把整个文件覆盖掉——用户改过的每一条设置连同注释一起没了。
            logger.error("[配置] %s 解析失败，这次不写盘：%s", config_path, exc)
            raise ValueError(f"{config_path} 读不出来，暂停写入以免覆盖你的配置") from exc
    snapshot: Any = None
    if snapshot_path.exists():
        try:
            snapshot = json.loads(_read_text(snapshot_path))
        except (OSError, ValueError):
            snapshot = None

    missing = object()
    defaults = engine_defaults(core_path)
    merged = _merge(defaults, disk, snapshot, overrides, missing)
    _apply_overrides(merged, overrides if isinstance(overrides, dict) else {})
    if isinstance(merged, dict) and isinstance(defaults, dict):
        _prune_optin(merged, defaults)
        _prune_legacy(merged, defaults)

    try:
        comments = comment_texts(core_path)
    except Exception:  # noqa: BLE001 - 拿不到注释就退化成纯键值，不能因此写不出配置
        logger.exception("读取引擎配置注释失败，生成的文件将不含说明")
        comments = {}

    lines: list[str] = []
    _render_section(merged, comments, (), 0, lines)
    body = _HEADER + "\n".join(lines) + "\n"
    changed = not config_path.exists() or _read_text(config_path) != body
    if changed:
        _atomic_write(config_path, body)
        logger.info("引擎配置已同步：%s（%d 个顶层段）", config_path, len(merged))

    # 快照记「引擎当前的默认值」——下次启动时某键与快照相同 = 用户没动过，可以跟随新默认；
    # 不同 = 用户改过，必须保留。这里绝不能记 merged：把用户的值记成基线，下一轮就会被当成
    # 「没动过」而换成默认值（踩过：`model:` 的 {default, provider, base_url} 被换成空串）。
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        snapshot_path,
        json.dumps(defaults, ensure_ascii=False, sort_keys=True),
    )

    legacy = home / _LEGACY_REFERENCE
    if legacy.exists() and _is_ours(legacy):
        legacy.unlink()
        logger.info("已清理旧版只读参考文件：%s", legacy)

    return changed


def workspace_from_config(home: Path, fallback: Path) -> Path:
    """启动时的实际工作区：配置里写过 `terminal.cwd` 就用它，没写过才用外壳给的默认值。

    界面上的「工作目录」写的就是 `terminal.cwd`；启动时若只认 `app.json` 的 `engine.workdir`，
    用户改过的目录会在重启后被自己的默认值盖住——两份值打架，看起来像"设了没用"。
    """
    import yaml

    raw = ""
    path = Path(home) / "config.yaml"
    if path.is_file():
        try:
            data = yaml.safe_load(_read_text(path)) or {}
            raw = str(((data.get("terminal") or {}).get("cwd") or "")).strip()
        except (OSError, yaml.YAMLError):
            raw = ""
    fallback = Path(fallback)
    if not raw or raw == ".":
        return fallback
    chosen = Path(raw).expanduser()
    return chosen if chosen.is_absolute() else (fallback.parent / chosen)


def read_engine_config(home: Path) -> dict[str, Any]:
    """盘上那份 `config.yaml`（读不出来就当空表，调用方自己决定要不要报）。"""
    import yaml

    path = Path(home) / "config.yaml"
    try:
        data = yaml.safe_load(_read_text(path)) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def memory_limits(core_path: Path, home: Path) -> tuple[int, int]:
    """(记忆上限, 画像上限)：盘上的值优先，盘上没有就用引擎自己的默认表。

    只认这两处来源。界面自己再写一份默认值，就会出现「界面显示 2200、引擎按别的数算」
    这种谁都看不出来的偏差——用户改完也看不出区别。
    """
    from engine_config_view import engine_defaults

    disk = read_engine_config(home)
    memory = disk.get("memory") if isinstance(disk.get("memory"), dict) else {}
    try:
        defaults = (engine_defaults(core_path) or {}).get("memory") or {}
    except Exception:  # noqa: BLE001 - 引擎目录不在：这一页只读不写，别让整个设置窗打不开
        logger.exception("读不到引擎默认配置")
        defaults = {}

    def pick(key: str) -> int:
        for source in (memory, defaults):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return 0

    return pick("memory_char_limit"), pick("user_char_limit")


def set_engine_config(core_path: Path, home: Path, values: dict[str, Any]) -> bool:
    """用户明确改过的键（如工作目录）：写进 `config.yaml`，再照旧重排成带注释的全量文件。

    `values` 用带点的路径，例如 `{"terminal.cwd": "D:/工作区"}`。
    为什么不用 `sync_engine_config(overrides=...)`：那里的 overrides 只在「用户没改过」时生效，
    而这里要的正是「用户改过」——直接把值落到磁盘，快照仍是引擎默认值，两者不等 → 下次启动也保住。
    """
    import yaml

    home = Path(home)
    config_path = home / "config.yaml"
    disk: Any = {}
    if config_path.is_file():
        try:
            disk = yaml.safe_load(_read_text(config_path)) or {}
        except (OSError, yaml.YAMLError) as exc:
            # 跟 sync_engine_config 一个道理：读不出来就不能写，否则一个键的修改会把
            # 整个文件换成只剩这一个键的新文件，其余全丢。
            logger.error("[配置] %s 解析失败，这次不写盘：%s", config_path, exc)
            raise ValueError(f"{config_path} 读不出来，暂停写入以免覆盖你的配置") from exc
    if not isinstance(disk, dict):
        raise ValueError(f"{config_path} 顶层不是键值表，暂停写入以免覆盖你的配置")

    for dotted, value in values.items():
        node: dict[str, Any] = disk
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    home.mkdir(parents=True, exist_ok=True)
    _atomic_write(config_path, yaml.safe_dump(disk, allow_unicode=True, sort_keys=False))
    changed = sync_engine_config(core_path, home)
    logger.info("已写入引擎配置：%s", ", ".join(values))
    return changed
