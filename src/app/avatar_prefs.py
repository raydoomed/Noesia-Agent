"""语音互动（数字人）：用哪家厂商（`avatar.json`）+ 凭证怎么取（`env_keys`）。

两件事分开存，各在各家：

- **配置**（用哪家、用哪张形象图）→ `data/settings/avatar.json`：`provider` / `model` / `image`
  三个键，模板显式落地；`image_asset` 是程序自己写的云端形象编号，可选键（删掉只是重传一次）；
- **Key** → `data/NoesiaHome/.env`，由 `env_keys.PROVIDERS` 里属于 `avatar` 的那几行决定
  （Vidu 是 `VIDU_TOKEN`）。读写只有 `env_keys.read/write` 一套，这里不再自己实现。

加一家厂商 = `env_keys.PROVIDERS` 加一行（第四列 `avatar`）+ 在
`model_dialog._TOKEN_VERIFIERS` 挂它自己的只读校验函数（没有就不挂）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from app import env_keys
from settings import ConfigError, RESOLVER

PREFS_PATH = Path("data/settings/avatar.json")

PROVIDERS = env_keys.providers("avatar")

MODELS = (
    ("vidu-s2", "vidu-s2（清晰，888×1184）"),
    ("vidu-s1", "vidu-s1（稳定，624×832）"),
)

_TEMPLATE = {"provider": "vidu", "model": "vidu-s2", "image": ""}
_REQUIRED = ("provider", "model")

IMAGE_DIR = Path("data/assets")

DEFAULT_IMAGE = RESOLVER.resolve(Path("assets/greenscreen/noesia_greenscreen.png"))


def _write(data: dict) -> None:
    """整份写回：`load()` 已经带上了不认识的键（比如 `image_asset`），这里不许筛。"""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def path() -> Path:
    return RESOLVER.resolve(PREFS_PATH)


def load() -> dict:
    """读 avatar.json：没有就按模板写一份；有但缺键就报错，不拿代码默认值兜着。"""
    target = path()
    if not target.is_file():
        _write(dict(_TEMPLATE))
        return dict(_TEMPLATE)
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读不懂 {target}：{exc}") from None
    if not isinstance(stored, dict):
        raise ConfigError(f"{target} 的最外层要是一个对象")
    missing = [key for key in _REQUIRED if key not in stored]
    if missing:
        raise ConfigError(f"{target} 缺 {', '.join(missing)}（语音互动的配置只有这一份，缺键不猜）")
    data = dict(_TEMPLATE)
    data.update(stored)
    return data


def save(provider: str | None = None, model: str | None = None) -> dict:
    """存"用哪家、用哪个模型"；Key 走 `write_token()`，一律不落 json。"""
    data = load()
    if provider:
        if provider not in {row[0] for row in PROVIDERS}:
            raise ValueError(f"不认识的语音互动厂商：{provider}")
        data["provider"] = provider
    if model:
        if model not in {row[0] for row in MODELS}:
            raise ValueError(f"不认识的数字人模型：{model}")
        data["model"] = model
    _write(data)
    return data




def image_setting() -> str:
    """`avatar.json` 里的 `image`；空串 = 用内置那张。"""
    return str(load().get("image") or "").strip()


def image_path() -> Path | None:
    """生效的形象图；返回 None 表示用内置那张。

    填了路径就必须能找到——静默退回内置的话，用户只会看到「换图没生效」。
    """
    raw = image_setting()
    if not raw:
        return None
    target = RESOLVER.resolve(raw)
    if not target.is_file():
        raise ConfigError(f"avatar.json 里的 image 指的图片找不到：{target}")
    return target


def save_image(value: str) -> dict:
    """改 `image`（空串 = 还原默认形象）；顺带把云端编号废掉，下次按新图重传。"""
    data = load()
    data["image"] = value
    data.pop("image_asset", None)
    _write(data)
    return data



def clear_image() -> dict:
    """还原默认形象：清 `image`、作废云端编号、删掉复制过来的那份图。"""
    folder = RESOLVER.resolve(IMAGE_DIR)
    if folder.is_dir():
        for old in folder.glob("avatar.*"):
            old.unlink(missing_ok=True)
    return save_image("")


_STAGING = ".pending"

_ALLOWED = (".png", ".jpg", ".jpeg", ".webp")


def stage_image(source: str | Path) -> Path:
    """把选中的图先放成「待提交」文件：不动 `image`、不动现在生效的那张。

    云端确认能用了才 `commit_staged()` 扶正——这样缩略图和实际生效的永远是同一张。
    """
    src = Path(source)
    suffix = src.suffix.lower()
    if suffix not in _ALLOWED:
        raise ValueError(f"形象图只认 png / jpg / jpeg / webp，这个是 {suffix or '没扩展名'}")
    folder = RESOLVER.resolve(IMAGE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"avatar{_STAGING}*"):
        old.unlink(missing_ok=True)
    target = folder / f"avatar{_STAGING}{suffix}"
    shutil.copyfile(src, target)
    return target


def commit_staged() -> str:
    """把待提交的那张扶正：替换 `avatar.*`、写 `image`、作废旧编号。"""
    folder = RESOLVER.resolve(IMAGE_DIR)
    staged = next(iter(folder.glob(f"avatar{_STAGING}*")), None)
    if staged is None:
        raise ValueError("没有待提交的形象图")
    for old in folder.glob("avatar.*"):
        if old.name.startswith(f"avatar{_STAGING}"):
            continue
        old.unlink(missing_ok=True)
    target = folder / f"avatar{staged.suffix}"
    staged.rename(target)
    relative = str(target.relative_to(RESOLVER.root)).replace("\\", "/")
    save_image(relative)
    return relative


def drop_staged() -> None:
    """上传没成：把待提交的那张删掉，正在用的那张一个字都没动。"""
    folder = RESOLVER.resolve(IMAGE_DIR)
    for old in folder.glob(f"avatar{_STAGING}*"):
        old.unlink(missing_ok=True)


def image_asset() -> dict:
    """云端形象编号：`{"hash", "id", "uploaded_at"}`；没有就返回空 dict。"""
    asset = load().get("image_asset")
    return dict(asset) if isinstance(asset, dict) else {}


def save_image_asset(asset: dict | None) -> dict:
    """存/清云端形象编号。传 None 表示作废（到期、换图、还原）。"""
    data = load()
    if asset:
        data["image_asset"] = dict(asset)
    else:
        data.pop("image_asset", None)
    _write(data)
    return data


def env_key(provider: str | None = None) -> str:
    return env_keys.env_key(provider or current()["provider"])


def current() -> dict:
    """生效值：`{"provider", "label", "env_key", "model"}`，界面和引擎都按这个取。"""
    stored = load()
    chosen = stored["provider"]
    for pid, label, key in PROVIDERS:
        if pid == chosen:
            return {
                "provider": pid,
                "label": label,
                "env_key": key,
                "model": stored["model"],
            }
    raise ConfigError(f"avatar.json 里的 provider 不认识：{chosen}")


def read_token(provider: str | None = None) -> str:
    """读凭证；没填返回空串（界面判断"填没填过"用它，不抛异常）。"""
    return env_keys.read(env_key(provider))


def write_token(value: str, provider: str | None = None) -> None:
    env_keys.write(env_key(provider), value)


def token_issue(value: str) -> str:
    """填之前先看格式：返回空串表示没问题，否则返回给人看的原因。"""
    if not value:
        return "还没填凭证"
    if not value.isascii() or any(ch.isspace() for ch in value):
        return "这个 Token 里混了中文或空格，重新粘贴一次（只要那一整串）"
    if len(value) < 20:
        return f"这个 Token 只有 {len(value)} 位，肯定不完整：到控制台复制整串再粘一次"
    return ""
