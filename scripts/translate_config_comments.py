"""把引擎配置键的注释译成中文，维护 `src/config_comments_zh.json`。

为什么需要：`<home>/config.yaml` 里的说明要给人看，引擎源码里是英文。译文是发布时一次性
翻好的静态表；引擎升级新增的键没有译文时自动回退英文原话（见
`engine_config_view.comment_texts`）。引擎改了注释就重跑本脚本，只翻缺的条目。

用法：`.venv/Scripts/python.exe scripts/translate_config_comments.py`
需要环境变量 `DEEPSEEK_API_KEY`（也会读项目根或 data/NoesiaHome 下的 .env）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "config_comments_zh.json"
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
BATCH = 25

sys.path.insert(0, str(ROOT / "src"))
from engine_config_view import engine_comments  # noqa: E402

SYSTEM = (
    "你是资深技术文档译者。把英文的软件配置项说明翻成简体中文。"
    "要求：术语保持英文（token、SQLite、WAL、OAuth、TLS 等）；"
    "命令、键名、路径、数字、占位符（如 <id>、{x}）原样保留；"
    "不添加原文没有的信息，不省略；单句陈述，不用“您”。"
    "只输出 JSON 数组，元素是译文，顺序与输入一致，不要任何解释。"
)


def api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for candidate in (ROOT / ".env", ROOT / "data" / "NoesiaHome" / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("没有找到 DEEPSEEK_API_KEY")


def translate(batch: list[str], key: str) -> tuple[list[str], dict]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    request = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    translated = json.loads(content)
    if not isinstance(translated, list) or len(translated) != len(batch):
        raise ValueError(f"条数对不上：{len(batch)} → {len(translated)}")
    return translated, body.get("usage", {})


def main() -> None:
    key = api_key()
    items = sorted(engine_comments(ROOT / "src" / "engine").items())
    translated: dict[str, str] = {}
    if OUT.exists():
        translated = json.loads(OUT.read_text(encoding="utf-8"))

    pending = [(path, note) for path, note in items if ".".join(path) not in translated]
    print(f"注释 {len(items)} 条，已有译文 {len(translated)}，待翻 {len(pending)}")

    tokens_in = tokens_out = 0
    dropped: list[int] = []
    for start in range(0, len(pending), BATCH):
        chunk = pending[start : start + BATCH]
        for attempt in range(3):
            try:
                result, usage = translate([note for _, note in chunk], key)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  批次 {start} 第 {attempt + 1} 次失败：{exc}")
                time.sleep(2 * (attempt + 1))
        else:
            print(f"  批次 {start} 放弃（{len(chunk)} 条）")
            dropped.append(start)
            continue
        tokens_in += usage.get("prompt_tokens", 0)
        tokens_out += usage.get("completion_tokens", 0)
        for (path, _), text in zip(chunk, result):
            translated[".".join(path)] = text
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(translated, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print(f"  {min(start + BATCH, len(pending))}/{len(pending)}")

    print(f"译文 {len(translated)} 条；tokens in={tokens_in} out={tokens_out}")
    if dropped:
        print(f"有 {len(dropped)} 个批次失败，{OUT.name} 只写进去了成功的部分——重跑一次补齐")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
