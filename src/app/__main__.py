"""`python -m app` 的入口。

配置坏掉、缺依赖这类错误发生在**导入期**（`settings` 在 import 的时候就把配置解析完）。
`Noesia.vbs` 用 `pythonw -E -m app` 启动，没有控制台：异常从 `main()` 外面抛出去，双击就是
「什么都不发生」——没有窗口、没有提示、连 `data/runtime/gui.log` 都没建（日志是在
`main()` 里才装的）。所以导入和启动都包在这一层，失败时落一份找得到的日志，再弹一个
系统对话框，直接告诉用户是哪份文件坏了。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _log_startup_error(detail: str) -> Path | None:
    """把启动失败的完整栈写到用户找得到的地方（`%LOCALAPPDATA%\\Noesia\\startup-error.log`）。

    刻意不写进 `data/runtime/`：配置坏掉时那个目录本身就可能不可写（装在只读盘、
    `data` 这个位置是个文件），而那正是要报的错之一。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ""
    path = Path(base or Path.home()) / "Noesia" / "startup-error.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(detail, encoding="utf-8")
    except OSError:
        return None
    return path


def _tell_user(message: str, log: Path | None) -> None:
    """把错误摆到用户面前：有桌面就弹窗，没有就只留 stderr（终端里跑的时候能看见）。"""
    text = message if log is None else f"{message}\n\n详细记录：{log}"
    print(text, file=sys.stderr)
    if os.environ.get("NOESIA_NO_DIALOG"):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Noesia 起不来", 0x10)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    try:
        from .main import main as run
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        log = _log_startup_error(detail)
        last = detail.strip().splitlines()[-1] if detail.strip() else "未知错误"
        _tell_user(f"Noesia 没能启动：{last}", log)
        return 1
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
