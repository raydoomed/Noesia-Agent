"""外壳的运行参数：**全部来自 `data/settings/app.json`**。

这台机器上外壳只有这一份配置：界面用哪些资源、引擎代码在哪、家目录和干活目录在哪、
语音念不念、说话画面用哪个 Python 和哪个模型仓库、碎片往哪落——都在那份 JSON 里。

代码里**不留默认值**：配置写什么就用什么。文件不在，第一次运行从仓库里的
`app.example.json` 复制一份过来（之后以 `data/settings/app.json` 为准）；
缺键、类型不对、JSON 读不懂，都当场抛 `ConfigError` 并指出是哪一处，
不猜、不悄悄退回某个内置值——那样只会让人以为改配置生效了，其实没有。

要挪引擎家目录也可以设 `NOESIA_HOME`（引擎自己认这个环境变量）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "NOESIA_HOME"

# 代码在哪。不受 NOESIA_HOME 影响：代码和资源跟着仓库走。
ROOT = Path(__file__).resolve().parents[1]
# 外壳的唯一配置，和它的模板（模板跟着仓库走；改名或挪位置要两处一起改）
APP_CONFIG = Path("data") / "settings" / "app.json"
APP_TEMPLATE = Path("app.example.json")
# 窗口大小/位置（她 + 聊天窗）落这里：项目 data/ 下的 INI。
# 不用 QSettings 的默认写法——Windows 上那会写进 HKCU\Software 注册表。
# 这里**刻意**手写 `ROOT / WINDOW_STATE`、不走引擎家目录那套解析：窗口档跟着仓库走，
# 换 `NOESIA_HOME` 也不该把「窗放在屏幕哪儿」挪到别的地方。
WINDOW_STATE = Path("data") / "settings" / "window.ini"


class ConfigError(Exception):
    """配置文件不在、读不懂、少了键或者类型不对。"""


def window_state_path(root: Path | None = None) -> Path:
    """窗口大小/位置的存档：`<项目根>/data/settings/window.ini`。"""
    return (Path(root) if root else ROOT) / WINDOW_STATE


@dataclass(frozen=True)
class _Reference:
    front: str


@dataclass(frozen=True)
class _Avatar:
    id: str
    dir: str
    idle_video: str
    reference: _Reference


@dataclass(frozen=True)
class _Engine:
    core_path: str
    home: str
    workdir: str


@dataclass(frozen=True)
class _Floating:
    always_on_top: bool
    transparent: bool



@dataclass(frozen=True)
class _Ui:
    floating: _Floating


@dataclass(frozen=True)
class Settings:
    avatar: _Avatar
    engine: _Engine
    ui: _Ui


class Resolver:
    """把「相对项目根的路径」变成绝对路径。"""

    def __init__(self, root: Path | str = ROOT) -> None:
        self.root = Path(root).resolve()

    def resolve(self, relative: str | Path) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.root / path


def app_config_path(root: Path | None = None) -> Path:
    """外壳自己的配置：`<项目根>/data/settings/app.json`。"""
    return (Path(root) if root else ROOT) / APP_CONFIG


# ---- 读配置：缺什么说什么，不填默认值 ----

def _at(data: dict, dotted: str, path: Path):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"配置缺 {dotted}（{path}）")
        node = node[part]
    return node


def _str(data: dict, dotted: str, path: Path) -> str:
    value = _at(data, dotted, path)
    if not isinstance(value, str):
        raise ConfigError(f"配置 {dotted} 要字符串，现在是 {type(value).__name__}（{path}）")
    return value




def _non_empty_str(data: dict, dotted: str, path: Path) -> str:
    """必须是非空字符串。

    空串不是「没填」那么轻：`Path("")` 就是 `.`，引擎家目录会退化成项目根，
    `cache/scratch`、会话、配置全长在项目根目录里（真发生过）。
    """
    value = _str(data, dotted, path)
    if not value.strip():
        raise ConfigError(f"配置 {dotted} 不能为空（{path}）")
    return value


def _bool(data: dict, dotted: str, path: Path) -> bool:
    value = _at(data, dotted, path)
    if not isinstance(value, bool):
        raise ConfigError(f"配置 {dotted} 要 true/false，现在是 {type(value).__name__}（{path}）")
    return value


def _read(path: Path, root: Path) -> dict:
    if not path.is_file():
        template = root / APP_TEMPLATE
        if not template.is_file():
            raise ConfigError(f"配置不在（{path}），模板也不在（{template}）：没有可用的参数")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 的最外层要是一个对象")
    return data


def _build(data: dict, path: Path) -> Settings:
    return Settings(
        avatar=_Avatar(
            id=_str(data, "avatar.id", path),
            dir=_non_empty_str(data, "avatar.dir", path),
            idle_video=_str(data, "avatar.idle_video", path),
            reference=_Reference(
                front=_str(data, "avatar.reference.front", path),
            ),
        ),

        engine=_Engine(
            core_path=_str(data, "engine.core_path", path),
            home=_non_empty_str(data, "engine.home", path),
            workdir=_str(data, "engine.workdir", path),
        ),
        ui=_Ui(
            floating=_Floating(
                always_on_top=_bool(data, "ui.floating.always_on_top", path),
                transparent=_bool(data, "ui.floating.transparent", path),
            ),
        ),
    )


def load_settings(path: str | Path | None = None, root: Path | None = None):
    """读配置，返回 (设置, 路径解析器)。读不到就抛 ConfigError，不给半套参数。"""
    base = Path(root).resolve() if root else ROOT
    config_path = Path(path) if path else app_config_path(base)
    return _build(_read(config_path, base), config_path), Resolver(base)


def ensure_runtime(*, root: Path | str, home: Path | str, workdir: Path | str) -> list[Path]:
    """建出运行时要用的目录（幂等），返回这次真正新建的路径。

    这里不写任何配置：`<home>/config.yaml` 是引擎自己的，引擎创建、引擎读写。
    """
    root = Path(root).resolve()
    home = Path(home).resolve()
    workdir = Path(workdir).resolve()
    created: list[Path] = []

    for directory in (root / "data", home, workdir, root / "data" / "runtime"):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)
    return created


# 导入即读配置：读不到就当场报错，别让界面带着半套参数跑起来。
SETTINGS, RESOLVER = load_settings()

# 兼容旧调用点的名字：类型别名和入口名保持可用。
PathResolver = Resolver
AvatarSection = _Avatar
ReferenceSection = _Reference
load_config = load_settings
