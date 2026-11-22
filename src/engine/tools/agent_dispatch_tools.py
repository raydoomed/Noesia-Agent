"""agent 派发的工具名单。

``tool_call`` 桥接拿不到 agent 上下文：任何由 agent loop 自己派发的工具一旦落进
defer 名单，模型就只能走桥接，回调是 None，工具等于消失。这里把「哪些工具是
agent 派发的」收成一处查询，名单直接取自执行器表，两边不会再漂移。
"""

from __future__ import annotations


def agent_dispatched_tool_names() -> frozenset[str]:
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS

    return frozenset(INLINE_TOOL_EXECUTORS)


__all__ = ["agent_dispatched_tool_names"]
