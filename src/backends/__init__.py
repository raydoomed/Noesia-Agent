"""模型清单的薄适配：真正干活的是引擎。

这里没有「LLM 后端」这一层——推理在引擎里（`src/engine`）。这个包只剩两样东西：

- `base`：回复的数据结构（正文 + 情感）与两种错误类型；
- `catalog`：模型清单直接读引擎的供应商表与模型表。
"""

from __future__ import annotations

from .base import LLMConfigError, LLMRequestError, LLMResult
from .catalog import LlmOption, apply_option, current_option, list_options, split_option

__all__ = [
    "LLMConfigError",
    "LLMRequestError",
    "LLMResult",
    "LlmOption",
    "apply_option",
    "current_option",
    "list_options",
    "split_option",
]
