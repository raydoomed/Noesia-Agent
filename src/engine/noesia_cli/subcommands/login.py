"""``noesia login`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_login_parser(subparsers, *, cmd_login: Callable) -> None:
    """挂载已弃用的 ``login`` 子命令（处理器只打印弃用提示）。

    保留注册是为了让老脚本看到可执行的提示，而不是 argparse 的 ``invalid choice``；不加 ``help=``
    是为了让它不出现在 ``noesia --help`` 里。
    """
    login_parser = subparsers.add_parser(
        "login",
        description="Deprecated. Use `noesia auth` to manage credentials, "
            "`noesia model` to select a provider, or `noesia setup` for full setup.")
    login_parser.add_argument(
        "--provider", default=None, help="(deprecated) Provider name; ignored — see `noesia model`")
    login_parser.set_defaults(func=cmd_login)
