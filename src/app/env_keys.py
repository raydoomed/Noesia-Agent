"""所有厂商凭证的唯一入口：一张表 + 一套读写。

**`.env` 里只放 Key**，配置（用哪个引擎/哪家厂商）在各自的 json 里。

- 谁用哪个变量名：只有 `PROVIDERS` 这一张表（第四列 `group` 标明它属于哪一摊：
  `speech`=语音引擎，`avatar`=语音互动的数字人厂商）；
- 读、写 `.env` 也只有这里一套：`read()` / `write()`，只改这一行，不额外留备份文件。

加一家厂商 = 表里加一行；别处不许再写死变量名、也不许留"没配就当成某家"的默认值。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# (id, 界面上显示的名字, .env 里的变量名, 归属)
PROVIDERS: tuple[tuple[str, str, str, str], ...] = (
    ("elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY", "speech"),
    ("doubao", "豆包", "DOUBAO_API_KEY", "speech"),
    ("vidu", "Vidu（组件版数字人）", "VIDU_TOKEN", "avatar"),
)


def providers(group: str) -> list[tuple[str, str, str]]:
    """某一摊要填 Key 的厂商：`[(id, 名字, 变量名)]`，顺序就是界面上的顺序。"""
    return [row[:3] for row in PROVIDERS if row[3] == group]


def env_key(provider: str) -> str:
    for pid, _label, key, _group in PROVIDERS:
        if pid == provider:
            return key
    raise ValueError(f"这个厂商没有凭证，或者名字不认识：{provider}")


def env_file() -> Path:
    """唯一放 Key 的文件（`NOESIA_HOME` 可以把它挪到别处）。"""
    home = Path(os.environ.get("NOESIA_HOME") or (REPO_ROOT / "data" / "NoesiaHome"))
    return home / ".env"


def read(key: str) -> str:
    """按变量名读 Key：先看 `.env`，再看进程环境；都没有返回空串。"""
    env = env_file()
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            entry = line.strip()
            if entry.startswith("export "):
                entry = entry[len("export "):]
            name, sep, value = entry.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip('"').strip("'")
    return os.environ.get(key, "").strip()


def write(key: str, value: str) -> None:
    """只改 `.env` 里这一个变量那一行，别的行原样留着。"""
    # 不再生成 `.env.bak`：留着它等于在家目录里多一份明文凭证，用户还得自己收拾。

    value = value.strip()
    env = env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    lines = env.read_text(encoding="utf-8", errors="replace").splitlines() if env.is_file() else []
    kept = []
    for line in lines:
        entry = line.strip()
        probe = entry[len("export "):] if entry.startswith("export ") else entry
        name, sep, _old = probe.partition("=")
        if sep and name.strip() == key:
            continue
        kept.append(line)
    kept.append(f"{key}={value}")
    # 临时文件 + replace：直接覆盖时中途失败会留下一个空的 `.env`，
    # 所有厂商的 Key 一起没，用户还看不出是哪一步坏的。
    body = "\n".join(kept) + "\n"
    tmp = env.with_name(f".{env.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, env)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.environ[key] = value  # 本进程立刻生效，不用等重启


def read_provider(provider: str) -> str:
    return read(env_key(provider))


def write_provider(provider: str, value: str) -> None:
    write(env_key(provider), value)
